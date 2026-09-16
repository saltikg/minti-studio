from __future__ import annotations

import json
from typing import Any, Dict, Optional
from uuid import uuid4

from app.video_shorts.services.db import _schema_management_enabled, get_db, table_columns


LEAD_PIPELINE_STATES = {
    "new",
    "downloading",
    "downloaded",
    "planning",
    "planned",
    "generating",
    "awaiting_approval",
    "approved",
    "scheduling",
    "scheduled",
    "sent",
    "failed",
}
LEAD_PIPELINE_EVENTS_TABLE = "lead_pipeline_events"
LEAD_PIPELINE_ALLOWED_TRANSITIONS = {
    "new": {"downloading", "downloaded", "planning", "planned"},
    "downloading": {"downloaded", "planning"},
    "downloaded": {"planning", "planned"},
    "planning": {"planned"},
    "planned": {"generating"},
    "generating": {"awaiting_approval"},
    "awaiting_approval": {"approved"},
    "approved": {"scheduling", "scheduled"},
    "scheduling": {"scheduled"},
    "scheduled": {"sent"},
    "sent": set(),
    "failed": set(),
}


def _json_value_sql(conn, param_placeholder: str = "?") -> str:
    if getattr(conn, "backend_name", "") == "postgres":
        return f"CAST({param_placeholder} AS JSONB)"
    return param_placeholder


def _serialize_detail(detail: Optional[Dict[str, Any]]) -> Optional[str]:
    if detail is None:
        return None
    try:
        return json.dumps(detail, ensure_ascii=False, sort_keys=True)
    except Exception:
        return json.dumps({"detail": str(detail)}, ensure_ascii=False, sort_keys=True)


def is_valid_lead_pipeline_transition(from_state: str, to_state: Optional[str]) -> bool:
    normalized_from = str(from_state or "new").strip().lower() or "new"
    normalized_to = str(to_state or "").strip().lower()
    if not normalized_to:
        return True
    if normalized_to == normalized_from:
        return True
    if normalized_to == "failed":
        return True
    return normalized_to in LEAD_PIPELINE_ALLOWED_TRANSITIONS.get(normalized_from, set())


def ensure_lead_pipeline_schema(conn) -> None:
    if not _schema_management_enabled():
        return
    conn.execute("ALTER TABLE autopilot_leads ADD COLUMN IF NOT EXISTS pipeline_state VARCHAR DEFAULT 'new'")
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {LEAD_PIPELINE_EVENTS_TABLE} (
            id VARCHAR PRIMARY KEY,
            lead_id VARCHAR NOT NULL,
            event_type VARCHAR NOT NULL,
            from_state VARCHAR,
            to_state VARCHAR,
            detail JSON,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_{LEAD_PIPELINE_EVENTS_TABLE}_lead_created
        ON {LEAD_PIPELINE_EVENTS_TABLE}(lead_id, created_at DESC)
        """
    )


def lead_pipeline_available(conn) -> bool:
    ensure_lead_pipeline_schema(conn)
    return "pipeline_state" in table_columns(conn, "autopilot_leads") and bool(
        table_columns(conn, LEAD_PIPELINE_EVENTS_TABLE)
    )


def find_active_lead_for_scope(
    conn,
    *,
    owner_user_id: Any,
    brand_id: Any,
    video_pk: Any = None,
) -> Optional[Dict[str, str]]:
    if not lead_pipeline_available(conn):
        return None
    where_video = "AND l.first_video_id = ?" if video_pk is not None else ""
    params = [str(owner_user_id or "").strip(), str(brand_id or "").strip()]
    if video_pk is not None:
        params.append(int(video_pk))
    row = conn.execute(
        f"""
        SELECT CAST(l.id AS VARCHAR), COALESCE(l.pipeline_state, 'new')
        FROM autopilot_leads l
        WHERE CAST(l.user_id AS VARCHAR) = CAST(? AS VARCHAR)
          AND CAST(l.brand_id AS VARCHAR) = CAST(? AS VARCHAR)
          AND l.converted_at IS NULL
          {where_video}
        ORDER BY l.created_at DESC, l.id DESC
        LIMIT 1
        """,
        params,
    ).fetchone()
    if not row:
        return None
    return {"id": str(row[0]), "pipeline_state": str(row[1] or "new")}


def record_lead_pipeline_event(
    conn,
    *,
    lead_id: Any,
    event_type: str,
    to_state: Optional[str] = None,
    detail: Optional[Dict[str, Any]] = None,
    update_state: bool = True,
) -> Optional[Dict[str, Any]]:
    if not lead_id or not event_type or not lead_pipeline_available(conn):
        return None
    normalized_to_state = str(to_state or "").strip().lower() or None
    if normalized_to_state and normalized_to_state not in LEAD_PIPELINE_STATES:
        raise ValueError(f"Invalid lead pipeline state: {normalized_to_state}")
    row = conn.execute(
        """
        SELECT COALESCE(pipeline_state, 'new')
        FROM autopilot_leads
        WHERE CAST(id AS VARCHAR) = CAST(? AS VARCHAR)
        LIMIT 1
        """,
        [str(lead_id)],
    ).fetchone()
    if not row:
        return None
    from_state = str(row[0] or "new")
    transition_allowed = is_valid_lead_pipeline_transition(from_state, normalized_to_state)
    effective_to_state = normalized_to_state if update_state and normalized_to_state and transition_allowed else None
    if effective_to_state:
        conn.execute(
            """
            UPDATE autopilot_leads
               SET pipeline_state = ?
             WHERE CAST(id AS VARCHAR) = CAST(? AS VARCHAR)
            """,
            [effective_to_state, str(lead_id)],
        )
    event_detail = detail
    if update_state and normalized_to_state and not transition_allowed:
        event_detail = dict(detail or {})
        event_detail["transition_rejected"] = True
        event_detail["rejected_from_state"] = from_state
        event_detail["rejected_to_state"] = normalized_to_state
    conn.execute(
        f"""
        INSERT INTO {LEAD_PIPELINE_EVENTS_TABLE} (
            id,
            lead_id,
            event_type,
            from_state,
            to_state,
            detail,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, {_json_value_sql(conn)}, CURRENT_TIMESTAMP)
        """,
        [
            str(uuid4()),
            str(lead_id),
            str(event_type).strip(),
            from_state,
            effective_to_state,
            _serialize_detail(event_detail),
        ],
    )
    return {
        "lead_id": str(lead_id),
        "event_type": str(event_type).strip(),
        "from_state": from_state,
        "to_state": effective_to_state,
        "transition_allowed": transition_allowed,
    }


def record_lead_pipeline_event_for_scope(
    *,
    owner_user_id: Any,
    brand_id: Any,
    video_pk: Any = None,
    event_type: str,
    to_state: Optional[str] = None,
    detail: Optional[Dict[str, Any]] = None,
    update_state: bool = True,
) -> Optional[Dict[str, Any]]:
    conn = get_db()
    try:
        lead = find_active_lead_for_scope(
            conn,
            owner_user_id=owner_user_id,
            brand_id=brand_id,
            video_pk=video_pk,
        )
        if not lead:
            conn.commit()
            return None
        result = record_lead_pipeline_event(
            conn,
            lead_id=lead["id"],
            event_type=event_type,
            to_state=to_state,
            detail=detail,
            update_state=update_state,
        )
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
