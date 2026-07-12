from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from app.video_shorts.services.db import ensure_user_events_schema, get_db


logger = logging.getLogger(__name__)


def _json_value_sql(conn, param_placeholder: str = "?") -> str:
    if getattr(conn, "backend_name", "") == "postgres":
        return f"CAST({param_placeholder} AS JSONB)"
    return param_placeholder


def track_event(
    user_id: str,
    event_name: str,
    *,
    video_id: Optional[str] = None,
    short_id: Optional[str] = None,
    platform: Optional[str] = None,
    status: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    owner_id = str(user_id or "").strip()
    name = str(event_name or "").strip()
    if not owner_id or not name:
        return
    conn = None
    try:
        conn = get_db()
        ensure_user_events_schema(conn)
        metadata_json = None
        if metadata is not None:
            metadata_json = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
        conn.execute(
            f"""
            INSERT INTO user_events (
                user_id,
                event_name,
                video_id,
                short_id,
                platform,
                status,
                metadata,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, {_json_value_sql(conn)}, now())
            """,
            [
                owner_id,
                name,
                str(video_id or "").strip() or None,
                str(short_id or "").strip() or None,
                str(platform or "").strip() or None,
                str(status or "").strip() or None,
                metadata_json,
            ],
        )
        conn.commit()
    except Exception as exc:
        logger.warning(
            "track_event failed user_id=%s event_name=%s: %s",
            owner_id,
            name,
            exc,
        )
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
