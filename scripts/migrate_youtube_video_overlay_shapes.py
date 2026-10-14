#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.video_shorts.services.db import get_db, table_columns  # noqa: E402


def main() -> int:
    conn = get_db()
    try:
        columns = table_columns(conn, "youtube_videos")
        if "overlay_shapes" not in columns:
            conn.execute("ALTER TABLE youtube_videos ADD COLUMN overlay_shapes VARCHAR")
            conn.commit()
        columns = table_columns(conn, "youtube_videos")
        if "overlay_shapes" not in columns:
            raise RuntimeError("youtube_videos.overlay_shapes was not created")
        print("youtube_videos.overlay_shapes: OK")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
