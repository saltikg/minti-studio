from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from werkzeug.utils import secure_filename

from app.video_shorts.services.brands import current_brand_id
from app.video_shorts.services.db import get_db, get_db_readonly
from app.video_shorts.services.storage import get_media_storage


def ensure_ai_video_schema(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS shorts_ai_characters (
            id VARCHAR PRIMARY KEY,
            user_id VARCHAR NOT NULL,
            brand_id VARCHAR,
            name VARCHAR NOT NULL,
            description TEXT,
            tone_notes TEXT,
            is_default BOOLEAN DEFAULT FALSE,
            heygen_avatar_id VARCHAR,
            heygen_avatar_name VARCHAR,
            heygen_avatar_gender VARCHAR,
            heygen_preview_image_url TEXT,
            heygen_preview_video_url TEXT,
            metadata_json TEXT,
            created_at TIMESTAMP DEFAULT now(),
            updated_at TIMESTAMP DEFAULT now()
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS shorts_ai_backgrounds (
            id VARCHAR PRIMARY KEY,
            user_id VARCHAR NOT NULL,
            brand_id VARCHAR,
            name VARCHAR NOT NULL,
            description TEXT,
            background_type VARCHAR NOT NULL DEFAULT 'image',
            is_default BOOLEAN DEFAULT FALSE,
            color_hex VARCHAR,
            source_url TEXT,
            storage_key TEXT,
            public_url TEXT,
            heygen_asset_id VARCHAR,
            metadata_json TEXT,
            created_at TIMESTAMP DEFAULT now(),
            updated_at TIMESTAMP DEFAULT now()
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS shorts_ai_videos (
            id VARCHAR PRIMARY KEY,
            video_id VARCHAR NOT NULL UNIQUE,
            user_id VARCHAR NOT NULL,
            brand_id VARCHAR,
            title VARCHAR NOT NULL,
            description TEXT,
            clip_filename VARCHAR NOT NULL,
            storage_key TEXT,
            public_url TEXT,
            duration_seconds INTEGER,
            provider VARCHAR DEFAULT 'manual_upload',
            source_kind VARCHAR DEFAULT 'uploaded_video',
            youtube_status VARCHAR,
            youtube_video_id VARCHAR,
            youtube_publish_at TEXT,
            youtube_published_at TEXT,
            metadata_json TEXT,
            created_at TIMESTAMP DEFAULT now(),
            updated_at TIMESTAMP DEFAULT now()
        )
        """
    )
    try:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_shorts_ai_characters_user_brand ON shorts_ai_characters(user_id, brand_id)"
        )
    except Exception:
        pass
    try:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_shorts_ai_backgrounds_user_brand ON shorts_ai_backgrounds(user_id, brand_id)"
        )
    except Exception:
        pass
    try:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_shorts_ai_videos_user_brand ON shorts_ai_videos(user_id, brand_id, created_at)"
        )
    except Exception:
        pass
    try:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_shorts_ai_videos_video_id ON shorts_ai_videos(video_id)"
        )
    except Exception:
        pass
    conn.commit()


def ensure_ai_video_schema_ready() -> None:
    conn = get_db()
    try:
        ensure_ai_video_schema(conn)
    finally:
        conn.close()


def _brand_filter_clause(brand_id: Optional[str], column_name: str = "brand_id") -> tuple[str, List[Any]]:
    if brand_id:
        return f"{column_name} = ?", [brand_id]
    return f"{column_name} IS NULL", []


def list_characters(user_id: str, brand_id: Optional[str]) -> List[Dict[str, Any]]:
    ensure_ai_video_schema_ready()
    conn = get_db_readonly()
    try:
        brand_sql, brand_params = _brand_filter_clause(brand_id)
        rows = conn.execute(
            f"""
            SELECT
                id,
                name,
                description,
                tone_notes,
                COALESCE(is_default, FALSE),
                heygen_avatar_id,
                heygen_avatar_name,
                heygen_avatar_gender,
                heygen_preview_image_url,
                heygen_preview_video_url,
                metadata_json
            FROM shorts_ai_characters
            WHERE user_id = ?
              AND {brand_sql}
            ORDER BY COALESCE(is_default, FALSE) DESC, lower(name), created_at DESC
            """,
            [user_id, *brand_params],
        ).fetchall()
    finally:
        conn.close()
    results: List[Dict[str, Any]] = []
    for row in rows:
        metadata = {}
        try:
            metadata = json.loads(row[10] or "{}")
        except Exception:
            metadata = {}
        results.append(
            {
                "id": row[0],
                "name": row[1] or "",
                "description": row[2] or "",
                "tone_notes": row[3] or "",
                "is_default": bool(row[4]),
                "heygen_avatar_id": row[5] or "",
                "heygen_avatar_name": row[6] or "",
                "heygen_avatar_gender": row[7] or "",
                "heygen_preview_image_url": row[8] or "",
                "heygen_preview_video_url": row[9] or "",
                "metadata": metadata,
            }
        )
    return results


def get_character(user_id: str, brand_id: Optional[str], character_id: str) -> Optional[Dict[str, Any]]:
    items = list_characters(user_id, brand_id)
    return next((item for item in items if item["id"] == character_id), None)


def _clear_default_character(conn, user_id: str, brand_id: Optional[str]) -> None:
    brand_sql, brand_params = _brand_filter_clause(brand_id)
    conn.execute(
        f"""
        UPDATE shorts_ai_characters
        SET is_default = FALSE,
            updated_at = now()
        WHERE user_id = ?
          AND {brand_sql}
        """,
        [user_id, *brand_params],
    )


def save_character(
    *,
    user_id: str,
    brand_id: Optional[str],
    character_id: Optional[str],
    name: str,
    description: str,
    tone_notes: str,
    is_default: bool,
    heygen_avatar: Optional[Dict[str, Any]],
) -> str:
    conn = get_db()
    try:
        ensure_ai_video_schema(conn)
        record_id = (character_id or str(uuid.uuid4())).strip()
        metadata_json = json.dumps({"source": "heygen"} if heygen_avatar else {}, ensure_ascii=False)
        if is_default:
            _clear_default_character(conn, user_id, brand_id)
        brand_sql, brand_params = _brand_filter_clause(brand_id)
        existing = conn.execute(
            f"""
            SELECT 1
            FROM shorts_ai_characters
            WHERE id = ?
              AND user_id = ?
              AND {brand_sql}
            """,
            [record_id, user_id, *brand_params],
        ).fetchone()
        params = [
            user_id,
            brand_id,
            name.strip(),
            description.strip(),
            tone_notes.strip(),
            bool(is_default),
            (heygen_avatar or {}).get("avatar_id"),
            (heygen_avatar or {}).get("avatar_name"),
            (heygen_avatar or {}).get("gender"),
            (heygen_avatar or {}).get("preview_image_url"),
            (heygen_avatar or {}).get("preview_video_url"),
            metadata_json,
        ]
        if existing:
            conn.execute(
                f"""
                UPDATE shorts_ai_characters
                SET user_id = ?,
                    brand_id = ?,
                    name = ?,
                    description = ?,
                    tone_notes = ?,
                    is_default = ?,
                    heygen_avatar_id = ?,
                    heygen_avatar_name = ?,
                    heygen_avatar_gender = ?,
                    heygen_preview_image_url = ?,
                    heygen_preview_video_url = ?,
                    metadata_json = ?,
                    updated_at = now()
                WHERE id = ?
                  AND user_id = ?
                  AND {brand_sql}
                """,
                [*params, record_id, user_id, *brand_params],
            )
        else:
            conn.execute(
                """
                INSERT INTO shorts_ai_characters (
                    id, user_id, brand_id, name, description, tone_notes, is_default,
                    heygen_avatar_id, heygen_avatar_name, heygen_avatar_gender,
                    heygen_preview_image_url, heygen_preview_video_url, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [record_id, *params],
            )
        count_row = conn.execute(
            f"""
            SELECT COUNT(*)
            FROM shorts_ai_characters
            WHERE user_id = ?
              AND {brand_sql}
            """,
            [user_id, *brand_params],
        ).fetchone()
        if int((count_row[0] if count_row else 0) or 0) == 1:
            conn.execute(
                "UPDATE shorts_ai_characters SET is_default = TRUE, updated_at = now() WHERE id = ?",
                [record_id],
            )
        conn.commit()
        return record_id
    finally:
        conn.close()


def set_default_character(user_id: str, brand_id: Optional[str], character_id: str) -> None:
    conn = get_db()
    try:
        ensure_ai_video_schema(conn)
        _clear_default_character(conn, user_id, brand_id)
        brand_sql, brand_params = _brand_filter_clause(brand_id)
        conn.execute(
            f"""
            UPDATE shorts_ai_characters
            SET is_default = TRUE,
                updated_at = now()
            WHERE id = ?
              AND user_id = ?
              AND {brand_sql}
            """,
            [character_id, user_id, *brand_params],
        )
        conn.commit()
    finally:
        conn.close()


def delete_character(user_id: str, brand_id: Optional[str], character_id: str) -> None:
    conn = get_db()
    try:
        ensure_ai_video_schema(conn)
        brand_sql, brand_params = _brand_filter_clause(brand_id)
        conn.execute(
            f"""
            DELETE FROM shorts_ai_characters
            WHERE id = ?
              AND user_id = ?
              AND {brand_sql}
            """,
            [character_id, user_id, *brand_params],
        )
        conn.commit()
    finally:
        conn.close()


def list_backgrounds(user_id: str, brand_id: Optional[str]) -> List[Dict[str, Any]]:
    ensure_ai_video_schema_ready()
    conn = get_db_readonly()
    try:
        brand_sql, brand_params = _brand_filter_clause(brand_id)
        rows = conn.execute(
            f"""
            SELECT
                id,
                name,
                description,
                background_type,
                COALESCE(is_default, FALSE),
                color_hex,
                source_url,
                storage_key,
                public_url,
                heygen_asset_id,
                metadata_json
            FROM shorts_ai_backgrounds
            WHERE user_id = ?
              AND {brand_sql}
            ORDER BY COALESCE(is_default, FALSE) DESC, lower(name), created_at DESC
            """,
            [user_id, *brand_params],
        ).fetchall()
    finally:
        conn.close()
    results: List[Dict[str, Any]] = []
    for row in rows:
        metadata = {}
        try:
            metadata = json.loads(row[10] or "{}")
        except Exception:
            metadata = {}
        results.append(
            {
                "id": row[0],
                "name": row[1] or "",
                "description": row[2] or "",
                "background_type": row[3] or "image",
                "is_default": bool(row[4]),
                "color_hex": row[5] or "",
                "source_url": row[6] or "",
                "storage_key": row[7] or "",
                "public_url": row[8] or "",
                "heygen_asset_id": row[9] or "",
                "metadata": metadata,
            }
        )
    return results


def get_background(user_id: str, brand_id: Optional[str], background_id: str) -> Optional[Dict[str, Any]]:
    items = list_backgrounds(user_id, brand_id)
    return next((item for item in items if item["id"] == background_id), None)


def _clear_default_background(conn, user_id: str, brand_id: Optional[str]) -> None:
    brand_sql, brand_params = _brand_filter_clause(brand_id)
    conn.execute(
        f"""
        UPDATE shorts_ai_backgrounds
        SET is_default = FALSE,
            updated_at = now()
        WHERE user_id = ?
          AND {brand_sql}
        """,
        [user_id, *brand_params],
    )


def _background_storage_key(user_id: str, brand_id: Optional[str], filename: str) -> str:
    brand_segment = secure_filename(brand_id or "default") or "default"
    return f"user_ai_backgrounds/{user_id}/{brand_segment}/{filename}"


def save_background(
    *,
    user_id: str,
    brand_id: Optional[str],
    background_id: Optional[str],
    name: str,
    description: str,
    background_type: str,
    is_default: bool,
    color_hex: str,
    source_url: str,
    heygen_asset_id: str,
    upload,
) -> str:
    conn = get_db()
    try:
        ensure_ai_video_schema(conn)
        record_id = (background_id or str(uuid.uuid4())).strip()
        cleaned_type = (background_type or "image").strip().lower()
        if cleaned_type not in {"image", "video", "color"}:
            cleaned_type = "image"
        storage_key = ""
        public_url = ""
        metadata: Dict[str, Any] = {}
        if upload and getattr(upload, "filename", ""):
            safe_name = secure_filename(Path(upload.filename).name) or f"{record_id}.bin"
            storage_key = _background_storage_key(user_id, brand_id, safe_name)
            data = upload.read()
            if data:
                storage = get_media_storage()
                storage.put_bytes(data, storage_key, content_type=getattr(upload, "mimetype", None) or None)
                public_url = storage.public_url(storage_key)
                metadata["uploaded_filename"] = safe_name
        if is_default:
            _clear_default_background(conn, user_id, brand_id)
        brand_sql, brand_params = _brand_filter_clause(brand_id)
        existing = conn.execute(
            f"""
            SELECT storage_key, public_url
            FROM shorts_ai_backgrounds
            WHERE id = ?
              AND user_id = ?
              AND {brand_sql}
            """,
            [record_id, user_id, *brand_params],
        ).fetchone()
        if existing:
            if not storage_key:
                storage_key = str(existing[0] or "")
            if not public_url:
                public_url = str(existing[1] or "")
        metadata_json = json.dumps(metadata, ensure_ascii=False)
        params = [
            user_id,
            brand_id,
            name.strip(),
            description.strip(),
            cleaned_type,
            bool(is_default),
            color_hex.strip(),
            source_url.strip(),
            storage_key,
            public_url,
            heygen_asset_id.strip(),
            metadata_json,
        ]
        if existing:
            conn.execute(
                f"""
                UPDATE shorts_ai_backgrounds
                SET user_id = ?,
                    brand_id = ?,
                    name = ?,
                    description = ?,
                    background_type = ?,
                    is_default = ?,
                    color_hex = ?,
                    source_url = ?,
                    storage_key = ?,
                    public_url = ?,
                    heygen_asset_id = ?,
                    metadata_json = ?,
                    updated_at = now()
                WHERE id = ?
                  AND user_id = ?
                  AND {brand_sql}
                """,
                [*params, record_id, user_id, *brand_params],
            )
        else:
            conn.execute(
                """
                INSERT INTO shorts_ai_backgrounds (
                    id, user_id, brand_id, name, description, background_type, is_default,
                    color_hex, source_url, storage_key, public_url, heygen_asset_id, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [record_id, *params],
            )
        count_row = conn.execute(
            f"""
            SELECT COUNT(*)
            FROM shorts_ai_backgrounds
            WHERE user_id = ?
              AND {brand_sql}
            """,
            [user_id, *brand_params],
        ).fetchone()
        if int((count_row[0] if count_row else 0) or 0) == 1:
            conn.execute(
                "UPDATE shorts_ai_backgrounds SET is_default = TRUE, updated_at = now() WHERE id = ?",
                [record_id],
            )
        conn.commit()
        return record_id
    finally:
        conn.close()


def set_default_background(user_id: str, brand_id: Optional[str], background_id: str) -> None:
    conn = get_db()
    try:
        ensure_ai_video_schema(conn)
        _clear_default_background(conn, user_id, brand_id)
        brand_sql, brand_params = _brand_filter_clause(brand_id)
        conn.execute(
            f"""
            UPDATE shorts_ai_backgrounds
            SET is_default = TRUE,
                updated_at = now()
            WHERE id = ?
              AND user_id = ?
              AND {brand_sql}
            """,
            [background_id, user_id, *brand_params],
        )
        conn.commit()
    finally:
        conn.close()


def delete_background(user_id: str, brand_id: Optional[str], background_id: str) -> None:
    conn = get_db()
    try:
        ensure_ai_video_schema(conn)
        brand_sql, brand_params = _brand_filter_clause(brand_id)
        row = conn.execute(
            f"""
            SELECT storage_key
            FROM shorts_ai_backgrounds
            WHERE id = ?
              AND user_id = ?
              AND {brand_sql}
            """,
            [background_id, user_id, *brand_params],
        ).fetchone()
        if row and row[0]:
            try:
                get_media_storage().delete(str(row[0]))
            except Exception:
                pass
        conn.execute(
            f"""
            DELETE FROM shorts_ai_backgrounds
            WHERE id = ?
              AND user_id = ?
              AND {brand_sql}
            """,
            [background_id, user_id, *brand_params],
        )
        conn.commit()
    finally:
        conn.close()


def create_ai_video(
    *,
    user_id: str,
    brand_id: Optional[str],
    title: str,
    description: str,
    clip_filename: str,
    storage_key: str,
    public_url: str,
    duration_seconds: int,
    provider: str = "manual_upload",
    source_kind: str = "uploaded_video",
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    conn = get_db()
    try:
        ensure_ai_video_schema(conn)
        record_id = str(uuid.uuid4())
        video_id = f"ai_{uuid.uuid4().hex[:18]}"
        metadata_json = json.dumps(metadata or {}, ensure_ascii=False)
        conn.execute(
            """
            INSERT INTO shorts_ai_videos (
                id,
                video_id,
                user_id,
                brand_id,
                title,
                description,
                clip_filename,
                storage_key,
                public_url,
                duration_seconds,
                provider,
                source_kind,
                youtube_status,
                youtube_video_id,
                youtube_publish_at,
                youtube_published_at,
                metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', NULL, NULL, NULL, ?)
            """,
            [
                record_id,
                video_id,
                user_id,
                brand_id,
                title.strip(),
                description.strip(),
                clip_filename.strip(),
                storage_key.strip(),
                public_url.strip(),
                int(duration_seconds or 0),
                provider.strip() or "manual_upload",
                source_kind.strip() or "uploaded_video",
                metadata_json,
            ],
        )
        conn.commit()
        return {
            "id": record_id,
            "video_id": video_id,
            "user_id": user_id,
            "brand_id": brand_id,
            "title": title.strip(),
            "description": description.strip(),
            "clip_filename": clip_filename.strip(),
            "storage_key": storage_key.strip(),
            "public_url": public_url.strip(),
            "duration_seconds": int(duration_seconds or 0),
            "provider": provider.strip() or "manual_upload",
            "source_kind": source_kind.strip() or "uploaded_video",
            "youtube_status": "",
            "youtube_video_id": "",
            "youtube_publish_at": "",
            "youtube_published_at": "",
            "metadata": metadata or {},
        }
    finally:
        conn.close()


def list_ai_videos(user_id: str, brand_id: Optional[str]) -> List[Dict[str, Any]]:
    ensure_ai_video_schema_ready()
    conn = get_db_readonly()
    try:
        brand_sql, brand_params = _brand_filter_clause(brand_id)
        rows = conn.execute(
            f"""
            SELECT
                id,
                video_id,
                title,
                description,
                clip_filename,
                public_url,
                duration_seconds,
                provider,
                source_kind,
                COALESCE(youtube_status, ''),
                COALESCE(youtube_video_id, ''),
                COALESCE(youtube_publish_at, ''),
                COALESCE(youtube_published_at, ''),
                metadata_json,
                created_at
            FROM shorts_ai_videos
            WHERE user_id = ?
              AND {brand_sql}
            ORDER BY created_at DESC, id DESC
            """,
            [user_id, *brand_params],
        ).fetchall()
    finally:
        conn.close()
    results: List[Dict[str, Any]] = []
    for row in rows:
        metadata = {}
        try:
            metadata = json.loads(row[13] or "{}")
        except Exception:
            metadata = {}
        results.append(
            {
                "id": row[0],
                "video_id": row[1] or "",
                "title": row[2] or "",
                "description": row[3] or "",
                "clip_filename": row[4] or "",
                "public_url": row[5] or "",
                "duration_seconds": int(row[6] or 0),
                "provider": row[7] or "",
                "source_kind": row[8] or "",
                "youtube_status": row[9] or "",
                "youtube_video_id": row[10] or "",
                "youtube_publish_at": row[11] or "",
                "youtube_published_at": row[12] or "",
                "metadata": metadata,
                "created_at": row[14],
            }
        )
    return results


def get_ai_video(user_id: str, brand_id: Optional[str], ai_video_id: str) -> Optional[Dict[str, Any]]:
    for item in list_ai_videos(user_id, brand_id):
        if item["id"] == ai_video_id:
            return item
    return None


def delete_ai_video(user_id: str, brand_id: Optional[str], ai_video_id: str) -> None:
    conn = get_db()
    try:
        ensure_ai_video_schema(conn)
        brand_sql, brand_params = _brand_filter_clause(brand_id)
        conn.execute(
            f"""
            DELETE FROM shorts_ai_videos
            WHERE id = ?
              AND user_id = ?
              AND {brand_sql}
            """,
            [ai_video_id, user_id, *brand_params],
        )
        conn.commit()
    finally:
        conn.close()


def update_ai_video_content(
    *,
    user_id: str,
    brand_id: Optional[str],
    ai_video_id: str,
    title: str,
    description: str,
) -> None:
    conn = get_db()
    try:
        ensure_ai_video_schema(conn)
        brand_sql, brand_params = _brand_filter_clause(brand_id)
        conn.execute(
            f"""
            UPDATE shorts_ai_videos
            SET title = ?,
                description = ?,
                updated_at = now()
            WHERE id = ?
              AND user_id = ?
              AND {brand_sql}
            """,
            [
                (title or "").strip(),
                (description or "").strip(),
                ai_video_id,
                user_id,
                *brand_params,
            ],
        )
        conn.commit()
    finally:
        conn.close()


def update_ai_video_youtube_state(
    *,
    user_id: str,
    brand_id: Optional[str],
    ai_video_id: str,
    youtube_status: str,
    youtube_video_id: Optional[str] = None,
    youtube_publish_at: Optional[str] = None,
    youtube_published_at: Optional[str] = None,
) -> None:
    conn = get_db()
    try:
        ensure_ai_video_schema(conn)
        brand_sql, brand_params = _brand_filter_clause(brand_id)
        conn.execute(
            f"""
            UPDATE shorts_ai_videos
            SET youtube_status = ?,
                youtube_video_id = ?,
                youtube_publish_at = ?,
                youtube_published_at = ?,
                updated_at = now()
            WHERE id = ?
              AND user_id = ?
              AND {brand_sql}
            """,
            [
                (youtube_status or "").strip(),
                (youtube_video_id or "").strip() or None,
                (youtube_publish_at or "").strip() or None,
                (youtube_published_at or "").strip() or None,
                ai_video_id,
                user_id,
                *brand_params,
            ],
        )
        conn.commit()
    finally:
        conn.close()


def list_ai_broadcast_entries(
    *,
    brand_id: Optional[str],
    user_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    ensure_ai_video_schema_ready()
    conn = get_db_readonly()
    try:
        brand_sql, brand_params = _brand_filter_clause(brand_id)
        sql = f"""
            SELECT
                id,
                video_id,
                user_id,
                brand_id,
                title,
                description,
                clip_filename,
                public_url,
                duration_seconds,
                COALESCE(youtube_status, ''),
                COALESCE(youtube_video_id, ''),
                COALESCE(youtube_publish_at, ''),
                COALESCE(youtube_published_at, ''),
                metadata_json,
                created_at
            FROM shorts_ai_videos
            WHERE {brand_sql}
        """
        params: List[Any] = [*brand_params]
        if user_id:
            sql += " AND user_id = ?"
            params.append(user_id)
        sql += " ORDER BY created_at DESC, id DESC"
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    results: List[Dict[str, Any]] = []
    for row in rows:
        metadata = {}
        try:
            metadata = json.loads(row[13] or "{}")
        except Exception:
            metadata = {}
        results.append(
            {
                "id": row[0],
                "video_id": row[1] or "",
                "user_id": row[2] or "",
                "brand_id": row[3],
                "title": row[4] or "",
                "description": row[5] or "",
                "clip_filename": row[6] or "",
                "public_url": row[7] or "",
                "duration_seconds": int(row[8] or 0),
                "youtube_status": row[9] or "",
                "youtube_video_id": row[10] or "",
                "youtube_publish_at": row[11] or "",
                "youtube_published_at": row[12] or "",
                "metadata": metadata,
                "created_at": row[14],
            }
        )
    return results


def current_ai_video_brand_id() -> Optional[str]:
    return current_brand_id()
