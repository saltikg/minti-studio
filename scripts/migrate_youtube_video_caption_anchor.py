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
        if "caption_anchor" not in columns:
            # The first default protects every row that predates anchors.
            conn.execute("ALTER TABLE youtube_videos ADD COLUMN caption_anchor VARCHAR DEFAULT 'bottom'")
        conn.execute("UPDATE youtube_videos SET caption_anchor = 'bottom' WHERE caption_anchor IS NULL OR caption_anchor NOT IN ('top', 'center', 'bottom')")
        conn.commit()
        # PostgreSQL can give future rows the product default without rewriting legacy rows.
        try:
            conn.execute("ALTER TABLE youtube_videos ALTER COLUMN caption_anchor SET DEFAULT 'top'")
            conn.commit()
        except Exception:
            conn.rollback()
        columns = table_columns(conn, "youtube_videos")
        if "caption_anchor" not in columns:
            raise RuntimeError("youtube_videos.caption_anchor was not created")
        counts = conn.execute("SELECT caption_anchor, COUNT(*) FROM youtube_videos GROUP BY caption_anchor ORDER BY caption_anchor").fetchall()
        print("youtube_videos.caption_anchor: OK")
        for anchor, count in counts:
            print(f"  {anchor}: {count}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
