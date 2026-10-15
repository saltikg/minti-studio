#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

load_dotenv(ROOT / ".env")

from app.video_shorts.config import FREE_TOOL_GUEST_TTL_DAYS, SHORTS_DIR, PRIVATE_STATE_DIR
from app.video_shorts.services.db import ensure_auth_user_schema, get_db, table_columns
from app.video_shorts.services.guest_objects import guest_storage_keys_for_user, non_guest_source_references
from app.video_shorts.services import tenant_media
from app.video_shorts.services.media_utils import purge_source_cache_for_video
from app.video_shorts.services.storage import get_media_storage


def _expired_guests(conn, *, ttl_days: int, limit: int = 0) -> list[tuple[str, Any]]:
    ensure_auth_user_schema(conn)
    cutoff = datetime.now(timezone.utc) - timedelta(days=int(ttl_days))
    sql = """
        SELECT CAST(id AS VARCHAR), guest_created_at
        FROM shorts_users
        WHERE COALESCE(is_guest, FALSE) = TRUE
          AND guest_created_at < ?
        ORDER BY guest_created_at ASC
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql, [cutoff]).fetchall()


def _video_ids_for_user(conn, user_id: str) -> list[str]:
    if not table_columns(conn, "youtube_videos"):
        return []
    return [
        str(row[0] or "").strip()
        for row in conn.execute(
            "SELECT video_id FROM youtube_videos WHERE CAST(owner_user_id AS VARCHAR) = ?",
            [user_id],
        ).fetchall()
        if str(row[0] or "").strip()
    ]


def _local_files_for_video(video_id: str) -> list[Path]:
    files: list[Path] = []
    for pattern in (
        f"{video_id}_plan*.json",
        f"{video_id}_auto_suggest_render.json",
        f"preview_frames/{video_id}_*",
    ):
        for root in (SHORTS_DIR, PRIVATE_STATE_DIR / "legacy"):
            files.extend(path for path in root.glob(pattern) if path.is_file())
    return files


def _s3_size(storage, key: str) -> int:
    client = getattr(storage, "client", None)
    bucket = getattr(storage, "bucket_name", "")
    if client is not None and bucket:
        try:
            return int(client.head_object(Bucket=bucket, Key=key).get("ContentLength") or 0)
        except Exception:
            return 0
    try:
        entry = storage.resolve_local_or_s3(key)
        return int(entry.size_bytes or 0)
    except Exception:
        return 0


def _delete_user_rows(conn, user_id: str) -> None:
    for table_name in (
        "shorts_render_jobs",
        "shorts_quick_sessions",
        "shorts_generated_videos",
        "shorts_storage_events",
        "shorts_storage_assets",
        "shorts_instagram_queue",
        "shorts_facebook_queue",
        "shorts_tiktok_queue",
        "user_events",
        "youtube_videos",
        "youtube_channels",
        "shorts_brands",
        "shorts_users",
    ):
        cols = table_columns(conn, table_name)
        if not cols:
            continue
        for col in ("user_id", "owner_user_id", "id"):
            if col not in cols:
                continue
            if table_name == "shorts_users" and col != "id":
                continue
            conn.execute(f"DELETE FROM {table_name} WHERE CAST({col} AS VARCHAR) = ?", [user_id])
            break


def main() -> int:
    parser = argparse.ArgumentParser(description="Clean up expired unclaimed free-tool guest accounts and media.")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be deleted.")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--ttl-days", type=int, default=int(FREE_TOOL_GUEST_TTL_DAYS))
    args = parser.parse_args()

    storage = get_media_storage()
    conn = get_db()
    guests = _expired_guests(conn, ttl_days=args.ttl_days, limit=max(0, args.limit))
    object_count = 0
    total_bytes = 0
    local_count = 0
    no_source = 0
    try:
        for user_id, created_at in guests:
            tenant_media.lock_source_references(conn)
            video_ids = _video_ids_for_user(conn, user_id)
            keys = guest_storage_keys_for_user(conn, user_id)
            object_count += len(keys)
            for key in keys:
                if non_guest_source_references(conn, key=key):
                    continue
                total_bytes += _s3_size(storage, key)
                if not args.dry_run:
                    try:
                        storage.delete(key)
                    except Exception:
                        pass
            for video_id in video_ids:
                if non_guest_source_references(conn, video_id=video_id):
                    continue
                if not keys:
                    no_source += 1
                if not args.dry_run:
                    purge_source_cache_for_video(video_id)
                for local_path in _local_files_for_video(video_id):
                    local_count += 1
                    if not args.dry_run:
                        try:
                            local_path.unlink(missing_ok=True)
                        except Exception:
                            pass
            print(f"guest id={user_id} created_at={created_at} videos={len(video_ids)} objects={len(keys)}")
            if not args.dry_run:
                owned_pks = conn.execute("SELECT id FROM youtube_videos WHERE CAST(owner_user_id AS VARCHAR) = ?", [user_id]).fetchall()
                for (video_pk,) in owned_pks:
                    for path in PRIVATE_STATE_DIR.glob(f"{int(video_pk)}_*.json"):
                        path.unlink(missing_ok=True)
                _delete_user_rows(conn, user_id)
                conn.commit()
        if not args.dry_run:
            conn.commit()
    except Exception:
        if not args.dry_run:
            conn.rollback()
        raise
    finally:
        conn.close()
    print(
        "FREE_TOOL_GUEST_CLEANUP "
        f"dry_run={1 if args.dry_run else 0} ttl_days={args.ttl_days} guests={len(guests)} "
        f"objects={object_count} bytes={total_bytes} local_files={local_count} no_source_videos={no_source}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
