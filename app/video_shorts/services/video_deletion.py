from __future__ import annotations

import json
import logging
from hashlib import sha256
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional
from uuid import uuid4

from app.video_shorts.config import PRIVATE_STATE_DIR, SHORTS_DIR
from app.video_shorts.services import tenant_media
from app.video_shorts.services.db import get_db, get_db_readonly, table_columns
from app.video_shorts.services.render_jobs import (
    JOB_STATUS_CANCELLED,
    JOB_STATUS_QUEUED,
    JOB_TYPE_PURGE_VIDEO,
    ensure_render_jobs_schema,
)
from app.video_shorts.services.storage import get_media_storage, invalidate_cloudfront_paths


logger = logging.getLogger(__name__)

AUDIT_TABLE = "shorts_video_deletion_audits"
PUBLISHED_ARCHIVE_TABLE = "shorts_deleted_published_posts"
RUNNING_JOB_STATUSES = {"claimed", "processing", "running"}
PENDING_SOCIAL_STATUSES = {"pending", "retry", "uploading", "queued", "scheduled"}
PUBLISHED_SOCIAL_STATUSES = {"published", "uploaded"}
STATE_KINDS = (
    "suggestions",
    "plan",
    "plan_v2",
    "plan_v3",
    "plan_v4",
    "auto_suggest_render",
    "non_speech_overrides",
    "debug",
    "debug_v2",
    "debug_v3",
    "debug_v4",
)


class VideoDeletionConflict(RuntimeError):
    pass


class VideoDeletionNotFound(LookupError):
    pass


def _json_type(conn) -> str:
    return "JSONB" if getattr(conn, "backend_name", "") == "postgres" else "TEXT"


def _json_param(conn) -> str:
    return "CAST(? AS JSONB)" if getattr(conn, "backend_name", "") == "postgres" else "?"


def _parse_json(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _is_future_timestamp(value: Any) -> bool:
    if not value:
        return False
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc) > datetime.now(timezone.utc)
    except (TypeError, ValueError):
        return False


def _iso_utc(value: Any) -> str:
    if not value:
        return ""
    if not isinstance(value, datetime):
        return str(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def ensure_video_deletion_schema(conn) -> None:
    video_columns = table_columns(conn, "youtube_videos")
    if "deleted_at" not in video_columns:
        conn.execute("ALTER TABLE youtube_videos ADD COLUMN deleted_at TIMESTAMP")
    if "deleted_by" not in video_columns:
        conn.execute("ALTER TABLE youtube_videos ADD COLUMN deleted_by VARCHAR")

    json_type = _json_type(conn)
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {AUDIT_TABLE} (
            id VARCHAR PRIMARY KEY,
            video_pk BIGINT NOT NULL,
            source_video_id VARCHAR NOT NULL,
            owner_user_id VARCHAR NOT NULL,
            brand_id VARCHAR,
            deleted_by VARCHAR NOT NULL,
            status VARCHAR NOT NULL DEFAULT 'queued',
            summary_json {json_type},
            manifest_json {json_type},
            purge_job_id VARCHAR,
            error TEXT,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            purge_started_at TIMESTAMP,
            purged_at TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {PUBLISHED_ARCHIVE_TABLE} (
            id VARCHAR PRIMARY KEY,
            deletion_audit_id VARCHAR NOT NULL,
            original_generated_video_id VARCHAR,
            user_id VARCHAR NOT NULL,
            brand_id VARCHAR,
            source_video_id VARCHAR NOT NULL,
            clip_filename VARCHAR,
            publish_status VARCHAR,
            youtube_video_id VARCHAR,
            instagram_media_id VARCHAR,
            facebook_video_id VARCHAR,
            tiktok_video_id VARCHAR,
            generated_title TEXT,
            planned_publish_at TIMESTAMP,
            published_at TIMESTAMP,
            metadata_json {json_type},
            archived_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(deletion_audit_id, original_generated_video_id)
        )
        """
    )
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{AUDIT_TABLE}_video ON {AUDIT_TABLE}(video_pk, created_at)"
    )


def _scope_row(conn, video_pk: int, *, owner_user_id: str, brand_id: Optional[str], lock: bool = False):
    suffix = " FOR UPDATE" if lock and getattr(conn, "backend_name", "") == "postgres" else ""
    return conn.execute(
        f"""
        SELECT id, video_id, title, owner_user_id, brand_id, deleted_at,
               thumbnail_url, duration_seconds,
               COALESCE(downloaded_at, created_at, published_at) AS added_at
        FROM youtube_videos
        WHERE id = ?
          AND CAST(owner_user_id AS VARCHAR) = CAST(? AS VARCHAR)
          AND brand_id IS NOT DISTINCT FROM ?
        {suffix}
        """,
        [int(video_pk), str(owner_user_id), brand_id],
    ).fetchone()


def _job_matches_video(conn, payload_column: str = "payload_json") -> str:
    if getattr(conn, "backend_name", "") == "postgres":
        return (
            f"(({payload_column}->>'video_pk') = CAST(? AS VARCHAR) "
            f"OR ({payload_column}->>'source_video_id') = CAST(? AS VARCHAR))"
        )
    return (
        f"(json_extract_string({payload_column}, '$.video_pk') = CAST(? AS VARCHAR) "
        f"OR json_extract_string({payload_column}, '$.source_video_id') = CAST(? AS VARCHAR))"
    )


def _generated_rows(conn, *, source_video_id: str, owner_user_id: str, brand_id: Optional[str]) -> list[dict[str, Any]]:
    if not table_columns(conn, "shorts_generated_videos"):
        return []
    columns = table_columns(conn, "shorts_generated_videos")
    wanted = [
        "id", "clip_filename", "output_filename", "storage_file_key", "raw_plan_entry_json",
        "publish_status", "youtube_video_id", "instagram_media_id", "facebook_video_id",
        "tiktok_video_id", "generated_title", "planned_publish_at", "published_at",
    ]
    selected = [column for column in wanted if column in columns]
    rows = conn.execute(
        f"""
        SELECT {', '.join(selected)}
        FROM shorts_generated_videos
        WHERE CAST(source_video_id AS VARCHAR) = CAST(? AS VARCHAR)
          AND CAST(user_id AS VARCHAR) = CAST(? AS VARCHAR)
          AND brand_id IS NOT DISTINCT FROM ?
        ORDER BY created_at, id
        """,
        [source_video_id, owner_user_id, brand_id],
    ).fetchall()
    return [dict(zip(selected, row)) for row in rows]


def _queue_rows(conn, table_name: str, *, source_video_id: str, owner_user_id: str, brand_id: Optional[str]) -> list[dict[str, Any]]:
    columns = table_columns(conn, table_name)
    if not columns:
        return []
    wanted = [
        "id", "status", "plan_index", "clip_filename", "publish_at", "published_at",
        "instagram_media_id", "facebook_video_id", "tiktok_video_id", "tiktok_publish_id",
    ]
    selected = [column for column in wanted if column in columns]
    rows = conn.execute(
        f"""
        SELECT {', '.join(selected)}
        FROM {table_name}
        WHERE CAST(video_id AS VARCHAR) = CAST(? AS VARCHAR)
          AND CAST(user_id AS VARCHAR) = CAST(? AS VARCHAR)
          AND brand_id IS NOT DISTINCT FROM ?
        """,
        [source_video_id, owner_user_id, brand_id],
    ).fetchall()
    return [dict(zip(selected, row)) for row in rows]


def _youtube_schedule_rows(conn, *, video_pk: int, owner_user_id: str, brand_id: Optional[str]) -> list[dict[str, Any]]:
    if not table_columns(conn, "shorts_youtube_schedules"):
        return []
    rows = conn.execute(
        """
        SELECT id, status, youtube_video_id, resumable_uri, asset_key, publish_at, job_id
        FROM shorts_youtube_schedules
        WHERE CAST(user_id AS VARCHAR) = CAST(? AS VARCHAR)
          AND brand_id IS NOT DISTINCT FROM ?
          AND asset_key LIKE ?
        """,
        [owner_user_id, brand_id, f"clip:{int(video_pk)}:%"],
    ).fetchall()
    names = ["id", "status", "youtube_video_id", "resumable_uri", "asset_key", "publish_at", "job_id"]
    return [dict(zip(names, row)) for row in rows]


def _poster_names(generated_rows: Iterable[dict[str, Any]]) -> set[str]:
    posters: set[str] = set()
    for row in generated_rows:
        entry = _parse_json(row.get("raw_plan_entry_json"))
        poster = Path(str(entry.get("poster_filename") or "")).name
        if poster:
            posters.add(poster)
    return posters


def _clip_names(generated_rows: Iterable[dict[str, Any]]) -> set[str]:
    return {
        Path(str(value)).name
        for row in generated_rows
        for value in (row.get("clip_filename"), row.get("output_filename"))
        if str(value or "").strip()
    }


def _rendered_clip_rows(
    conn,
    *,
    video_pk: int,
    source_video_id: str,
    owner_user_id: str,
    brand_id: Optional[str],
) -> list[dict[str, Any]]:
    """Use the same durable-media rule as the My Videos SHORTS column."""
    if not table_columns(conn, "shorts_generated_videos"):
        return []
    rows = conn.execute(
        """
        SELECT gv.clip_filename, gv.output_filename, gv.raw_plan_entry_json
        FROM shorts_generated_videos gv
        JOIN shorts_storage_assets render_asset
          ON render_asset.file_key = gv.storage_file_key
         AND CAST(render_asset.user_id AS VARCHAR) = CAST(gv.user_id AS VARCHAR)
         AND render_asset.brand_id IS NOT DISTINCT FROM gv.brand_id
         AND lower(coalesce(render_asset.status, 'active')) = 'active'
         AND render_asset.size_bytes > 0
        JOIN youtube_videos source_video
          ON source_video.id = ?
         AND source_video.video_id = gv.source_video_id
         AND source_video.owner_user_id = gv.user_id
         AND source_video.brand_id IS NOT DISTINCT FROM gv.brand_id
         AND source_video.deleted_at IS NULL
        WHERE gv.source_video_id = ?
          AND CAST(gv.user_id AS VARCHAR) = CAST(? AS VARCHAR)
          AND gv.brand_id IS NOT DISTINCT FROM ?
          AND NULLIF(trim(coalesce(gv.clip_filename, '')), '') IS NOT NULL
          AND NULLIF(trim(coalesce(gv.storage_file_key, '')), '') IS NOT NULL
          AND lower(coalesce(gv.generation_status, '')) NOT IN
              ('pending', 'queued', 'processing', 'rendering')
        """,
        [int(video_pk), source_video_id, owner_user_id, brand_id],
    ).fetchall()
    return [
        {
            "clip_filename": row[0],
            "output_filename": row[1],
            "raw_plan_entry_json": row[2],
        }
        for row in rows
    ]


def _plan_entries(source_video_id: str, *, video_pk: int) -> list[dict[str, Any]]:
    try:
        payload = tenant_media.load_state(
            source_video_id,
            "plan",
            video_pk=video_pk,
            migrate=False,
        )
    except (OSError, ValueError, TypeError):
        return []
    entries = payload.get("plan") or payload.get("clips") or []
    if not isinstance(entries, list):
        return []
    return [dict(entry) for entry in entries if isinstance(entry, dict)]


def _unrendered_plan_count(
    plan_entries: Iterable[dict[str, Any]],
    rendered_rows: Iterable[dict[str, Any]],
) -> int:
    rendered_names = _clip_names(rendered_rows)
    rendered_indexes: set[str] = set()
    for row in rendered_rows:
        snapshot = _parse_json(row.get("raw_plan_entry_json"))
        plan_index = snapshot.get("plan_index")
        if plan_index is not None and str(plan_index).strip():
            rendered_indexes.add(str(plan_index).strip())

    count = 0
    for entry in plan_entries:
        entry_names = {
            Path(str(entry.get(key) or "")).name
            for key in ("clip_filename", "output_filename")
            if str(entry.get(key) or "").strip()
        }
        plan_index = entry.get("plan_index")
        index_key = str(plan_index).strip() if plan_index is not None else ""
        if entry_names.intersection(rendered_names) or (index_key and index_key in rendered_indexes):
            continue
        count += 1
    return count


def _compilation_rows(conn, *, video_pk: int, owner_user_id: str, brand_id: Optional[str]) -> list[dict[str, Any]]:
    if not table_columns(conn, "shorts_compilations"):
        return []
    rows = conn.execute(
        """
        SELECT filename, storage_file_key
        FROM shorts_compilations
        WHERE video_pk = ?
          AND CAST(owner_user_id AS VARCHAR) = CAST(? AS VARCHAR)
          AND brand_id IS NOT DISTINCT FROM ?
        """,
        [int(video_pk), owner_user_id, brand_id],
    ).fetchall()
    return [{"filename": str(row[0] or ""), "storage_file_key": str(row[1] or "")} for row in rows]


def _asset_manifest(conn, file_keys: set[str]) -> list[dict[str, Any]]:
    if not file_keys:
        return []
    placeholders = ", ".join("?" for _ in file_keys)
    rows = conn.execute(
        f"SELECT file_key, file_path, file_type, size_bytes, status FROM shorts_storage_assets WHERE file_key IN ({placeholders})",
        list(file_keys),
    ).fetchall()
    return [
        {
            "file_key": str(row[0]),
            "file_path": str(row[1]),
            "file_type": str(row[2]),
            "size_bytes": int(row[3] or 0),
            "status": str(row[4] or "active"),
        }
        for row in rows
    ]


def _complete_asset_manifest(
    assets: list[dict[str, Any]],
    *,
    clip_names: set[str],
    poster_names: set[str],
    compilations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    known = {str(asset.get("file_key") or "") for asset in assets}
    for name in sorted(clip_names):
        file_key = tenant_media.asset_key(name)
        if file_key not in known:
            assets.append({"file_key": file_key, "file_path": f"s3://shorts/{name}", "file_type": "short", "size_bytes": 0})
            known.add(file_key)
    for name in sorted(poster_names):
        file_key = tenant_media.poster_key(name)
        if file_key not in known:
            assets.append({"file_key": file_key, "file_path": f"s3://{file_key}", "file_type": "poster", "size_bytes": 0})
            known.add(file_key)
    for item in compilations:
        name = Path(str(item.get("filename") or "")).name
        if not name:
            continue
        file_key = str(item.get("storage_file_key") or tenant_media.asset_key(name))
        if file_key not in known:
            assets.append({"file_key": file_key, "file_path": f"s3://shorts/{name}", "file_type": "short", "size_bytes": 0})
            known.add(file_key)
    return assets


def _build_snapshot(conn, row, *, video_pk: int) -> dict[str, Any]:
    source_video_id = str(row[1] or "")
    owner_user_id = str(row[3] or "")
    brand_id = str(row[4]) if row[4] else None
    generated = _generated_rows(
        conn,
        source_video_id=source_video_id,
        owner_user_id=owner_user_id,
        brand_id=brand_id,
    )
    clips = _clip_names(generated)
    rendered = _rendered_clip_rows(
        conn,
        video_pk=video_pk,
        source_video_id=source_video_id,
        owner_user_id=owner_user_id,
        brand_id=brand_id,
    )
    suggestions = _unrendered_plan_count(
        _plan_entries(source_video_id, video_pk=video_pk),
        rendered,
    )
    posters = _poster_names(generated)
    compilations = _compilation_rows(
        conn,
        video_pk=video_pk,
        owner_user_id=owner_user_id,
        brand_id=brand_id,
    )
    social = {
        table_name: _queue_rows(
            conn,
            table_name,
            source_video_id=source_video_id,
            owner_user_id=owner_user_id,
            brand_id=brand_id,
        )
        for table_name in ("shorts_instagram_queue", "shorts_facebook_queue", "shorts_tiktok_queue")
    }
    youtube_schedules = _youtube_schedule_rows(
        conn,
        video_pk=video_pk,
        owner_user_id=owner_user_id,
        brand_id=brand_id,
    )

    scheduled_social = sum(
        1
        for rows in social.values()
        for item in rows
        if str(item.get("status") or "").lower() in PENDING_SOCIAL_STATUSES
    )
    pending_youtube = sum(
        1
        for item in youtube_schedules
        if not str(item.get("youtube_video_id") or "").strip()
        and str(item.get("status") or "").lower() in {"pending", "queued", "retry", "failed", "missed"}
    )
    remote_youtube = sum(
        1
        for item in youtube_schedules
        if str(item.get("youtube_video_id") or "").strip() and _is_future_timestamp(item.get("publish_at"))
    )

    file_keys = {str(row.get("storage_file_key") or tenant_media.asset_key(name)) for row in generated for name in [Path(str(row.get("clip_filename") or row.get("output_filename") or "")).name] if name}
    file_keys.update(tenant_media.poster_key(name) for name in posters)
    file_keys.update(str(item.get("storage_file_key") or tenant_media.asset_key(item["filename"])) for item in compilations if item.get("filename"))

    shared_source = tenant_media.other_source_references(conn, source_video_id, excluding_pks=[video_pk])
    if not shared_source:
        for suffix in tenant_media.SOURCE_VIDEO_SUFFIXES:
            file_keys.add(f"downloaded:{source_video_id}{suffix}")

    assets = _complete_asset_manifest(
        _asset_manifest(conn, file_keys),
        clip_names=clips,
        poster_names=posters,
        compilations=compilations,
    )
    return {
        "summary": {
            "video_pk": int(video_pk),
            "title": str(row[2] or "Untitled video"),
            "source_video_id": source_video_id,
            "thumbnail_url": str(row[6] or ""),
            "duration_seconds": float(row[7] or 0),
            "added_at": _iso_utc(row[8]),
            "clip_count": len(rendered),
            "suggestion_count": suggestions,
            "scheduled_post_count": scheduled_social + pending_youtube,
            "remote_youtube_schedule_count": remote_youtube,
            "asset_bytes": sum(
                int(asset.get("size_bytes") or 0)
                for asset in assets
                if str(asset.get("status") or "active").lower() == "active"
            ),
        },
        "manifest": {
            "video_pk": int(video_pk),
            "source_video_id": source_video_id,
            "owner_user_id": owner_user_id,
            "brand_id": brand_id,
            "clip_names": sorted(clips),
            "poster_names": sorted(posters),
            "compilations": compilations,
            "assets": assets,
            "generated_rows": generated,
            "social_queues": social,
            "youtube_schedules": youtube_schedules,
            "shared_source": bool(shared_source),
        },
    }


def preview_video_deletion(*, video_pk: int, owner_user_id: str, brand_id: Optional[str]) -> dict[str, Any]:
    conn = get_db_readonly()
    try:
        row = _scope_row(conn, video_pk, owner_user_id=owner_user_id, brand_id=brand_id)
        if not row or row[5] is not None:
            raise VideoDeletionNotFound("Video not found.")
        return _build_snapshot(conn, row, video_pk=int(video_pk))["summary"]
    finally:
        conn.close()


def _assert_not_processing(conn, *, video_pk: int, source_video_id: str, owner_user_id: str, brand_id: Optional[str]) -> None:
    match_sql = _job_matches_video(conn)
    statuses = sorted(RUNNING_JOB_STATUSES)
    placeholders = ", ".join("?" for _ in statuses)
    row = conn.execute(
        f"""
        SELECT id
        FROM shorts_render_jobs
        WHERE CAST(user_id AS VARCHAR) = CAST(? AS VARCHAR)
          AND status IN ({placeholders})
          AND {match_sql}
        LIMIT 1
        """,
        [owner_user_id, *statuses, str(video_pk), source_video_id],
    ).fetchone()
    if row:
        raise VideoDeletionConflict("This video is being processed. Try again in a minute.")

    for table_name in ("shorts_instagram_queue", "shorts_facebook_queue", "shorts_tiktok_queue"):
        if not table_columns(conn, table_name):
            continue
        active = conn.execute(
            f"""
            SELECT 1 FROM {table_name}
            WHERE CAST(video_id AS VARCHAR) = CAST(? AS VARCHAR)
              AND CAST(user_id AS VARCHAR) = CAST(? AS VARCHAR)
              AND brand_id IS NOT DISTINCT FROM ?
              AND lower(coalesce(status, '')) IN ('processing', 'running', 'claimed', 'uploading')
            LIMIT 1
            """,
            [source_video_id, owner_user_id, brand_id],
        ).fetchone()
        if active:
            raise VideoDeletionConflict("This video is being processed. Try again in a minute.")

    if table_columns(conn, "shorts_youtube_schedules"):
        active = conn.execute(
            """
            SELECT 1 FROM shorts_youtube_schedules schedule
            LEFT JOIN shorts_render_jobs job ON job.id = schedule.job_id
            WHERE CAST(schedule.user_id AS VARCHAR) = CAST(? AS VARCHAR)
              AND schedule.brand_id IS NOT DISTINCT FROM ?
              AND schedule.asset_key LIKE ?
              AND schedule.youtube_video_id IS NULL
              AND (
                schedule.resumable_uri IS NOT NULL
                OR lower(coalesce(schedule.status, '')) IN ('processing', 'running', 'uploading')
                OR lower(coalesce(job.status, '')) IN ('claimed', 'processing', 'running')
              )
            LIMIT 1
            """,
            [owner_user_id, brand_id, f"clip:{int(video_pk)}:%"],
        ).fetchone()
        if active:
            raise VideoDeletionConflict("This video is being processed. Try again in a minute.")


def _cancel_queued_jobs(conn, *, video_pk: int, source_video_id: str, owner_user_id: str) -> int:
    result = conn.execute(
        f"""
        UPDATE shorts_render_jobs
        SET status = ?, error = 'Cancelled because the source video was deleted.',
            finished_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
        WHERE CAST(user_id AS VARCHAR) = CAST(? AS VARCHAR)
          AND status = ?
          AND {_job_matches_video(conn)}
        """,
        [JOB_STATUS_CANCELLED, owner_user_id, JOB_STATUS_QUEUED, str(video_pk), source_video_id],
    )
    return max(0, int(getattr(result, "rowcount", 0) or 0))


def _cancel_publish_schedules(conn, manifest: dict[str, Any]) -> dict[str, int]:
    counts = {"youtube": 0, "instagram": 0, "facebook": 0, "tiktok": 0}
    for row in manifest.get("youtube_schedules") or []:
        if str(row.get("youtube_video_id") or "").strip():
            continue
        if str(row.get("status") or "").lower() not in {"pending", "queued", "retry", "failed", "missed"}:
            continue
        changed = conn.execute(
            """
            UPDATE shorts_youtube_schedules
            SET status = 'cancelled', updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND youtube_video_id IS NULL AND resumable_uri IS NULL
              AND lower(coalesce(status, '')) IN ('pending', 'queued', 'retry', 'failed', 'missed')
            """,
            [row["id"]],
        )
        counts["youtube"] += max(0, int(getattr(changed, "rowcount", 0) or 0))
        job_id = str(row.get("job_id") or "").strip()
        if job_id:
            conn.execute(
                """
                UPDATE shorts_render_jobs
                SET status='cancelled', error='Cancelled because the source video was deleted.',
                    finished_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND status='queued'
                """,
                [job_id],
            )

    table_platform = {
        "shorts_instagram_queue": "instagram",
        "shorts_facebook_queue": "facebook",
        "shorts_tiktok_queue": "tiktok",
    }
    for table_name, rows in (manifest.get("social_queues") or {}).items():
        platform = table_platform.get(table_name)
        if not platform:
            continue
        ids = [str(row.get("id")) for row in rows if str(row.get("status") or "").lower() in PENDING_SOCIAL_STATUSES]
        if not ids:
            continue
        placeholders = ", ".join("?" for _ in ids)
        changed = conn.execute(
            f"""
            UPDATE {table_name}
            SET status = 'canceled', status_detail = 'Canceled because the source video was deleted.',
                updated_at = CAST(CURRENT_TIMESTAMP AS VARCHAR)
            WHERE id IN ({placeholders})
              AND lower(coalesce(status, '')) IN ('pending', 'retry', 'uploading', 'queued', 'scheduled')
            """,
            ids,
        )
        counts[platform] += max(0, int(getattr(changed, "rowcount", 0) or 0))
    return counts


def request_video_deletion(
    *,
    video_pk: int,
    owner_user_id: str,
    brand_id: Optional[str],
    deleted_by: str,
) -> dict[str, Any]:
    conn = get_db()
    try:
        ensure_video_deletion_schema(conn)
        ensure_render_jobs_schema(conn)
        row = _scope_row(
            conn,
            video_pk,
            owner_user_id=owner_user_id,
            brand_id=brand_id,
            lock=True,
        )
        if not row:
            raise VideoDeletionNotFound("Video not found.")
        if row[5] is not None:
            audit = conn.execute(
                f"""
                SELECT id, summary_json, purge_job_id
                FROM {AUDIT_TABLE}
                WHERE video_pk = ? AND CAST(owner_user_id AS VARCHAR) = CAST(? AS VARCHAR)
                ORDER BY created_at DESC LIMIT 1
                """,
                [int(video_pk), owner_user_id],
            ).fetchone()
            if not audit:
                raise VideoDeletionNotFound("Video not found.")
            summary = _parse_json(audit[1])
            return {**summary, "audit_id": str(audit[0]), "purge_job_id": str(audit[2] or ""), "already_deleted": True}

        source_video_id = str(row[1] or "")
        _assert_not_processing(
            conn,
            video_pk=int(video_pk),
            source_video_id=source_video_id,
            owner_user_id=owner_user_id,
            brand_id=brand_id,
        )
        snapshot = _build_snapshot(conn, row, video_pk=int(video_pk))
        summary = snapshot["summary"]
        manifest = snapshot["manifest"]

        queued_jobs_cancelled = _cancel_queued_jobs(
            conn,
            video_pk=int(video_pk),
            source_video_id=source_video_id,
            owner_user_id=owner_user_id,
        )
        cancelled_schedules = _cancel_publish_schedules(conn, manifest)
        summary["queued_jobs_cancelled"] = queued_jobs_cancelled
        summary["cancelled_schedules"] = cancelled_schedules
        summary["cancelled_schedule_count"] = sum(cancelled_schedules.values())

        asset_keys = [str(asset.get("file_key") or "") for asset in manifest.get("assets") or [] if asset.get("file_key")]
        if asset_keys:
            placeholders = ", ".join("?" for _ in asset_keys)
            conn.execute(
                f"""
                UPDATE shorts_storage_assets
                SET status = 'inactive', updated_at = CURRENT_TIMESTAMP
                WHERE file_key IN ({placeholders})
                  AND CAST(user_id AS VARCHAR) = CAST(? AS VARCHAR)
                  AND brand_id IS NOT DISTINCT FROM ?
                """,
                [*asset_keys, owner_user_id, brand_id],
            )

        audit_id = str(uuid4())
        purge_job_id = str(uuid4())
        conn.execute(
            """
            UPDATE youtube_videos
            SET deleted_at = CURRENT_TIMESTAMP, deleted_by = ?
            WHERE id = ? AND deleted_at IS NULL
            """,
            [deleted_by, int(video_pk)],
        )
        conn.execute(
            f"""
            INSERT INTO {AUDIT_TABLE} (
                id, video_pk, source_video_id, owner_user_id, brand_id, deleted_by,
                status, summary_json, manifest_json, purge_job_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'queued', {_json_param(conn)}, {_json_param(conn)}, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            [
                audit_id,
                int(video_pk),
                source_video_id,
                owner_user_id,
                brand_id,
                deleted_by,
                json.dumps(summary, ensure_ascii=False, sort_keys=True),
                json.dumps(manifest, ensure_ascii=False, sort_keys=True, default=str),
                purge_job_id,
            ],
        )
        payload = {
            "audit_id": audit_id,
            "video_pk": int(video_pk),
            "source_video_id": source_video_id,
            "brand_id": brand_id,
            "job_origin": "customer",
        }
        conn.execute(
            f"""
            INSERT INTO shorts_render_jobs (
                id, user_id, type, status, priority, payload_json, input_hash,
                attempts, max_attempts, created_at, updated_at
            ) VALUES (?, ?, ?, ?, -100, {_json_param(conn)}, ?, 0, 5, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            [
                purge_job_id,
                owner_user_id,
                JOB_TYPE_PURGE_VIDEO,
                JOB_STATUS_QUEUED,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                f"purge-video:{audit_id}",
            ],
        )
        conn.commit()
        return {**summary, "audit_id": audit_id, "purge_job_id": purge_job_id, "already_deleted": False}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
        conn = None


def _delete_storage_object(asset: dict[str, Any]) -> None:
    reference = str(asset.get("file_path") or "").strip()
    file_key = str(asset.get("file_key") or "").strip()
    if not reference and not file_key:
        return
    try:
        location = tenant_media.storage_location(reference or file_key)
    except (ValueError, tenant_media.TenantMediaConflict):
        logger.warning("Skipping invalid deletion asset reference key=%s", file_key)
        return
    storage = get_media_storage()
    if getattr(storage, "backend_name", "local") == "s3" and storage.exists(location["storage_key"]):
        storage.delete(location["storage_key"])
    get_media_storage("local").delete(location["storage_key"])
    location["local_path"].unlink(missing_ok=True)


def _delete_private_state(video_pk: int, source_video_id: str) -> None:
    names = {f"{int(video_pk)}_{kind}.json" for kind in STATE_KINDS}
    names.update(f"{source_video_id}_{kind}.json" for kind in STATE_KINDS)
    for name in names:
        for root in (PRIVATE_STATE_DIR, PRIVATE_STATE_DIR / "legacy", SHORTS_DIR):
            (root / name).unlink(missing_ok=True)
    face_cache_key = sha256(str(source_video_id or "").strip().encode("utf-8")).hexdigest()
    face_cache_dir = PRIVATE_STATE_DIR / "face_detection_cache"
    (face_cache_dir / f"{face_cache_key}.json").unlink(missing_ok=True)
    (face_cache_dir / f"{face_cache_key}.lock").unlink(missing_ok=True)


def _archive_published_rows(conn, audit_id: str, manifest: dict[str, Any]) -> int:
    archived = 0
    for row in manifest.get("generated_rows") or []:
        remote_ids = {
            key: str(row.get(key) or "").strip()
            for key in ("youtube_video_id", "instagram_media_id", "facebook_video_id", "tiktok_video_id")
            if str(row.get(key) or "").strip()
        }
        status = str(row.get("publish_status") or "").strip().lower()
        if not remote_ids and status not in {"published", "uploaded", "scheduled"}:
            continue
        conn.execute(
            f"""
            INSERT INTO {PUBLISHED_ARCHIVE_TABLE} (
                id, deletion_audit_id, original_generated_video_id, user_id, brand_id,
                source_video_id, clip_filename, publish_status, youtube_video_id,
                instagram_media_id, facebook_video_id, tiktok_video_id, generated_title,
                planned_publish_at, published_at, metadata_json, archived_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, {_json_param(conn)}, CURRENT_TIMESTAMP)
            ON CONFLICT(deletion_audit_id, original_generated_video_id) DO NOTHING
            """,
            [
                str(uuid4()), audit_id, str(row.get("id") or ""), manifest["owner_user_id"],
                manifest.get("brand_id"), manifest["source_video_id"], row.get("clip_filename"),
                row.get("publish_status"), row.get("youtube_video_id"), row.get("instagram_media_id"),
                row.get("facebook_video_id"), row.get("tiktok_video_id"), row.get("generated_title"),
                row.get("planned_publish_at"), row.get("published_at"),
                json.dumps({"remote_ids": remote_ids, "raw_plan_entry": _parse_json(row.get("raw_plan_entry_json"))}, ensure_ascii=False, sort_keys=True),
            ],
        )
        archived += 1
    return archived


def execute_video_purge_job(payload: dict[str, Any]) -> dict[str, Any]:
    audit_id = str(payload.get("audit_id") or "").strip()
    if not audit_id:
        raise ValueError("Video purge audit id is missing")
    conn = get_db()
    try:
        ensure_video_deletion_schema(conn)
        audit = conn.execute(
            f"SELECT video_pk, source_video_id, owner_user_id, brand_id, status, manifest_json FROM {AUDIT_TABLE} WHERE id = ?",
            [audit_id],
        ).fetchone()
        if not audit:
            raise VideoDeletionNotFound("Video deletion audit not found")
        if str(audit[4] or "").lower() == "purged":
            return {"audit_id": audit_id, "status": "purged", "already_purged": True}
        video_pk = int(audit[0])
        source_video_id = str(audit[1] or "")
        owner_user_id = str(audit[2] or "")
        brand_id = str(audit[3]) if audit[3] else None
        manifest = _parse_json(audit[5])
        conn.execute(
            f"UPDATE {AUDIT_TABLE} SET status='purging', purge_started_at=COALESCE(purge_started_at, CURRENT_TIMESTAMP), updated_at=CURRENT_TIMESTAMP WHERE id=?",
            [audit_id],
        )
        conn.commit()
        conn.close()
        conn = None

        deleted_keys: list[str] = []
        deleted_assets: list[dict[str, Any]] = []
        source_assets: list[dict[str, Any]] = []
        for asset in manifest.get("assets") or []:
            if str(asset.get("file_type") or "").lower() == "downloaded" or str(asset.get("file_key") or "").startswith("downloaded:"):
                source_assets.append(asset)
                continue
            _delete_storage_object(asset)
            deleted_assets.append(asset)
            if asset.get("file_key"):
                deleted_keys.append(str(asset["file_key"]))
        try:
            cdn_paths: set[str] = set()
            for asset in deleted_assets:
                reference = str(asset.get("file_path") or asset.get("file_key") or "").strip()
                if reference:
                    cdn_paths.add(f"/{tenant_media.storage_location(reference)['storage_key']}")
            ordered_paths = sorted(cdn_paths)
            for offset in range(0, len(ordered_paths), 1000):
                invalidate_cloudfront_paths(ordered_paths[offset : offset + 1000])
        except Exception:
            logger.warning("Could not invalidate deleted video CDN paths audit_id=%s", audit_id, exc_info=True)
        _delete_private_state(video_pk, source_video_id)

        conn = get_db()
        ensure_video_deletion_schema(conn)
        archived_count = _archive_published_rows(conn, audit_id, manifest)

        generated_ids = [str(row.get("id") or "") for row in manifest.get("generated_rows") or [] if str(row.get("id") or "")]
        share_link_ids: list[str] = []
        if generated_ids and table_columns(conn, "short_share_links"):
            placeholders = ", ".join("?" for _ in generated_ids)
            share_link_ids = [
                str(row[0])
                for row in conn.execute(
                    f"SELECT id FROM short_share_links WHERE CAST(generated_video_id AS VARCHAR) IN ({placeholders})",
                    generated_ids,
                ).fetchall()
            ]
        if share_link_ids:
            placeholders = ", ".join("?" for _ in share_link_ids)
            if table_columns(conn, "outreach_scheduled_emails"):
                conn.execute(f"DELETE FROM outreach_scheduled_emails WHERE CAST(share_link_id AS VARCHAR) IN ({placeholders})", share_link_ids)
            if table_columns(conn, "onboarding_magic_links"):
                conn.execute(f"DELETE FROM onboarding_magic_links WHERE CAST(share_link_id AS VARCHAR) IN ({placeholders})", share_link_ids)
            conn.execute(f"DELETE FROM short_share_links WHERE CAST(id AS VARCHAR) IN ({placeholders})", share_link_ids)

        if table_columns(conn, "shorts_quick_sessions"):
            conn.execute("DELETE FROM shorts_quick_sessions WHERE video_pk = ?", [video_pk])
        if table_columns(conn, "shorts_compilations"):
            conn.execute("DELETE FROM shorts_compilations WHERE video_pk = ?", [video_pk])
        if table_columns(conn, "shorts_generated_videos"):
            conn.execute(
                """
                DELETE FROM shorts_generated_videos
                WHERE CAST(source_video_id AS VARCHAR) = CAST(? AS VARCHAR)
                  AND CAST(user_id AS VARCHAR) = CAST(? AS VARCHAR)
                  AND brand_id IS NOT DISTINCT FROM ?
                """,
                [source_video_id, owner_user_id, brand_id],
            )

        for table_name in ("shorts_instagram_queue", "shorts_facebook_queue", "shorts_tiktok_queue"):
            if not table_columns(conn, table_name):
                continue
            conn.execute(
                f"""
                DELETE FROM {table_name}
                WHERE CAST(video_id AS VARCHAR) = CAST(? AS VARCHAR)
                  AND CAST(user_id AS VARCHAR) = CAST(? AS VARCHAR)
                  AND brand_id IS NOT DISTINCT FROM ?
                  AND lower(coalesce(status, '')) NOT IN ('published', 'uploaded')
                """,
                [source_video_id, owner_user_id, brand_id],
            )
        if table_columns(conn, "shorts_youtube_schedules"):
            conn.execute(
                """
                DELETE FROM shorts_youtube_schedules
                WHERE CAST(user_id AS VARCHAR) = CAST(? AS VARCHAR)
                  AND brand_id IS NOT DISTINCT FROM ?
                  AND asset_key LIKE ? AND youtube_video_id IS NULL
                """,
                [owner_user_id, brand_id, f"clip:{video_pk}:%"],
            )

        if not manifest.get("shared_source"):
            removed_source = tenant_media.delete_source_media(conn, source_video_id, excluding_pks=[video_pk])
            if removed_source:
                conn.execute("DELETE FROM youtube_transcripts WHERE video_id = ?", [source_video_id])
                deleted_assets.extend(source_assets)
                deleted_keys.extend(
                    str(asset["file_key"])
                    for asset in source_assets
                    if asset.get("file_key")
                )

        if table_columns(conn, "free_tool_text_cache"):
            active_source = conn.execute(
                """
                SELECT 1
                FROM youtube_videos
                WHERE video_id = ? AND deleted_at IS NULL
                LIMIT 1
                """,
                [source_video_id],
            ).fetchone()
            if not active_source:
                conn.execute(
                    "DELETE FROM free_tool_text_cache WHERE youtube_video_id = ?",
                    [source_video_id],
                )

        if table_columns(conn, "shorts_video_legacy_owners"):
            conn.execute("DELETE FROM shorts_video_legacy_owners WHERE video_pk = ?", [video_pk])
        if "promoted_source_video_id" in table_columns(conn, "discovery_leads"):
            conn.execute("UPDATE discovery_leads SET promoted_source_video_id = NULL WHERE promoted_source_video_id = ?", [video_pk])
        if "first_video_id" in table_columns(conn, "autopilot_leads"):
            conn.execute("UPDATE autopilot_leads SET first_video_id = NULL WHERE first_video_id = ?", [video_pk])

        if deleted_keys:
            placeholders = ", ".join("?" for _ in deleted_keys)
            conn.execute(
                f"DELETE FROM shorts_storage_assets WHERE file_key IN ({placeholders}) AND CAST(user_id AS VARCHAR)=CAST(? AS VARCHAR) AND brand_id IS NOT DISTINCT FROM ?",
                [*deleted_keys, owner_user_id, brand_id],
            )

        # Keep a minimal tombstone instead of hard-deleting the row. This keeps
        # historical foreign references and deletion idempotency intact while
        # every customer/media lookup excludes deleted_at rows.
        conn.execute(
            """
            UPDATE youtube_videos
            SET video_url = NULL, thumbnail_url = NULL, download_status = 'deleted',
                transcript_status = 'deleted', downloaded_at = NULL
            WHERE id = ? AND deleted_at IS NOT NULL
            """,
            [video_pk],
        )
        conn.execute(
            f"""
            UPDATE {AUDIT_TABLE}
            SET status='purged', purged_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP,
                error=NULL
            WHERE id=?
            """,
            [audit_id],
        )
        conn.commit()
        return {
            "audit_id": audit_id,
            "status": "purged",
            "deleted_object_count": len(deleted_keys),
            "archived_published_count": archived_count,
            "kept_tombstone": True,
        }
    except Exception as exc:
        try:
            if conn is not None:
                conn.rollback()
                conn.close()
            error_conn = get_db()
            error_conn.execute(
                f"UPDATE {AUDIT_TABLE} SET status='retry', error=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                [str(exc)[:1000], audit_id],
            )
            error_conn.commit()
            error_conn.close()
        except Exception:
            logger.exception("Could not update video purge audit failure audit_id=%s", audit_id)
        raise
    finally:
        if conn is not None:
            conn.close()
