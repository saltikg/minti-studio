#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.video_shorts.services.db import get_db  # noqa: E402


def _to_iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    text = str(value).strip()
    return text or None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backfill missing published_at for Instagram comments."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write updates. Without this flag, runs as a dry-run.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=10000,
        help="Max rows to inspect per run.",
    )
    args = parser.parse_args()

    conn = get_db()
    try:
        rows = conn.execute(
            """
            SELECT comment_id, created_at, updated_at
            FROM social_comment_cache
            WHERE platform = ?
              AND (published_at IS NULL OR TRIM(CAST(published_at AS VARCHAR)) = '')
            ORDER BY created_at DESC
            LIMIT ?
            """,
            ["instagram", max(1, int(args.limit))],
        ).fetchall()

        updates: list[tuple[str, str]] = []
        for comment_id, created_at, updated_at in rows:
            fallback = _to_iso(created_at) or _to_iso(updated_at)
            if not fallback:
                continue
            updates.append((fallback, str(comment_id)))

        print(
            f"found={len(rows)} updatable={len(updates)} mode={'apply' if args.apply else 'dry-run'}"
        )

        if not args.apply or not updates:
            for fallback, comment_id in updates[:20]:
                print(f"candidate comment_id={comment_id} published_at={fallback}")
            return 0

        conn.executemany(
            """
            UPDATE social_comment_cache
            SET published_at = ?
            WHERE platform = ?
              AND comment_id = ?
              AND (published_at IS NULL OR TRIM(CAST(published_at AS VARCHAR)) = '')
            """,
            [(fallback, "instagram", comment_id) for fallback, comment_id in updates],
        )
        conn.commit()
        print(f"updated={len(updates)}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
