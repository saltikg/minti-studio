from __future__ import annotations

import html
import json
import logging
import os
import shutil
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

from app.video_shorts.services.db import get_db, get_db_readonly
from app.video_shorts.services.email_verification import send_resend_email
from app.video_shorts.services.instagram_metrics_state import (
    _ensure_local_schema,
    claim_instagram_metrics_alert,
)
from app.video_shorts.services.lead_pipeline import record_lead_pipeline_event


logger = logging.getLogger(__name__)
_PT = ZoneInfo("America/Los_Angeles")
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_GATE_CATEGORIES = ("disk", "memory", "customer_queue")


def _utc(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _json_payload(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = json.loads(str(value or "{}"))
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _origin_sql(conn, alias: str = "j") -> str:
    column = f"{alias}.payload_json"
    if getattr(conn, "backend_name", "") == "postgres":
        return f"COALESCE({column}->>'job_origin', '')"
    return f"COALESCE(json_extract_string({column}, '$.job_origin'), '')"


def _video_pk_sql(conn, alias: str = "j") -> str:
    column = f"{alias}.payload_json"
    if getattr(conn, "backend_name", "") == "postgres":
        return f"COALESCE({column}->>'video_pk', '')"
    return f"COALESCE(json_extract_string({column}, '$.video_pk'), '')"


def _load_alert_state(alert_key: str) -> tuple[Optional[datetime], Dict[str, Any]]:
    conn = get_db_readonly()
    try:
        row = conn.execute(
            "SELECT last_sent_at, summary_json FROM instagram_metrics_alert_state WHERE alert_key = ?",
            [alert_key],
        ).fetchone()
        if not row:
            return None, {}
        return _utc(row[0]), _json_payload(row[1])
    except Exception:
        logger.exception("Discovery health alert state read failed key=%s", alert_key)
        return None, {}
    finally:
        conn.close()


def _store_observation(alert_key: str, summary: Dict[str, Any]) -> None:
    conn = get_db()
    payload = json.dumps(summary, default=str, sort_keys=True)
    try:
        _ensure_local_schema(conn)
        if getattr(conn, "backend_name", "") == "postgres":
            conn.execute(
                """
                INSERT INTO instagram_metrics_alert_state (alert_key, last_sent_at, summary_json)
                VALUES (?, ?, CAST(? AS JSONB))
                ON CONFLICT(alert_key) DO UPDATE
                SET summary_json = excluded.summary_json
                """,
                [alert_key, _EPOCH, payload],
            )
        else:
            conn.execute(
                """
                INSERT INTO instagram_metrics_alert_state (alert_key, last_sent_at, summary_json)
                VALUES (?, ?, ?)
                ON CONFLICT(alert_key) DO UPDATE
                SET summary_json = excluded.summary_json
                """,
                [alert_key, _EPOCH, payload],
            )
        conn.commit()
    except Exception:
        conn.rollback()
        logger.exception("Discovery health alert state write failed key=%s", alert_key)
    finally:
        conn.close()


def _send_admin_alert(reason: str, summary: Dict[str, Any]) -> None:
    details = json.dumps(summary, indent=2, default=str, sort_keys=True)
    subject = f"[Minti Studio] Discovery stalled: {reason}"
    text = f"Discovery health alert\n\nReason: {reason}\n\n{details}\n"
    html_body = (
        "<div style='font-family:Arial,sans-serif;line-height:1.5'>"
        "<h2>Discovery health alert</h2>"
        f"<p><strong>Reason:</strong> {html.escape(reason)}</p>"
        f"<pre>{html.escape(details)}</pre></div>"
    )
    send_resend_email(
        to_email=(os.getenv("ADMIN_NOTIFICATION_EMAIL") or "info@mintistudio.com").strip(),
        subject=subject,
        html=html_body,
        text=text,
        error_message="Discovery health alert could not be sent.",
    )


def _emit_condition(
    *,
    alert_key: str,
    reason: str,
    active: bool,
    summary: Dict[str, Any],
    dry_run: bool,
    now: datetime,
    send_alerts: bool,
) -> Dict[str, Any]:
    last_sent_at, previous = _load_alert_state(alert_key)
    eligible = last_sent_at is None or last_sent_at <= now - timedelta(hours=24)
    result = {
        "alert_key": alert_key,
        "reason": reason,
        "active": bool(active),
        "eligible": bool(eligible),
        "would_alert": bool(active and eligible),
        "last_sent_at": last_sent_at.isoformat() if last_sent_at else None,
        "summary": summary,
    }
    if dry_run:
        return result
    observation = {**previous, **summary, "active": bool(active), "observed_at": now.isoformat()}
    _store_observation(alert_key, observation)
    if not active or not send_alerts:
        return result
    if claim_instagram_metrics_alert(observation, cooldown_hours=24, alert_key=alert_key):
        try:
            _send_admin_alert(reason, observation)
            result["sent"] = True
        except Exception:
            logger.exception("Discovery health email failed key=%s", alert_key)
            result["sent"] = False
    return result


def _queue_snapshot(now: datetime) -> Dict[str, Any]:
    conn = get_db_readonly()
    try:
        origin = _origin_sql(conn)
        cutoff = now - timedelta(hours=24)
        old_row = conn.execute(
            f"""
            SELECT COUNT(*), MIN(j.created_at)
            FROM shorts_render_jobs j
            WHERE {origin} = 'discovery_demo'
              AND j.status = 'queued'
              AND j.created_at <= ?
            """,
            [cutoff],
        ).fetchone()
        processing_row = conn.execute(
            f"""
            SELECT COUNT(*)
            FROM shorts_render_jobs j
            WHERE {origin} = 'discovery_demo' AND j.status = 'processing'
            """
        ).fetchone()
        activity_row = conn.execute(
            f"""
            SELECT MAX(COALESCE(j.finished_at, j.started_at, j.updated_at, j.created_at))
            FROM shorts_render_jobs j
            WHERE {origin} = 'discovery_demo'
            """
        ).fetchone()
        return {
            "queued_over_24h": int((old_row[0] if old_row else 0) or 0),
            "oldest_queued_at": str(old_row[1]) if old_row and old_row[1] else None,
            "processing": int((processing_row[0] if processing_row else 0) or 0),
            "last_activity_at": str(activity_row[0]) if activity_row and activity_row[0] else None,
        }
    finally:
        conn.close()


def _generating_queue_stalls(now: datetime) -> list[Dict[str, Any]]:
    conn = get_db_readonly()
    try:
        video_pk = _video_pk_sql(conn)
        cutoff = now - timedelta(hours=48)
        rows = conn.execute(
            f"""
            SELECT DISTINCT
                CAST(l.id AS VARCHAR),
                COALESCE(c.channel_name, l.creator_name, l.youtube_channel_id, 'unknown'),
                CAST(j.id AS VARCHAR),
                j.created_at
            FROM autopilot_leads l
            LEFT JOIN youtube_channels c ON c.channel_id = l.channel_id
            JOIN shorts_render_jobs j
              ON CAST({video_pk} AS VARCHAR) = CAST(l.first_video_id AS VARCHAR)
            WHERE COALESCE(l.pipeline_state, 'new') = 'generating'
              AND j.type = 'render_short'
              AND j.status = 'queued'
              AND j.started_at IS NULL
              AND j.created_at <= ?
            ORDER BY j.created_at ASC
            LIMIT 50
            """,
            [cutoff],
        ).fetchall()
        return [
            {
                "lead_id": str(row[0]),
                "channel": str(row[1]),
                "job_id": str(row[2]),
                "queued_at": str(row[3]),
            }
            for row in rows
        ]
    finally:
        conn.close()


def _gate_category(reason: Optional[str]) -> Optional[str]:
    normalized = str(reason or "").strip().lower()
    if normalized.startswith("root disk usage"):
        return "disk"
    if normalized.startswith("memavailable"):
        return "memory"
    if normalized == "customer job queued/processing":
        return "customer_queue"
    return None


def evaluate_discovery_health(
    *,
    gate_reason: Optional[str],
    in_night_window: bool,
    dry_run: bool = False,
    send_alerts: bool = True,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    now = _utc(now) or datetime.now(timezone.utc)
    usage = shutil.disk_usage("/")
    disk_pct = int(round((usage.used / usage.total) * 100)) if usage.total else 0
    queue = _queue_snapshot(now)
    generating = _generating_queue_stalls(now)
    conditions: list[Dict[str, Any]] = []

    conditions.append(
        _emit_condition(
            alert_key="discovery_health:disk_70",
            reason="root_disk_at_or_above_70_percent",
            active=disk_pct >= 70,
            summary={"disk_usage_pct": disk_pct, "threshold_pct": 70},
            dry_run=dry_run,
            now=now,
            send_alerts=send_alerts,
        )
    )
    last_activity = _utc(queue.get("last_activity_at"))
    recent_progress = bool(last_activity and last_activity > now - timedelta(hours=2))
    queue_stalled = (
        queue["queued_over_24h"] > 0
        and queue["processing"] == 0
        and not recent_progress
    )
    queue_summary = {**queue, "generating_queued_over_48h": generating}
    queue_summary["recent_progress_within_2h"] = recent_progress
    conditions.append(
        _emit_condition(
            alert_key="discovery_health:queue_stall",
            reason="old_discovery_queue_with_zero_processing",
            active=queue_stalled,
            summary=queue_summary,
            dry_run=dry_run,
            now=now,
            send_alerts=send_alerts,
        )
    )

    current_category = _gate_category(gate_reason) if in_night_window else None
    for category in _GATE_CATEGORIES:
        alert_key = f"discovery_health:gate:{category}"
        _last_sent, previous = _load_alert_state(alert_key)
        active = category == current_category
        blocked_since = _utc(previous.get("blocked_since")) if active and previous.get("active") else None
        blocked_since = blocked_since or (now if active else None)
        blocked_hours = max(0.0, (now - blocked_since).total_seconds() / 3600.0) if blocked_since else 0.0
        summary = {
            "gate": category,
            "gate_reason": str(gate_reason or ""),
            "night_window": bool(in_night_window),
            "blocked_since": blocked_since.isoformat() if blocked_since else None,
            "blocked_hours": round(blocked_hours, 2),
        }
        conditions.append(
            _emit_condition(
                alert_key=alert_key,
                reason=f"continuous_{category}_gate_over_2h",
                active=bool(active and blocked_hours >= 2.0),
                summary={**summary, "active": active},
                dry_run=dry_run,
                now=now,
                send_alerts=send_alerts,
            )
        )
        if not dry_run and active and blocked_hours < 2.0:
            _store_observation(alert_key, {**summary, "active": True, "observed_at": now.isoformat()})

    return {
        "dry_run": bool(dry_run),
        "evaluated_at": now.isoformat(),
        "evaluated_at_pt": now.astimezone(_PT).isoformat(),
        "disk_usage_pct": disk_pct,
        "queue": queue,
        "generating_queued_over_48h": generating,
        "conditions": conditions,
    }


def reconcile_failed_discovery_downloads(*, dry_run: bool = False) -> Dict[str, Any]:
    conn = get_db()
    try:
        video_pk = _video_pk_sql(conn)
        rows = conn.execute(
            f"""
            WITH ranked AS (
                SELECT
                    CAST(l.id AS VARCHAR) AS lead_id,
                    COALESCE(c.channel_name, l.creator_name, l.youtube_channel_id, 'unknown') AS channel_name,
                    CAST(j.id AS VARCHAR) AS job_id,
                    j.status,
                    COALESCE(j.attempts, 0) AS attempts,
                    COALESCE(j.max_attempts, 0) AS max_attempts,
                    COALESCE(j.error, '') AS error,
                    ROW_NUMBER() OVER (PARTITION BY l.id ORDER BY j.created_at DESC, j.id DESC) AS rn
                FROM autopilot_leads l
                LEFT JOIN youtube_channels c ON c.channel_id = l.channel_id
                JOIN shorts_render_jobs j
                  ON CAST({video_pk} AS VARCHAR) = CAST(l.first_video_id AS VARCHAR)
                WHERE COALESCE(l.pipeline_state, 'new') = 'downloading'
                  AND j.type = 'ingest_youtube'
            )
            SELECT lead_id, channel_name, job_id, attempts, max_attempts, error
            FROM ranked
            WHERE rn = 1
              AND status = 'failed'
              AND attempts >= max_attempts
            ORDER BY channel_name, lead_id
            """
        ).fetchall()
        candidates = [
            {
                "lead_id": str(row[0]),
                "channel": str(row[1]),
                "job_id": str(row[2]),
                "attempts": int(row[3] or 0),
                "max_attempts": int(row[4] or 0),
                "error": str(row[5] or ""),
            }
            for row in rows
        ]
        changed: list[Dict[str, Any]] = []
        if not dry_run:
            for item in candidates:
                result = record_lead_pipeline_event(
                    conn,
                    lead_id=item["lead_id"],
                    event_type="download_job_attempts_exhausted",
                    to_state="failed",
                    detail={
                        "phase": "download",
                        "job_id": item["job_id"],
                        "attempts": item["attempts"],
                        "max_attempts": item["max_attempts"],
                        "error": item["error"],
                        "mechanism": "discovery_hourly_reconciliation",
                    },
                )
                if result and result.get("to_state") == "failed":
                    changed.append(item)
            conn.commit()
        else:
            conn.rollback()
        return {"dry_run": bool(dry_run), "candidates": candidates, "changed": changed}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
