#!/usr/bin/env python
from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.video_shorts.services.db import get_db, table_columns  # noqa: E402
from app.video_shorts.services.render_jobs import ensure_render_jobs_schema  # noqa: E402
from app.video_shorts.services.video_deletion import (  # noqa: E402
    AUDIT_TABLE,
    PUBLISHED_ARCHIVE_TABLE,
    ensure_video_deletion_schema,
)


def main() -> int:
    conn = get_db()
    try:
        ensure_render_jobs_schema(conn)
        ensure_video_deletion_schema(conn)
        conn.commit()
        video_columns = table_columns(conn, "youtube_videos")
        if not {"deleted_at", "deleted_by"}.issubset(video_columns):
            raise RuntimeError("youtube_videos deletion columns were not created")
        if not table_columns(conn, AUDIT_TABLE):
            raise RuntimeError(f"{AUDIT_TABLE} was not created")
        if not table_columns(conn, PUBLISHED_ARCHIVE_TABLE):
            raise RuntimeError(f"{PUBLISHED_ARCHIVE_TABLE} was not created")
        print("VIDEO_DELETION_SCHEMA_OK")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
