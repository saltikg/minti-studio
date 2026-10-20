#!/usr/bin/env python3
"""One-time, dry-run-by-default migration of rendered music to clip plans.

Only a generated video's stored render snapshot proves that music was mixed.
Run with --apply after inspecting the dry-run output.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
from pathlib import Path

from dotenv import load_dotenv

try:
    import psycopg
except ImportError:  # pragma: no cover
    import psycopg2 as psycopg


ROOT = Path(__file__).resolve().parents[1]
PLAN_DIR = ROOT / "app/video_shorts/private_state"


def _snapshot(value):
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "null")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write plan files and clear legacy video music IDs")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    db_url = next((os.getenv(key) for key in ("VIDEO_SHORTS_DATABASE_URL", "VIDEO_SHORTS_POSTGRES_URL", "DATABASE_URL") if os.getenv(key)), None)
    if not db_url:
        raise RuntimeError("Database URL is missing")
    conn = psycopg.connect(db_url, options="-c search_path=main,public")
    conn.autocommit = True
    backup_dir = Path(tempfile.mkdtemp(prefix="minti-music-plan-backup-")) if args.apply else None
    migrated = 0
    cleared = 0
    try:
        # Explicit all-tenant administrative migration; every subsequent query is scoped.
        videos = conn.execute(
            """SELECT id, video_id, owner_user_id, brand_id, music_audio_id
               FROM youtube_videos WHERE music_audio_id IS NOT NULL AND music_audio_id <> ''
               ORDER BY id"""
        ).fetchall()
        for video_pk, video_id, owner_id, brand_id, legacy_audio_id in videos:
            plan_path = PLAN_DIR / f"{int(video_pk)}_plan.json"
            if not plan_path.is_file():
                raise RuntimeError(f"Plan is missing for video {video_pk}: {plan_path}")
            plan_state = json.loads(plan_path.read_text(encoding="utf-8"))
            entries = plan_state.get("plan")
            if not isinstance(entries, list):
                raise RuntimeError(f"Invalid plan for video {video_pk}")
            generated = conn.execute(
                """SELECT clip_filename, raw_plan_entry_json
                   FROM shorts_generated_videos
                   WHERE CAST(source_video_id AS VARCHAR) = %s
                     AND user_id = %s AND brand_id = %s
                     AND lower(coalesce(source_channel_type, 'youtube')) = 'youtube'
                     AND raw_plan_entry_json IS NOT NULL""",
                (video_id, owner_id, brand_id),
            ).fetchall()
            snapshots = {str(name): _snapshot(raw) for name, raw in generated}
            changes = []
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                clip_name = str(entry.get("output_filename") or entry.get("clip_filename") or "")
                snap = snapshots.get(clip_name) or {}
                rendered_audio_id = str(snap.get("music_rendered_audio_id", snap.get("music_audio_id")) or "").strip()
                if not rendered_audio_id:
                    continue
                if str(entry.get("music_audio_id") or "").strip():
                    continue  # Preserve a newer per-clip selection.
                entry["music_audio_id"] = rendered_audio_id
                entry["music_level"] = snap.get("music_level", 50)
                entry["music_start_seconds"] = snap.get("music_start_seconds", 0)
                entry["music_duck"] = snap.get("music_duck", True)
                changes.append((entry.get("plan_index"), rendered_audio_id))
            print(f"video={video_pk} legacy={legacy_audio_id} backfill={changes} clear_video_id=yes")
            if not args.apply:
                migrated += len(changes)
                cleared += 1
                continue
            if changes:
                shutil.copy2(plan_path, backup_dir / plan_path.name)
                with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=plan_path.parent, prefix=f".{video_pk}_plan.", suffix=".tmp", delete=False) as output:
                    temp_path = Path(output.name)
                    json.dump(plan_state, output, ensure_ascii=False)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temp_path, plan_path)
            with conn.transaction():
                result = conn.execute(
                    """UPDATE youtube_videos SET music_audio_id = NULL
                       WHERE id = %s AND owner_user_id = %s AND brand_id = %s AND music_audio_id = %s""",
                    (video_pk, owner_id, brand_id, legacy_audio_id),
                )
                if result.rowcount != 1:
                    raise RuntimeError(f"Video {video_pk} changed during migration")
            migrated += len(changes)
            cleared += 1
        print(f"{'APPLIED' if args.apply else 'DRY_RUN'} videos={cleared} clips_backfilled={migrated} backup={backup_dir or '-'}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
