#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.video_shorts.services.comment_store import ensure_comment_cache_schema  # noqa: E402
from app.video_shorts.services.db import get_db  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Report comments that would have been auto-hidden in shadow mode."
    )
    parser.add_argument(
        "--start",
        default="1970-01-01",
        help="Inclusive lower bound for auto_moderation_at (ISO date or timestamp).",
    )
    parser.add_argument(
        "--end",
        default="9999-12-31",
        help="Inclusive upper bound for auto_moderation_at (ISO date or timestamp).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=500,
        help="Maximum rows to print.",
    )
    args = parser.parse_args()

    conn = get_db()
    try:
        ensure_comment_cache_schema(conn)
        rows = conn.execute(
            """
            SELECT
                platform,
                comment_id,
                author,
                text,
                moderation_reason,
                auto_moderation_action,
                auto_moderation_at,
                created_at
            FROM social_comment_cache
            WHERE auto_moderation_action = ?
              AND auto_moderation_at >= ?
              AND auto_moderation_at <= ?
            ORDER BY auto_moderation_at DESC, created_at DESC
            LIMIT ?
            """,
            [
                "would_hide",
                str(args.start).strip(),
                str(args.end).strip(),
                max(1, int(args.limit)),
            ],
        ).fetchall()

        print(
            "platform\tcomment_id\tauthor\treason\tauto_action\tauto_moderation_at\tcreated_at\ttext"
        )
        for row in rows:
            platform, comment_id, author, text, reason, action, auto_at, created_at = row
            clean_text = " ".join(str(text or "").split())
            print(
                f"{platform}\t{comment_id}\t{author or ''}\t{reason or ''}\t{action or ''}\t"
                f"{auto_at or ''}\t{created_at or ''}\t{clean_text}"
            )
        print(f"rows={len(rows)}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
