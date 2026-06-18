from __future__ import annotations

import os
import socket
import time
from typing import Any, Dict, Optional, Tuple

from flask import g

from app import create_app
from app.video_shorts.config import JOB_POLL_INTERVAL_SECONDS, JOB_TIMEOUT_SECONDS, WORKER_CONCURRENCY
from app.video_shorts.routes import generation
from app.video_shorts.services.db import get_db_readonly
from app.video_shorts.services.render_jobs import (
    claim_next_job,
    finalize_job_success,
    get_job,
    mark_job_done,
    mark_job_failed,
    requeue_job,
    requeue_timed_out_jobs,
)


class PermanentRenderJobError(RuntimeError):
    pass


def _worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def _load_user_context(user_id: str, brand_id: Optional[str]) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    conn = get_db_readonly()
    try:
        row = conn.execute(
            """
            SELECT id, name, email, username, role, plan_id
            FROM shorts_users
            WHERE id = ?
            """,
            [user_id],
        ).fetchone()
        brand = None
        if brand_id:
            brand_row = conn.execute(
                """
                SELECT id, name, slug, is_default
                FROM shorts_brands
                WHERE id = ? AND owner_user_id = ?
                """,
                [brand_id, user_id],
            ).fetchone()
            if brand_row:
                brand = {
                    "id": brand_row[0],
                    "name": brand_row[1],
                    "slug": brand_row[2],
                    "is_default": bool(brand_row[3]),
                }
    finally:
        conn.close()
    if not row:
        raise PermanentRenderJobError("User not found for queued render job.")
    user = {
        "id": str(row[0]),
        "name": row[1],
        "email": row[2],
        "username": row[3],
        "role": row[4],
        "plan_id": row[5],
    }
    return user, brand


def _normalize_response(result: Any) -> Tuple[int, Dict[str, Any]]:
    response = result
    status_code = getattr(response, "status_code", None)
    if isinstance(result, tuple):
        response = result[0]
        if len(result) > 1 and isinstance(result[1], int):
            status_code = result[1]
    payload = {}
    if hasattr(response, "get_json"):
        try:
            payload = response.get_json(silent=True) or {}
        except Exception:
            payload = {}
    if status_code is None:
        status_code = 200
    return int(status_code), payload


def _mark_plan_failure(job: Dict[str, Any], error_message: str) -> None:
    payload = job.get("payload") or {}
    source_video_id = str(payload.get("source_video_id") or "").strip()
    plan_index = payload.get("plan_index")
    if not source_video_id or plan_index is None:
        return
    try:
        entries = generation._load_plan_entries(source_video_id)
        generation._update_plan_entry_job_state(
            source_video_id,
            entries,
            plan_index=int(plan_index),
            status="failed",
            error_message=error_message,
        )
    except Exception:
        pass


def _update_plan_status(job: Dict[str, Any], status: str) -> None:
    payload = job.get("payload") or {}
    source_video_id = str(payload.get("source_video_id") or "").strip()
    plan_index = payload.get("plan_index")
    if not source_video_id or plan_index is None:
        return
    try:
        entries = generation._load_plan_entries(source_video_id)
        generation._update_plan_entry_job_state(
            source_video_id,
            entries,
            plan_index=int(plan_index),
            status=status,
            render_job_id=job.get("id"),
        )
    except Exception:
        pass


def _execute_render_job(app, job: Dict[str, Any]) -> Dict[str, Any]:
    payload = job.get("payload") or {}
    video_pk = int(payload.get("video_pk"))
    plan_index = int(payload.get("plan_index"))
    title = str(payload.get("title") or "").strip()
    user, brand = _load_user_context(job["user_id"], payload.get("brand_id"))
    with app.app_context():
        with app.test_request_context(
            f"/video_shorts/generate/{video_pk}/autoclip",
            method="POST",
            data={
                "plan_index": str(plan_index),
                "title": title,
                "_queued_job": "1",
            },
            headers={"X-Requested-With": "XMLHttpRequest"},
        ):
            g.vs_current_user = user
            if brand:
                g.vs_current_brand = brand
            result = generation.autoclip_video(video_pk)
            status_code, response_payload = _normalize_response(result)
    if status_code >= 500:
        raise RuntimeError(response_payload.get("message") or "Render job failed.")
    if status_code >= 400:
        raise PermanentRenderJobError(response_payload.get("message") or "Render job failed.")
    if not response_payload.get("success"):
        raise RuntimeError(response_payload.get("message") or "Render job returned unsuccessful response.")
    return response_payload


def process_next_job(app, worker_id: str) -> bool:
    job = claim_next_job(worker_id)
    if not job:
        return False
    _update_plan_status(job, "processing")
    try:
        result = _execute_render_job(app, job)
        mark_job_done(job["id"], result)
        finalize_job_success(job["id"])
        return True
    except PermanentRenderJobError as exc:
        _mark_plan_failure(job, str(exc))
        mark_job_failed(job["id"], str(exc))
        return True
    except Exception as exc:
        latest = get_job(job["id"]) or job
        if int(latest.get("attempts") or 0) >= int(latest.get("max_attempts") or 1):
            _mark_plan_failure(latest, str(exc))
            mark_job_failed(job["id"], str(exc))
        else:
            _update_plan_status(latest, "queued")
            requeue_job(job["id"], str(exc))
        return True


def run_worker_loop() -> None:
    worker_id = _worker_id()
    app = create_app()
    with app.app_context():
        while True:
            requeue_timed_out_jobs(timeout_seconds=JOB_TIMEOUT_SECONDS)
            processed_any = False
            for _ in range(max(1, int(WORKER_CONCURRENCY or 1))):
                if process_next_job(app, worker_id):
                    processed_any = True
                else:
                    break
            if not processed_any:
                time.sleep(JOB_POLL_INTERVAL_SECONDS)


def main() -> None:
    run_worker_loop()


if __name__ == "__main__":
    main()
