from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from app.video_shorts.services.db import table_columns


OutreachFollowupBucket = Literal[
    "converted",
    "failed",
    "hot_repeat",
    "watched_no_convert",
    "visited_once",
    "sent_no_visit",
    "scheduled",
]


def _user_event_metadata_text_sql(conn, field_name: str) -> str:
    safe_field = re.sub(r"[^a-zA-Z0-9_]", "", str(field_name or ""))
    if getattr(conn, "backend_name", "") == "postgres":
        return f"NULLIF(ue.metadata->>'{safe_field}', '')"
    return f"NULLIF(json_extract_string(ue.metadata, '$.{safe_field}'), '')"


def _user_event_metadata_numeric_sql(conn, field_name: str) -> str:
    text_expr = _user_event_metadata_text_sql(conn, field_name)
    if getattr(conn, "backend_name", "") == "postgres":
        return f"CAST({text_expr} AS DOUBLE PRECISION)"
    return f"CAST({text_expr} AS DOUBLE)"


def _in_clause(values: list[Any]) -> str:
    return ", ".join("?" for _ in values)


def _as_utc(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def decide_outreach_followup_bucket(
    *,
    converted: bool,
    any_failed: bool,
    any_sent: bool,
    any_scheduled: bool,
    visit_count: int,
    max_watched: float,
) -> OutreachFollowupBucket:
    if converted:
        return "converted"
    if any_failed:
        return "failed"
    if visit_count >= 2:
        return "hot_repeat"
    if max_watched >= 50:
        return "watched_no_convert"
    if visit_count == 1:
        return "visited_once"
    if any_sent and visit_count == 0:
        return "sent_no_visit"
    if any_scheduled and not any_sent:
        return "scheduled"
    return "scheduled"


def load_current_outreach_bucket(conn, share_link_id: int) -> OutreachFollowupBucket:
    share_link_columns = table_columns(conn, "short_share_links")
    if not share_link_columns:
        return "scheduled"
    has_archived = "archived" in share_link_columns
    archived_sql = "COALESCE(archived, false) = false" if has_archived else "TRUE"
    row = conn.execute(
        f"""
        SELECT
          id,
          NULLIF(CAST(autopilot_lead_id AS VARCHAR), '') AS autopilot_lead_id,
          lower(NULLIF(recipient_email, '')) AS recipient_email,
          token
        FROM short_share_links
        WHERE id = ?
          AND {archived_sql}
        LIMIT 1
        """,
        [share_link_id],
    ).fetchone()
    if not row:
        return "scheduled"

    lead_id = str(row[1] or "").strip()
    recipient_email = str(row[2] or "").strip().lower()
    if lead_id:
        link_rows = conn.execute(
            f"""
            SELECT id, token, NULLIF(CAST(autopilot_lead_id AS VARCHAR), '') AS autopilot_lead_id
            FROM short_share_links
            WHERE NULLIF(CAST(autopilot_lead_id AS VARCHAR), '') = ?
              AND {archived_sql}
            """,
            [lead_id],
        ).fetchall()
    elif recipient_email:
        link_rows = conn.execute(
            f"""
            SELECT id, token, NULLIF(CAST(autopilot_lead_id AS VARCHAR), '') AS autopilot_lead_id
            FROM short_share_links
            WHERE lower(NULLIF(recipient_email, '')) = ?
              AND {archived_sql}
            """,
            [recipient_email],
        ).fetchall()
    else:
        link_rows = [(row[0], row[3], lead_id)]

    link_ids = [int(link_row[0]) for link_row in link_rows if link_row and link_row[0] is not None]
    tokens = [str(link_row[1] or "").strip() for link_row in link_rows if str(link_row[1] or "").strip()]
    lead_ids = sorted(
        {
            str(link_row[2] or "").strip()
            for link_row in link_rows
            if str(link_row[2] or "").strip()
        }
    )
    if not link_ids:
        return "scheduled"

    converted = False
    if lead_ids and table_columns(conn, "autopilot_leads"):
        converted_row = conn.execute(
            f"""
            SELECT 1
            FROM autopilot_leads
            WHERE CAST(id AS VARCHAR) IN ({_in_clause(lead_ids)})
              AND converted_at IS NOT NULL
            LIMIT 1
            """,
            lead_ids,
        ).fetchone()
        converted = bool(converted_row)

    any_failed = False
    any_sent = False
    any_scheduled = False
    outreach_columns = table_columns(conn, "outreach_scheduled_emails")
    if outreach_columns:
        send_rows = conn.execute(
            f"""
            SELECT status
            FROM outreach_scheduled_emails
            WHERE CAST(share_link_id AS BIGINT) IN ({_in_clause(link_ids)})
            """,
            link_ids,
        ).fetchall()
        for send_row in send_rows:
            status = str(send_row[0] or "").strip().lower()
            any_failed = any_failed or status == "failed"
            any_sent = any_sent or status == "sent"
            any_scheduled = any_scheduled or status in {"scheduled", "processing"}

    if "emailed_at" in share_link_columns:
        followup_sent_at_sql = "followup_sent_at" if "followup_sent_at" in share_link_columns else "NULL"
        direct_rows = conn.execute(
            f"""
            SELECT emailed_at, {followup_sent_at_sql}
            FROM short_share_links
            WHERE id IN ({_in_clause(link_ids)})
            """,
            link_ids,
        ).fetchall()
        for direct_row in direct_rows:
            any_sent = any_sent or bool(direct_row[0])
            if len(direct_row) > 1:
                any_sent = any_sent or bool(direct_row[1])

    visit_count = 0
    max_watched = 0.0
    if table_columns(conn, "user_events"):
        share_expr = _user_event_metadata_text_sql(conn, "share_link_id")
        token_expr = _user_event_metadata_text_sql(conn, "token")
        lead_expr = _user_event_metadata_text_sql(conn, "autopilot_lead_id")
        percent_expr = _user_event_metadata_numeric_sql(conn, "percent_watched")
        filters: list[str] = []
        params: list[Any] = []
        if link_ids:
            filters.append(f"{share_expr} IN ({_in_clause([str(value) for value in link_ids])})")
            params.extend(str(value) for value in link_ids)
        if tokens:
            filters.append(f"({share_expr} IS NULL AND {token_expr} IN ({_in_clause(tokens)}))")
            params.extend(tokens)
        if lead_ids:
            filters.append(
                f"(ue.event_name = ? AND {lead_expr} IN ({_in_clause(lead_ids)}))"
            )
            params.append("lead_feed_view")
            params.extend(lead_ids)
        if filters:
            event_rows = conn.execute(
                f"""
                SELECT DISTINCT
                  ue.created_at,
                  ue.event_name,
                  CASE WHEN ue.event_name = 'share_watch_progress' THEN {percent_expr} ELSE NULL END AS percent_watched
                FROM user_events ue
                WHERE ue.event_name IN (
                    'share_view',
                    'share_play',
                    'share_cta_click',
                    'share_watch_progress',
                    'lead_feed_view'
                )
                  AND ({' OR '.join(filters)})
                ORDER BY ue.created_at ASC
                """,
                params,
            ).fetchall()
            visit_times: list[datetime] = []
            for event_row in event_rows:
                event_name = str(event_row[1] or "").strip()
                if event_name == "share_watch_progress" and event_row[2] is not None:
                    try:
                        watched = float(event_row[2])
                        if math.isfinite(watched):
                            max_watched = max(max_watched, watched)
                    except (TypeError, ValueError):
                        pass
                if event_name in {"share_view", "lead_feed_view"}:
                    created_at = _as_utc(event_row[0])
                    if created_at:
                        visit_times.append(created_at)
            previous_visit_at: datetime | None = None
            for visit_at in sorted(visit_times):
                if previous_visit_at is None or visit_at > previous_visit_at + timedelta(minutes=30):
                    visit_count += 1
                previous_visit_at = visit_at

    return decide_outreach_followup_bucket(
        converted=converted,
        any_failed=any_failed,
        any_sent=any_sent,
        any_scheduled=any_scheduled,
        visit_count=visit_count,
        max_watched=max_watched,
    )
