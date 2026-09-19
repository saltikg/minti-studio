from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List

from app.video_shorts.services.apify_enrich import apify_trakk_enrich
from app.video_shorts.services.db import get_db, table_columns


DEFAULT_BATCH_SIZE = 5
MAX_BATCH_SIZE = 20
ENRICHMENT_TIMEOUT_SECONDS = 240
DEFAULT_DAILY_TRAKK_CAP = 50
logger = logging.getLogger(__name__)


def _coerce_batch_size(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = DEFAULT_BATCH_SIZE
    return max(1, min(MAX_BATCH_SIZE, parsed))


def _channel_url(channel_id: str, channel_url: str | None = None) -> str:
    url = str(channel_url or "").strip()
    if url:
        return url
    channel_id = str(channel_id or "").strip()
    return f"https://www.youtube.com/channel/{channel_id}" if channel_id else ""


def _utc_day_start() -> datetime:
    return datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)


def _auto_trakk_remaining(conn) -> tuple[bool, int, int, int]:
    control_columns = table_columns(conn, "discovery_automation_control")
    if not control_columns:
        return False, DEFAULT_DAILY_TRAKK_CAP, 0, 0
    max_trakk_per_day_sql = "max_trakk_per_day" if "max_trakk_per_day" in control_columns else str(DEFAULT_DAILY_TRAKK_CAP)
    row = conn.execute(
        f"""
        SELECT enabled, {max_trakk_per_day_sql}
        FROM discovery_automation_control
        WHERE id = 1
        LIMIT 1
        """
    ).fetchone()
    enabled = bool(row and row[0])
    daily_cap = max(0, int(row[1] if row and row[1] is not None else DEFAULT_DAILY_TRAKK_CAP))
    lead_columns = table_columns(conn, "discovery_leads")
    if "enrichment_attempted_at" not in lead_columns:
        return enabled, daily_cap, 0, daily_cap
    discovery_used = 0
    used_row = conn.execute(
        """
        SELECT COUNT(*)
        FROM discovery_leads
        WHERE enrichment_attempted_at >= ?
          AND COALESCE(email_source, '') IN ('', 'trakk')
        """,
        [_utc_day_start()],
    ).fetchone()
    discovery_used = int((used_row[0] if used_row else 0) or 0)
    autopilot_used = 0
    autopilot_columns = table_columns(conn, "autopilot_leads")
    if "email_enrichment_attempted_at" in autopilot_columns:
        autopilot_row = conn.execute(
            """
            SELECT COUNT(*)
            FROM autopilot_leads
            WHERE email_enrichment_attempted_at >= ?
              AND COALESCE(email_source, '') IN ('', 'trakk')
            """,
            [_utc_day_start()],
        ).fetchone()
        autopilot_used = int((autopilot_row[0] if autopilot_row else 0) or 0)
    used = discovery_used + autopilot_used
    return enabled, daily_cap, used, max(0, daily_cap - used)


def _claim_leads(conn, batch_size: int, *, auto: bool = False) -> List[Dict[str, Any]]:
    if auto:
        enabled, daily_cap, used, remaining = _auto_trakk_remaining(conn)
        if not enabled:
            logger.info("trakk_auto_enrich_disabled")
            return []
        if remaining <= 0:
            logger.info("trakk_daily_cap_reached used=%s cap=%s", used, daily_cap)
            return []
        batch_size = min(batch_size, remaining)
    statuses_sql = "('icp_qualified')" if auto else "('icp_qualified', 'email_failed')"
    if getattr(conn, "backend_name", "") == "postgres":
        rows = conn.execute(
            f"""
            WITH candidates AS (
                SELECT id
                FROM discovery_leads
                WHERE status IN {statuses_sql}
                  AND COALESCE(creator_email, '') = ''
                  AND COALESCE(autopilot_lead_id, '') = ''
                  AND NOT EXISTS (
                      SELECT 1
                      FROM autopilot_leads al
                      WHERE al.youtube_channel_id = discovery_leads.youtube_channel_id
                  )
                ORDER BY last_discovered_at DESC NULLS LAST, id DESC
                LIMIT ?
                FOR UPDATE SKIP LOCKED
            )
            UPDATE discovery_leads AS d
            SET status = 'email_enriching',
                enrichment_attempted_at = CURRENT_TIMESTAMP,
                email_enrichment_error = NULL
            FROM candidates
            WHERE d.id = candidates.id
            RETURNING d.id, d.youtube_channel_id, d.channel_url
            """,
            [batch_size],
        ).fetchall()
    else:
        rows = conn.execute(
            f"""
            SELECT id, youtube_channel_id, channel_url
            FROM discovery_leads
            WHERE status IN {statuses_sql}
              AND COALESCE(creator_email, '') = ''
              AND COALESCE(autopilot_lead_id, '') = ''
              AND NOT EXISTS (
                  SELECT 1
                  FROM autopilot_leads al
                  WHERE al.youtube_channel_id = discovery_leads.youtube_channel_id
              )
            ORDER BY last_discovered_at DESC, id DESC
            LIMIT ?
            """,
            [batch_size],
        ).fetchall()
        ids = [row[0] for row in rows]
        if ids:
            placeholders = ", ".join("?" for _ in ids)
            conn.execute(
                f"""
                UPDATE discovery_leads
                SET status = 'email_enriching',
                    enrichment_attempted_at = CURRENT_TIMESTAMP,
                    email_enrichment_error = NULL
                WHERE id IN ({placeholders})
                """,
                ids,
            )

    return [
        {
            "id": row[0],
            "youtube_channel_id": str(row[1] or ""),
            "channel_url": str(row[2] or ""),
        }
        for row in rows
    ]


def _result_key(result: Dict[str, Any]) -> str:
    return str(result.get("channel_id") or result.get("channel_url") or "").strip().rstrip("/")


def _short_error(value: Any) -> str:
    text = str(value or "").strip()
    return text[:500] if text else "no_email_found"


def _mark_failed(conn, lead_id: Any, error: str) -> None:
    conn.execute(
        """
        UPDATE discovery_leads
        SET status = 'email_failed',
            email_enrichment_error = ?
        WHERE id = ?
        """,
        [_short_error(error), lead_id],
    )


def _mark_enriched(conn, lead_id: Any, result: Dict[str, Any]) -> None:
    conn.execute(
        """
        UPDATE discovery_leads
        SET creator_email = CASE
                WHEN COALESCE(creator_email, '') = '' THEN NULLIF(?, '')
                ELSE creator_email
            END,
            email_confidence = ?,
            email_validation = NULLIF(?, ''),
            email_validation_scope = NULLIF(?, ''),
            email_role = NULLIF(?, ''),
            is_generic_email = ?,
            email_source = 'trakk',
            email_source_url = NULLIF(?, ''),
            website = NULLIF(?, ''),
            phone = NULLIF(?, ''),
            lead_tier = NULLIF(?, ''),
            has_hidden_email = ?,
            protected_email_status = NULLIF(?, ''),
            email_enriched_at = CURRENT_TIMESTAMP,
            email_enrichment_error = NULL,
            status = 'email_enriched'
        WHERE id = ?
        """,
        [
            str(result.get("email") or "").strip(),
            result.get("email_confidence"),
            str(result.get("email_validation") or "").strip(),
            str(result.get("email_validation_scope") or "").strip(),
            str(result.get("email_role") or "").strip(),
            result.get("is_generic_email"),
            str(result.get("email_source_url") or "").strip(),
            str(result.get("website") or "").strip(),
            str(result.get("phone") or "").strip(),
            str(result.get("lead_tier") or "").strip(),
            result.get("has_hidden_email"),
            str(result.get("protected_email_status") or "").strip(),
            lead_id,
        ],
    )


def enrich_discovery_email_batch(payload: Dict[str, Any] | None = None) -> Dict[str, Any]:
    payload = payload or {}
    batch_size = _coerce_batch_size(payload.get("batch_size"))
    auto = bool(payload.get("auto"))

    conn = get_db()
    try:
        claimed = _claim_leads(conn, batch_size, auto=auto)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    if not claimed:
        return {"claimed": 0, "enriched": 0, "failed": 0, "cost_estimate": 0.0, "auto": auto}

    urls = [_channel_url(row["youtube_channel_id"], row.get("channel_url")) for row in claimed]
    urls = [url for url in urls if url]
    response = apify_trakk_enrich(urls, timeout_seconds=ENRICHMENT_TIMEOUT_SECONDS, prefer_sync=False)
    results = response.get("results") or []
    errors = response.get("errors") or []
    fallback_error = "; ".join(str(error.get("message") or error.get("error") or "") for error in errors if isinstance(error, dict))

    by_key = {_result_key(result): result for result in results if isinstance(result, dict) and _result_key(result)}
    enriched = 0
    failed = 0

    conn = get_db()
    try:
        for lead in claimed:
            try:
                lead_url = _channel_url(lead["youtube_channel_id"], lead.get("channel_url")).rstrip("/")
                result = by_key.get(lead["youtube_channel_id"]) or by_key.get(lead_url) or {}
                if result.get("email"):
                    _mark_enriched(conn, lead["id"], result)
                    enriched += 1
                else:
                    _mark_failed(conn, lead["id"], result.get("error") or fallback_error or "no_email_found")
                    failed += 1
                conn.commit()
            except Exception as exc:
                conn.rollback()
                try:
                    _mark_failed(conn, lead["id"], str(exc))
                    conn.commit()
                except Exception:
                    conn.rollback()
                failed += 1
    finally:
        conn.close()

    return {
        "claimed": len(claimed),
        "enriched": enriched,
        "failed": failed,
        "cost_estimate": round(len(claimed) * 0.005, 3),
        "auto": auto,
    }
