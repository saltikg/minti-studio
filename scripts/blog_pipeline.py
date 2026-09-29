#!/home/ubuntu/apps/minti_studio/.venv/bin/python
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.video_shorts.services.blog_articles import _slugify  # noqa: E402
from app.video_shorts.services.blog_llm import (  # noqa: E402
    BLOG_MODEL_DESIGNER,
    BLOG_MODEL_REVIEWER,
    BLOG_MODEL_WRITER,
    call_json,
    log_usage,
)
from app.video_shorts.services.blog_pipeline import current_month_spend, json_dumps_compact  # noqa: E402
from app.video_shorts.services.blog_pipeline_runs import (  # noqa: E402
    create_run,
    finish_run,
    record_stage,
    set_current_stage,
)
from app.video_shorts.services.db import get_db, get_db_readonly, table_columns  # noqa: E402


BLOG_REVIEW_PASS = int(os.getenv("BLOG_REVIEW_PASS", "85") or "85")
BLOG_RUN_MAX_USD = Decimal(os.getenv("BLOG_RUN_MAX_USD", "1.50") or "1.50")
STATIC_BLOG_ROOT = ROOT / "app" / "video_shorts" / "static" / "img" / "blog"
LIBRARY_ROOT = STATIC_BLOG_ROOT / "library"
MANIFEST_PATH = LIBRARY_ROOT / "screenshot_manifest.json"
CONTEXT_ROOT = ROOT / "app" / "video_shorts" / "blog_pipeline"
CTA_URL = "https://mintistudio.com/video_shorts/login"


@dataclass
class PipelineState:
    topic: dict[str, Any]
    run_id: int | None
    dry_run: bool
    run_cost: Decimal = Decimal("0")
    seq: int = 0


def _row_to_dict(description, row) -> dict[str, Any]:
    return {description[index][0]: value for index, value in enumerate(row)}


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _word_count(markdown_text: str) -> int:
    text = re.sub(r"<!--\s*IMAGE_[123]\s*-->", " ", markdown_text or "")
    text = re.sub(r"\[[^\]]+\]\([^)]+\)", " ", text)
    text = re.sub(r"[#>*_`|:-]+", " ", text)
    return len(re.findall(r"\b[\w'-]+\b", text))


def _load_manifest() -> list[dict[str, Any]]:
    raw = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    items: list[dict[str, Any]] = []
    for item in raw:
        filename = str(item.get("filename") or "")
        exists = bool(filename and (LIBRARY_ROOT / filename).is_file())
        normalized = dict(item)
        normalized["exists"] = exists
        normalized["hold"] = bool(item.get("hold"))
        if exists and not normalized["hold"]:
            items.append(normalized)
    return items


def _all_manifest_entries() -> list[dict[str, Any]]:
    raw = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    for item in raw:
        filename = str(item.get("filename") or "")
        item["exists"] = bool(filename and (LIBRARY_ROOT / filename).is_file())
        item["hold"] = bool(item.get("hold"))
    return raw


def _published_articles(limit: int | None = None, *, include_content: bool = False) -> list[dict[str, Any]]:
    conn = get_db_readonly()
    try:
        content_sql = ", content" if include_content else ""
        sql = f"""
            SELECT title, slug, summary{content_sql}
            FROM blog_articles
            WHERE status = 'published'
            ORDER BY published_at DESC NULLS LAST, created_at DESC
        """
        params: list[Any] = []
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        rows = conn.execute(sql, params).fetchall()
        articles = []
        for row in rows:
            item = {"title": row[0], "slug": row[1], "summary": row[2], "url": f"https://mintistudio.com/blog/{row[1]}/"}
            if include_content:
                item["content"] = row[3]
            articles.append(item)
        return articles
    finally:
        conn.close()


def _stable_prefix() -> dict[str, Any]:
    return {
        "facts": _read_text(CONTEXT_ROOT / "minti_facts.md"),
        "style_guide": _read_text(CONTEXT_ROOT / "style_guide.md"),
        "example_articles": _published_articles(3, include_content=True),
        "screenshots": _load_manifest(),
        "allowed_components": {
            "callouts": [":::info Optional title\\nBody\\n:::", ":::tip Optional title\\nBody\\n:::", ":::warning Optional title\\nBody\\n:::"],
            "key": ":::key\\nOne key sentence.\\n:::",
            "action": ":::action Optional title\\nCTA sentence.\\n:::",
            "specimens": [":::short\\nShort example text.\\n:::", ":::long\\nLong-form example text.\\n:::"],
            "tables": "Markdown pipe tables are allowed.",
            "youtube": "[youtube: https://www.youtube.com/watch?v=VIDEO_ID]",
            "images": "Markdown images only: ![alt](/video_shorts/static/img/blog/slug/file.png)",
        },
    }


def _pop_topic(conn, topic_id: int | None, *, dry_run: bool = False) -> dict[str, Any] | None:
    columns = table_columns(conn, "blog_topics")
    source_summary_sql = "source_summary" if "source_summary" in columns else "NULL AS source_summary"
    fit_breakdown_sql = "fit_breakdown" if "fit_breakdown" in columns else "NULL AS fit_breakdown"
    adapted_from_sql = "adapted_from" if "adapted_from" in columns else "NULL AS adapted_from"
    if topic_id:
        row = conn.execute(
            f"""
            SELECT id, title, primary_keyword, category, intent, brief, source_type, source_name,
                   source_url, source_title, fit_score, judge_reason, {source_summary_sql},
                   {fit_breakdown_sql}, {adapted_from_sql}
            FROM blog_topics
            WHERE id = ?
              AND status = 'queued'
            FOR UPDATE SKIP LOCKED
            LIMIT 1
            """,
            [int(topic_id)],
        ).fetchone()
    else:
        row = conn.execute(
            f"""
            SELECT id, title, primary_keyword, category, intent, brief, source_type, source_name,
                   source_url, source_title, fit_score, judge_reason, {source_summary_sql},
                   {fit_breakdown_sql}, {adapted_from_sql}
            FROM blog_topics
            WHERE status = 'queued'
              AND created_at >= CURRENT_TIMESTAMP - INTERVAL '45 days'
            ORDER BY fit_score DESC NULLS LAST, created_at ASC
            FOR UPDATE SKIP LOCKED
            LIMIT 1
            """
        ).fetchone()
    if not row:
        return None
    topic = _row_to_dict(conn.description, row)
    if not dry_run:
        conn.execute("UPDATE blog_topics SET status = 'in_production' WHERE id = ?", [int(topic["id"])])
    return topic


def _check_run_budget(state: PipelineState) -> None:
    if state.run_cost >= BLOG_RUN_MAX_USD:
        raise RuntimeError(f"BLOG_RUN_MAX_USD reached: spent ${state.run_cost} of ${BLOG_RUN_MAX_USD}")


def _call_stage(state: PipelineState, stage: str, model: str, system_prompt: str, payload: dict[str, Any]) -> dict[str, Any]:
    _check_run_budget(state)
    result = call_json(stage, model=model, system_prompt=system_prompt, user_prompt=json_dumps_compact(payload))
    state.run_cost += result.cost_usd
    if not state.dry_run:
        log_usage(stage, result, topic_id=int(state.topic["id"]), run_id=state.run_id)
    return json.loads(result.content), result.cost_usd


def _internal_link_urls(articles: list[dict[str, Any]]) -> set[str]:
    return {str(article["url"]) for article in articles}


def _article_links(markdown_text: str) -> list[str]:
    return re.findall(r"\[[^\]]+\]\((https?://[^)]+)\)", markdown_text or "")


def _slug_exists(slug: str) -> bool:
    conn = get_db_readonly()
    try:
        row = conn.execute("SELECT 1 FROM blog_articles WHERE slug = ? LIMIT 1", [slug]).fetchone()
        return bool(row)
    finally:
        conn.close()


def _unique_slug(slug: str) -> str:
    base = _slugify(slug)
    candidate = base
    counter = 2
    while _slug_exists(candidate):
        candidate = f"{base}-{counter}"
        counter += 1
    return candidate


def _code_checks(article: dict[str, Any], screenshots: list[dict[str, Any]], published: list[dict[str, Any]]) -> dict[str, Any]:
    issues: list[str] = []
    fixed: dict[str, Any] = {}
    slug = _unique_slug(article.get("slug") or article.get("title") or "article")
    if slug != article.get("slug"):
        fixed["slug"] = slug
        article["slug"] = slug
    content = str(article.get("content_md") or "")
    if len(str(article.get("meta_title") or "")) > 60:
        issues.append("meta_title exceeds 60 characters")
    if len(str(article.get("meta_description") or "")) > 155:
        issues.append("meta_description exceeds 155 characters")
    if CTA_URL not in content:
        issues.append("CTA link is missing")
    placeholders = re.findall(r"<!--\s*IMAGE_[123]\s*-->", content)
    if sorted(placeholders) != ["<!-- IMAGE_1 -->", "<!-- IMAGE_2 -->", "<!-- IMAGE_3 -->"]:
        issues.append("content_md must contain exactly <!-- IMAGE_1 -->, <!-- IMAGE_2 -->, <!-- IMAGE_3 -->")
    if re.search(r"<(iframe|script|style|div|span|img)\b", content, flags=re.I):
        issues.append("raw HTML is not allowed except IMAGE comments")
    word_count = _word_count(content)
    if word_count < 1200 or word_count > 1800:
        issues.append(f"word count {word_count} is outside 1200-1800")
    allowed_urls = _internal_link_urls(published) | {CTA_URL}
    for url in _article_links(content):
        if "mintistudio.com/blog/" in url and url not in allowed_urls:
            issues.append(f"internal link not in published list: {url}")
    screenshot_by_id = {item["id"]: item for item in screenshots}
    screenshot_count = 0
    visuals = article.get("visuals") or []
    if len(visuals) != 3:
        issues.append("visuals must contain exactly 3 items")
    for visual in visuals:
        if visual.get("type") == "screenshot":
            screenshot_count += 1
            screenshot_id = str(visual.get("screenshot_id") or "")
            if screenshot_id not in screenshot_by_id:
                issues.append(f"screenshot_id is unavailable or on hold: {screenshot_id}")
    if screenshot_count > 2:
        issues.append("at most 2 visuals may be screenshots")
    return {"ok": not issues, "issues": issues, "fixed": fixed, "word_count": word_count}


def _strip_design_syntax(text: str) -> str:
    text = re.sub(r"^:::[A-Za-z]+.*$|^:::$", "", text or "", flags=re.M)
    text = re.sub(r"[#>*_`|:-]+", " ", text)
    text = re.sub(r"\[[^\]]+\]\(([^)]+)\)", " ", text)
    return " ".join(re.findall(r"\b[\w'-]+\b", text.lower()))


def _guard_designer(before: str, after: str) -> tuple[bool, str]:
    before_links = _article_links(before)
    after_links = _article_links(after)
    before_markers = re.findall(r"<!--\s*IMAGE_[123]\s*-->", before)
    after_markers = re.findall(r"<!--\s*IMAGE_[123]\s*-->", after)
    ratio = difflib.SequenceMatcher(None, _strip_design_syntax(before), _strip_design_syntax(after)).ratio()
    if before_links != after_links:
        return False, "designer changed links"
    if before_markers != after_markers:
        return False, "designer changed image placeholders"
    if ratio < 0.97:
        return False, f"designer changed wording too much: similarity {ratio:.3f}"
    return True, f"similarity {ratio:.3f}"


def _replace_screenshot_placeholders(article: dict[str, Any], screenshots: list[dict[str, Any]]) -> dict[str, Any]:
    slug = str(article["slug"])
    content = str(article.get("content_md") or "")
    screenshot_by_id = {item["id"]: item for item in screenshots}
    target_dir = STATIC_BLOG_ROOT / slug
    target_dir.mkdir(parents=True, exist_ok=True)
    placed: list[dict[str, str]] = []
    for visual in article.get("visuals") or []:
        marker = str(visual.get("marker") or "")
        if visual.get("type") != "screenshot" or marker not in {"IMAGE_1", "IMAGE_2", "IMAGE_3"}:
            continue
        screenshot = screenshot_by_id.get(str(visual.get("screenshot_id") or ""))
        if not screenshot:
            continue
        source = LIBRARY_ROOT / str(screenshot["filename"])
        target = target_dir / source.name
        shutil.copy2(source, target)
        url = f"/video_shorts/static/img/blog/{quote(slug)}/{quote(target.name)}"
        alt = str(screenshot.get("alt") or visual.get("alt") or "")
        content = content.replace(f"<!-- {marker} -->", f"![{alt}]({url})")
        placed.append({"marker": marker, "filename": target.name, "url": url, "alt": alt})
    article["content_md"] = content
    article["screenshots_placed"] = placed
    return article


def _save_draft(conn, *, state: PipelineState, article: dict[str, Any], visuals_plan: dict[str, Any]) -> int:
    row = conn.execute(
        """
        INSERT INTO blog_articles (
            title, slug, summary, content, cover_image_url, meta_title, meta_description,
            author_name, reading_time, view_count, status, published_at, import_source, import_source_id
        )
        VALUES (?, ?, ?, ?, NULL, ?, ?, 'MintiStudio Team', ?, 0, 'draft', NULL, 'blog_pipeline', ?)
        RETURNING id
        """,
        [
            article["title"],
            article["slug"],
            article.get("summary"),
            article.get("content_md"),
            article.get("meta_title"),
            article.get("meta_description"),
            article.get("reading_time"),
            str(state.run_id or "dry-run"),
        ],
    ).fetchone()
    article_id = int(row[0])
    conn.execute("UPDATE blog_topics SET status = 'draft_ready' WHERE id = ?", [int(state.topic["id"])])
    record_stage(
        conn,
        run_id=state.run_id,
        seq=state.seq + 1,
        stage="final",
        status="done",
        output={"article_id": article_id, "cover": article.get("cover"), "visuals": visuals_plan},
    )
    return article_id


def _writer_prompt() -> str:
    return """You are the MintiStudio blog Writer. Return strict JSON only. Write original, practical long-form blog content for the supplied topic. Use the stable context as binding instructions. Never claim anything about MintiStudio unless it is in minti_facts.md. Never send raw HTML except the exact IMAGE placeholders."""


def _reviewer_prompt() -> str:
    return """You are the MintiStudio blog Reviewer. Return strict JSON only. Score the article against the supplied facts, checks, screenshots, and existing titles. Be concrete and conservative."""


def _revision_prompt() -> str:
    return """You are the MintiStudio blog Reviser. Return strict JSON only. Apply only the listed reviewer fixes and blocking issues. Preserve valid metadata, links, and IMAGE placeholders unless a fix explicitly requires changing them."""


def _designer_prompt() -> str:
    return """You are the MintiStudio blog Designer. Return strict JSON only with content_md. You may only add presentation syntax from the allowed component vocabulary. Never add, remove, or rewrite sentences. Never change links, metadata, or IMAGE placeholders."""


def run_pipeline(*, topic_id: int | None = None, dry_run: bool = False) -> dict[str, Any]:
    prefix = _stable_prefix()
    published = _published_articles()
    screenshots = prefix["screenshots"]
    conn = get_db()
    state: PipelineState | None = None
    article: dict[str, Any] | None = None
    try:
        topic = _pop_topic(conn, topic_id, dry_run=dry_run)
        if not topic:
            conn.rollback()
            print("BLOG_PIPELINE_NO_TOPIC")
            return {"status": "no_topic"}
        run_id = create_run(conn, topic_id=int(topic["id"]), dry_run=dry_run)
        state = PipelineState(topic=topic, run_id=run_id, dry_run=dry_run)
        conn.rollback() if dry_run else conn.commit()

        user_base = {
            "stable_context": prefix,
            "topic": topic,
            "published_articles": published,
            "requirements": {
                "visual_markers": ["IMAGE_1", "IMAGE_2", "IMAGE_3"],
                "cta_url": CTA_URL,
                "writer_model": BLOG_MODEL_WRITER,
                "reviewer_model": BLOG_MODEL_REVIEWER,
                "designer_model": BLOG_MODEL_DESIGNER,
            },
        }
        article, cost = _call_stage(state, "writer", BLOG_MODEL_WRITER, _writer_prompt(), user_base)
        conn = get_db()
        state.seq += 1
        set_current_stage(conn, state.run_id, "writer")
        record_stage(conn, run_id=state.run_id, seq=state.seq, stage="writer", status="done", model=BLOG_MODEL_WRITER, output=article, cost_usd=cost)
        conn.commit()

        checks = _code_checks(article, screenshots, published)
        state.seq += 1
        record_stage(conn, run_id=state.run_id, seq=state.seq, stage="checks", status="done" if checks["ok"] else "failed", output=checks, notes="; ".join(checks["issues"]))
        conn.commit()

        reviewer_payload = {**user_base, "article": article, "checks": checks, "existing_titles": [item["title"] for item in published]}
        review_1, cost = _call_stage(state, "reviewer_1", BLOG_MODEL_REVIEWER, _reviewer_prompt(), reviewer_payload)
        state.seq += 1
        review_1_score = int(review_1.get("total") or 0)
        record_stage(conn, run_id=state.run_id, seq=state.seq, stage="reviewer_1", status="done", model=BLOG_MODEL_REVIEWER, output=review_1, score=review_1_score, cost_usd=cost)
        conn.commit()

        final_review = review_1
        if review_1_score < BLOG_REVIEW_PASS or review_1.get("blocking_issues"):
            revision_payload = {**user_base, "article": article, "review": review_1}
            article, cost = _call_stage(state, "revision_1", BLOG_MODEL_WRITER, _revision_prompt(), revision_payload)
            state.seq += 1
            record_stage(conn, run_id=state.run_id, seq=state.seq, stage="revision_1", status="done", model=BLOG_MODEL_WRITER, output=article, cost_usd=cost)
            checks = _code_checks(article, screenshots, published)
            state.seq += 1
            record_stage(conn, run_id=state.run_id, seq=state.seq, stage="checks", status="done" if checks["ok"] else "failed", output=checks, notes="; ".join(checks["issues"]))
            reviewer_payload = {**user_base, "article": article, "checks": checks, "existing_titles": [item["title"] for item in published]}
            review_2, cost = _call_stage(state, "reviewer_2", BLOG_MODEL_REVIEWER, _reviewer_prompt(), reviewer_payload)
            final_review = review_2
            state.seq += 1
            review_2_score = int(review_2.get("total") or 0)
            record_stage(conn, run_id=state.run_id, seq=state.seq, stage="reviewer_2", status="done", model=BLOG_MODEL_REVIEWER, output=review_2, score=review_2_score, cost_usd=cost)
            conn.commit()
            if review_2_score < BLOG_REVIEW_PASS or review_2.get("blocking_issues"):
                article, cost = _call_stage(state, "revision_2", BLOG_MODEL_WRITER, _revision_prompt(), {**user_base, "article": article, "review": review_2})
                state.seq += 1
                record_stage(conn, run_id=state.run_id, seq=state.seq, stage="revision_2", status="done", model=BLOG_MODEL_WRITER, output=article, cost_usd=cost)
                checks = _code_checks(article, screenshots, published)
                state.seq += 1
                record_stage(conn, run_id=state.run_id, seq=state.seq, stage="checks", status="done" if checks["ok"] else "failed", output=checks, notes="; ".join(checks["issues"]))
                conn.commit()

        before_design = str(article.get("content_md") or "")
        designer_output, cost = _call_stage(state, "designer", BLOG_MODEL_DESIGNER, _designer_prompt(), {**user_base, "article": article})
        after_design = str(designer_output.get("content_md") or "")
        guard_ok, guard_note = _guard_designer(before_design, after_design)
        if guard_ok:
            article["content_md"] = after_design
            designer_status = "done"
        else:
            designer_status = "failed"
        state.seq += 1
        record_stage(conn, run_id=state.run_id, seq=state.seq, stage="designer", status=designer_status, model=BLOG_MODEL_DESIGNER, output={"guard": guard_note, "content_md": after_design, "visuals": article.get("visuals")}, notes=guard_note, cost_usd=cost)
        conn.commit()

        article = _replace_screenshot_placeholders(article, screenshots)
        run_status = "draft_ready"
        final_score = int(final_review.get("total") or 0)
        if final_score < BLOG_REVIEW_PASS or final_review.get("blocking_issues") or not checks.get("ok"):
            run_status = "needs_you"

        result = {
            "status": run_status,
            "topic": topic,
            "article": article,
            "review": final_review,
            "checks": checks,
            "designer_guard": guard_note,
            "total_cost_usd": str(state.run_cost),
        }
        if dry_run:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return result

        article_id = _save_draft(conn, state=state, article=article, visuals_plan={"cover": article.get("cover"), "visuals": article.get("visuals"), "screenshots_placed": article.get("screenshots_placed")})
        finish_run(conn, state.run_id, status=run_status, article_id=article_id, final_review_score=final_score)
        conn.commit()
        result["article_id"] = article_id
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return result
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        if state and not state.dry_run:
            fail_conn = get_db()
            try:
                finish_run(fail_conn, state.run_id, status="failed", error=str(exc)[:1000])
                fail_conn.execute(
                    "UPDATE blog_topics SET status = 'queued', judge_reason = COALESCE(judge_reason, '') || ? WHERE id = ?",
                    [f"\nPipeline failed: {str(exc)[:500]}", int(state.topic["id"])],
                )
                fail_conn.commit()
            finally:
                fail_conn.close()
        raise
    finally:
        try:
            conn.close()
        except Exception:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one MintiStudio blog production pipeline.")
    parser.add_argument("--dry-run", action="store_true", help="Do not save the draft or run rows.")
    parser.add_argument("--topic-id", type=int, default=None, help="Run a specific queued topic.")
    parser.add_argument("--manifest-report", action="store_true", help="Print screenshot manifest availability and exit.")
    args = parser.parse_args()
    if args.manifest_report:
        print(json.dumps(_all_manifest_entries(), ensure_ascii=False, indent=2))
        return 0
    run_pipeline(topic_id=args.topic_id, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
