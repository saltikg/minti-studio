from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from flask import current_app

from app.video_shorts.services.db import get_db, table_columns


PACIFIC_TZ = ZoneInfo("America/Los_Angeles")
CONTROL_TABLE = "discovery_automation_control"
RUNS_TABLE = "discovery_automation_runs"
SUMMARY_TABLE = "discovery_keyword_generation_summaries"
DEFAULT_LOCK_MINUTES = 45
AUTO_GENERATE_COLUMN = "last_keyword_auto_generated_at"


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
        "max_trakk_per_day": _parse_int(row[9] if len(row) > 14 else 50, 50, minimum=0, maximum=500),
        "next_run_at": row[10] if len(row) > 14 else row[9],
        "last_started_at": row[11] if len(row) > 14 else row[10],
        "last_finished_at": row[12] if len(row) > 14 else row[11],
        "lock_expires_at": row[13] if len(row) > 14 else row[12],
        "updated_at": row[14] if len(row) > 14 else row[13],
        "last_keyword_auto_generated_at": row[15] if len(row) > 15 else None,
    }


def _select_control(conn) -> Dict[str, Any]:
    if not table_columns(conn, CONTROL_TABLE):
        return {}
    columns = table_columns(conn, CONTROL_TABLE)
    max_trakk_per_day_sql = "max_trakk_per_day" if "max_trakk_per_day" in columns else "50"
    auto_generated_at_sql = AUTO_GENERATE_COLUMN if AUTO_GENERATE_COLUMN in columns else "NULL"
    row = conn.execute(
        f"""
        SELECT id, enabled, paused_reason, offpeak_hours_pt_json, runs_per_day,
               max_keywords_per_cycle, max_results_per_keyword,
               max_channels_enriched_per_cycle, max_trakk_per_cycle,
               {max_trakk_per_day_sql},
               next_run_at, last_started_at, last_finished_at, lock_expires_at, updated_at,
               {auto_generated_at_sql}
        FROM discovery_automation_control
        WHERE id = 1
        LIMIT 1
        """
    ).fetchone()
    return _row_to_control(row)


def _same_pacific_day(value: Any, now_utc: datetime) -> bool:
    generated_at = _as_utc(value)
    if not generated_at:
        return False
    return generated_at.astimezone(PACIFIC_TZ).date() == now_utc.astimezone(PACIFIC_TZ).date()


def _mark_keyword_auto_generated(conn) -> None:
    conn.execute(
        f"""
        UPDATE discovery_automation_control
        SET {AUTO_GENERATE_COLUMN} = CURRENT_TIMESTAMP,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = 1
        """
    )


def _mark_keyword_auto_generated_after_failure() -> None:
    try:
        conn = get_db()
        try:
            if AUTO_GENERATE_COLUMN in table_columns(conn, CONTROL_TABLE):
                _mark_keyword_auto_generated(conn)
                conn.commit()
        finally:
            conn.close()
    except Exception:
        current_app.logger.exception("Failed to record discovery keyword auto-generation attempt after failure")


def _automation_daily_keyword_target(control: Dict[str, Any]) -> int:
    runs_per_day = _parse_int(control.get("runs_per_day"), 1, minimum=1, maximum=24)
    batch_size = _parse_int(control.get("max_keywords_per_cycle"), 1, minimum=1, maximum=10)
    return runs_per_day * batch_size


def _today_pt_string(now_utc: datetime) -> str:
    return now_utc.astimezone(PACIFIC_TZ).date().isoformat()


def _ensure_keyword_generation_summary_table(conn) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SUMMARY_TABLE} (
            date_pt DATE PRIMARY KEY,
            target INTEGER NOT NULL DEFAULT 0,
            generated_raw INTEGER NOT NULL DEFAULT 0,
            dropped_duplicates INTEGER NOT NULL DEFAULT 0,
            inserted INTEGER NOT NULL DEFAULT 0,
            pulled_forward INTEGER NOT NULL DEFAULT 0,
            eligible_final INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def _store_keyword_generation_summary(conn, summary: Dict[str, Any]) -> None:
    _ensure_keyword_generation_summary_table(conn)
    date_pt = str(summary.get("date_pt") or "")
    conn.execute(f"DELETE FROM {SUMMARY_TABLE} WHERE date_pt = ?", [date_pt])
    conn.execute(
        f"""
        INSERT INTO {SUMMARY_TABLE} (
            date_pt, target, generated_raw, dropped_duplicates, inserted,
            pulled_forward, eligible_final, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        [
            date_pt,
            int(summary.get("target") or 0),
            int(summary.get("generated_raw") or 0),
            int(summary.get("dropped_duplicates") or 0),
            int(summary.get("inserted") or 0),
            int(summary.get("pulled_forward") or 0),
            int(summary.get("eligible_final") or 0),
        ],
    )


def _pull_forward_keyword_queue(conn, needed: int) -> Dict[str, Any]:
    needed = max(0, int(needed or 0))
    if needed <= 0:
        return {"pulled_forward": 0, "keywords": []}
    rows = conn.execute(
        """
        SELECT id, keyword
        FROM keyword_queue
        WHERE status = 'queued'
          AND next_run_at > CURRENT_TIMESTAMP
        ORDER BY COALESCE(found_count, 0) DESC,
                 last_searched_at ASC NULLS FIRST,
                 id ASC
        LIMIT ?
        """,
        [needed],
    ).fetchall()
    ids = [int(row[0]) for row in rows]
    keywords = [str(row[1] or "").strip() for row in rows if str(row[1] or "").strip()]
    if not ids:
        return {"pulled_forward": 0, "keywords": []}
    placeholders = ",".join(["?"] * len(ids))
    conn.execute(
        f"""
        UPDATE keyword_queue
        SET next_run_at = CURRENT_TIMESTAMP,
            updated_at = CURRENT_TIMESTAMP
        WHERE id IN ({placeholders})
        """,
        ids,
    )
    return {"pulled_forward": len(ids), "keywords": keywords}


def _maybe_auto_generate_seed_keywords(control: Dict[str, Any]) -> Dict[str, Any]:
    target = _automation_daily_keyword_target(control)
    now_utc = _utc_now()
    metadata: Dict[str, Any] = {
        "eligible_before": 0,
        "auto_generated": 0,
        "auto_generated_total": 0,
        "auto_generation_attempted": False,
        "auto_generation_skipped": "",
        "date_pt": _today_pt_string(now_utc),
        "target": target,
        "generated_raw": 0,
        "dropped_duplicates": 0,
        "inserted": 0,
        "pulled_forward": 0,
        "eligible_final": 0,
        "attempts": 0,
    }
    conn = get_db()
    try:
        from app.video_shorts.routes.api import count_eligible_keyword_queue, generate_seed_keywords_into_queue

        if not table_columns(conn, "keyword_queue"):
            metadata["auto_generation_skipped"] = "keyword_queue_missing"
            return metadata

        eligible_before = count_eligible_keyword_queue(conn)
        metadata["eligible_before"] = eligible_before
        metadata["eligible_final"] = eligible_before
        if eligible_before >= target:
            return metadata

        control_columns = table_columns(conn, CONTROL_TABLE)
        if AUTO_GENERATE_COLUMN not in control_columns:
            metadata["auto_generation_skipped"] = "state_column_missing"
            current_app.logger.warning(
                "Discovery keyword auto-generation skipped: %s column is missing",
                AUTO_GENERATE_COLUMN,
            )
            return metadata

        row = conn.execute(
            f"SELECT {AUTO_GENERATE_COLUMN} FROM discovery_automation_control WHERE id = 1"
        ).fetchone()
        if _same_pacific_day(row[0] if row else None, now_utc):
            metadata["auto_generation_skipped"] = "already_attempted_today"
            current_app.logger.info(
                "Discovery keyword auto-generation skipped: already attempted today (eligible=%s target=%s)",
                eligible_before,
                target,
            )
            return metadata

        metadata["auto_generation_attempted"] = True
        generation_errors: List[Dict[str, Any]] = []
        for attempt in range(1, 4):
            if count_eligible_keyword_queue(conn) >= target:
                break
            generation = generate_seed_keywords_into_queue(conn)
            metadata["attempts"] = attempt
            metadata["generated_raw"] += int(generation.get("generated_raw") or generation.get("generated") or 0)
            metadata["auto_generated_total"] += int(generation.get("generated") or 0)
            inserted = int(generation.get("newly_enqueued") or 0)
            duplicates = int(generation.get("already_present") or 0)
            metadata["inserted"] += inserted
            metadata["auto_generated"] += inserted
            metadata["dropped_duplicates"] += duplicates
            generation_errors.extend(generation.get("errors") or [])
            metadata["auto_generation_success"] = bool(generation.get("success"))
            if count_eligible_keyword_queue(conn) >= target:
                break

        eligible_after_generation = count_eligible_keyword_queue(conn)
        if eligible_after_generation < target:
            pull_result = _pull_forward_keyword_queue(conn, target - eligible_after_generation)
            metadata["pulled_forward"] = int(pull_result.get("pulled_forward") or 0)
            metadata["pulled_forward_keywords"] = pull_result.get("keywords") or []

        eligible_final = count_eligible_keyword_queue(conn)
        metadata["eligible_final"] = eligible_final
        metadata["auto_generation_errors"] = generation_errors
        _store_keyword_generation_summary(conn, metadata)
        _mark_keyword_auto_generated(conn)
        conn.commit()
        current_app.logger.info(
            "Discovery keyword daily summary date_pt=%s target=%s generated_raw=%s dropped_duplicates=%s inserted=%s pulled_forward=%s eligible_final=%s attempts=%s eligible_before=%s",
            metadata["date_pt"],
            target,
            metadata["generated_raw"],
            metadata["dropped_duplicates"],
            metadata["inserted"],
            metadata["pulled_forward"],
            eligible_final,
            metadata["attempts"],
            eligible_before,
        )
        if metadata["inserted"] <= 0 and metadata["pulled_forward"] <= 0:
            current_app.logger.warning(
                "Discovery keyword auto-generation returned no usable keywords: generated=%s eligible_before=%s target=%s errors=%s",
                metadata["auto_generated_total"],
                eligible_before,
                target,
                metadata["auto_generation_errors"],
            )
        return metadata
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        _mark_keyword_auto_generated_after_failure()
        current_app.logger.exception(
            "Discovery keyword auto-generation failed; continuing scheduled run with eligible queue"
        )
        metadata["auto_generation_attempted"] = True
        metadata["auto_generation_failed"] = True
        return metadata
    finally:
        try:
            conn.close()
        except Exception:
            pass


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
        max_trakk_per_day = _parse_int(updates.get("max_trakk_per_day", current.get("max_trakk_per_day")), 50, minimum=0, maximum=500)
        columns = table_columns(conn, CONTROL_TABLE)
        daily_cap_assignment = ", max_trakk_per_day = ?" if "max_trakk_per_day" in columns else ""
        daily_cap_param = [max_trakk_per_day] if "max_trakk_per_day" in columns else []
        conn.execute(
            f"""
            UPDATE discovery_automation_control
            SET enabled = ?,
                paused_reason = ?,
                runs_per_day = ?,
                offpeak_hours_pt_json = ?,
                max_keywords_per_cycle = ?,
                max_results_per_keyword = ?,
                max_channels_enriched_per_cycle = ?,
                max_trakk_per_cycle = ?
                {daily_cap_assignment},
                updated_at = CURRENT_TIMESTAMP
            WHERE id = 1
            """,
            [enabled, paused_reason, runs_per_day, _json_hours(hours), max_keywords, max_results, max_channels, max_trakk, *daily_cap_param],
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
        batch_size = int(control.get("max_keywords_per_cycle") or 1)
        auto_generation = _maybe_auto_generate_seed_keywords(control)
        try:
            conn.close()
        except Exception:
            pass
        payload = {
            "use_queue": True,
            "take_n": batch_size,
            "max_keywords": batch_size,
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
        result["queue_eligible_before_auto_generate"] = int(auto_generation.get("eligible_before") or 0)
        result["auto_generated_keywords"] = int(auto_generation.get("auto_generated") or 0)
        result["auto_generation"] = auto_generation
        if status_code >= 500:
            raise RuntimeError((result.get("errors") or [{}])[0].get("message") or "Discovery run failed.")

        icp_count = sum(1 for row in result.get("results") or [] if row.get("icp_fit") is True)
        auto_trakk = result.get("auto_trakk") or {}
        batch_size = int(auto_trakk.get("batch_size") or 0)
        result["enrich_job_id"] = str(auto_trakk.get("job_id") or "")
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
