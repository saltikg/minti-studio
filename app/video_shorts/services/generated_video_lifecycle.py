from __future__ import annotations

import json
from typing import Any, Dict, Optional

from app.video_shorts.services.db import get_db, table_columns

TABLE_NAME = "shorts_generated_videos"


def _sanitize_json_value(value: Any) -> Any:
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, list):
        return [_sanitize_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key).replace("\x00", ""): _sanitize_json_value(item) for key, item in value.items()}
    return value


def _clean_text(value: Any) -> Optional[str]:
    text = str(value or "").replace("\x00", "").strip()
    return text or None


def _first_non_empty(*values: Any) -> Optional[str]:
    for value in values:
        cleaned = _clean_text(value)
        if cleaned:
            return cleaned
    return None


def _extract_content_fields(raw_plan_entry: Optional[Dict[str, Any]]) -> Dict[str, Optional[str]]:
    entry = raw_plan_entry or {}
    return {
        "generated_title": _first_non_empty(entry.get("yt_title"), entry.get("title")),
        "generated_description": _first_non_empty(
            entry.get("yt_description"),
            entry.get("description"),
            entry.get("caption_text"),
        ),
        "generated_excerpt": _first_non_empty(entry.get("excerpt")),
        "generated_transcript_full": _first_non_empty(
            entry.get("transcript_full"),
            entry.get("transcript_full_custom"),
        ),
    }


def _normalize_platform(value: Optional[str]) -> Optional[str]:
    normalized = _clean_text(value)
    if not normalized:
        return None
    normalized = normalized.lower()
    return normalized if normalized in {"youtube", "instagram", "facebook", "tiktok"} else None


def _derive_primary_publish_platform(
    existing_row: Optional[Dict[str, Any]],
    incoming: Dict[str, Any],
) -> Optional[str]:
    explicit = _normalize_platform(incoming.get("primary_publish_platform"))
    if explicit:
        return explicit
    existing_primary = _normalize_platform((existing_row or {}).get("primary_publish_platform"))
    if existing_primary:
        return existing_primary
    published_status = _clean_text(incoming.get("publish_status"))
    if published_status and published_status.lower() == "published":
        for platform in ("youtube", "instagram", "facebook", "tiktok"):
            if incoming.get(f"{platform}_published_at"):
                return platform
    merged = {}
    for key in (
        "youtube_video_id",
        "instagram_media_id",
        "facebook_video_id",
        "tiktok_video_id",
        "youtube_published_at",
        "instagram_published_at",
        "facebook_published_at",
        "tiktok_published_at",
    ):
        merged[key] = incoming.get(key) or ((existing_row or {}).get(key) if existing_row else None)
    for platform in ("youtube", "instagram", "facebook", "tiktok"):
        if merged.get(f"{platform}_published_at"):
            return platform
    if merged.get("youtube_video_id"):
        return "youtube"
    if merged.get("instagram_media_id"):
        return "instagram"
    if merged.get("facebook_video_id"):
        return "facebook"
    if merged.get("tiktok_video_id"):
        return "tiktok"
    return None


def ensure_generated_videos_schema(conn) -> None:
    backend_name = getattr(conn, "backend_name", "")
    id_type = "BIGSERIAL PRIMARY KEY" if backend_name == "postgres" else "BIGINT PRIMARY KEY"
    json_type = "JSONB" if backend_name == "postgres" else "JSON"
    try:
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
                id {id_type},
                user_id VARCHAR,
                brand_id VARCHAR,
                source_video_id VARCHAR NOT NULL,
                source_channel_type VARCHAR NOT NULL DEFAULT 'youtube',
                clip_filename VARCHAR NOT NULL,
                output_filename VARCHAR,
                storage_file_key VARCHAR,
                generation_status VARCHAR,
                publish_status VARCHAR,
                youtube_video_id VARCHAR,
                instagram_media_id VARCHAR,
                facebook_video_id VARCHAR,
                tiktok_video_id VARCHAR,
                planned_publish_at TIMESTAMP,
                published_at TIMESTAMP,
                plan_run_id VARCHAR,
                raw_plan_entry_json {json_type},
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    except Exception as exc:
        if "read-only" in str(exc).lower():
            return
        raise

    cols = table_columns(conn, TABLE_NAME)
    for col_name, col_type in (
        ("user_id", "VARCHAR"),
        ("generated_title", "TEXT"),
        ("generated_description", "TEXT"),
        ("generated_excerpt", "TEXT"),
        ("generated_transcript_full", "TEXT"),
        ("youtube_published_at", "TIMESTAMP"),
        ("instagram_published_at", "TIMESTAMP"),
        ("facebook_published_at", "TIMESTAMP"),
        ("tiktok_published_at", "TIMESTAMP"),
        ("primary_publish_platform", "VARCHAR"),
    ):
        if col_name in cols:
            continue
        try:
            conn.execute(f"ALTER TABLE {TABLE_NAME} ADD COLUMN {col_name} {col_type}")
        except Exception:
            pass
    if "id" in cols and backend_name != "postgres":
        try:
            conn.execute(f"CREATE SEQUENCE IF NOT EXISTS {TABLE_NAME}_id_seq")
            conn.execute(
                f"ALTER TABLE {TABLE_NAME} ALTER COLUMN id SET DEFAULT nextval('{TABLE_NAME}_id_seq')"
            )
        except Exception:
            pass
    try:
        conn.execute(
            f"""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_{TABLE_NAME}_source_clip
            ON {TABLE_NAME}(source_video_id, source_channel_type, clip_filename)
            """
        )
    except Exception:
        pass
    for col_name in (
        "user_id",
        "source_video_id",
        "clip_filename",
        "youtube_video_id",
        "publish_status",
        "created_at",
        "brand_id",
    ):
        try:
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{TABLE_NAME}_{col_name} ON {TABLE_NAME}({col_name})"
            )
        except Exception:
            pass
    if "user_id" in table_columns(conn, TABLE_NAME):
        try:
            conn.execute(
                f"""
                UPDATE {TABLE_NAME} AS gv
                SET user_id = b.owner_user_id
                FROM shorts_brands AS b
                WHERE gv.user_id IS NULL
                  AND gv.brand_id IS NOT NULL
                  AND b.id = gv.brand_id
                """
            )
        except Exception:
            pass
        try:
            conn.execute(
                f"""
                UPDATE {TABLE_NAME} AS gv
                SET user_id = yv.owner_user_id
                FROM youtube_videos AS yv
                WHERE gv.user_id IS NULL
                  AND yv.video_id = gv.source_video_id
                """
            )
        except Exception:
            pass
    if backend_name == "postgres":
        try:
            conn.execute(
                f"""
                CREATE OR REPLACE FUNCTION set_{TABLE_NAME}_updated_at()
                RETURNS TRIGGER AS $$
                BEGIN
                    NEW.updated_at = CURRENT_TIMESTAMP;
                    RETURN NEW;
                END;
                $$ LANGUAGE plpgsql
                """
            )
            conn.execute(
                f"""
                DROP TRIGGER IF EXISTS trg_{TABLE_NAME}_updated_at ON {TABLE_NAME}
                """
            )
            conn.execute(
                f"""
                CREATE TRIGGER trg_{TABLE_NAME}_updated_at
                BEFORE UPDATE ON {TABLE_NAME}
                FOR EACH ROW
                EXECUTE FUNCTION set_{TABLE_NAME}_updated_at()
                """
            )
        except Exception:
            pass
    try:
        conn.commit()
    except Exception:
        pass


def _serialize_raw_plan_entry(raw_plan_entry: Optional[Dict[str, Any]]) -> Optional[str]:
    if not raw_plan_entry:
        return None
    try:
        return json.dumps(_sanitize_json_value(raw_plan_entry), ensure_ascii=False)
    except Exception:
        return None


def _normalize_generation_status(
    generation_status: Optional[str],
    publish_status: Optional[str],
) -> Optional[str]:
    status = (generation_status or "").strip().lower() or None
    publish = (publish_status or "").strip().lower()
    if status:
        return status
    if publish == "published":
        return "created"
    return None


def _resolve_brand_id(conn, source_video_id: Optional[str], brand_id: Optional[str]) -> Optional[str]:
    clean_brand_id = str(brand_id or "").strip() or None
    if clean_brand_id or not source_video_id:
        return clean_brand_id
    try:
        row = conn.execute(
            """
            SELECT CAST(brand_id AS VARCHAR)
            FROM youtube_videos
            WHERE video_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            [source_video_id],
        ).fetchone()
    except Exception:
        return None
    return str((row[0] if row else "") or "").strip() or None


def _resolve_user_id(
    conn,
    source_video_id: Optional[str],
    brand_id: Optional[str],
    user_id: Optional[str],
) -> Optional[str]:
    clean_user_id = str(user_id or "").strip() or None
    if clean_user_id:
        return clean_user_id
    clean_brand_id = str(brand_id or "").strip() or None
    if clean_brand_id:
        try:
            row = conn.execute(
                """
                SELECT CAST(owner_user_id AS VARCHAR)
                FROM shorts_brands
                WHERE id = ?
                LIMIT 1
                """,
                [clean_brand_id],
            ).fetchone()
        except Exception:
            row = None
        owner_user_id = str((row[0] if row else "") or "").strip() or None
        if owner_user_id:
            return owner_user_id
    if not source_video_id:
        return None
    try:
        row = conn.execute(
            """
            SELECT CAST(owner_user_id AS VARCHAR)
            FROM youtube_videos
            WHERE video_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            [source_video_id],
        ).fetchone()
    except Exception:
        return None
    return str((row[0] if row else "") or "").strip() or None


def upsert_generated_video_record(
    *,
    user_id: Optional[str] = None,
    source_video_id: str,
    clip_filename: str,
    source_channel_type: str = "youtube",
    brand_id: Optional[str] = None,
    output_filename: Optional[str] = None,
    storage_file_key: Optional[str] = None,
    generation_status: Optional[str] = None,
    publish_status: Optional[str] = None,
    youtube_video_id: Optional[str] = None,
    instagram_media_id: Optional[str] = None,
    facebook_video_id: Optional[str] = None,
    tiktok_video_id: Optional[str] = None,
    planned_publish_at: Optional[str] = None,
    published_at: Optional[str] = None,
    plan_run_id: Optional[str] = None,
    raw_plan_entry: Optional[Dict[str, Any]] = None,
    generated_title: Optional[str] = None,
    generated_description: Optional[str] = None,
    generated_excerpt: Optional[str] = None,
    generated_transcript_full: Optional[str] = None,
    youtube_published_at: Optional[str] = None,
    instagram_published_at: Optional[str] = None,
    facebook_published_at: Optional[str] = None,
    tiktok_published_at: Optional[str] = None,
    primary_publish_platform: Optional[str] = None,
) -> None:
    if not source_video_id or not clip_filename:
        return
    conn = get_db()
    try:
        ensure_generated_videos_schema(conn)
        resolved_brand_id = _resolve_brand_id(conn, source_video_id, brand_id)
        resolved_user_id = _resolve_user_id(conn, source_video_id, resolved_brand_id, user_id)
        raw_plan_entry_json = _serialize_raw_plan_entry(raw_plan_entry)
        normalized_generation_status = _normalize_generation_status(generation_status, publish_status)
        content_fields = _extract_content_fields(raw_plan_entry)
        existing_row_raw = conn.execute(
            f"""
            SELECT
                primary_publish_platform,
                youtube_video_id,
                instagram_media_id,
                facebook_video_id,
                tiktok_video_id,
                youtube_published_at,
                instagram_published_at,
                facebook_published_at,
                tiktok_published_at
            FROM {TABLE_NAME}
            WHERE source_video_id = ? AND source_channel_type = ? AND clip_filename = ?
            LIMIT 1
            """,
            [source_video_id, (source_channel_type or "youtube").strip().lower(), clip_filename],
        ).fetchone()
        existing_row = None
        if existing_row_raw:
            existing_row = {
                "primary_publish_platform": existing_row_raw[0],
                "youtube_video_id": existing_row_raw[1],
                "instagram_media_id": existing_row_raw[2],
                "facebook_video_id": existing_row_raw[3],
                "tiktok_video_id": existing_row_raw[4],
                "youtube_published_at": existing_row_raw[5],
                "instagram_published_at": existing_row_raw[6],
                "facebook_published_at": existing_row_raw[7],
                "tiktok_published_at": existing_row_raw[8],
            }
        incoming_primary = _derive_primary_publish_platform(
            existing_row,
            {
                "publish_status": publish_status,
                "primary_publish_platform": primary_publish_platform,
                "youtube_video_id": youtube_video_id,
                "instagram_media_id": instagram_media_id,
                "facebook_video_id": facebook_video_id,
                "tiktok_video_id": tiktok_video_id,
                "youtube_published_at": youtube_published_at,
                "instagram_published_at": instagram_published_at,
                "facebook_published_at": facebook_published_at,
                "tiktok_published_at": tiktok_published_at,
            },
        )
        conn.execute(
            f"""
            INSERT INTO {TABLE_NAME} (
                user_id,
                brand_id,
                source_video_id,
                source_channel_type,
                clip_filename,
                output_filename,
                storage_file_key,
                generation_status,
                publish_status,
                youtube_video_id,
                instagram_media_id,
                facebook_video_id,
                tiktok_video_id,
                planned_publish_at,
                published_at,
                plan_run_id,
                generated_title,
                generated_description,
                generated_excerpt,
                generated_transcript_full,
                youtube_published_at,
                instagram_published_at,
                facebook_published_at,
                tiktok_published_at,
                primary_publish_platform,
                raw_plan_entry_json,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT (source_video_id, source_channel_type, clip_filename)
            DO UPDATE SET
                user_id = COALESCE(EXCLUDED.user_id, {TABLE_NAME}.user_id),
                brand_id = COALESCE(EXCLUDED.brand_id, {TABLE_NAME}.brand_id),
                output_filename = COALESCE(EXCLUDED.output_filename, {TABLE_NAME}.output_filename),
                storage_file_key = COALESCE(EXCLUDED.storage_file_key, {TABLE_NAME}.storage_file_key),
                generation_status = COALESCE(EXCLUDED.generation_status, {TABLE_NAME}.generation_status),
                publish_status = COALESCE(EXCLUDED.publish_status, {TABLE_NAME}.publish_status),
                youtube_video_id = COALESCE(EXCLUDED.youtube_video_id, {TABLE_NAME}.youtube_video_id),
                instagram_media_id = COALESCE(EXCLUDED.instagram_media_id, {TABLE_NAME}.instagram_media_id),
                facebook_video_id = COALESCE(EXCLUDED.facebook_video_id, {TABLE_NAME}.facebook_video_id),
                tiktok_video_id = COALESCE(EXCLUDED.tiktok_video_id, {TABLE_NAME}.tiktok_video_id),
                planned_publish_at = COALESCE(EXCLUDED.planned_publish_at, {TABLE_NAME}.planned_publish_at),
                published_at = COALESCE(EXCLUDED.published_at, {TABLE_NAME}.published_at),
                plan_run_id = COALESCE(EXCLUDED.plan_run_id, {TABLE_NAME}.plan_run_id),
                generated_title = COALESCE(EXCLUDED.generated_title, {TABLE_NAME}.generated_title),
                generated_description = COALESCE(EXCLUDED.generated_description, {TABLE_NAME}.generated_description),
                generated_excerpt = COALESCE(EXCLUDED.generated_excerpt, {TABLE_NAME}.generated_excerpt),
                generated_transcript_full = COALESCE(EXCLUDED.generated_transcript_full, {TABLE_NAME}.generated_transcript_full),
                youtube_published_at = COALESCE(EXCLUDED.youtube_published_at, {TABLE_NAME}.youtube_published_at),
                instagram_published_at = COALESCE(EXCLUDED.instagram_published_at, {TABLE_NAME}.instagram_published_at),
                facebook_published_at = COALESCE(EXCLUDED.facebook_published_at, {TABLE_NAME}.facebook_published_at),
                tiktok_published_at = COALESCE(EXCLUDED.tiktok_published_at, {TABLE_NAME}.tiktok_published_at),
                primary_publish_platform = COALESCE(EXCLUDED.primary_publish_platform, {TABLE_NAME}.primary_publish_platform),
                raw_plan_entry_json = COALESCE(EXCLUDED.raw_plan_entry_json, {TABLE_NAME}.raw_plan_entry_json),
                updated_at = CURRENT_TIMESTAMP
            """,
            [
                resolved_user_id,
                resolved_brand_id,
                source_video_id,
                (source_channel_type or "youtube").strip().lower(),
                clip_filename,
                output_filename,
                storage_file_key,
                normalized_generation_status,
                (publish_status or "").strip().lower() or None,
                youtube_video_id,
                instagram_media_id,
                facebook_video_id,
                tiktok_video_id,
                planned_publish_at,
                published_at,
                plan_run_id,
                _first_non_empty(generated_title, content_fields.get("generated_title")),
                _first_non_empty(generated_description, content_fields.get("generated_description")),
                _first_non_empty(generated_excerpt, content_fields.get("generated_excerpt")),
                _first_non_empty(generated_transcript_full, content_fields.get("generated_transcript_full")),
                youtube_published_at,
                instagram_published_at,
                facebook_published_at,
                tiktok_published_at,
                incoming_primary,
                raw_plan_entry_json,
            ],
        )
        conn.commit()
    finally:
        conn.close()
