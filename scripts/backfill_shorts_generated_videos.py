#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.video_shorts.config import SHORTS_DIR
from app.video_shorts.services.db import get_db_readonly
from app.video_shorts.services.generated_video_lifecycle import upsert_generated_video_record


def _plan_paths():
    if not SHORTS_DIR.exists():
        return []
    return sorted(path for path in SHORTS_DIR.glob("*_plan.json") if path.is_file())


def _backfill_plan_entries() -> int:
    count = 0
    for plan_path in _plan_paths():
        source_video_id = plan_path.name[: -len("_plan.json")]
        try:
            payload = json.loads(plan_path.read_text())
        except Exception:
            continue
        entries = payload.get("plan") or payload.get("clips") or []
        for entry in entries:
            clip_filename = str(entry.get("clip_filename") or entry.get("output_filename") or "").strip()
            if not clip_filename:
                continue
            upsert_generated_video_record(
                source_video_id=source_video_id,
                source_channel_type="youtube",
                clip_filename=clip_filename,
                output_filename=str(entry.get("output_filename") or clip_filename),
                storage_file_key=f"short:{clip_filename}",
                generation_status=str(entry.get("status") or "").strip().lower() or None,
                publish_status=str(entry.get("publish_status") or "").strip().lower() or None,
                youtube_video_id=entry.get("yt_video_id") or entry.get("short_video_id"),
                planned_publish_at=entry.get("publish_at_iso") or entry.get("publish_at"),
                plan_run_id=entry.get("plan_run_id") or entry.get("batch_id"),
                generated_title=entry.get("yt_title") or entry.get("title"),
                generated_description=entry.get("yt_description") or entry.get("description"),
                generated_excerpt=entry.get("excerpt"),
                generated_transcript_full=entry.get("transcript_full"),
                youtube_published_at=entry.get("youtube_published_at") or entry.get("yt_published_at"),
                raw_plan_entry=entry,
            )
            count += 1
    return count


def _backfill_queue_table(conn, table_name: str, id_column: str) -> int:
    try:
        rows = conn.execute(
            f"""
            SELECT video_id, clip_filename, publish_at, published_at, status, {id_column}
            FROM {table_name}
            WHERE clip_filename IS NOT NULL AND clip_filename <> ''
            """
        ).fetchall()
    except Exception:
        return 0
    count = 0
    for source_video_id, clip_filename, publish_at, published_at, status, platform_id in rows:
        clip_name = str(clip_filename or "").strip()
        if not clip_name or not source_video_id:
            continue
        kwargs = {
            "source_video_id": str(source_video_id),
            "source_channel_type": "youtube",
            "clip_filename": clip_name,
            "output_filename": clip_name,
            "storage_file_key": f"short:{clip_name}",
            "generation_status": "created",
            "publish_status": (
                "published" if str(status or "").strip().lower() == "published"
                else ("failed" if str(status or "").strip().lower() == "failed" else "queued")
            ),
            "planned_publish_at": publish_at,
            "published_at": published_at,
        }
        if table_name == "shorts_instagram_queue":
            kwargs["instagram_media_id"] = platform_id
            kwargs["instagram_published_at"] = published_at if kwargs["publish_status"] == "published" else None
            kwargs["primary_publish_platform"] = "instagram" if kwargs["publish_status"] == "published" else None
        elif table_name == "shorts_facebook_queue":
            kwargs["facebook_video_id"] = platform_id
            kwargs["facebook_published_at"] = published_at if kwargs["publish_status"] == "published" else None
            kwargs["primary_publish_platform"] = "facebook" if kwargs["publish_status"] == "published" else None
        elif table_name == "shorts_tiktok_queue":
            kwargs["tiktok_video_id"] = platform_id
            kwargs["tiktok_published_at"] = published_at if kwargs["publish_status"] == "published" else None
            kwargs["primary_publish_platform"] = "tiktok" if kwargs["publish_status"] == "published" else None
        upsert_generated_video_record(**kwargs)
        count += 1
    return count


def main() -> int:
    plan_count = _backfill_plan_entries()
    conn = get_db_readonly()
    try:
        instagram_count = _backfill_queue_table(conn, "shorts_instagram_queue", "instagram_media_id")
        facebook_count = _backfill_queue_table(conn, "shorts_facebook_queue", "facebook_video_id")
        tiktok_count = _backfill_queue_table(conn, "shorts_tiktok_queue", "tiktok_video_id")
    finally:
        conn.close()
    print(
        json.dumps(
            {
                "plan_entries": plan_count,
                "instagram_queue": instagram_count,
                "facebook_queue": facebook_count,
                "tiktok_queue": tiktok_count,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
