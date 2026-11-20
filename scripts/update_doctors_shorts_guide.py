#!/usr/bin/env python3
"""Apply the reviewed doctors Shorts guide corrections to the published row."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.safe_to_deploy import _load_env_file  # noqa: E402


OLD_SLUG = "youtube-shorts-for-doctors-practical-guide-educational-clips-without-extra-admin"
TARGET_SLUG = "youtube-shorts-for-doctors-guide"
TARGET_TITLE = "YouTube Shorts for Doctors: The Complete Guide"
META_TITLE = TARGET_TITLE
META_DESCRIPTION = (
    "Learn how doctors can create professional YouTube Shorts from long videos while protecting context, patient privacy, and their time."
)
AUTOPILOT_DESCRIPTION = (
    "Minti prepares, captions and schedules Shorts from your long videos; you can change or remove any Short before it goes live — no approval step required."
)
PRICE_RE = re.compile(r"\$\s*\d|\bper\s+month\b|/\s*mo\b", re.I)


def rewrite_content(content: str) -> str:
    text = str(content or "")
    text, section_count = re.subn(
        r"(?ms)^## DIY or Autopilot: a simple choice\s*\n.*?(?=^##\s+|\Z)",
        "",
        text,
        count=1,
    )
    if section_count != 1:
        raise RuntimeError("Expected exactly one DIY or Autopilot section")

    text, autopilot_count = re.subn(
        r"(?m)^If your bottleneck is not ideas but time, Autopilot is the cleanest fit\..*$",
        AUTOPILOT_DESCRIPTION,
        text,
        count=1,
    )
    if autopilot_count != 1:
        raise RuntimeError("Expected the existing Where Minti Studio helps Autopilot paragraph")

    text, example_count = re.subn(
        r"(?m)^If you want more control, the DIY plans let you work through the process yourself\..*$",
        "",
        text,
        count=1,
    )
    if example_count != 1:
        raise RuntimeError("Expected the practical-example Autopilot pitch")

    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if PRICE_RE.search(text):
        raise RuntimeError("A price mention remains in the edited article")
    if text.count(AUTOPILOT_DESCRIPTION) != 1:
        raise RuntimeError("Correct Autopilot description must appear exactly once")

    sections: dict[str, str] = {}
    parts = re.split(r"(?m)^##\s+(.+?)\s*$", text)
    for index in range(1, len(parts), 2):
        sections[parts[index].strip()] = parts[index + 1]
    autopilot_sections = [heading for heading, body in sections.items() if "Autopilot" in body]
    allowed = {"Where Minti Studio helps", "The practical next step"}
    if set(autopilot_sections) - allowed:
        raise RuntimeError(f"Autopilot pitch remains outside allowed sections: {autopilot_sections}")
    return text


def apply_update(conn) -> int:
    rows = conn.execute(
        "SELECT id, slug, content, status FROM blog_articles WHERE slug IN (?, ?)",
        [OLD_SLUG, TARGET_SLUG],
    ).fetchall()
    if len(rows) != 1:
        raise RuntimeError(f"Expected exactly one doctors guide row, found {len(rows)}")
    article_id, _slug, content, status = rows[0]
    if str(status or "") != "published":
        raise RuntimeError("Doctors guide is not published")
    updated_content = rewrite_content(str(content or ""))
    reading_time = max(1, round(len(re.findall(r"\b[\w'-]+\b", updated_content)) / 220))
    conn.execute(
        """
        UPDATE blog_articles
        SET title = ?, slug = ?, content = ?, meta_title = ?, meta_description = ?,
            reading_time = ?, content_updated_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        [TARGET_TITLE, TARGET_SLUG, updated_content, META_TITLE, META_DESCRIPTION, reading_time, int(article_id)],
    )
    return int(article_id)


def review_updated_article(article_id: int) -> dict:
    from app.video_shorts.services.blog_llm import BLOG_MODEL_REVIEWER, call_json, log_usage
    from app.video_shorts.services.db import get_db
    from scripts import blog_pipeline

    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id FROM blog_pipeline_runs WHERE article_id = ? ORDER BY id DESC LIMIT 1",
            [int(article_id)],
        ).fetchone()
        if not row:
            raise RuntimeError("No pipeline run found for the doctors guide")
        run_id = int(row[0])
        article, topic = blog_pipeline._article_for_existing_draft(
            conn,
            article_id=int(article_id),
            run_id=run_id,
        )
    finally:
        conn.close()

    topic["cluster_slug"] = "doctors"
    published = blog_pipeline._published_articles()
    payload = {
        "stable_context": blog_pipeline._stable_prefix(),
        "topic": topic,
        "published_articles": published,
        "requirements": {
            "cta_url": blog_pipeline.CTA_URL,
            "cluster_audience_rule": (
                "Doctors-cluster posts are about channel growth, publishing workflow, professional context, "
                "and privacy-safe operations. Do not provide medical advice, diagnosis or treatment claims, "
                "invent statistics, or encourage sharing patient information."
            ),
        },
        "article": article,
        "checks": {"ok": True, "issues": [], "blocking_issues": [], "fixes": []},
        "existing_titles": [item["title"] for item in published],
    }
    result = call_json(
        "reviewer_manual",
        model=BLOG_MODEL_REVIEWER,
        system_prompt=blog_pipeline._reviewer_prompt("doctors", pre_image=False),
        user_prompt=blog_pipeline.json_dumps_compact(payload),
    )
    log_usage("reviewer_manual", result, topic_id=int(topic["id"]), run_id=run_id)
    return json.loads(result.content)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--review", action="store_true")
    args = parser.parse_args()
    _load_env_file()
    from app.video_shorts.services.db import get_db

    conn = get_db()
    try:
        article_id = apply_update(conn)
        conn.commit() if args.apply else conn.rollback()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    print(f"{'APPLIED' if args.apply else 'DRY_RUN'} article_id={article_id} slug={TARGET_SLUG}")
    if args.review:
        if not args.apply:
            raise RuntimeError("--review requires --apply so the reviewer sees the edited post")
        print("REVIEW=" + json.dumps(review_updated_article(article_id), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
