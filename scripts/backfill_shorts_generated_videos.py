#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.video_shorts.config import SHORTS_DIR, PRIVATE_STATE_DIR
from app.video_shorts.services import tenant_media
from app.video_shorts.services.social_queue_identity import queue_identity
from app.video_shorts.services.db import get_db_readonly
from app.video_shorts.services.generated_video_lifecycle import upsert_generated_video_record


def _plan_paths(video):
    # Parse identity from the DB, never treat a numeric plan prefix as a YouTube ID.
    names = [f"{video.video_pk}_plan.json"]
    conn = get_db_readonly()
    try:
        if tenant_media._legacy_owner(conn, video) == video.video_pk:
            names.append(f"{video.youtube_id}_plan.json")
    finally:
        conn.close()
    for name in names:
        for root in (PRIVATE_STATE_DIR, PRIVATE_STATE_DIR / "legacy", SHORTS_DIR):
            path = root / name
            if path.is_file():
                return [path]  # Prefer canonical state; do not move files in an audit.
    return []


def _check_clip(video, name):
    conn = get_db_readonly()
    try:
        tenant_media.assert_clip_write_owner(conn, name, video.owner_user_id, video.brand_id,
                                            video_pk=video.video_pk, source_id=video.youtube_id)
        tenant_media.assert_asset_owner(conn, tenant_media.asset_key(name), video.owner_user_id, video.brand_id)
    finally:
        conn.close()


def _backfill_plan_entries(video, apply=False) -> int:
    count = 0
    for plan_path in _plan_paths(video):
        source_video_id = video.youtube_id
        try:
            payload = json.loads(plan_path.read_text())
        except Exception:
            continue
        entries = payload.get("plan") or payload.get("clips") or []
        for entry in entries:
            clip_filename = str(entry.get("clip_filename") or entry.get("output_filename") or "").strip()
            if not clip_filename:
                continue
            _check_clip(video, clip_filename)
            _check_clip(video, str(entry.get("output_filename") or clip_filename))
            if not apply:
                count += 1
                continue
            upsert_generated_video_record(
                user_id=video.owner_user_id, brand_id=video.brand_id,
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


def _backfill_queue_table(conn, table_name: str, id_column: str, video, apply=False) -> int:
    try:
        rows = conn.execute(
            f"""
            SELECT video_id, clip_filename, publish_at, published_at, status, {id_column}, user_id, brand_id
            FROM {table_name}
            WHERE clip_filename IS NOT NULL AND clip_filename <> ''
              AND video_id = ? AND user_id IN (?, ?)
            """,
            [video.youtube_id, video.owner_user_id, video.owner_user_id + "::" + str(video.brand_id or "")],
        ).fetchall()
    except Exception:
        return 0
    count = 0
    for source_video_id, clip_filename, publish_at, published_at, status, platform_id, user_id, brand_id in rows:
        if queue_identity(user_id, brand_id) != (video.owner_user_id, video.brand_id):
            continue
        clip_name = str(clip_filename or "").strip()
        if not clip_name or not source_video_id:
            continue
        _check_clip(video, clip_name)
        if not apply:
            count += 1
            continue
        kwargs = {
            "user_id": video.owner_user_id, "brand_id": video.brand_id,
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
    parser = argparse.ArgumentParser(description="Explicit tenant/video lifecycle backfill; read-only unless --apply.")
    parser.add_argument("--owner", required=True)
    parser.add_argument("--brand", required=True, help="Brand ID, or an empty string for an unbranded video")
    parser.add_argument("--video-pk", required=True, type=int)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    video = tenant_media.resolve_video(video_pk=args.video_pk, owner_user_id=args.owner, brand_id=args.brand or None)
    plan_count = _backfill_plan_entries(video, args.apply)
    conn = get_db_readonly()
    try:
        instagram_count = _backfill_queue_table(conn, "shorts_instagram_queue", "instagram_media_id", video, args.apply)
        facebook_count = _backfill_queue_table(conn, "shorts_facebook_queue", "facebook_video_id", video, args.apply)
        tiktok_count = _backfill_queue_table(conn, "shorts_tiktok_queue", "tiktok_video_id", video, args.apply)
    finally:
        conn.close()
    print(
        json.dumps(
            {
                "mode": "APPLIED" if args.apply else "READ_ONLY",
                "owner": video.owner_user_id, "brand": video.brand_id, "video_pk": video.video_pk,
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
