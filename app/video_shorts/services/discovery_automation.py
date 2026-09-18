from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4
from zoneinfo import ZoneInfo

from flask import current_app

from app.video_shorts.services.db import get_db, table_columns
from app.video_shorts.services.render_jobs import JOB_TYPE_ENRICH_DISCOVERY_EMAILS, enqueue_worker_job


PACIFIC_TZ = ZoneInfo("America/Los_Angeles")
CONTROL_TABLE = "discovery_automation_control"
RUNS_TABLE = "discovery_automation_runs"
DEFAULT_LOCK_MINUTES = 45


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _parse_int(value: Any, default: int, *, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _parse_offpeak_hours(value: Any) -> List[int]:
    try:
        raw = json.loads(value) if isinstance(value, str) else value
    except Exception:
        raw = []
    hours: List[int] = []
    if isinstance(raw, list):
        for item in raw:
            try:
                hour = int(item)
            except (TypeError, ValueError):
                continue
            if 0 <= hour <= 23 and hour not in hours:
                hours.append(hour)
    return sorted(hours) or [1, 2, 3, 4]


def _json_hours(hours: List[int]) -> str:
    return json.dumps(_parse_offpeak_hours(hours))


def _as_utc(value: Any) -> Optional[datetime]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _next_offpeak_slot_utc(now_utc: datetime, hours: List[int], runs_per_day: int) -> datetime:
    now_pt = now_utc.astimezone(PACIFIC_TZ)
    selected_hours = sorted(_parse_offpeak_hours(hours))[: max(1, min(24, int(runs_per_day or 1)))]
    for day_offset in range(0, 3):
        base_day = (now_pt + timedelta(days=day_offset)).date()
        for hour in selected_hours:
            slot_pt = datetime(
                base_day.year,
                base_day.month,
                base_day.day,
                hour,
                0,
                0,
                tzinfo=PACIFIC_TZ,
            )
            if slot_pt > now_pt:
                return slot_pt.astimezone(timezone.utc).replace(tzinfo=None, microsecond=0)
    fallback_pt = (now_pt + timedelta(days=1)).replace(
        hour=selected_hours[0],
        minute=0,
        second=0,
        microsecond=0,
    )
    return fallback_pt.astimezone(timezone.utc).replace(tzinfo=None, microsecond=0)


def _row_to_control(row: Any) -> Dict[str, Any]:
    if not row:
        return {}
    hours = _parse_offpeak_hours(row[3])
    return {
        "id": int(row[0] or 1),
        "enabled": bool(row[1]),
        "paused_reason": str(row[2] or ""),
        "runs_per_day": _parse_int(row[4], 1, minimum=1, maximum=24),
        "offpeak_hours_pt": hours,
        "offpeak_hours_pt_json": _json_hours(hours),
        "max_keywords_per_cycle": _parse_int(row[5], 1, minimum=1, maximum=10),
        "max_results_per_keyword": _parse_int(row[6], 15, minimum=1, maximum=40),
        "max_channels_enriched_per_cycle": _parse_int(row[7], 15, minimum=1, maximum=150),
        "max_trakk_per_cycle": _parse_int(row[8], 5, minimum=0, maximum=20),
        "next_run_at": row[9],
        "last_started_at": row[10],
        "last_finished_at": row[11],
        "lock_expires_at": row[12],
        "updated_at": row[13],
    }


def _select_control(conn) -> Dict[str, Any]:
    if not table_columns(conn, CONTROL_TABLE):
        return {}
    row = conn.execute(
        """
        SELECT id, enabled, paused_reason, offpeak_hours_pt_json, runs_per_day,
               max_keywords_per_cycle, max_results_per_keyword,
               max_channels_enriched_per_cycle, max_trakk_per_cycle,
               next_run_at, last_started_at, last_finished_at, lock_expires_at, updated_at
        FROM discovery_automation_control
        WHERE id = 1
        LIMIT 1
        """
    ).fetchone()
    return _row_to_control(row)


def load_discovery_automation_dashboard() -> Dict[str, Any]:
    conn = get_db()
    try:
        control = _select_control(conn)
        runs: List[Dict[str, Any]] = []
        has_runs = bool(table_columns(conn, RUNS_TABLE))
        if has_runs:
            rows = conn.execute(
                """
                SELECT id, started_at, finished_at, status, keywords_used, search_calls,
                       enrichment_read_calls, leads_discovered, leads_icp_qualified,
                       enrich_job_id, trakk_estimated_cost, error
                FROM discovery_automation_runs
                ORDER BY started_at DESC, id DESC
                LIMIT 20
                """
            ).fetchall()
            runs = [
                {
                    "id": row[0],
                    "started_at": row[1],
                    "finished_at": row[2],
                    "status": str(row[3] or ""),
                    "keywords_used": str(row[4] or ""),
                    "search_calls": int(row[5] or 0),
                    "enrichment_read_calls": int(row[6] or 0),
                    "leads_discovered": int(row[7] or 0),
                    "leads_icp_qualified": int(row[8] or 0),
                    "enrich_job_id": str(row[9] or ""),
                    "trakk_estimated_cost": float(row[10] or 0),
                    "error": str(row[11] or ""),
                }
                for row in rows
            ]
        return {
            "has_automation": bool(control) and has_runs,
            "control": control,
            "runs": runs,
        }
    finally:
        conn.close()


def update_discovery_automation_control(updates: Dict[str, Any]) -> Dict[str, Any]:
    conn = get_db()
    try:
        if not table_columns(conn, CONTROL_TABLE):
            raise RuntimeError("discovery_automation_control table is not available.")
        current = _select_control(conn)
        enabled = bool(updates.get("enabled")) if "enabled" in updates else bool(current.get("enabled"))
        paused_reason = str(updates.get("paused_reason", current.get("paused_reason") or "") or "").strip()[:500]
        runs_per_day = _parse_int(updates.get("runs_per_day", current.get("runs_per_day")), 1, minimum=1, maximum=24)
        hours = _parse_offpeak_hours(updates.get("offpeak_hours_pt", current.get("offpeak_hours_pt") or [1, 2, 3, 4]))
        max_keywords = _parse_int(updates.get("max_keywords_per_cycle", current.get("max_keywords_per_cycle")), 1, minimum=1, maximum=10)
        max_results = _parse_int(updates.get("max_results_per_keyword", current.get("max_results_per_keyword")), 15, minimum=1, maximum=40)
        max_channels = _parse_int(
            updates.get("max_channels_enriched_per_cycle", current.get("max_channels_enriched_per_cycle")),
            15,
            minimum=1,
            maximum=150,
        )
        max_trakk = _parse_int(updates.get("max_trakk_per_cycle", current.get("max_trakk_per_cycle")), 5, minimum=0, maximum=20)
        conn.execute(
            """
            UPDATE discovery_automation_control
            SET enabled = ?,
                paused_reason = ?,
                runs_per_day = ?,
                offpeak_hours_pt_json = ?,
                max_keywords_per_cycle = ?,
                max_results_per_keyword = ?,
                max_channels_enriched_per_cycle = ?,
                max_trakk_per_cycle = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = 1
            """,
            [enabled, paused_reason, runs_per_day, _json_hours(hours), max_keywords, max_results, max_channels, max_trakk],
        )
        conn.commit()
        return _select_control(conn)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _create_run(conn) -> int:
    row = conn.execute(
        """
        INSERT INTO discovery_automation_runs (status, started_at)
        VALUES ('running', CURRENT_TIMESTAMP)
        RETURNING id
        """
    ).fetchone()
    return int(row[0])


def _finish_run(conn, run_id: int, *, status: str, result: Dict[str, Any], error: str = "") -> None:
    conn.execute(
        """
        UPDATE discovery_automation_runs
        SET finished_at = CURRENT_TIMESTAMP,
            status = ?,
            keywords_used = ?,
            search_calls = ?,
            enrichment_read_calls = ?,
            leads_discovered = ?,
            leads_icp_qualified = ?,
            enrich_job_id = ?,
            trakk_estimated_cost = ?,
            error = ?
        WHERE id = ?
        """,
        [
            status,
            ", ".join(str(keyword) for keyword in result.get("keywords", []) if keyword),
            int(result.get("search_calls") or 0),
            int(result.get("enrichment_read_calls") or 0),
            int(result.get("rows_returned") or 0),
            int(result.get("icp_qualified_count") or 0),
            str(result.get("enrich_job_id") or ""),
            float(result.get("trakk_estimated_cost") or 0),
            str(error or "")[:1000],
            run_id,
        ],
    )


def _mark_control_finished(conn) -> None:
    if table_columns(conn, CONTROL_TABLE):
        conn.execute(
            """
            UPDATE discovery_automation_control
            SET last_finished_at = CURRENT_TIMESTAMP,
                lock_expires_at = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = 1
            """
        )


def _claim_scheduled_control(conn) -> Optional[Dict[str, Any]]:
    if not table_columns(conn, CONTROL_TABLE) or not table_columns(conn, RUNS_TABLE):
        return None
    control = _select_control(conn)
    if not control or not control.get("enabled"):
        return None

    now_utc = _utc_now()
    now_pt = now_utc.astimezone(PACIFIC_TZ)
    hours = _parse_offpeak_hours(control.get("offpeak_hours_pt"))
    if now_pt.hour not in hours:
        return None
    next_run_at = _as_utc(control.get("next_run_at"))
    if next_run_at and next_run_at > now_utc:
        return None

    next_slot = _next_offpeak_slot_utc(now_utc, hours, int(control.get("runs_per_day") or 1))
    lock_until = (now_utc + timedelta(minutes=DEFAULT_LOCK_MINUTES)).replace(tzinfo=None)
    row = conn.execute(
        """
        UPDATE discovery_automation_control
        SET lock_expires_at = ?,
            last_started_at = CURRENT_TIMESTAMP,
            next_run_at = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = 1
          AND enabled IS TRUE
          AND (lock_expires_at IS NULL OR lock_expires_at <= CURRENT_TIMESTAMP)
          AND (next_run_at IS NULL OR next_run_at <= CURRENT_TIMESTAMP)
        RETURNING id
        """,
        [lock_until, next_slot],
    ).fetchone()
    if not row:
        return None
    claimed = dict(control)
    claimed["next_run_at"] = next_slot
    return claimed


def run_discovery_automation_cycle(*, manual: bool = False, require_enabled: bool = False) -> Dict[str, Any]:
    conn = get_db()
    run_id: Optional[int] = None
    try:
        if not table_columns(conn, CONTROL_TABLE) or not table_columns(conn, RUNS_TABLE):
            raise RuntimeError("Discovery automation tables are not available.")
        if manual:
            control = _select_control(conn)
            if require_enabled and not control.get("enabled"):
                return {"success": False, "skipped": True, "reason": "automation_disabled"}
            conn.execute(
                """
                UPDATE discovery_automation_control
                SET last_started_at = CURRENT_TIMESTAMP,
                    lock_expires_at = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = 1
                """,
                [(_utc_now() + timedelta(minutes=DEFAULT_LOCK_MINUTES)).replace(tzinfo=None)],
            )
        else:
            control = _claim_scheduled_control(conn)
            if not control:
                conn.rollback()
                return {"success": True, "skipped": True, "reason": "not_due"}
        run_id = _create_run(conn)
        conn.commit()

        if not control:
            raise RuntimeError("Discovery automation control row is missing.")
        try:
            conn.close()
        except Exception:
            pass
        payload = {
            "use_queue": True,
            "take_n": int(control.get("max_keywords_per_cycle") or 1),
            "max_keywords": int(control.get("max_keywords_per_cycle") or 1),
            "max_results_per_keyword": int(control.get("max_results_per_keyword") or 15),
            "max_channels_enriched": int(control.get("max_channels_enriched_per_cycle") or 15),
            "ai_icp": True,
            "lang": "en",
            "region": "US",
        }
        from app.video_shorts.routes.api import run_lead_discovery_payload

        discovery_payload, status_code = run_lead_discovery_payload(payload)
        result = dict(discovery_payload)
        result["status_code"] = status_code
        if status_code >= 500:
            raise RuntimeError((result.get("errors") or [{}])[0].get("message") or "Discovery run failed.")

        icp_count = sum(1 for row in result.get("results") or [] if row.get("icp_fit") is True)
        batch_size = min(int(control.get("max_trakk_per_cycle") or 0), icp_count)
        enrich_job_id = ""
        if batch_size > 0:
            enqueue_result = enqueue_worker_job(
                user_id="system",
                job_type=JOB_TYPE_ENRICH_DISCOVERY_EMAILS,
                payload={"batch_size": batch_size},
                input_hash=f"discovery-automation-email-enrich:{uuid4()}",
                max_attempts=1,
                priority=30,
            )
            enrich_job_id = str((enqueue_result.get("job") or {}).get("id") or "")
        result["enrich_job_id"] = enrich_job_id
        result["trakk_estimated_cost"] = batch_size * 0.005
        result["icp_qualified_count"] = icp_count

        conn = get_db()
        _finish_run(conn, run_id, status="completed" if result.get("success") else "skipped", result=result)
        _mark_control_finished(conn)
        conn.commit()
        return {"success": bool(result.get("success")), "run_id": run_id, "result": result}
    except Exception as exc:
        current_app.logger.exception("Discovery automation cycle failed")
        try:
            conn.rollback()
        except Exception:
            pass
        try:
            fail_conn = get_db()
            if run_id:
                _finish_run(fail_conn, run_id, status="failed", result={}, error=str(exc) or "Automation cycle failed.")
            _mark_control_finished(fail_conn)
            fail_conn.commit()
        except Exception:
            current_app.logger.exception("Could not record failed discovery automation cycle")
        return {"success": False, "run_id": run_id, "error": str(exc) or "Automation cycle failed."}
    finally:
        try:
            conn.close()
        except Exception:
            pass


def process_due_discovery_automation_cycle() -> bool:
    result = run_discovery_automation_cycle(manual=False)
    return bool(result and not result.get("skipped"))
