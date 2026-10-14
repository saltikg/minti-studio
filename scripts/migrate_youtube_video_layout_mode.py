#!/usr/bin/env python
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.video_shorts.services.db import _ensure_video_crop_schema, get_db, table_columns  # noqa: E402

FACE_AWARE_DEFAULT_PREFERENCE_KEY = "face_aware_layout_default"


def _enabled_owner_ids(conn) -> set[str]:
    enabled = {
        item.strip()
        for item in str(os.getenv("FACE_AWARE_LAYOUT_USER_IDS") or "").split(",")
        if item.strip()
    }
    try:
        rows = conn.execute(
            """
            SELECT user_id
            FROM shorts_user_preferences
            WHERE preference_key = ?
              AND lower(coalesce(preference_value, '')) IN ('1', 'true', 'yes', 'on')
            """,
            [FACE_AWARE_DEFAULT_PREFERENCE_KEY],
        ).fetchall()
        enabled.update(str(row[0] or "").strip() for row in rows if str(row[0] or "").strip())
    except Exception:
        pass
    return enabled


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    conn = get_db()
    try:
        _ensure_video_crop_schema(conn)
        cols = table_columns(conn, "youtube_videos")
        if "layout_mode" not in cols:
            conn.execute("ALTER TABLE youtube_videos ADD COLUMN layout_mode VARCHAR")
            conn.commit()
            cols = table_columns(conn, "youtube_videos")
        if "layout_mode" not in cols:
            raise RuntimeError("youtube_videos.layout_mode was not created")

        enabled_owner_ids = _enabled_owner_ids(conn)
        rows = conn.execute(
            """
            SELECT id, owner_user_id, crop_locked, split_enabled, crop_aspect, layout_mode
            FROM youtube_videos
            """
        ).fetchall()
        updates: list[tuple[str, int]] = []
        for row in rows:
            video_pk = int(row[0])
            owner_user_id = str(row[1] or "").strip()
            crop_locked = bool(row[2])
            split_enabled = bool(row[3])
            crop_aspect = str(row[4] or "portrait").strip().lower() or "portrait"
            manual = crop_locked or split_enabled or crop_aspect != "portrait"
            next_mode = "manual" if manual else ("auto" if owner_user_id in enabled_owner_ids else "manual")
            updates.append((next_mode, video_pk))

        counts = {"auto": 0, "manual": 0}
        for mode, _video_pk in updates:
            counts[mode] = counts.get(mode, 0) + 1
        print(f"layout_mode backfill candidates: auto={counts.get('auto', 0)} manual={counts.get('manual', 0)} total={len(updates)}")
        if dry_run:
            conn.rollback()
            return 0
        conn.executemany("UPDATE youtube_videos SET layout_mode = ? WHERE id = ?", updates)
        conn.commit()
        final_rows = conn.execute("SELECT layout_mode, COUNT(*) FROM youtube_videos GROUP BY layout_mode ORDER BY layout_mode").fetchall()
        print("layout_mode final counts:")
        for row in final_rows:
            print(f"  {row[0] or 'NULL'}: {row[1]}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
