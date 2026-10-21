#!/usr/bin/env python3
"""Promote legacy clip-level music into blank video-level design fields.

Dry-run by default. Use ``--apply`` once after reviewing the conflict list.
Plan JSON is read-only: legacy music keys are retained as history.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from dotenv import load_dotenv

try:
    import psycopg
except ImportError:  # pragma: no cover
    import psycopg2 as psycopg


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PRIVATE_STATE_DIR = ROOT / "app/video_shorts/private_state"
SHORTS_DIR = ROOT / "app/video_shorts/static/shorts"
MUSIC_FIELDS = (
    "music_audio_id",
    "music_level",
    "music_start_seconds",
    "music_duck",
    "music_fade_out",
)


def _load_entries(video_pk: int, video_id: str):
    private_state_dir = Path(os.getenv("VIDEO_SHORTS_PRIVATE_STATE_DIR") or DEFAULT_PRIVATE_STATE_DIR)
    candidates = (
        private_state_dir / f"{video_pk}_plan.json",
        private_state_dir / "legacy" / f"{video_pk}_plan.json",
        private_state_dir / "legacy" / f"{video_id}_plan.json",
        SHORTS_DIR / f"{video_pk}_plan.json",
        SHORTS_DIR / f"{video_id}_plan.json",
    )
    path = next((item for item in candidates if item.is_file()), None)
    if path is None:
        return [], None
    state = json.loads(path.read_text(encoding="utf-8"))
    entries = state.get("plan") or state.get("clips") or []
    return [entry for entry in entries if isinstance(entry, dict)], path


def _snapshot(value):
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "null")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _entry_index(entry):
    try:
        return int(entry.get("plan_index") or 0)
    except (TypeError, ValueError):
        return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write promoted values to youtube_videos")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    db_url = next(
        (
            os.getenv(key)
            for key in ("VIDEO_SHORTS_DATABASE_URL", "VIDEO_SHORTS_POSTGRES_URL", "DATABASE_URL")
            if os.getenv(key)
        ),
        None,
    )
    if not db_url:
        raise RuntimeError("Database URL is missing")

    conn = psycopg.connect(db_url, options="-c search_path=main,public")
    conn.autocommit = True
    promoted = 0
    conflicts = []
    try:
        # Explicit all-tenant administrative migration; each update is owner/brand scoped.
        videos = conn.execute(
            """SELECT id, video_id, owner_user_id, brand_id
               FROM youtube_videos
               WHERE music_audio_id IS NULL OR btrim(music_audio_id) = ''
               ORDER BY id"""
        ).fetchall()
        for video_pk, video_id, owner_id, brand_id in videos:
            entries, plan_path = _load_entries(int(video_pk), str(video_id))
            music_entries = [
                entry
                for entry in entries
                if str(entry.get("music_audio_id") or "").strip()
            ]
            if not music_entries:
                continue

            generated = conn.execute(
                """SELECT clip_filename, raw_plan_entry_json,
                          COALESCE(updated_at, created_at)::text
                   FROM shorts_generated_videos
                   WHERE CAST(source_video_id AS VARCHAR) = %s
                     AND user_id = %s
                     AND brand_id IS NOT DISTINCT FROM %s
                   ORDER BY COALESCE(updated_at, created_at) DESC NULLS LAST""",
                (str(video_id), owner_id, brand_id),
            ).fetchall()
            rendered_at = {}
            for filename, raw_snapshot, timestamp in generated:
                snapshot = _snapshot(raw_snapshot)
                plan_index = _entry_index(snapshot)
                if plan_index and timestamp:
                    rendered_at[plan_index] = max(rendered_at.get(plan_index, ""), str(timestamp))
                if filename and timestamp:
                    rendered_at[str(filename)] = max(rendered_at.get(str(filename), ""), str(timestamp))

            def recency(entry):
                filename = str(entry.get("output_filename") or entry.get("clip_filename") or "")
                timestamp = str(
                    entry.get("music_updated_at")
                    or entry.get("updated_at")
                    or rendered_at.get(_entry_index(entry))
                    or rendered_at.get(filename)
                    or ""
                )
                return timestamp, _entry_index(entry)

            chosen = max(music_entries, key=recency)
            chosen_track = str(chosen.get("music_audio_id") or "").strip()
            distinct_tracks = sorted(
                {str(entry.get("music_audio_id") or "").strip() for entry in music_entries}
            )
            if len(distinct_tracks) > 1:
                conflict = {
                    "video_pk": int(video_pk),
                    "video_id": str(video_id),
                    "chosen_plan_index": _entry_index(chosen),
                    "chosen_track": chosen_track,
                    "tracks": distinct_tracks,
                }
                conflicts.append(conflict)
                print("CONFLICT " + json.dumps(conflict, ensure_ascii=False, sort_keys=True))

            values = {
                "music_audio_id": chosen_track,
                "music_level": chosen.get("music_level", 50),
                "music_start_seconds": chosen.get("music_start_seconds", 0),
                "music_duck": chosen.get("music_duck", True),
                "music_fade_out": chosen.get("music_fade_out", True),
            }
            print(
                f"video={video_pk} plan={_entry_index(chosen)} track={chosen_track} "
                f"source={plan_path} values={json.dumps(values, ensure_ascii=False, sort_keys=True)}"
            )
            if args.apply:
                result = conn.execute(
                    """UPDATE youtube_videos
                       SET music_audio_id = %s,
                           music_level = %s,
                           music_start_seconds = %s,
                           music_duck = %s,
                           music_fade_out = %s
                       WHERE id = %s
                         AND owner_user_id = %s
                         AND brand_id IS NOT DISTINCT FROM %s
                         AND (music_audio_id IS NULL OR btrim(music_audio_id) = '')""",
                    (
                        values["music_audio_id"],
                        values["music_level"],
                        values["music_start_seconds"],
                        values["music_duck"],
                        values["music_fade_out"],
                        int(video_pk),
                        owner_id,
                        brand_id,
                    ),
                )
                if result.rowcount != 1:
                    raise RuntimeError(f"Video {video_pk} changed while migration was running")
            promoted += 1

        print(
            f"{'APPLIED' if args.apply else 'DRY_RUN'} "
            f"videos_promoted={promoted} conflicting_videos={len(conflicts)}"
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
