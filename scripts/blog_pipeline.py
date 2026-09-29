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
from urllib import request
from urllib.error import HTTPError

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
from app.video_shorts.services.blog_images import (  # noqa: E402
    BLOG_IMAGE_COVER,
    BLOG_IMAGE_COVER_QUALITY,
    BLOG_IMAGE_INLINE,
    BLOG_IMAGE_INLINE_QUALITY,
    BlogImageResult,
    generate_blog_image,
    generated_visual_filename,
    render_markdown_image,
)
from app.video_shorts.services.blog_articles import update_blog_article_status  # noqa: E402
from app.video_shorts.services.blog_notifications import BLOG_NOTIFY_EMAIL, send_blog_run_notification  # noqa: E402
from app.video_shorts.services.blog_pipeline import current_month_spend, json_dumps_compact  # noqa: E402
from app.video_shorts.services.blog_pipeline_runs import (  # noqa: E402
    create_run,
    finish_run,
    mark_run_notified,
    record_stage,
    run_was_notified,
    set_current_stage,
)
from app.video_shorts.services.db import get_db, get_db_readonly, table_columns  # noqa: E402


BLOG_REVIEW_PASS = int(os.getenv("BLOG_REVIEW_PASS", "85") or "85")
BLOG_RUN_MAX_USD = Decimal(os.getenv("BLOG_RUN_MAX_USD", "1.50") or "1.50")
BLOG_AUTO_PUBLISH = str(os.getenv("BLOG_AUTO_PUBLISH", "true")).strip().lower() not in {"0", "false", "no", "off"}
STATIC_BLOG_ROOT = ROOT / "app" / "video_shorts" / "static" / "img" / "blog"
LIBRARY_ROOT = STATIC_BLOG_ROOT / "library"
MANIFEST_PATH = LIBRARY_ROOT / "screenshot_manifest.json"
CONTEXT_ROOT = ROOT / "app" / "video_shorts" / "blog_pipeline"
CTA_URL = "https://mintistudio.com/video_shorts/login"
BASE_URL = (os.getenv("BLOG_PUBLIC_BASE_URL") or "https://mintistudio.com").rstrip("/")
SELF_DISCLAIMER_RE = re.compile(
    r"(not a claim that MintiStudio|do not assume Autopilot|should not be inferred from the price|"
    r"not a promised MintiStudio|confirm .*MintiStudio|MintiStudio .*does not (?:claim|promise|specify))",
    re.I,
)
IMAGE_MARKERS = ("IMAGE_1", "IMAGE_2", "IMAGE_3")
IMAGE_COMMENT_RE = re.compile(r"<!--\s*IMAGE_([123])\s*-->")
IMAGE_PLACEHOLDER_VARIANT_RE = re.compile(
    r"<!--\s*IMAGE_([123])\s*-->|"
    r"\{\{\s*IMAGE_([123])\s*\}\}|"
    r"\[\s*IMAGE_([123])\s*\]|"
    r"(?<![A-Za-z0-9_])IMAGE_([123])(?![A-Za-z0-9_])"
)


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


def _normalize_marker(value: Any) -> str:
    text = str(value or "").strip()
    match = re.search(r"IMAGE_[123]", text)
    return match.group(0) if match else text


def _image_comment(marker: str) -> str:
    return f"<!-- {marker} -->"


def _normalize_image_placeholder_syntax(content: str) -> str:
    def replace(match: re.Match[str]) -> str:
        number = next(group for group in match.groups() if group)
        return _image_comment(f"IMAGE_{number}")

    return IMAGE_PLACEHOLDER_VARIANT_RE.sub(replace, content or "")


def _remove_duplicate_image_placeholders(content: str) -> str:
    seen: set[str] = set()

    def replace(match: re.Match[str]) -> str:
        marker = f"IMAGE_{match.group(1)}"
        if marker in seen:
            return ""
        seen.add(marker)
        return _image_comment(marker)

    content = IMAGE_COMMENT_RE.sub(replace, content or "")
    return re.sub(r"\n{3,}", "\n\n", content).strip()


def _is_h2(line: str) -> bool:
    return bool(re.match(r"^##\s+\S", line or ""))


def _is_final_section_heading(line: str) -> bool:
    if not _is_h2(line):
        return False
    heading = re.sub(r"^##\s+", "", line).strip().lower()
    return any(term in heading for term in ("final", "conclusion", "cta", "next step", "related"))


def _is_safe_paragraph_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    return not (
        stripped.startswith(("#", "|", ">", ":::", "<!--", "!["))
        or re.match(r"^([-*+]\s+|\d+\.\s+)", stripped)
    )


def _candidate_h2_indexes(lines: list[str]) -> list[int]:
    indexes = [index for index, line in enumerate(lines) if _is_h2(line) and not _is_final_section_heading(line)]
    return indexes or [index for index, line in enumerate(lines) if _is_h2(line)]


def _insert_position_after_first_paragraph(lines: list[str], start_index: int) -> int:
    index = start_index + 1
    while index < len(lines):
        line = lines[index]
        if _is_h2(line) or _is_final_section_heading(line):
            return index
        if _is_safe_paragraph_line(line):
            while index + 1 < len(lines) and _is_safe_paragraph_line(lines[index + 1]):
                index += 1
            return index + 1
        index += 1
    return len(lines)


def _fallback_insert_position(lines: list[str]) -> int:
    for index, line in enumerate(lines):
        if _is_final_section_heading(line):
            return index
    return len(lines)


def _insert_missing_image_placeholders(content: str) -> str:
    lines = (content or "").splitlines()
    present = {f"IMAGE_{match.group(1)}" for match in IMAGE_COMMENT_RE.finditer(content or "")}
    missing = [marker for marker in IMAGE_MARKERS if marker not in present]
    if not missing:
        return content

    h2_indexes = _candidate_h2_indexes(lines)
    fallback_index = _fallback_insert_position(lines)
    slot_positions = [_insert_position_after_first_paragraph(lines, index) for index in h2_indexes]
    if fallback_index not in slot_positions:
        slot_positions.append(fallback_index)
    slot_positions = sorted(set(slot_positions)) or [len(lines)]
    planned: dict[int, list[str]] = {}
    for marker in missing:
        marker_index = IMAGE_MARKERS.index(marker)
        slot_index = min(round(marker_index * (len(slot_positions) - 1) / 2), len(slot_positions) - 1)
        planned.setdefault(slot_positions[slot_index], []).append(marker)
    for insert_at in sorted(planned, reverse=True):
        insertion: list[str] = [""]
        for marker in sorted(planned[insert_at], key=IMAGE_MARKERS.index):
            insertion.extend([_image_comment(marker), ""])
        lines[insert_at:insert_at] = insertion
    return "\n".join(lines).strip()


def _previous_h2_and_first_paragraph(content: str, marker: str) -> tuple[str, str]:
    lines = (content or "").splitlines()
    marker_line = next((index for index, line in enumerate(lines) if _image_comment(marker) in line), len(lines))
    heading = ""
    heading_index = 0
    for index in range(marker_line, -1, -1):
        if index < len(lines) and _is_h2(lines[index]):
            heading = re.sub(r"^##\s+", "", lines[index]).strip()
            heading_index = index
            break
    paragraph = ""
    for line in lines[heading_index + 1 : marker_line]:
        if _is_safe_paragraph_line(line):
            paragraph = line.strip()
            break
    return heading or "MintiStudio workflow", paragraph


def _generated_visual_for_marker(content: str, marker: str) -> dict[str, Any]:
    heading, paragraph = _previous_h2_and_first_paragraph(content, marker)
    prompt_detail = f" Section context: {paragraph[:320]}" if paragraph else ""
    return {
        "marker": marker,
        "type": "generate",
        "alt": f"{heading} illustration",
        "prompt": f"Landscape editorial visual for the section '{heading}'.{prompt_detail}",
    }


def _normalize_image_placeholders_and_visuals(article: dict[str, Any]) -> dict[str, Any]:
    content = _normalize_image_placeholder_syntax(str(article.get("content_md") or ""))
    content = _remove_duplicate_image_placeholders(content)
    content = _insert_missing_image_placeholders(content)
    article["content_md"] = content

    visual_by_marker: dict[str, dict[str, Any]] = {}
    for visual in article.get("visuals") or []:
        marker = _normalize_marker(visual.get("marker") or visual.get("id"))
        if marker not in IMAGE_MARKERS or marker in visual_by_marker:
            continue
        normalized = dict(visual)
        normalized["marker"] = marker
        if normalized.get("type") not in {"screenshot", "generate"}:
            normalized["type"] = "generate"
        if normalized.get("type") == "generate" and not normalized.get("prompt"):
            generated = _generated_visual_for_marker(content, marker)
            normalized["prompt"] = normalized.get("brief") or normalized.get("description") or generated["prompt"]
            normalized.setdefault("alt", generated["alt"])
        visual_by_marker[marker] = normalized

    screenshot_count = 0
    normalized_visuals: list[dict[str, Any]] = []
    for marker in IMAGE_MARKERS:
        visual = dict(visual_by_marker.get(marker) or _generated_visual_for_marker(content, marker))
        if visual.get("type") == "screenshot":
            screenshot_count += 1
            if screenshot_count > 2:
                visual = _generated_visual_for_marker(content, marker)
        normalized_visuals.append(visual)
    article["visuals"] = normalized_visuals
    return article


def _normalize_article_payload(article: dict[str, Any]) -> dict[str, Any]:
    article = dict(article or {})
    if not article.get("content_md"):
        article["content_md"] = article.get("content") or article.get("content_markdown")
    if not article.get("summary") and article.get("excerpt"):
        article["summary"] = article.get("excerpt")
    article["slug"] = _slugify(article.get("slug") or article.get("title") or "article")
    content = str(article.get("content_md") or "")
    visuals: list[dict[str, Any]] = []
    for visual in article.get("visuals") or []:
        normalized = dict(visual)
        marker = _normalize_marker(normalized.get("marker") or normalized.get("id"))
        normalized["marker"] = marker
        if normalized.get("type") not in {"screenshot", "generate"}:
            normalized["type"] = "generate"
        if normalized.get("type") == "generate" and not normalized.get("prompt"):
            normalized["prompt"] = normalized.get("brief") or normalized.get("description") or ""
        visuals.append(normalized)
    article["visuals"] = visuals
    article["content_md"] = content
    article = _normalize_image_placeholders_and_visuals(article)
    if not article.get("meta_title"):
        article["meta_title"] = str(article.get("title") or "")[:60]
    if not article.get("meta_description"):
        article["meta_description"] = str(article.get("summary") or "")[:155]
    if not article.get("reading_time"):
        article["reading_time"] = max(1, round(_word_count(content) / 220))
    return article


def _review_score(review: dict[str, Any]) -> int:
    for key in ("total", "score", "overall_score"):
        value = review.get(key)
        if value is not None:
            try:
                parsed = int(value)
                return parsed * 10 if key == "overall_score" and parsed <= 10 else parsed
            except (TypeError, ValueError):
                pass
    scores = review.get("scores")
    if isinstance(scores, dict):
        total = 0
        for value in scores.values():
            try:
                total += int(value)
            except (TypeError, ValueError):
                pass
        return total
    return 0


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
            item = {"title": row[0], "slug": row[1], "summary": row[2], "url": _canonical_blog_url(str(row[1] or ""))}
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
        "example_articles": _published_articles(1, include_content=True),
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
    usage_stage = f"{stage}_dry" if state.dry_run else stage
    result = call_json(usage_stage, model=model, system_prompt=system_prompt, user_prompt=json_dumps_compact(payload))
    state.run_cost += result.cost_usd
    log_usage(usage_stage, result, topic_id=int(state.topic["id"]), run_id=state.run_id)
    return json.loads(result.content), result.cost_usd


def _internal_link_urls(articles: list[dict[str, Any]]) -> set[str]:
    return {str(article["url"]) for article in articles}


def _canonical_blog_url(slug: str) -> str:
    from app import create_app
    from flask import url_for

    app = create_app()
    with app.test_request_context(base_url=BASE_URL):
        return url_for("video_shorts_bp.blog_article", slug=slug, _external=True)


def _article_links(markdown_text: str) -> list[str]:
    return re.findall(r"\[[^\]]+\]\((https?://[^)]+)\)", markdown_text or "")


def _http_status(url: str, *, timeout: int = 5) -> int | None:
    headers = {"User-Agent": "MintiStudioBlogPipeline/1.0 (+https://mintistudio.com)"}
    for method in ("HEAD", "GET"):
        try:
            req = request.Request(url, method=method, headers=headers)
            with request.urlopen(req, timeout=timeout) as resp:
                return int(resp.status)
        except HTTPError as exc:
            return int(exc.code)
        except Exception:
            if method == "GET":
                return None
    return None


def _public_blog_url(slug: str) -> str:
    return f"{BASE_URL}/video_shorts/blog/{quote(str(slug).strip())}/"


def _admin_edit_url(article_id: int | None) -> str | None:
    return f"{BASE_URL}/video_shorts/admin/blog/{int(article_id)}/edit" if article_id else None


def _run_detail_url(run_id: int | None) -> str | None:
    return f"{BASE_URL}/video_shorts/admin/blog-pipeline/runs/{int(run_id)}" if run_id else None


def _sitemap_status(public_url: str) -> dict[str, Any]:
    sitemap_url = f"{BASE_URL}/sitemap.xml"
    try:
        req = request.Request(sitemap_url, headers={"User-Agent": "MintiStudioBlogPipeline/1.0"})
        with request.urlopen(req, timeout=5) as resp:
            body = resp.read(2_000_000).decode("utf-8", "ignore")
            return {"url": sitemap_url, "status": int(resp.status), "contains_article": public_url in body}
    except HTTPError as exc:
        return {"url": sitemap_url, "status": int(exc.code), "contains_article": False}
    except Exception as exc:
        return {"url": sitemap_url, "status": None, "contains_article": False, "error": str(exc)[:200]}


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
    while _slug_blocks_new_draft(candidate):
        candidate = f"{base}-{counter}"
        counter += 1
    return candidate


def _slug_blocks_new_draft(slug: str) -> bool:
    conn = get_db_readonly()
    try:
        row = conn.execute(
            """
            SELECT status, import_source
            FROM blog_articles
            WHERE slug = ?
            LIMIT 1
            """,
            [slug],
        ).fetchone()
        if not row:
            return False
        return not (str(row[0] or "") == "archived" and str(row[1] or "") == "blog_pipeline")
    finally:
        conn.close()


def _rename_archived_pipeline_slug_conflict(conn, slug: str) -> str | None:
    row = conn.execute(
        """
        SELECT id
        FROM blog_articles
        WHERE slug = ?
          AND status = 'archived'
          AND import_source = 'blog_pipeline'
        LIMIT 1
        """,
        [slug],
    ).fetchone()
    if not row:
        return None
    archived_id = int(row[0])
    archived_slug = f"{slug}-archived-{archived_id}"
    conn.execute(
        """
        UPDATE blog_articles
        SET slug = ?
        WHERE id = ?
        """,
        [archived_slug, archived_id],
    )
    return archived_slug


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
    if re.search(r"(?m)^\s*IMAGE_[123]\s*$", content):
        issues.append("bare IMAGE_n placeholder text is not allowed; use exact <!-- IMAGE_n --> comments")
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
        if "mintistudio.com" in url and url not in allowed_urls:
            issues.append(f"internal link not in published list: {url}")
        if "mintistudio.com" in url and "/video_shorts/blog/" in url:
            status = _http_status(url)
            if status != 200:
                issues.append(f"internal blog link returned {status or 'error'}: {url}")
    if SELF_DISCLAIMER_RE.search(content):
        issues.append("MintiStudio self-disclaimer is not allowed")
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


def _blocking_issues(review: dict[str, Any], checks: dict[str, Any] | None = None) -> list[Any]:
    items: list[Any] = []
    for key in ("blocking_issues", "required_fixes"):
        value = review.get(key)
        if isinstance(value, list):
            items.extend(value)
    for item in review.get("issues") or []:
        if isinstance(item, dict) and str(item.get("severity") or "").lower() == "blocking":
            items.append(item)
    if checks and not checks.get("ok"):
        items.extend([{"severity": "blocking", "issue": issue} for issue in checks.get("issues") or []])
    return items


def _pre_save_visual_guard(article: dict[str, Any], screenshots: list[dict[str, Any]], published: list[dict[str, Any]]) -> None:
    checks = _code_checks(article, screenshots, published)
    if not checks.get("ok"):
        raise RuntimeError("pre-save blog checks failed: " + "; ".join(checks.get("issues") or []))


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


def _replace_screenshot_placeholders(article: dict[str, Any], screenshots: list[dict[str, Any]], *, copy_files: bool = True) -> dict[str, Any]:
    slug = str(article["slug"])
    content = str(article.get("content_md") or "")
    screenshot_by_id = {item["id"]: item for item in screenshots}
    target_dir = STATIC_BLOG_ROOT / slug
    if copy_files:
        target_dir.mkdir(parents=True, exist_ok=True)
    placed: list[dict[str, str]] = []
    for visual in article.get("visuals") or []:
        marker = _normalize_marker(visual.get("marker") or visual.get("id"))
        if visual.get("type") != "screenshot" or marker not in {"IMAGE_1", "IMAGE_2", "IMAGE_3"}:
            continue
        screenshot = screenshot_by_id.get(str(visual.get("screenshot_id") or ""))
        if not screenshot:
            continue
        source = LIBRARY_ROOT / str(screenshot["filename"])
        target_name = source.name
        if copy_files:
            target = target_dir / source.name
            shutil.copy2(source, target)
            target_name = target.name
        url = f"/video_shorts/static/img/blog/{quote(slug)}/{quote(target_name)}"
        alt = str(screenshot.get("alt") or visual.get("alt") or "")
        content = content.replace(f"<!-- {marker} -->", f"![{alt}]({url})")
        placed.append({"marker": marker, "filename": target_name, "url": url, "alt": alt})
    article["content_md"] = content
    article["screenshots_placed"] = placed
    return article


def _image_prompt_for_cover(article: dict[str, Any]) -> str:
    cover = article.get("cover") if isinstance(article.get("cover"), dict) else {}
    prompt = str(cover.get("prompt") or cover.get("description") or "").strip()
    if prompt:
        return prompt
    return f"Editorial cover illustration for a MintiStudio blog article titled: {article.get('title') or 'MintiStudio guide'}"


def _image_result_payload(result: BlogImageResult) -> dict[str, Any]:
    payload = dict(result.__dict__)
    payload["cost_usd"] = str(result.cost_usd)
    return payload


def _replace_generated_placeholder(content: str, marker: str, markdown: str) -> str:
    return re.sub(r"<!--\s*" + re.escape(marker) + r"\s*-->", markdown, content, count=1)


def _article_image_urls(article: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    cover = str(article.get("cover_image_url") or "").strip()
    if cover:
        urls.append(cover)
    urls.extend(re.findall(r"!\[[^\]]*\]\((/video_shorts/static/img/blog/[^)]+)\)", str(article.get("content_md") or "")))
    absolute: list[str] = []
    for url in urls:
        absolute.append(url if url.startswith("http") else f"{BASE_URL}{url}")
    return absolute


def _no_publish_reason(*, run_status: str, article: dict[str, Any], checks: dict[str, Any], final_score: int, image_status: str) -> str | None:
    content = str(article.get("content_md") or "")
    if run_status != "draft_ready":
        if image_status != "done":
            return "image failure"
        if final_score < BLOG_REVIEW_PASS:
            return f"reviewer score {final_score} below pass threshold {BLOG_REVIEW_PASS}"
        if _blocking_issues({}, checks):
            return "blocking check issues"
        return run_status
    if "FACT-CHECK REQUIRED" in content:
        return "FACT-CHECK REQUIRED marker present"
    if re.search(r"<!--\s*IMAGE_[123]\s*-->", content):
        return "remaining IMAGE placeholder"
    if not article.get("cover_image_url"):
        return "missing cover_image_url"
    if not checks.get("ok"):
        return "final checks failed: " + "; ".join(checks.get("issues") or [])
    return None


def _notify_run(
    *,
    state: PipelineState | None,
    status_label: str,
    title: str,
    article: dict[str, Any] | None = None,
    article_id: int | None = None,
    reviewer_score: int | None = None,
    total_cost: str | None = None,
    reason: str | None = None,
    published: bool = False,
) -> dict[str, Any]:
    article = article or {}
    if state and state.run_id and not state.dry_run:
        check_conn = get_db()
        try:
            if run_was_notified(check_conn, state.run_id):
                return {"status": "skipped", "notes": "already notified", "payload": {}}
        finally:
            check_conn.close()
    if status_label == "published":
        subject = f"[Minti Blog] Published — {title}"
    elif status_label == "failed":
        subject = f"[Minti Blog] Run failed — {title}"
    else:
        subject = f"[Minti Blog] Needs your review — {title} ({reason or 'review needed'})"
    try:
        result = send_blog_run_notification(
            subject=subject,
            status_label=status_label,
            title=title,
            public_url=_public_blog_url(str(article.get("slug") or "")) if published and article.get("slug") else None,
            admin_edit_url=_admin_edit_url(article_id),
            run_detail_url=_run_detail_url(state.run_id if state else None),
            reviewer_score=reviewer_score,
            total_cost=total_cost,
            images=_article_image_urls(article),
            reason=reason,
            to_email=BLOG_NOTIFY_EMAIL,
        )
        payload = {"email": result}
        status = "done"
        notes = "SMTP accepted"
    except Exception as exc:
        payload = {"error_class": exc.__class__.__name__, "message": str(exc)[:500]}
        status = "failed"
        notes = str(exc)[:1000]
    if state and state.run_id and not state.dry_run:
        conn = get_db()
        try:
            row = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM blog_pipeline_stages WHERE run_id = ?", [state.run_id]).fetchone()
            record_stage(conn, run_id=state.run_id, seq=int(row[0] or 0) + 1, stage="notification", status=status, output=payload, notes=notes)
            if status == "done":
                mark_run_notified(conn, state.run_id)
            conn.commit()
        finally:
            conn.close()
    return {"status": status, "notes": notes, "payload": payload}


def _publish_if_clean(
    *,
    article_id: int,
    article: dict[str, Any],
    checks: dict[str, Any],
    run_status: str,
    final_score: int,
    image_status: str,
) -> tuple[bool, str | None, dict[str, Any]]:
    reason = _no_publish_reason(run_status=run_status, article=article, checks=checks, final_score=final_score, image_status=image_status)
    verification: dict[str, Any] = {}
    if not BLOG_AUTO_PUBLISH:
        return False, "BLOG_AUTO_PUBLISH disabled", verification
    if reason:
        return False, reason, verification
    updated = update_blog_article_status(article_id, "published")
    if not updated:
        return False, "publish helper returned no update", verification
    public_url = _public_blog_url(str(article["slug"]))
    verification["public_url"] = public_url
    verification["http_status"] = _http_status(public_url)
    verification["sitemap"] = _sitemap_status(public_url)
    return True, None, verification


def _run_image_stage(
    *,
    state: PipelineState,
    article: dict[str, Any],
    article_id: int | None = None,
    low_medium_test: bool = False,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    set_current = state.run_id and not state.dry_run
    conn = get_db() if set_current else None
    try:
        if conn:
            set_current_stage(conn, state.run_id, "images")
            conn.commit()
    finally:
        if conn:
            conn.close()
    output: dict[str, Any] = {"cover": None, "visuals": [], "errors": [], "test_variants": []}
    run_status = "done"
    slug = str(article["slug"])
    try:
        _check_run_budget(state)
        cover_result = generate_blog_image(
            prompt=_image_prompt_for_cover(article),
            slug=slug,
            filename="cover.png",
            kind="cover",
            marker=None,
            alt=str(article.get("title") or "Blog cover"),
            model=BLOG_IMAGE_COVER,
            quality=BLOG_IMAGE_COVER_QUALITY,
            topic_id=int(state.topic["id"]),
            run_id=state.run_id,
            overwrite=True,
            dry_run=state.dry_run,
        )
        article["cover_image_url"] = cover_result.url
        output["cover"] = _image_result_payload(cover_result)
        state.run_cost += cover_result.cost_usd
    except Exception as exc:
        run_status = "needs_you"
        output["errors"].append({"kind": "cover", "error_class": exc.__class__.__name__, "message": str(exc)[:300]})

    content = str(article.get("content_md") or "")
    generate_index = 1
    for visual in article.get("visuals") or []:
        marker = _normalize_marker(visual.get("marker") or visual.get("id"))
        if visual.get("type") != "generate" or marker not in {"IMAGE_1", "IMAGE_2", "IMAGE_3"}:
            continue
        prompt = str(visual.get("prompt") or visual.get("description") or "").strip()
        filename = generated_visual_filename(visual, generate_index)
        generate_index += 1
        try:
            _check_run_budget(state)
            result = generate_blog_image(
                prompt=prompt,
                slug=slug,
                filename=filename,
                kind="inline",
                marker=marker,
                alt=str(visual.get("alt") or marker),
                model=BLOG_IMAGE_INLINE,
                quality=BLOG_IMAGE_INLINE_QUALITY,
                topic_id=int(state.topic["id"]),
                run_id=state.run_id,
                overwrite=True,
                dry_run=state.dry_run,
            )
            output["visuals"].append(_image_result_payload(result))
            state.run_cost += result.cost_usd
            content = _replace_generated_placeholder(content, marker, render_markdown_image(visual, result))
            if low_medium_test and marker == "IMAGE_1":
                medium_filename = filename.rsplit(".", 1)[0] + "-medium.png"
                _check_run_budget(state)
                medium = generate_blog_image(
                    prompt=prompt,
                    slug=slug,
                    filename=medium_filename,
                    kind="inline",
                    marker=marker,
                    alt=str(visual.get("alt") or marker),
                    model=BLOG_IMAGE_INLINE,
                    quality="medium",
                    topic_id=int(state.topic["id"]),
                    run_id=state.run_id,
                    overwrite=True,
                    dry_run=state.dry_run,
                )
                state.run_cost += medium.cost_usd
                output["test_variants"].append({"low": _image_result_payload(result), "medium": _image_result_payload(medium)})
        except Exception as exc:
            run_status = "needs_you"
            output["errors"].append({"kind": "inline", "marker": marker, "error_class": exc.__class__.__name__, "message": str(exc)[:300]})
    article["content_md"] = content
    if article_id and not state.dry_run:
        conn = get_db()
        try:
            conn.execute(
                """
                UPDATE blog_articles
                SET content = ?, cover_image_url = ?
                WHERE id = ?
                """,
                [article.get("content_md"), article.get("cover_image_url"), int(article_id)],
            )
            conn.commit()
        finally:
            conn.close()
    return article, output, run_status


def _save_draft(conn, *, state: PipelineState, article: dict[str, Any], visuals_plan: dict[str, Any], run_status: str) -> int:
    _rename_archived_pipeline_slug_conflict(conn, str(article["slug"]))
    row = conn.execute(
        """
        INSERT INTO blog_articles (
            title, slug, summary, content, cover_image_url, meta_title, meta_description,
            author_name, reading_time, view_count, status, published_at, import_source, import_source_id
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, 'MintiStudio Team', ?, 0, 'draft', NULL, 'blog_pipeline', ?)
        RETURNING id
        """,
        [
            article["title"],
            article["slug"],
            article.get("summary"),
            article.get("content_md"),
            article.get("cover_image_url"),
            article.get("meta_title"),
            article.get("meta_description"),
            article.get("reading_time"),
            str(state.run_id or "dry-run"),
        ],
    ).fetchone()
    article_id = int(row[0])
    topic_status = "draft_ready" if run_status == "draft_ready" else "in_production"
    conn.execute("UPDATE blog_topics SET status = ? WHERE id = ?", [topic_status, int(state.topic["id"])])
    record_stage(
        conn,
        run_id=state.run_id,
        seq=state.seq + 1,
        stage="final",
        status="done",
        output={"article_id": article_id, "cover": article.get("cover"), "visuals": visuals_plan},
    )
    return article_id


def _stage_output_from_run(conn, run_id: int, stage_names: tuple[str, ...]) -> dict[str, Any]:
    placeholders = ",".join(["?"] * len(stage_names))
    row = conn.execute(
        f"""
        SELECT output
        FROM blog_pipeline_stages
        WHERE run_id = ?
          AND stage IN ({placeholders})
        ORDER BY seq DESC, id DESC
        LIMIT 1
        """,
        [int(run_id), *stage_names],
    ).fetchone()
    if not row or not row[0]:
        return {}
    try:
        return json.loads(row[0]) if isinstance(row[0], str) else dict(row[0])
    except Exception:
        return {}


def _article_for_existing_draft(conn, *, article_id: int, run_id: int) -> tuple[dict[str, Any], dict[str, Any]]:
    row = conn.execute(
        """
        SELECT id, title, slug, summary, content, cover_image_url, meta_title, meta_description, reading_time
        FROM blog_articles
        WHERE id = ?
        LIMIT 1
        """,
        [int(article_id)],
    ).fetchone()
    if not row:
        raise RuntimeError(f"blog article {article_id} was not found")
    topic_row = conn.execute(
        """
        SELECT t.id, t.title, t.primary_keyword, t.category, t.intent, t.brief
        FROM blog_pipeline_runs r
        LEFT JOIN blog_topics t ON t.id = r.topic_id
        WHERE r.id = ?
        LIMIT 1
        """,
        [int(run_id)],
    ).fetchone()
    if not topic_row:
        raise RuntimeError(f"blog pipeline run {run_id} was not found")
    article = {
        "id": int(row[0]),
        "title": row[1],
        "slug": row[2],
        "summary": row[3],
        "content_md": row[4],
        "cover_image_url": row[5],
        "meta_title": row[6],
        "meta_description": row[7],
        "reading_time": row[8],
    }
    final_output = _stage_output_from_run(conn, run_id, ("final",))
    designer_output = _stage_output_from_run(conn, run_id, ("designer",))
    writer_output = _stage_output_from_run(conn, run_id, ("revision_2", "revision_1", "writer"))
    plan = final_output.get("visuals") if isinstance(final_output.get("visuals"), dict) else {}
    visuals = plan.get("visuals") if isinstance(plan, dict) else None
    if not visuals:
        visuals = designer_output.get("visuals") or writer_output.get("visuals") or []
    article["visuals"] = visuals
    article["cover"] = (plan or {}).get("cover") or writer_output.get("cover") or {}
    topic = {
        "id": int(topic_row[0]),
        "title": topic_row[1],
        "primary_keyword": topic_row[2],
        "category": topic_row[3],
        "intent": topic_row[4],
        "brief": topic_row[5],
    }
    return article, topic


def run_images_for_existing_draft(*, run_id: int, article_id: int, low_medium_test: bool = False) -> dict[str, Any]:
    conn = get_db()
    try:
        article, topic = _article_for_existing_draft(conn, article_id=article_id, run_id=run_id)
        state = PipelineState(topic=topic, run_id=int(run_id), dry_run=False)
        row = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM blog_pipeline_stages WHERE run_id = ?", [int(run_id)]).fetchone()
        state.seq = int(row[0] or 0)
        conn.commit()
    finally:
        conn.close()
    article, image_output, image_status = _run_image_stage(
        state=state,
        article=article,
        article_id=article_id,
        low_medium_test=low_medium_test,
    )
    image_cost = sum((Decimal(str((item or {}).get("cost_usd") or "0")) for item in ([image_output.get("cover")] + list(image_output.get("visuals") or []))), Decimal("0"))
    for variant in image_output.get("test_variants") or []:
        image_cost += Decimal(str((variant.get("medium") or {}).get("cost_usd") or "0"))
    conn = get_db()
    try:
        record_stage(
            conn,
            run_id=run_id,
            seq=state.seq + 1,
            stage="images",
            status=image_status,
            model=f"{BLOG_IMAGE_COVER}/{BLOG_IMAGE_INLINE}",
            output=image_output,
            notes="; ".join(f"{item.get('kind')} {item.get('marker') or ''}: {item.get('message')}" for item in image_output.get("errors") or [])[:1000],
            cost_usd=image_cost,
        )
        if image_status != "done":
            finish_run(conn, run_id, status="needs_you", article_id=article_id, error="Image generation needs review")
        else:
            finish_run(conn, run_id, status="draft_ready", article_id=article_id)
        conn.commit()
    finally:
        conn.close()
    return {"status": image_status, "article_id": article_id, "run_id": run_id, "images": image_output, "image_cost_usd": str(image_cost)}


def _writer_prompt() -> str:
    return """You are the MintiStudio blog Writer. Return strict JSON only. Write original, practical long-form blog content for the supplied topic. Use the stable context as binding instructions.

Required JSON keys: title, slug, summary, content_md, meta_title, meta_description, reading_time, cover, visuals.
content_md must include exactly these three placeholders as standalone HTML comments: <!-- IMAGE_1 -->, <!-- IMAGE_2 -->, <!-- IMAGE_3 -->. Bare IMAGE_1 text is forbidden.
visuals must contain exactly 3 items with marker values IMAGE_1, IMAGE_2, IMAGE_3. At most 2 may be screenshots. Use type=\"generate\" for the remaining visual and provide a prompt.
Use only the supplied published_articles URLs for internal links. Do not invent blog URLs.
Never claim anything about MintiStudio unless it is in minti_facts.md.
Never write sentences that disclaim, hedge, or caution about MintiStudio itself. If a Minti detail is not in minti_facts.md, omit it. Make an honest, clear case for Autopilot where it genuinely fits and tie Minti features to the reader's problem.
Use the primary keyword naturally, with correct hyphenation such as "done-for-you"; never place it as a standalone bolded SEO phrase.
When MintiStudio features are mentioned, tie each one to the reader's task in the same sentence; never use a comma-separated feature list.
Screenshot placement: if the article covers Autopilot, prefer placing a relevant screenshot in or near the Autopilot or "Where MintiStudio helps" section when the screenshot library has a suitable image."""


def _reviewer_prompt() -> str:
    return """You are the MintiStudio blog Reviewer. Return strict JSON only. Score the article against the supplied facts, checks, screenshots, published URLs, and existing titles. Be concrete and conservative.

Return JSON with total, scores, blocking_issues, fixes.
Any deterministic check issue must be copied into blocking_issues.
Blocking issues regardless of total score: MintiStudio self-disclaimers or hedges; any internal link not exactly in published_articles or the CTA URL; bare IMAGE_n placeholder text; missing exact IMAGE comment placeholders; visuals not exactly 3 items.
Non-blocking fix: a MintiStudio section that reads as a feature list without tying features to the reader's problem."""


def _revision_prompt() -> str:
    return """You are the MintiStudio blog Reviser. Return strict JSON only. Apply only the listed reviewer fixes and blocking issues. Preserve valid metadata, links, cover, visuals, and exact IMAGE comment placeholders unless a fix explicitly requires changing them. Never drop the visuals array."""


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
        article = _normalize_article_payload(article)
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
        review_1_score = _review_score(review_1)
        record_stage(conn, run_id=state.run_id, seq=state.seq, stage="reviewer_1", status="done", model=BLOG_MODEL_REVIEWER, output=review_1, score=review_1_score, cost_usd=cost)
        conn.commit()

        final_review = review_1
        if review_1_score < BLOG_REVIEW_PASS or _blocking_issues(review_1, checks):
            revision_payload = {**user_base, "article": article, "review": review_1}
            previous_visuals = list(article.get("visuals") or [])
            article, cost = _call_stage(state, "revision_1", BLOG_MODEL_WRITER, _revision_prompt(), revision_payload)
            if not article.get("visuals") and previous_visuals:
                article["visuals"] = previous_visuals
            article = _normalize_article_payload(article)
            state.seq += 1
            record_stage(conn, run_id=state.run_id, seq=state.seq, stage="revision_1", status="done", model=BLOG_MODEL_WRITER, output=article, cost_usd=cost)
            checks = _code_checks(article, screenshots, published)
            state.seq += 1
            record_stage(conn, run_id=state.run_id, seq=state.seq, stage="checks", status="done" if checks["ok"] else "failed", output=checks, notes="; ".join(checks["issues"]))
            reviewer_payload = {**user_base, "article": article, "checks": checks, "existing_titles": [item["title"] for item in published]}
            review_2, cost = _call_stage(state, "reviewer_2", BLOG_MODEL_REVIEWER, _reviewer_prompt(), reviewer_payload)
            final_review = review_2
            state.seq += 1
            review_2_score = _review_score(review_2)
            record_stage(conn, run_id=state.run_id, seq=state.seq, stage="reviewer_2", status="done", model=BLOG_MODEL_REVIEWER, output=review_2, score=review_2_score, cost_usd=cost)
            conn.commit()
            if review_2_score < BLOG_REVIEW_PASS or _blocking_issues(review_2, checks):
                previous_visuals = list(article.get("visuals") or [])
                article, cost = _call_stage(state, "revision_2", BLOG_MODEL_WRITER, _revision_prompt(), {**user_base, "article": article, "review": review_2})
                if not article.get("visuals") and previous_visuals:
                    article["visuals"] = previous_visuals
                article = _normalize_article_payload(article)
                state.seq += 1
                record_stage(conn, run_id=state.run_id, seq=state.seq, stage="revision_2", status="done", model=BLOG_MODEL_WRITER, output=article, cost_usd=cost)
                checks = _code_checks(article, screenshots, published)
                state.seq += 1
                record_stage(conn, run_id=state.run_id, seq=state.seq, stage="checks", status="done" if checks["ok"] else "failed", output=checks, notes="; ".join(checks["issues"]))
                conn.commit()

        before_design = str(article.get("content_md") or "")
        designer_payload = {
            "stable_context": {
                "allowed_components": prefix["allowed_components"],
                "style_guide": prefix["style_guide"],
            },
            "content_md": article.get("content_md") or "",
        }
        designer_output, cost = _call_stage(state, "designer", BLOG_MODEL_DESIGNER, _designer_prompt(), designer_payload)
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

        checks = _code_checks(article, screenshots, published)
        state.seq += 1
        record_stage(conn, run_id=state.run_id, seq=state.seq, stage="checks", status="done" if checks["ok"] else "failed", output=checks, notes="; ".join(checks["issues"]))
        conn.commit()
        if not checks.get("ok"):
            raise RuntimeError("pre-save blog checks failed: " + "; ".join(checks.get("issues") or []))

        article = _replace_screenshot_placeholders(article, screenshots, copy_files=not dry_run)
        article, image_output, image_status = _run_image_stage(state=state, article=article)
        image_cost = sum((Decimal(str((item or {}).get("cost_usd") or "0")) for item in ([image_output.get("cover")] + list(image_output.get("visuals") or []))), Decimal("0"))
        for variant in image_output.get("test_variants") or []:
            image_cost += Decimal(str((variant.get("medium") or {}).get("cost_usd") or "0"))
        state.seq += 1
        record_stage(
            conn,
            run_id=state.run_id,
            seq=state.seq,
            stage="images",
            status=image_status,
            model=f"{BLOG_IMAGE_COVER}/{BLOG_IMAGE_INLINE}",
            output=image_output,
            notes="; ".join(f"{item.get('kind')} {item.get('marker') or ''}: {item.get('message')}" for item in image_output.get("errors") or [])[:1000],
            cost_usd=image_cost,
        )
        conn.commit()
        run_status = "draft_ready"
        final_score = _review_score(final_review)
        if image_status != "done" or final_score < BLOG_REVIEW_PASS or _blocking_issues(final_review, checks) or not checks.get("ok"):
            run_status = "needs_you"

        result = {
            "status": run_status,
            "topic": topic,
            "article": article,
            "review": final_review,
            "checks": checks,
            "designer_guard": guard_note,
            "images": image_output,
            "total_cost_usd": str(state.run_cost),
        }
        if dry_run:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return result

        article_id = _save_draft(conn, state=state, article=article, visuals_plan={"cover": article.get("cover"), "visuals": article.get("visuals"), "screenshots_placed": article.get("screenshots_placed")}, run_status=run_status)
        finish_run(conn, state.run_id, status=run_status, article_id=article_id, final_review_score=final_score)
        conn.commit()
        result["article_id"] = article_id
        published, publish_reason, publish_verification = _publish_if_clean(
            article_id=article_id,
            article=article,
            checks=checks,
            run_status=run_status,
            final_score=final_score,
            image_status=image_status,
        )
        result["published"] = published
        result["publish_reason"] = publish_reason
        result["publish_verification"] = publish_verification
        conn = get_db()
        try:
            row = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM blog_pipeline_stages WHERE run_id = ?", [state.run_id]).fetchone()
            record_stage(
                conn,
                run_id=state.run_id,
                seq=int(row[0] or 0) + 1,
                stage="publish",
                status="done" if published else "skipped",
                output={"published": published, "reason": publish_reason, "verification": publish_verification},
                notes=publish_reason,
            )
            conn.commit()
        finally:
            conn.close()
        if published:
            conn = get_db()
            try:
                finish_run(conn, state.run_id, status="published", article_id=article_id, final_review_score=final_score)
                conn.commit()
            finally:
                conn.close()
            run_status = "published"
            result["status"] = "published"
        notification = _notify_run(
            state=state,
            status_label="published" if published else "draft_kept",
            title=str(article.get("title") or topic.get("title") or "Untitled"),
            article=article,
            article_id=article_id,
            reviewer_score=final_score,
            total_cost=str(state.run_cost),
            reason=publish_reason,
            published=published,
        )
        result["notification"] = notification
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
            _notify_run(
                state=state,
                status_label="failed",
                title=str(state.topic.get("title") or "Untitled topic"),
                reviewer_score=None,
                total_cost=str(state.run_cost),
                reason=str(exc)[:500],
            )
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
    parser.add_argument("--images-only", action="store_true", help="Run the image stage for an existing pipeline draft.")
    parser.add_argument("--run-id", type=int, default=None, help="Existing blog_pipeline_runs id for --images-only.")
    parser.add_argument("--article-id", type=int, default=None, help="Existing blog_articles id for --images-only.")
    parser.add_argument("--low-medium-test", action="store_true", help="Generate IMAGE_1 low and medium variants for comparison.")
    parser.add_argument("--manifest-report", action="store_true", help="Print screenshot manifest availability and exit.")
    args = parser.parse_args()
    if args.manifest_report:
        print(json.dumps(_all_manifest_entries(), ensure_ascii=False, indent=2))
        return 0
    if args.images_only:
        if not args.run_id or not args.article_id:
            raise SystemExit("--images-only requires --run-id and --article-id")
        result = run_images_for_existing_draft(run_id=args.run_id, article_id=args.article_id, low_medium_test=args.low_medium_test)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0
    run_pipeline(topic_id=args.topic_id, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
