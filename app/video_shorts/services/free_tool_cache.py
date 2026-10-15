from __future__ import annotations

import json
import logging
from typing import Any, Optional

from app.video_shorts.services.db import ensure_free_tool_text_cache_schema, get_db, table_columns


logger = logging.getLogger(__name__)


def _json_dumps(value: Any) -> str:
    return json.dumps(value or [], ensure_ascii=False, sort_keys=True)


def _json_loads(raw: Any, default: Any) -> Any:
    if raw is None:
        return default
    if isinstance(raw, (list, dict)):
        return raw
    try:
        return json.loads(raw or "")
    except Exception:
        return default


def write_transcript_cache(video_id: str, *, full_text: str, segments: list[dict]) -> bool:
    clean_video_id = str(video_id or "").strip()
    if not clean_video_id:
        return False
    conn = get_db()
    try:
        ensure_free_tool_text_cache_schema(conn)
        conn.execute(
            """
            INSERT INTO free_tool_text_cache (
                youtube_video_id, full_text, segments_json,
                transcript_cached_at, created_at, updated_at
            )
            VALUES (?, ?, ?, now(), now(), now())
            ON CONFLICT (youtube_video_id) DO UPDATE
            SET full_text = EXCLUDED.full_text,
                segments_json = EXCLUDED.segments_json,
                transcript_cached_at = now(),
                updated_at = now()
            """,
            [clean_video_id, str(full_text or ""), _json_dumps(segments)],
        )
        conn.commit()
        return True
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.warning("free_tool_text_cache transcript write failed video_id=%s", clean_video_id, exc_info=True)
        return False
    finally:
        conn.close()


def write_suggestions_cache(video_id: str, *, entries: list[dict], video_pk: int) -> bool:
    from app.video_shorts.services import tenant_media
    video = tenant_media.resolve_video(video_id, video_pk=video_pk)
    tenant_media.write_state(video, {"entries": entries}, kind="suggestions")
    return True


def load_text_cache(video_id: str, *, video_pk: int | None = None) -> Optional[dict[str, Any]]:
    clean_video_id = str(video_id or "").strip()
    if not clean_video_id:
        return None
    conn = get_db()
    try:
        ensure_free_tool_text_cache_schema(conn)
        row = conn.execute(
            """
            SELECT full_text, segments_json, plan_entries_json,
                   transcript_cached_at, suggestions_cached_at
            FROM free_tool_text_cache
            WHERE youtube_video_id = ?
            LIMIT 1
            """,
            [clean_video_id],
        ).fetchone()
        if not row:
            return None
        return {
            "full_text": str(row[0] or ""),
            "segments": _json_loads(row[1], []),
            "plan_entries": _load_tenant_suggestions(clean_video_id, video_pk),
            "transcript_cached_at": row[3],
            "suggestions_cached_at": None,
        }
    except Exception:
        logger.warning("free_tool_text_cache load failed video_id=%s", clean_video_id, exc_info=True)
        return None
    finally:
        conn.close()


def _load_tenant_suggestions(video_id, video_pk):
    if video_pk is None:
        return []
    from app.video_shorts.services import tenant_media
    return tenant_media.load_state(video_id, "suggestions", video_pk=video_pk).get("entries", [])


def user_is_guest(user_id: Any) -> bool:
    clean_user_id = str(user_id or "").strip()
    if not clean_user_id:
        return False
    conn = get_db()
    try:
        if "is_guest" not in table_columns(conn, "shorts_users"):
            return False
        row = conn.execute(
            "SELECT COALESCE(is_guest, FALSE) FROM shorts_users WHERE CAST(id AS VARCHAR) = CAST(? AS VARCHAR) LIMIT 1",
            [clean_user_id],
        ).fetchone()
        return bool(row and row[0])
    except Exception:
        return False
    finally:
        conn.close()
