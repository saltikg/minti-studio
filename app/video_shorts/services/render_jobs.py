from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timedelta
from typing import Any, Dict, Optional
from uuid import uuid4

from app.video_shorts.services.db import get_db, table_columns
from app.video_shorts.services.usage_metering import (
    ensure_usage_metering_schema,
    finalize_export,
    release_export,
)


JOBS_TABLE = "shorts_render_jobs"
JOB_TYPE_RENDER_SHORT = "render_short"
JOB_TYPE_INGEST_YOUTUBE = "ingest_youtube"
JOB_TYPE_TRANSCRIBE_UPLOAD = "transcribe_upload"
JOB_TYPE_PUBLISH_SHORT = "publish_short"
JOB_TYPE_INSTAGRAM_COMMENT_WEBHOOK = "instagram_comment_webhook"
JOB_STATUS_QUEUED = "queued"
JOB_STATUS_PROCESSING = "processing"
JOB_STATUS_DONE = "done"
JOB_STATUS_FAILED = "failed"
JOB_STATUS_CANCELLED = "cancelled"
ACTIVE_JOB_STATUSES = (JOB_STATUS_QUEUED, JOB_STATUS_PROCESSING)
DEFAULT_MAX_ATTEMPTS = 3

_duckdb_claim_lock = threading.Lock()


def _utc_now() -> datetime:
    return datetime.utcnow().replace(microsecond=0)


def _json_column_type(conn) -> str:
    return "JSONB" if getattr(conn, "backend_name", "") == "postgres" else "TEXT"


def _json_value_sql(conn, param_placeholder: str = "?") -> str:
    if getattr(conn, "backend_name", "") == "postgres":
        return f"CAST({param_placeholder} AS JSONB)"
    return param_placeholder


def _serialize_json(value: Any) -> Optional[str]:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _parse_json(value: Any) -> Any:
    if value in (None, ""):
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return None


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    text = str(value).strip()
    return text or None


def _row_to_job(row: Any) -> Dict[str, Any]:
    return {
        "id": str(row[0]),
        "user_id": str(row[1]),
        "type": str(row[2]),
        "status": str(row[3]),
        "priority": int(row[4] or 0),
        "payload": _parse_json(row[5]) or {},
        "input_hash": row[6],
        "result": _parse_json(row[7]),
        "attempts": int(row[8] or 0),
        "max_attempts": int(row[9] or DEFAULT_MAX_ATTEMPTS),
        "error": row[10],
        "worker_id": row[11],
        "created_at": _iso(row[12]),
        "started_at": _iso(row[13]),
        "finished_at": _iso(row[14]),
        "updated_at": _iso(row[15]),
    }


def _job_select_sql() -> str:
    return f"""
        SELECT
            id,
            user_id,
            type,
            status,
            priority,
            payload_json,
            input_hash,
            result_json,
            attempts,
            max_attempts,
            error,
            worker_id,
            created_at,
            started_at,
            finished_at,
            updated_at
        FROM {JOBS_TABLE}
    """


def ensure_render_jobs_schema(conn) -> None:
    ensure_usage_metering_schema(conn)
    json_type = _json_column_type(conn)
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {JOBS_TABLE} (
            id VARCHAR PRIMARY KEY,
            user_id VARCHAR NOT NULL,
            type VARCHAR NOT NULL,
            status VARCHAR NOT NULL,
            priority INTEGER NOT NULL DEFAULT 0,
            payload_json {json_type},
            input_hash VARCHAR,
            result_json {json_type},
            attempts INTEGER DEFAULT 0,
            max_attempts INTEGER DEFAULT 3,
            error TEXT,
            worker_id VARCHAR,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            started_at TIMESTAMP,
            finished_at TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    cols = table_columns(conn, JOBS_TABLE)
    extra_columns = [
        ("worker_id", "VARCHAR"),
        ("result_json", f"{json_type}"),
        ("attempts", "INTEGER DEFAULT 0"),
        ("max_attempts", "INTEGER DEFAULT 3"),
        ("error", "TEXT"),
        ("started_at", "TIMESTAMP"),
        ("finished_at", "TIMESTAMP"),
        ("updated_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
        ("input_hash", "VARCHAR"),
    ]
    for column_name, definition in extra_columns:
        if column_name not in cols:
            conn.execute(f"ALTER TABLE {JOBS_TABLE} ADD COLUMN {column_name} {definition}")
    try:
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{JOBS_TABLE}_status_priority_created ON {JOBS_TABLE}(status, priority, created_at)"
        )
    except Exception:
        pass
    try:
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{JOBS_TABLE}_user_status ON {JOBS_TABLE}(user_id, status)"
        )
    except Exception:
        pass
    try:
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{JOBS_TABLE}_input_hash ON {JOBS_TABLE}(input_hash)"
        )
    except Exception:
        pass
    conn.commit()


def build_input_hash(*, source_id: str, start: float, end: float, options: Dict[str, Any]) -> str:
    canonical = {
        "source_id": str(source_id or ""),
        "start": round(float(start or 0.0), 3),
        "end": round(float(end or 0.0), 3),
        "options": options or {},
    }
    raw = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _fetch_plan_settings(conn, user_id: str) -> Dict[str, Any]:
    row = conn.execute(
        """
        SELECT
            COALESCE(p.render_priority, 0),
            COALESCE(p.max_concurrent_jobs, 1),
            COALESCE(su.plan_id, '')
        FROM shorts_users su
        LEFT JOIN shorts_storage_plans p ON p.plan_id = su.plan_id
        WHERE su.id = ?
        """,
        [user_id],
    ).fetchone()
    if not row:
        return {"render_priority": 0, "max_concurrent_jobs": 1, "plan_id": None}
    return {
        "render_priority": int(row[0] or 0),
        "max_concurrent_jobs": max(1, int(row[1] or 1)),
        "plan_id": row[2] or None,
    }


def _count_user_inflight(conn, user_id: str) -> int:
    row = conn.execute(
        f"""
        SELECT COUNT(*)
        FROM {JOBS_TABLE}
        WHERE user_id = ?
          AND status IN (?, ?)
        """,
        [user_id, JOB_STATUS_QUEUED, JOB_STATUS_PROCESSING],
    ).fetchone()
    return int((row[0] if row else 0) or 0)


def _find_job_by_hash(conn, *, user_id: str, input_hash: str, statuses: tuple[str, ...]) -> Optional[Dict[str, Any]]:
    if not input_hash:
        return None
    placeholders = ", ".join("?" for _ in statuses)
    row = conn.execute(
        f"""
        {_job_select_sql()}
        WHERE user_id = ?
          AND input_hash = ?
          AND status IN ({placeholders})
        ORDER BY created_at DESC
        LIMIT 1
        """,
        [user_id, input_hash, *statuses],
    ).fetchone()
    return _row_to_job(row) if row else None


def _queue_position(conn, job_id: str) -> Optional[int]:
    row = conn.execute(
        f"""
        SELECT priority, created_at, id, status
        FROM {JOBS_TABLE}
        WHERE id = ?
        """,
        [job_id],
    ).fetchone()
    if not row or row[3] != JOB_STATUS_QUEUED:
        return None
    priority, created_at, row_id = int(row[0] or 0), row[1], row[2]
    ahead = conn.execute(
        f"""
        SELECT COUNT(*)
        FROM {JOBS_TABLE}
        WHERE status = ?
          AND (
                priority > ?
             OR (priority = ? AND created_at < ?)
             OR (priority = ? AND created_at = ? AND id < ?)
          )
        """,
        [JOB_STATUS_QUEUED, priority, priority, created_at, priority, created_at, row_id],
    ).fetchone()
    return int((ahead[0] if ahead else 0) or 0)


def get_job(job_id: str, *, user_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    conn = get_db()
    try:
        ensure_render_jobs_schema(conn)
        sql = _job_select_sql()
        params: list[Any] = []
        if user_id:
            sql += " WHERE id = ? AND user_id = ?"
            params.extend([job_id, user_id])
        else:
            sql += " WHERE id = ?"
            params.append(job_id)
        row = conn.execute(sql, params).fetchone()
        if not row:
            conn.commit()
            return None
        job = _row_to_job(row)
        job["queue_position"] = _queue_position(conn, job_id)
        conn.commit()
        return job
    finally:
        conn.close()


def update_job_result(job_id: str, result: Dict[str, Any], *, touch_status: bool = False) -> Dict[str, Any]:
    conn = get_db()
    try:
        ensure_render_jobs_schema(conn)
        if touch_status:
            conn.execute(
                f"""
                UPDATE {JOBS_TABLE}
                SET result_json = {_json_value_sql(conn)},
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                [_serialize_json(result) or "{}", job_id],
            )
        else:
            conn.execute(
                f"""
                UPDATE {JOBS_TABLE}
                SET result_json = {_json_value_sql(conn)},
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                [_serialize_json(result) or "{}", job_id],
            )
        conn.commit()
        return get_job(job_id) or {}
    finally:
        conn.close()


def update_job_payload(job_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    conn = get_db()
    try:
        ensure_render_jobs_schema(conn)
        conn.execute(
            f"""
            UPDATE {JOBS_TABLE}
            SET payload_json = {_json_value_sql(conn)},
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            [_serialize_json(payload) or "{}", job_id],
        )
        conn.commit()
        return get_job(job_id) or {}
    finally:
        conn.close()


def enqueue_job(
    *,
    user_id: str,
    job_type: str,
    payload: Dict[str, Any],
    input_hash: str,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> Dict[str, Any]:
    conn = get_db()
    try:
        ensure_render_jobs_schema(conn)
        existing_done = _find_job_by_hash(conn, user_id=user_id, input_hash=input_hash, statuses=(JOB_STATUS_DONE,))
        if existing_done:
            existing_done["cached"] = True
            existing_done["queue_position"] = None
            conn.commit()
            return {"kind": "cached", "job": existing_done}
        existing_active = _find_job_by_hash(
            conn,
            user_id=user_id,
            input_hash=input_hash,
            statuses=ACTIVE_JOB_STATUSES,
        )
        if existing_active:
            existing_active["queue_position"] = _queue_position(conn, existing_active["id"])
            conn.commit()
            return {"kind": "existing", "job": existing_active}

        plan = _fetch_plan_settings(conn, user_id)
        if job_type in {JOB_TYPE_RENDER_SHORT, JOB_TYPE_PUBLISH_SHORT}:
            inflight = _count_user_inflight(conn, user_id)
            if inflight >= plan["max_concurrent_jobs"]:
                conn.commit()
                return {
                    "kind": "concurrency_limit",
                    "limit": plan["max_concurrent_jobs"],
                    "inflight": inflight,
                    "plan_id": plan["plan_id"],
                }

        job_id = str(uuid4())
        payload_json = _serialize_json(payload) or "{}"
        conn.execute(
            f"""
            INSERT INTO {JOBS_TABLE} (
                id,
                user_id,
                type,
                status,
                priority,
                payload_json,
                input_hash,
                attempts,
                max_attempts,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, {_json_value_sql(conn)}, ?, 0, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            [
                job_id,
                user_id,
                job_type,
                JOB_STATUS_QUEUED,
                plan["render_priority"],
                payload_json,
                input_hash,
                max(1, int(max_attempts or DEFAULT_MAX_ATTEMPTS)),
            ],
        )
        row = conn.execute(
            f"{_job_select_sql()} WHERE id = ?",
            [job_id],
        ).fetchone()
        job = _row_to_job(row) if row else {"id": job_id}
        job["queue_position"] = _queue_position(conn, job_id)
        conn.commit()
        return {"kind": "queued", "job": job}
    finally:
        conn.close()


def enqueue_worker_job(
    *,
    user_id: str,
    job_type: str,
    payload: Dict[str, Any],
    input_hash: str,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    priority: int = 0,
) -> Dict[str, Any]:
    conn = get_db()
    try:
        ensure_render_jobs_schema(conn)
        existing_done = _find_job_by_hash(conn, user_id=user_id, input_hash=input_hash, statuses=(JOB_STATUS_DONE,))
        if existing_done:
            existing_done["cached"] = True
            existing_done["queue_position"] = None
            conn.commit()
            return {"kind": "cached", "job": existing_done}
        existing_active = _find_job_by_hash(
            conn,
            user_id=user_id,
            input_hash=input_hash,
            statuses=ACTIVE_JOB_STATUSES,
        )
        if existing_active:
            existing_active["queue_position"] = _queue_position(conn, existing_active["id"])
            conn.commit()
            return {"kind": "existing", "job": existing_active}

        job_id = str(uuid4())
        payload_json = _serialize_json(payload) or "{}"
        # Non-render ingestion jobs should still use the existing worker/jobs
        # table, but must not depend on render-plan quota lookups.
        conn.execute(
            f"""
            INSERT INTO {JOBS_TABLE} (
                id,
                user_id,
                type,
                status,
                priority,
                payload_json,
                input_hash,
                attempts,
                max_attempts,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, {_json_value_sql(conn)}, ?, 0, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            [
                job_id,
                user_id,
                job_type,
                JOB_STATUS_QUEUED,
                int(priority or 0),
                payload_json,
                input_hash,
                max(1, int(max_attempts or DEFAULT_MAX_ATTEMPTS)),
            ],
        )
        row = conn.execute(
            f"{_job_select_sql()} WHERE id = ?",
            [job_id],
        ).fetchone()
        job = _row_to_job(row) if row else {"id": job_id}
        job["queue_position"] = _queue_position(conn, job_id)
        conn.commit()
        return {"kind": "queued", "job": job}
    finally:
        conn.close()


def enqueue_render_job(
    *,
    user_id: str,
    payload: Dict[str, Any],
    input_hash: str,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> Dict[str, Any]:
    return enqueue_job(
        user_id=user_id,
        job_type=JOB_TYPE_RENDER_SHORT,
        payload=payload,
        input_hash=input_hash,
        max_attempts=max_attempts,
    )


def claim_next_job(worker_id: str, *, job_type: Optional[str] = None) -> Optional[Dict[str, Any]]:
    conn = get_db()
    try:
        ensure_render_jobs_schema(conn)
        backend_name = getattr(conn, "backend_name", "")
        job_type_clause = ""
        params: list[Any] = []
        if job_type:
            job_type_clause = "AND type = ?"
            params.append(job_type)
        if backend_name == "postgres":
            row = conn.execute(
                f"""
                SELECT id
                FROM {JOBS_TABLE}
                WHERE status = ?
                  {job_type_clause}
                ORDER BY priority DESC, created_at ASC
                FOR UPDATE SKIP LOCKED
                LIMIT 1
                """,
                [JOB_STATUS_QUEUED, *params],
            ).fetchone()
            if not row:
                conn.commit()
                return None
            job_id = row[0]
            updated = conn.execute(
                f"""
                UPDATE {JOBS_TABLE}
                SET status = ?,
                    worker_id = ?,
                    started_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP,
                    attempts = COALESCE(attempts, 0) + 1
                WHERE id = ?
                RETURNING
                    id,
                    user_id,
                    type,
                    status,
                    priority,
                    payload_json,
                    input_hash,
                    result_json,
                    attempts,
                    max_attempts,
                    error,
                    worker_id,
                    created_at,
                    started_at,
                    finished_at,
                    updated_at
                """,
                [JOB_STATUS_PROCESSING, worker_id, job_id],
            ).fetchone()
            conn.commit()
            return _row_to_job(updated) if updated else None

        with _duckdb_claim_lock:
            row = conn.execute(
                f"""
                SELECT id
                FROM {JOBS_TABLE}
                WHERE status = ?
                  {job_type_clause}
                ORDER BY priority DESC, created_at ASC
                LIMIT 1
                """,
                [JOB_STATUS_QUEUED, *params],
            ).fetchone()
            if not row:
                conn.commit()
                return None
            job_id = row[0]
            conn.execute(
                f"""
                UPDATE {JOBS_TABLE}
                SET status = ?,
                    worker_id = ?,
                    started_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP,
                    attempts = COALESCE(attempts, 0) + 1
                WHERE id = ?
                """,
                [JOB_STATUS_PROCESSING, worker_id, job_id],
            )
            conn.commit()
            return get_job(job_id)
    finally:
        conn.close()


def mark_job_done(job_id: str, result: Dict[str, Any]) -> Dict[str, Any]:
    conn = get_db()
    try:
        ensure_render_jobs_schema(conn)
        conn.execute(
            f"""
            UPDATE {JOBS_TABLE}
            SET status = ?,
                result_json = {_json_value_sql(conn)},
                error = NULL,
                finished_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            [JOB_STATUS_DONE, _serialize_json(result) or "{}", job_id],
        )
        conn.commit()
        return get_job(job_id) or {}
    finally:
        conn.close()


def requeue_job(job_id: str, error: str) -> Dict[str, Any]:
    conn = get_db()
    try:
        ensure_render_jobs_schema(conn)
        conn.execute(
            f"""
            UPDATE {JOBS_TABLE}
            SET status = ?,
                error = ?,
                worker_id = NULL,
                started_at = NULL,
                finished_at = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            [JOB_STATUS_QUEUED, str(error or "").strip()[:4000], job_id],
        )
        conn.commit()
        return get_job(job_id) or {}
    finally:
        conn.close()


def mark_job_failed(job_id: str, error: str, *, release_reservation: bool = True) -> Dict[str, Any]:
    conn = get_db()
    try:
        ensure_render_jobs_schema(conn)
        row = conn.execute(
            f"SELECT user_id FROM {JOBS_TABLE} WHERE id = ?",
            [job_id],
        ).fetchone()
        user_id = str(row[0]) if row and row[0] else None
        conn.execute(
            f"""
            UPDATE {JOBS_TABLE}
            SET status = ?,
                error = ?,
                finished_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            [JOB_STATUS_FAILED, str(error or "").strip()[:4000], job_id],
        )
        conn.commit()
    finally:
        conn.close()
    if release_reservation and user_id:
        release_export(user_id)
    return get_job(job_id) or {}


def cancel_job(job_id: str, error: Optional[str] = None) -> Dict[str, Any]:
    conn = get_db()
    try:
        ensure_render_jobs_schema(conn)
        conn.execute(
            f"""
            UPDATE {JOBS_TABLE}
            SET status = ?,
                error = ?,
                finished_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            [JOB_STATUS_CANCELLED, str(error or "").strip()[:4000] or None, job_id],
        )
        conn.commit()
        return get_job(job_id) or {}
    finally:
        conn.close()


def requeue_timed_out_jobs(*, timeout_seconds: int) -> Dict[str, int]:
    cutoff = _utc_now() - timedelta(seconds=max(1, int(timeout_seconds or 0)))
    conn = get_db()
    requeued = 0
    failed = 0
    try:
        ensure_render_jobs_schema(conn)
        rows = conn.execute(
            f"""
            {_job_select_sql()}
            WHERE status = ?
              AND started_at IS NOT NULL
              AND started_at < ?
            ORDER BY started_at ASC
            """,
            [JOB_STATUS_PROCESSING, cutoff],
        ).fetchall()
        conn.commit()
    finally:
        conn.close()
    for row in rows:
        job = _row_to_job(row)
        if job["attempts"] >= job["max_attempts"]:
            mark_job_failed(job["id"], "Job timed out and exhausted retries.")
            failed += 1
        else:
            requeue_job(job["id"], "Job timed out; requeued automatically.")
            requeued += 1
    return {"requeued": requeued, "failed": failed}


def finalize_job_success(job_id: str) -> Dict[str, Any]:
    job = get_job(job_id)
    if job and job.get("user_id"):
        finalize_export(job["user_id"])
    return job or {}


def clear_done_job_cache_for_plan(*, user_id: str, source_video_id: str, plan_index: int) -> int:
    if not user_id or not source_video_id:
        return 0
    conn = get_db()
    cleared = 0
    try:
        ensure_render_jobs_schema(conn)
        rows = conn.execute(
            f"""
            {_job_select_sql()}
            WHERE user_id = ?
              AND status = ?
            ORDER BY created_at DESC
            """,
            [user_id, JOB_STATUS_DONE],
        ).fetchall()
        matching_ids = []
        for row in rows:
            job = _row_to_job(row)
            payload = job.get("payload") or {}
            try:
                payload_plan_index = int(payload.get("plan_index"))
            except Exception:
                continue
            if str(payload.get("source_video_id") or "").strip() != str(source_video_id).strip():
                continue
            if payload_plan_index != int(plan_index):
                continue
            matching_ids.append(job["id"])
        for job_id in matching_ids:
            conn.execute(
                f"""
                UPDATE {JOBS_TABLE}
                SET input_hash = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                [job_id],
            )
            cleared += 1
        conn.commit()
        return cleared
    finally:
        conn.close()
