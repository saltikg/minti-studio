#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.video_shorts.services.comment_store import ensure_comment_cache_schema  # noqa: E402
from app.video_shorts.services.db import get_db, table_columns  # noqa: E402


def main() -> int:
    conn = get_db()
    try:
        ensure_comment_cache_schema(conn)
        conn.commit()
        columns = sorted(table_columns(conn, "social_comment_cache"))
        print("social_comment_cache columns:")
        for column in columns:
            print(f"- {column}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
