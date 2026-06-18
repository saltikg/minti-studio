from __future__ import annotations

from datetime import datetime, timedelta
from threading import Lock, Thread
from uuid import uuid4

from app import create_app
from app.video_shorts.services import db as db_service
from app.video_shorts.services import render_jobs, usage_metering


def _configure_duckdb(monkeypatch, tmp_path, filename: str) -> None:
    db_path = tmp_path / filename
    monkeypatch.setattr(db_service, "VIDEO_SHORTS_DB_BACKEND", "duckdb")
    monkeypatch.setattr(db_service, "VIDEO_SHORTS_DB", str(db_path))


def _insert_user(user_id: str, plan_id: str) -> None:
    conn = db_service.get_db()
    try:
        render_jobs.ensure_render_jobs_schema(conn)
        conn.execute(
            """
            INSERT INTO shorts_users (id, name, email, username, role, plan_id)
            VALUES (?, ?, ?, ?, 'member', ?)
            """,
            [user_id, "Render User", f"{user_id}@example.com", f"user_{user_id[:8]}", plan_id],
        )
        conn.commit()
    finally:
        conn.close()


def _payload(video_pk: int, source_video_id: str, plan_index: int = 1) -> dict:
    return {
        "video_pk": video_pk,
        "source_video_id": source_video_id,
        "brand_id": "brand-1",
        "plan_index": plan_index,
        "title": f"Clip {plan_index}",
        "start": 10.0,
        "end": 20.0,
        "options": {"plan_index": plan_index, "title": f"Clip {plan_index}"},
    }


def _input_hash(source_video_id: str, plan_index: int = 1) -> str:
    return render_jobs.build_input_hash(
        source_id=source_video_id,
        start=10.0,
        end=20.0,
        options={"plan_index": plan_index, "title": f"Clip {plan_index}"},
    )


def test_priority_claims_paid_before_free(monkeypatch, tmp_path):
    _configure_duckdb(monkeypatch, tmp_path, "priority_claims.duckdb")
    free_user = str(uuid4())
    paid_user = str(uuid4())
    _insert_user(free_user, "plan_free")
    _insert_user(paid_user, "plan_10gb")

    free_job = render_jobs.enqueue_render_job(
        user_id=free_user,
        payload=_payload(1, "free-video"),
        input_hash=_input_hash("free-video"),
    )
    paid_job = render_jobs.enqueue_render_job(
        user_id=paid_user,
        payload=_payload(2, "paid-video"),
        input_hash=_input_hash("paid-video"),
    )

    first = render_jobs.claim_next_job("worker-a")
    second = render_jobs.claim_next_job("worker-a")

    assert free_job["kind"] == "queued"
    assert paid_job["kind"] == "queued"
    assert first["user_id"] == paid_user
    assert second["user_id"] == free_user


def test_claim_is_atomic_under_concurrency(monkeypatch, tmp_path):
    _configure_duckdb(monkeypatch, tmp_path, "claim_atomic.duckdb")
    user_id = str(uuid4())
    _insert_user(user_id, "plan_10gb")
    render_jobs.enqueue_render_job(
        user_id=user_id,
        payload=_payload(1, "atomic-video"),
        input_hash=_input_hash("atomic-video"),
    )

    claimed = []
    errors = []
    lock = Lock()

    def _worker() -> None:
        try:
            job = render_jobs.claim_next_job(f"worker-{uuid4()}")
            with lock:
                claimed.append(job["id"] if job else None)
        except Exception as exc:  # pragma: no cover
            with lock:
                errors.append(str(exc))

    threads = [Thread(target=_worker) for _ in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    non_null = [job_id for job_id in claimed if job_id]
    assert not errors
    assert len(non_null) == 1
    assert len(set(non_null)) == 1


def test_free_plan_inflight_cap_blocks_second_job(monkeypatch, tmp_path):
    _configure_duckdb(monkeypatch, tmp_path, "free_cap.duckdb")
    user_id = str(uuid4())
    _insert_user(user_id, "plan_free")

    first = render_jobs.enqueue_render_job(
        user_id=user_id,
        payload=_payload(1, "video-a", 1),
        input_hash=_input_hash("video-a", 1),
    )
    second = render_jobs.enqueue_render_job(
        user_id=user_id,
        payload=_payload(2, "video-b", 2),
        input_hash=_input_hash("video-b", 2),
    )

    assert first["kind"] == "queued"
    assert second["kind"] == "concurrency_limit"
    assert second["limit"] == 1


def test_timed_out_processing_job_requeues_then_fails_and_releases_quota(monkeypatch, tmp_path):
    _configure_duckdb(monkeypatch, tmp_path, "timeout_requeue.duckdb")
    user_id = str(uuid4())
    _insert_user(user_id, "plan_free")

    queued = render_jobs.enqueue_render_job(
        user_id=user_id,
        payload=_payload(1, "timeout-video"),
        input_hash=_input_hash("timeout-video"),
    )
    job_id = queued["job"]["id"]
    usage_metering.reserve_export(user_id)
    processing = render_jobs.claim_next_job("worker-timeout")
    assert processing["id"] == job_id

    conn = db_service.get_db()
    try:
        stale_start = datetime.utcnow() - timedelta(seconds=900)
        conn.execute(
            "UPDATE shorts_render_jobs SET started_at = ? WHERE id = ?",
            [stale_start, job_id],
        )
        conn.commit()
    finally:
        conn.close()

    first_pass = render_jobs.requeue_timed_out_jobs(timeout_seconds=600)
    requeued = render_jobs.get_job(job_id, user_id=user_id)
    assert first_pass["requeued"] == 1
    assert requeued["status"] == "queued"

    render_jobs.claim_next_job("worker-timeout-2")
    conn = db_service.get_db()
    try:
        stale_start = datetime.utcnow() - timedelta(seconds=900)
        conn.execute(
            "UPDATE shorts_render_jobs SET started_at = ?, attempts = ?, max_attempts = ? WHERE id = ?",
            [stale_start, 3, 3, job_id],
        )
        conn.commit()
    finally:
        conn.close()

    second_pass = render_jobs.requeue_timed_out_jobs(timeout_seconds=600)
    failed = render_jobs.get_job(job_id, user_id=user_id)
    snapshot = usage_metering.get_usage_snapshot(user_id)
    assert second_pass["failed"] == 1
    assert failed["status"] == "failed"
    assert snapshot["exports"]["used"] == 0


def test_done_job_cache_returns_existing_result_without_new_job(monkeypatch, tmp_path):
    _configure_duckdb(monkeypatch, tmp_path, "idempotent_cache.duckdb")
    user_id = str(uuid4())
    _insert_user(user_id, "plan_10gb")

    queued = render_jobs.enqueue_render_job(
        user_id=user_id,
        payload=_payload(1, "cache-video"),
        input_hash=_input_hash("cache-video"),
    )
    job_id = queued["job"]["id"]
    usage_metering.reserve_export(user_id)
    render_jobs.mark_job_done(job_id, {"clip_filename": "1_cache-video.mp4", "status": "created"})
    render_jobs.finalize_job_success(job_id)

    cached = render_jobs.enqueue_render_job(
        user_id=user_id,
        payload=_payload(1, "cache-video"),
        input_hash=_input_hash("cache-video"),
    )
    snapshot = usage_metering.get_usage_snapshot(user_id)

    assert cached["kind"] == "cached"
    assert cached["job"]["id"] == job_id
    assert cached["job"]["result"]["clip_filename"] == "1_cache-video.mp4"
    assert snapshot["exports"]["used"] == 1


def test_job_status_endpoint_is_owner_scoped(monkeypatch, tmp_path):
    _configure_duckdb(monkeypatch, tmp_path, "job_status_api.duckdb")
    owner_id = str(uuid4())
    other_id = str(uuid4())
    _insert_user(owner_id, "plan_10gb")
    _insert_user(other_id, "plan_free")

    queued = render_jobs.enqueue_render_job(
        user_id=owner_id,
        payload=_payload(1, "status-video"),
        input_hash=_input_hash("status-video"),
    )
    job_id = queued["job"]["id"]

    app = create_app()
    app.secret_key = "test-secret"

    owner_client = app.test_client()
    with owner_client.session_transaction() as session:
        session["vs_user_id"] = owner_id
    owner_response = owner_client.get(f"/api/jobs/{job_id}")
    owner_payload = owner_response.get_json()

    other_client = app.test_client()
    with other_client.session_transaction() as session:
        session["vs_user_id"] = other_id
    other_response = other_client.get(f"/api/jobs/{job_id}")

    assert owner_response.status_code == 200
    assert owner_payload["id"] == job_id
    assert owner_payload["status"] == "queued"
    assert owner_payload["queue_position"] == 0
    assert other_response.status_code == 404
