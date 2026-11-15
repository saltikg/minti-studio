#!/usr/bin/env python3
"""Apply the reviewed search CTR metadata to the three published blog rows."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from scripts.safe_to_deploy import _load_env_file
except ModuleNotFoundError:  # Direct execution from the scripts directory.
    from safe_to_deploy import _load_env_file


METADATA = {
    "can-your-shorts-stay-original-when-you-repurpose-your-own-long-videos": (
        "Keep Repurposed YouTube Shorts Original | Minti Studio",
        "Learn how to turn your own long videos into original Shorts while preserving your voice, adding fresh context, and following YouTube policy.",
    ),
    "done-for-you-vs-diy-video-clipping": (
        "Done-for-You Shorts vs DIY Clipping | Minti Studio",
        "Compare DIY clipping with a done-for-you workflow where we make and schedule Shorts from your long videos. See which approach fits your week.",
    ),
    "free-youtube-to-shorts-tool": (
        "YouTube to Shorts for Free: Step-by-Step | Minti Studio",
        "A step-by-step guide to turning a YouTube video into a captioned Short: paste the link, pick the moment, edit, and download. Free to try.",
    ),
}


def _snapshot(conn, slug: str) -> dict[str, str]:
    row = conn.execute(
        """
        SELECT slug, meta_title, meta_description
        FROM blog_articles
        WHERE slug = ? AND status = 'published'
        LIMIT 1
        """,
        [slug],
    ).fetchone()
    if not row:
        raise RuntimeError(f"Published article not found: {slug}")
    return {
        "slug": str(row[0]),
        "meta_title": str(row[1] or ""),
        "meta_description": str(row[2] or ""),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Commit the reviewed metadata updates.")
    args = parser.parse_args()

    _load_env_file()
    from app.video_shorts.services.db import get_db

    conn = get_db()
    changes: list[dict[str, object]] = []
    try:
        for slug, (meta_title, meta_description) in METADATA.items():
            before = _snapshot(conn, slug)
            conn.execute(
                """
                UPDATE blog_articles
                SET meta_title = ?, meta_description = ?, updated_at = CURRENT_TIMESTAMP
                WHERE slug = ? AND status = 'published'
                """,
                [meta_title, meta_description, slug],
            )
            after = _snapshot(conn, slug)
            changes.append({"before": before, "after": after})
        if args.apply:
            conn.commit()
        else:
            conn.rollback()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    print(json.dumps({"mode": "APPLIED" if args.apply else "DRY_RUN", "changes": changes}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
