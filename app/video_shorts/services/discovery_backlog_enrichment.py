from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4
from zoneinfo import ZoneInfo

from app.video_shorts.services.apify_enrich import ApifyOutOfCreditError, apify_trakk_enrich
from app.video_shorts.services.db import get_db, table_columns
from app.video_shorts.services.discovery_blocklist import discovery_blocklist_sql
from app.video_shorts.services.discovery_email_enrichment import (
    ENRICHMENT_TIMEOUT_SECONDS,
    _channel_url,
    _mark_enriched,
    _mark_failed,
    _result_key,
)
from app.video_shorts.services.discovery_promote_queue import store_outreach_first_name_if_needed


logger = logging.getLogger(__name__)
PACIFIC_TZ = ZoneInfo("America/Los_Angeles")
RUNS_TABLE = "discovery_backlog_enrichment_runs"
DEFAULT_BATCH_SIZE = 5
DEFAULT_DAILY_CAP = 40
START_HOUR_PT = 8
END_HOUR_PT = 20

MEDICAL_CATEGORY_TERMS = (
    "menopause",
    "psychiatry",
    "mental health",
    "cardiology",
    "heart health",
    "endocrinology",
    "diabetes",
    "thyroid",
    "geriatrics",
    "longevity",
    "obesity",
    "glp-1",
    "urology",
    "men's health",
    "gastroenterology",
    "gut health",
    "ob/gyn",
    "pregnancy",
)
SECONDARY_CATEGORY_TERMS = ("coach", "coaching", "education", "relationships", "family", "parenting")


def configured_daily_cap() -> int:
    try:
        return max(0, int(os.getenv("ENRICH_BACKLOG_DAILY_CAP", str(DEFAULT_DAILY_CAP)) or 0))
    except (TypeError, ValueError):
        return DEFAULT_DAILY_CAP


def ensure_backlog_enrichment_schema(conn) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {RUNS_TABLE} (
            id VARCHAR PRIMARY KEY,
            slot_key VARCHAR NOT NULL UNIQUE,
            date_pt DATE NOT NULL,
            scheduled_for TIMESTAMP,
            started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            finished_at TIMESTAMP,
            status VARCHAR NOT NULL DEFAULT 'running',
            claimed INTEGER NOT NULL DEFAULT 0,
            enriched INTEGER NOT NULL DEFAULT 0,
            failed INTEGER NOT NULL DEFAULT 0,
            categories TEXT,
            error TEXT
        )
        """
    )


def _category_sql(alias: str = "d") -> str:
    return f"""
        COALESCE((
            SELECT dc.name
            FROM keyword_queue kq
            JOIN discovery_categories dc ON dc.id = kq.category_id
            WHERE lower(kq.keyword) = lower(COALESCE({alias}.matched_keyword, ''))
            ORDER BY kq.updated_at DESC NULLS LAST, kq.id DESC
            LIMIT 1
        ), 'Uncategorized')
    """


def _priority_case(category_expression: str) -> str:
    medical = " OR ".join(
        f"lower({category_expression}) LIKE '%{term.replace(chr(39), chr(39) * 2)}%'"
        for term in MEDICAL_CATEGORY_TERMS
    )
    secondary = " OR ".join(
        f"lower({category_expression}) LIKE '%{term.replace(chr(39), chr(39) * 2)}%'"
        for term in SECONDARY_CATEGORY_TERMS
    )
    return f"CASE WHEN ({medical}) THEN 0 WHEN ({secondary}) THEN 1 ELSE 2 END"


def _used_today(conn, date_pt) -> int:
    ensure_backlog_enrichment_schema(conn)
    row = conn.execute(
        f"SELECT COALESCE(SUM(claimed), 0) FROM {RUNS_TABLE} WHERE date_pt = ?",
        [date_pt],
    ).fetchone()
    return int((row or [0])[0] or 0)


def _claim_candidates(conn, *, run_id: str, limit: int) -> List[Dict[str, Any]]:
    if limit <= 0:
        return []
    category_expression = _category_sql("d")
    blocked = discovery_blocklist_sql(["d.matched_keyword", category_expression])
    priority = _priority_case(category_expression)
    job_assignment = ", email_enrichment_job_id = ?" if "email_enrichment_job_id" in table_columns(conn, "discovery_leads") else ""
    job_params = [run_id] if job_assignment else []
    rows = conn.execute(
        f"""
        WITH candidates AS (
            SELECT d.id, {category_expression} AS category
            FROM discovery_leads d
            WHERE d.status = 'icp_qualified'
              AND COALESCE(d.creator_email, '') = ''
              AND d.enrichment_attempted_at IS NULL
              AND COALESCE(CAST(d.autopilot_lead_id AS VARCHAR), '') = ''
              AND NOT ({blocked})
              AND NOT EXISTS (
                  SELECT 1 FROM autopilot_leads al
                  WHERE al.youtube_channel_id = d.youtube_channel_id
              )
            ORDER BY {priority}, d.last_discovered_at DESC NULLS LAST, d.id DESC
            LIMIT ?
            FOR UPDATE SKIP LOCKED
        )
        UPDATE discovery_leads AS d
        SET status = 'email_enriching',
            enrichment_attempted_at = CURRENT_TIMESTAMP,
            email_enrichment_error = NULL
            {", email_enrichment_lane = 'backlog'" if "email_enrichment_lane" in table_columns(conn, "discovery_leads") else ""}
            {job_assignment}
        FROM candidates
        WHERE d.id = candidates.id
        RETURNING d.id, d.youtube_channel_id, d.channel_url, candidates.category
        """,
        [limit, *job_params],
    ).fetchall()
    return [
        {
            "id": row[0],
            "youtube_channel_id": str(row[1] or ""),
            "channel_url": str(row[2] or ""),
            "category": str(row[3] or "Uncategorized"),
        }
        for row in rows
    ]


def _finish_run(conn, run_id: str, result: Dict[str, Any], *, status: str = "done", error: str = "") -> None:
    conn.execute(
        f"""
        UPDATE {RUNS_TABLE}
        SET finished_at = CURRENT_TIMESTAMP,
            status = ?, claimed = ?, enriched = ?, failed = ?, categories = ?, error = ?
        WHERE id = ?
        """,
        [
            status,
            int(result.get("claimed") or 0),
            int(result.get("enriched") or 0),
            int(result.get("failed") or 0),
            ", ".join(result.get("categories") or []),
            str(error or "")[:1000],
            run_id,
        ],
    )


def run_backlog_enrichment_batch(*, now: Optional[datetime] = None, force: bool = False) -> Dict[str, Any]:
    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    now_pt = now_utc.astimezone(PACIFIC_TZ)
    if not force and not (START_HOUR_PT <= now_pt.hour <= END_HOUR_PT):
        return {"skipped": True, "reason": "outside_window", "claimed": 0, "enriched": 0, "failed": 0}
    date_pt = now_pt.date()
    slot_key = f"{date_pt.isoformat()}:{now_pt.hour:02d}" if not force else f"manual:{uuid4()}"
    run_id = str(uuid4())
    conn = get_db()
    claimed: List[Dict[str, Any]] = []
    try:
        ensure_backlog_enrichment_schema(conn)
        inserted = conn.execute(
            f"""
            INSERT INTO {RUNS_TABLE} (id, slot_key, date_pt, scheduled_for, status)
            VALUES (?, ?, ?, ?, 'running')
            ON CONFLICT (slot_key) DO NOTHING
            RETURNING id
            """,
            [run_id, slot_key, date_pt, now_utc.replace(tzinfo=None)],
        ).fetchone()
        if not inserted:
            conn.rollback()
            return {"skipped": True, "reason": "slot_already_processed", "claimed": 0, "enriched": 0, "failed": 0}
        cap = configured_daily_cap()
        used = _used_today(conn, date_pt)
        remaining = max(0, cap - used)
        if remaining <= 0:
            result = {"skipped": True, "reason": "daily_cap", "claimed": 0, "enriched": 0, "failed": 0, "daily_cap": cap, "used": used}
            _finish_run(conn, run_id, result, status="skipped")
            conn.commit()
            logger.info("discovery_backlog_enrichment_daily_cap used=%s cap=%s", used, cap)
            return result
        claimed = _claim_candidates(conn, run_id=run_id, limit=min(DEFAULT_BATCH_SIZE, remaining))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    result: Dict[str, Any] = {
        "claimed": len(claimed),
        "enriched": 0,
        "failed": 0,
        "categories": [row["category"] for row in claimed],
        "daily_cap": configured_daily_cap(),
    }
    if not claimed:
        conn = get_db()
        try:
            _finish_run(conn, run_id, result, status="skipped")
            conn.commit()
        finally:
            conn.close()
        result.update({"skipped": True, "reason": "no_candidates"})
        return result

    urls = [_channel_url(row["youtube_channel_id"], row.get("channel_url")) for row in claimed]
    try:
        response = apify_trakk_enrich(urls, timeout_seconds=ENRICHMENT_TIMEOUT_SECONDS, prefer_sync=False)
    except ApifyOutOfCreditError:
        conn = get_db()
        try:
            for lead in claimed:
                conn.execute(
                    """UPDATE discovery_leads
                       SET status = 'email_out_of_credit', email_enrichment_error = 'apify_out_of_credit',
                           email_enrichment_job_id = NULL
                       WHERE id = ?""",
                    [lead["id"]],
                )
            result["failed"] = len(claimed)
            result["out_of_credit"] = True
            _finish_run(conn, run_id, result, status="failed", error="apify_out_of_credit")
            conn.commit()
        finally:
            conn.close()
        return result
    except Exception as exc:
        conn = get_db()
        try:
            conn.execute(
                """UPDATE discovery_leads
                   SET status = 'icp_qualified', enrichment_attempted_at = NULL,
                       email_enrichment_job_id = NULL, email_enrichment_error = ?
                   WHERE status = 'email_enriching' AND email_enrichment_job_id = ?""",
                [str(exc)[:500], run_id],
            )
            _finish_run(conn, run_id, result, status="failed", error=str(exc))
            conn.commit()
        finally:
            conn.close()
        raise

    results = response.get("results") or []
    errors = response.get("errors") or []
    fallback_error = "; ".join(
        str(item.get("message") or item.get("error") or "") for item in errors if isinstance(item, dict)
    )
    by_key = {_result_key(item): item for item in results if isinstance(item, dict) and _result_key(item)}
    greetings: list[tuple[Any, Dict[str, Any]]] = []
    conn = get_db()
    try:
        for lead in claimed:
            lead_url = _channel_url(lead["youtube_channel_id"], lead.get("channel_url")).rstrip("/")
            item = by_key.get(lead["youtube_channel_id"]) or by_key.get(lead_url) or {}
            if item.get("email"):
                _mark_enriched(conn, lead["id"], item)
                greetings.append((lead["id"], item))
                result["enriched"] += 1
            else:
                _mark_failed(conn, lead["id"], item.get("error") or fallback_error or "no_email_found")
                result["failed"] += 1
        _finish_run(conn, run_id, result)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    for lead_id, item in greetings:
        try:
            store_outreach_first_name_if_needed(lead_id, item)
        except Exception:
            logger.exception("Could not store outreach first name for backlog lead id=%s", lead_id)
    logger.info(
        "discovery_backlog_enrichment_done claimed=%s enriched=%s failed=%s categories=%s",
        result["claimed"], result["enriched"], result["failed"], result["categories"],
    )
    return result


def process_due_backlog_enrichment(*, now: Optional[datetime] = None) -> bool:
    result = run_backlog_enrichment_batch(now=now, force=False)
    return bool(result and not result.get("skipped"))


def next_backlog_run(now: Optional[datetime] = None) -> datetime:
    current = (now or datetime.now(timezone.utc)).astimezone(PACIFIC_TZ)
    candidate = current.replace(minute=0, second=0, microsecond=0)
    if current.minute or current.second or current.microsecond:
        candidate += timedelta(hours=1)
    if candidate.hour < START_HOUR_PT:
        candidate = candidate.replace(hour=START_HOUR_PT)
    elif candidate.hour > END_HOUR_PT:
        candidate = (candidate + timedelta(days=1)).replace(hour=START_HOUR_PT)
    return candidate


def backlog_runtime_status(now: Optional[datetime] = None) -> Dict[str, Any]:
    conn = get_db()
    try:
        ensure_backlog_enrichment_schema(conn)
        date_pt = (now or datetime.now(timezone.utc)).astimezone(PACIFIC_TZ).date()
        used = _used_today(conn, date_pt)
        conn.commit()
    finally:
        conn.close()
    return {
        "batch_size": DEFAULT_BATCH_SIZE,
        "daily_cap": configured_daily_cap(),
        "used_today": used,
        "next_run_pt": next_backlog_run(now).isoformat(),
        "window_pt": f"{START_HOUR_PT:02d}:00-{END_HOUR_PT:02d}:00",
    }
