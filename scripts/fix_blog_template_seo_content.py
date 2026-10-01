#!/home/ubuntu/apps/minti_studio/.venv/bin/python
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.video_shorts.services.db import get_db  # noqa: E402


LOGIN_CTA = "https://mintistudio.com/video_shorts/login"
REGISTER_CTA = "https://mintistudio.com/video_shorts/register"

ALT_REPLACEMENTS = {
    "![Alt text](/video_shorts/static/img/blog/weekly-shorts-workflow-for-busy-creators/weekly-shorts-workflow-for-busy-creators.png)": (
        "![Weekly Shorts workflow calendar and clip cards](/video_shorts/static/img/blog/weekly-shorts-workflow-for-busy-creators/weekly-shorts-workflow-for-busy-creators.png)"
    ),
    "![Alt text](/video_shorts/static/img/blog/shorts-are-not-the-whole-youtube-strategy/better-shorts-checklist.png)": (
        "![Checklist for making a Short stand on its own](/video_shorts/static/img/blog/shorts-are-not-the-whole-youtube-strategy/better-shorts-checklist.png)"
    ),
    "![Alt text](/video_shorts/static/img/blog/shorts-are-not-the-whole-youtube-strategy/shorts-learning-loop-minimal.png)": (
        "![Shorts learning loop from publishing to improving](/video_shorts/static/img/blog/shorts-are-not-the-whole-youtube-strategy/shorts-learning-loop-minimal.png)"
    ),
}


def _repair_content(content: str) -> tuple[str, list[str]]:
    repaired = str(content or "")
    notes: list[str] = []
    if LOGIN_CTA in repaired:
        repaired = repaired.replace(LOGIN_CTA, REGISTER_CTA)
        notes.append("login CTA changed to register")
    for before, after in ALT_REPLACEMENTS.items():
        if before in repaired:
            repaired = repaired.replace(before, after)
            notes.append(f"placeholder alt replaced: {after.split(']', 1)[0][2:]}")
    return repaired, notes


def main() -> int:
    parser = argparse.ArgumentParser(description="Fix published blog body SEO placeholders.")
    parser.add_argument("--apply", action="store_true", help="Write repaired content. Without this, prints a dry-run list.")
    args = parser.parse_args()

    conn = get_db()
    try:
        rows = conn.execute(
            """
            SELECT id, slug, content
            FROM blog_articles
            WHERE status = 'published'
              AND (
                content LIKE ?
                OR content LIKE ?
              )
            ORDER BY published_at DESC NULLS LAST, created_at DESC
            """,
            [f"%{LOGIN_CTA}%", "%Alt text%"],
        ).fetchall()
        repairs: list[tuple[int, str, str, list[str]]] = []
        for article_id, slug, content in rows:
            repaired, notes = _repair_content(content or "")
            if notes and repaired != (content or ""):
                repairs.append((int(article_id), str(slug or ""), repaired, notes))

        print(("APPLY" if args.apply else "DRY_RUN") + f" repairs: {len(repairs)}")
        for _article_id, slug, _repaired, notes in repairs:
            print(f"{slug}: " + "; ".join(notes))

        if not args.apply:
            return 0

        for article_id, _slug, repaired, _notes in repairs:
            conn.execute(
                """
                UPDATE blog_articles
                SET content = ?,
                    content_updated_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                [repaired, article_id],
            )
        conn.commit()
        print(f"UPDATED {len(repairs)} published posts")
        return 0
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
