#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.video_shorts.tasks.sync_short_comments import _ensure_sync_state_table  # noqa: E402
from app.video_shorts.services.db import get_db, table_columns  # noqa: E402


def main() -> int:
    conn = get_db()
    try:
        _ensure_sync_state_table(conn)
        conn.commit()
        columns = sorted(table_columns(conn, "short_comment_sync_state"))
        print("short_comment_sync_state columns:")
        for column in columns:
            print(f"- {column}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
