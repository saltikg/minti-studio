from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from app.video_shorts.services.db import (
    ensure_onboarding_magic_links_schema,
    ensure_user_events_schema,
    get_db,
    table_columns,
)


logger = logging.getLogger(__name__)

OUTREACH_ENGAGEMENT_EVENTS = {
    "share_play",
    "share_watch_progress",
    "share_cta_click",
    "share_yes_intent",
    "lead_feed_watch_start",
    "lead_feed_trial_cta_click",
    "free_tool_gate_click",
    "free_tool_submit",
    "channel_connected",
}


def _json_value_sql(conn, param_placeholder: str = "?") -> str:
    if getattr(conn, "backend_name", "") == "postgres":
        return f"CAST({param_placeholder} AS JSONB)"
    return param_placeholder


def _record_outreach_engagement(
    conn,
    *,
    user_id: str,
    event_name: str,
    metadata: Optional[Dict[str, Any]],
) -> None:
    if event_name not in OUTREACH_ENGAGEMENT_EVENTS:
        return
    ensure_onboarding_magic_links_schema(conn)
    magic_columns = table_columns(conn, "onboarding_magic_links")
    if not {"clicked_at", "engaged_at"}.issubset(magic_columns):
        return

    event_metadata = metadata if isinstance(metadata, dict) else {}
    share_link_id = str(event_metadata.get("share_link_id") or "").strip()
    share_token = str(event_metadata.get("token") or "").strip()
    lead_id = str(
        event_metadata.get("autopilot_lead_id") or event_metadata.get("lead_id") or ""
    ).strip()
    identity_conditions = []
    identity_params: list[str] = []
    share_columns = table_columns(conn, "short_share_links")
    if share_columns:
        if share_link_id:
            identity_conditions.append("CAST(sl.id AS VARCHAR) = ?")
            identity_params.append(share_link_id)
        if share_token and "token" in share_columns:
            identity_conditions.append("sl.token = ?")
            identity_params.append(share_token)
        if lead_id and "autopilot_lead_id" in share_columns:
            identity_conditions.append("CAST(sl.autopilot_lead_id AS VARCHAR) = ?")
            identity_params.append(lead_id)

    match_sql = "CAST(oml.user_id AS VARCHAR) = ?"
    params: list[str] = [user_id]
    if identity_conditions:
        link_match_parts = []
        if "share_link_id" in magic_columns:
            link_match_parts.append(
                "(oml.share_link_id IS NOT NULL AND CAST(oml.share_link_id AS VARCHAR) = CAST(sl.id AS VARCHAR))"
            )
        if "share_link_token" in magic_columns and "token" in share_columns:
            link_match_parts.append(
                "(COALESCE(oml.share_link_token, '') <> '' AND oml.share_link_token = sl.token)"
            )
        if link_match_parts:
            match_sql = f"""
                EXISTS (
                    SELECT 1
                    FROM short_share_links sl
                    WHERE ({' OR '.join(link_match_parts)})
                      AND ({' OR '.join(identity_conditions)})
                )
            """
            params = identity_params

    conn.execute(
        f"""
        UPDATE onboarding_magic_links oml
        SET engaged_at = COALESCE(engaged_at, now())
        WHERE clicked_at IS NOT NULL
          AND clicked_at <= now()
          AND engaged_at IS NULL
          AND ({match_sql})
        """,
        params,
    )


def prepare_transcript_completed_transition(
    conn,
    *,
    video_pk: Optional[int] = None,
    video_id: Optional[str] = None,
) -> tuple[Optional[str], bool]:
    resolved_video_id = str(video_id or "").strip() or None
    prior_status = ""
    row = None
    if video_pk is not None:
        row = conn.execute(
            """
            SELECT video_id, COALESCE(transcript_status, '')
            FROM youtube_videos
            WHERE id = ?
            """,
            [video_pk],
        ).fetchone()
    elif resolved_video_id:
        row = conn.execute(
            """
            SELECT video_id, COALESCE(transcript_status, '')
            FROM youtube_videos
            WHERE video_id = ?
            LIMIT 1
            """,
            [resolved_video_id],
        ).fetchone()
    if row:
        resolved_video_id = str(row[0] or "").strip() or resolved_video_id
        prior_status = str(row[1] or "").strip().lower()
    return resolved_video_id, prior_status != "done"


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
        _record_outreach_engagement(
            conn,
            user_id=owner_id,
            event_name=name,
            metadata=metadata,
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
