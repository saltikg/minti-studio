from __future__ import annotations

import logging
from typing import Any, Dict, List

from app.video_shorts.services.apify_enrich import apify_trakk_enrich
from app.video_shorts.services.autopilot_leads import provision_discovery_lead_email
from app.video_shorts.services.db import get_db, table_columns
from app.video_shorts.services.discovery_email_enrichment import (
    DEFAULT_BATCH_SIZE,
    ENRICHMENT_TIMEOUT_SECONDS,
    _auto_trakk_remaining,
    _coerce_batch_size,
    _short_error,
)


logger = logging.getLogger(__name__)


def _channel_url(channel_id: str) -> str:
    channel_id = str(channel_id or "").strip()
    return f"https://www.youtube.com/channel/{channel_id}" if channel_id else ""


def _result_key(result: Dict[str, Any]) -> str:
    return str(result.get("channel_id") or result.get("channel_url") or "").strip().rstrip("/")


def _selected_lead_ids(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    lead_ids: List[str] = []
    for item in value:
        lead_id = str(item or "").strip()
        if lead_id and lead_id not in lead_ids:
            lead_ids.append(lead_id)
    return lead_ids


def _claim_autopilot_discovery_leads(conn, lead_ids: List[str], batch_size: int) -> List[Dict[str, Any]]:
    if not lead_ids:
        return []
    enabled, daily_cap, used, remaining = _auto_trakk_remaining(conn)
    if not enabled:
        logger.info("autopilot_trakk_enrich_disabled")
        return []
    if remaining <= 0:
        logger.info("autopilot_trakk_daily_cap_reached used=%s cap=%s", used, daily_cap)
        return []
    batch_size = min(batch_size, remaining, len(lead_ids))
    columns = table_columns(conn, "autopilot_leads")
    if "email_enrichment_attempted_at" not in columns:
        raise RuntimeError("autopilot_leads email enrichment columns are missing")
    placeholders = ", ".join("?" for _ in lead_ids)
    if getattr(conn, "backend_name", "") == "postgres":
        rows = conn.execute(
            f"""
            WITH candidates AS (
                SELECT id
                FROM autopilot_leads
                WHERE CAST(id AS VARCHAR) IN ({placeholders})
                  AND COALESCE(creator_email, '') = ''
                  AND COALESCE(youtube_channel_id, '') <> ''
                  AND (COALESCE(user_id, '') = '' OR COALESCE(brand_id, '') = '')
                ORDER BY created_at DESC NULLS LAST, id DESC
                LIMIT ?
                FOR UPDATE SKIP LOCKED
            )
            UPDATE autopilot_leads AS l
            SET email_enrichment_attempted_at = CURRENT_TIMESTAMP,
                email_enrichment_error = NULL
            FROM candidates
            WHERE l.id = candidates.id
            RETURNING CAST(l.id AS VARCHAR), l.youtube_channel_id
            """,
            [*lead_ids, batch_size],
        ).fetchall()
    else:
        rows = conn.execute(
            f"""
            SELECT CAST(id AS VARCHAR), youtube_channel_id
            FROM autopilot_leads
            WHERE CAST(id AS VARCHAR) IN ({placeholders})
              AND COALESCE(creator_email, '') = ''
              AND COALESCE(youtube_channel_id, '') <> ''
              AND (COALESCE(user_id, '') = '' OR COALESCE(brand_id, '') = '')
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            [*lead_ids, batch_size],
        ).fetchall()
        claimed_ids = [str(row[0]) for row in rows]
        if claimed_ids:
            claimed_placeholders = ", ".join("?" for _ in claimed_ids)
            conn.execute(
                f"""
                UPDATE autopilot_leads
                SET email_enrichment_attempted_at = CURRENT_TIMESTAMP,
                    email_enrichment_error = NULL
                WHERE CAST(id AS VARCHAR) IN ({claimed_placeholders})
                """,
                claimed_ids,
            )
    return [{"id": str(row[0]), "youtube_channel_id": str(row[1] or "")} for row in rows]


def _update_contact_metadata(conn, lead_id: str, result: Dict[str, Any], *, error: str | None = None) -> None:
    columns = table_columns(conn, "autopilot_leads")
    assignments: List[str] = []
    params: List[Any] = []
    values = {
        "creator_website": str(result.get("website") or "").strip() or None,
        "email_confidence": result.get("email_confidence"),
        "email_source": "trakk" if result.get("email") else None,
        "email_enrichment_error": _short_error(error) if error else None,
    }
    for column, value in values.items():
        if column in columns:
            assignments.append(f"{column} = ?")
            params.append(value)
    if not assignments:
        return
    conn.execute(
        f"""
        UPDATE autopilot_leads
        SET {", ".join(assignments)}
        WHERE CAST(id AS VARCHAR) = CAST(? AS VARCHAR)
        """,
        [*params, lead_id],
    )


def _mark_no_email(conn, lead_id: str, error: str) -> None:
    columns = table_columns(conn, "autopilot_leads")
    if "email_enrichment_error" not in columns:
        return
    conn.execute(
        """
        UPDATE autopilot_leads
        SET email_enrichment_error = ?
        WHERE CAST(id AS VARCHAR) = CAST(? AS VARCHAR)
        """,
        [_short_error(error), lead_id],
    )


def enrich_autopilot_discovery_email_batch(payload: Dict[str, Any] | None = None) -> Dict[str, Any]:
    payload = payload or {}
    lead_ids = _selected_lead_ids(payload.get("lead_ids"))
    batch_size = _coerce_batch_size(payload.get("batch_size") or len(lead_ids) or DEFAULT_BATCH_SIZE)

    conn = get_db()
    try:
        claimed = _claim_autopilot_discovery_leads(conn, lead_ids, batch_size)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    if not claimed:
        return {"claimed": 0, "enriched": 0, "failed": 0, "converted": 0, "cost_estimate": 0.0}

    urls = [_channel_url(row["youtube_channel_id"]) for row in claimed]
    urls = [url for url in urls if url]
    response = apify_trakk_enrich(urls, timeout_seconds=ENRICHMENT_TIMEOUT_SECONDS, prefer_sync=False)
    results = response.get("results") or []
    errors = response.get("errors") or []
    fallback_error = "; ".join(
        str(error.get("message") or error.get("error") or "") for error in errors if isinstance(error, dict)
    )
    by_key = {_result_key(result): result for result in results if isinstance(result, dict) and _result_key(result)}

    enriched = 0
    converted = 0
    failed = 0
    conn = get_db()
    try:
        for lead in claimed:
            lead_id = lead["id"]
            channel_id = lead["youtube_channel_id"]
            lead_url = _channel_url(channel_id).rstrip("/")
            result = by_key.get(channel_id) or by_key.get(lead_url) or {}
            try:
                email = str(result.get("email") or "").strip()
                if email:
                    _update_contact_metadata(conn, lead_id, result)
                    provision_discovery_lead_email(conn, lead_id=lead_id, creator_email=email)
                    enriched += 1
                    converted += 1
                else:
                    _mark_no_email(conn, lead_id, result.get("error") or fallback_error or "no_email_found")
                    failed += 1
                conn.commit()
            except Exception as exc:
                conn.rollback()
                try:
                    _mark_no_email(conn, lead_id, str(exc))
                    conn.commit()
                except Exception:
                    conn.rollback()
                failed += 1
    finally:
        conn.close()

    return {
        "claimed": len(claimed),
        "enriched": enriched,
        "converted": converted,
        "failed": failed,
        "cost_estimate": round(len(claimed) * 0.005, 3),
    }
