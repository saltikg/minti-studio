from __future__ import annotations

import json
import errno
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock, Thread
from types import ModuleType, SimpleNamespace
from uuid import uuid4

def _ensure_module(name: str) -> ModuleType:
    module = sys.modules.get(name)
    if isinstance(module, ModuleType):
        return module
    module = ModuleType(name)
    sys.modules[name] = module
    return module


dotenv_module = _ensure_module("dotenv")
dotenv_module.load_dotenv = lambda *args, **kwargs: None

google_auth_oauthlib_module = _ensure_module("google_auth_oauthlib")
google_auth_oauthlib_flow_module = _ensure_module("google_auth_oauthlib.flow")
google_auth_oauthlib_flow_module.Flow = type("Flow", (), {})
google_auth_oauthlib_module.flow = google_auth_oauthlib_flow_module

google_oauth2_module = _ensure_module("google.oauth2")
google_oauth2_id_token_module = _ensure_module("google.oauth2.id_token")
google_oauth2_id_token_module.verify_oauth2_token = lambda *args, **kwargs: {}
google_oauth2_credentials_module = _ensure_module("google.oauth2.credentials")
google_oauth2_credentials_module.Credentials = type("Credentials", (), {})
google_oauth2_module.id_token = google_oauth2_id_token_module
google_oauth2_module.credentials = google_oauth2_credentials_module

google_auth_module = _ensure_module("google.auth")
google_auth_exceptions_module = _ensure_module("google.auth.exceptions")
google_auth_exceptions_module.RefreshError = type("RefreshError", (Exception,), {})
google_auth_transport_module = _ensure_module("google.auth.transport")
google_auth_transport_requests_module = _ensure_module("google.auth.transport.requests")
google_auth_transport_requests_module.Request = type("Request", (), {})
google_auth_module.exceptions = google_auth_exceptions_module
google_auth_module.transport = google_auth_transport_module
google_auth_transport_module.requests = google_auth_transport_requests_module

googleapiclient_module = _ensure_module("googleapiclient")
googleapiclient_discovery_module = _ensure_module("googleapiclient.discovery")
googleapiclient_discovery_module.build = lambda *args, **kwargs: None
googleapiclient_http_module = _ensure_module("googleapiclient.http")
googleapiclient_http_module.MediaFileUpload = type("MediaFileUpload", (), {})
googleapiclient_module.discovery = googleapiclient_discovery_module
googleapiclient_module.http = googleapiclient_http_module

stripe_module = _ensure_module("stripe")
stripe_module.Customer = type("Customer", (), {"create": staticmethod(lambda *args, **kwargs: None)})
stripe_module.Subscription = type(
    "Subscription",
    (),
    {
        "retrieve": staticmethod(lambda *args, **kwargs: None),
        "modify": staticmethod(lambda *args, **kwargs: None),
    },
)
stripe_module.checkout = SimpleNamespace(
    Session=type(
        "Session",
        (),
        {
            "create": staticmethod(lambda *args, **kwargs: None),
            "retrieve": staticmethod(lambda *args, **kwargs: None),
        },
    )
)
stripe_module.billing_portal = SimpleNamespace(
    Session=type("Session", (), {"create": staticmethod(lambda *args, **kwargs: None)})
)

requests_module = _ensure_module("requests")
requests_module.Response = type("Response", (), {})
requests_module.get = lambda *args, **kwargs: None
requests_module.post = lambda *args, **kwargs: None
requests_module.delete = lambda *args, **kwargs: None
requests_module.request = lambda *args, **kwargs: None

boto3_module = _ensure_module("boto3")
boto3_module.client = lambda *args, **kwargs: None
boto3_module.resource = lambda *args, **kwargs: None

botocore_module = _ensure_module("botocore")
botocore_exceptions_module = _ensure_module("botocore.exceptions")
botocore_exceptions_module.ClientError = type("ClientError", (Exception,), {})
botocore_module.exceptions = botocore_exceptions_module

from app import create_app
from app.video_shorts.routes import generation
from app.video_shorts.services import db as db_service
from app.video_shorts.services import compositor
from app.video_shorts.services import render_jobs, usage_metering
from app.video_shorts.services.media_utils import MediaSubprocessTimeoutError
from app.video_shorts import worker as worker_module
from app.video_shorts.worker import process_next_job


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
            CREATE TABLE IF NOT EXISTS youtube_videos (
                id BIGINT PRIMARY KEY,
                video_id VARCHAR,
                title VARCHAR,
                duration_seconds DOUBLE,
                owner_user_id VARCHAR,
                brand_id VARCHAR,
                transcript_status VARCHAR,
                fetch_transcript BOOLEAN,
                published_at TIMESTAMP,
                downloaded_at TIMESTAMP,
                last_checked_at TIMESTAMP
            )
            """
        )
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


def _insert_video(video_pk: int, source_video_id: str, owner_user_id: str) -> None:
    conn = db_service.get_db()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS youtube_videos (
                id BIGINT PRIMARY KEY,
                video_id VARCHAR,
                title VARCHAR,
                duration_seconds DOUBLE,
                owner_user_id VARCHAR,
                brand_id VARCHAR,
                transcript_status VARCHAR,
                fetch_transcript BOOLEAN,
                published_at TIMESTAMP,
                downloaded_at TIMESTAMP,
                last_checked_at TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            INSERT INTO youtube_videos (
                id,
                video_id,
                title,
                duration_seconds,
                owner_user_id,
                brand_id,
                transcript_status,
                fetch_transcript,
                published_at,
                downloaded_at,
                last_checked_at
            )
            VALUES (?, ?, ?, ?, ?, NULL, '', FALSE, NULL, NULL, NULL)
            """,
            [video_pk, source_video_id, f"Video {source_video_id}", 180.0, owner_user_id],
        )
        conn.commit()
    finally:
        conn.close()


def _write_plan(shorts_dir: Path, source_video_id: str, *, plan_index: int = 1) -> None:
    shorts_dir.mkdir(parents=True, exist_ok=True)
    plan_path = shorts_dir / f"{source_video_id}_plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "plan": [
                    {
                        "origin": "ai",
                        "plan_index": plan_index,
                        "clip_filename": f"{plan_index}_{source_video_id}.mp4",
                        "title": f"Clip {plan_index}",
                        "start": 10.0,
                        "end": 20.0,
                        "status": "queued",
                        "publish_status": "not_ready",
                    }
                ]
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


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


def test_free_plan_processing_cap_blocks_second_claim(monkeypatch, tmp_path):
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
    claimed = render_jobs.claim_next_job("worker-free-cap")
    blocked = render_jobs.enqueue_render_job(
        user_id=user_id,
        payload=_payload(3, "video-c", 3),
        input_hash=_input_hash("video-c", 3),
    )

    assert first["kind"] == "queued"
    assert second["kind"] == "queued"
    assert claimed["id"] == first["job"]["id"]
    assert blocked["kind"] == "concurrency_limit"
    assert blocked["limit"] == 1


def test_global_processing_cap_keeps_next_job_queued_without_attempt(monkeypatch, tmp_path):
    _configure_duckdb(monkeypatch, tmp_path, "global_processing_cap.duckdb")
    user_a = str(uuid4())
    user_b = str(uuid4())
    _insert_user(user_a, "plan_free")
    _insert_user(user_b, "plan_free")
    monkeypatch.setattr(worker_module, "MAX_GLOBAL_CONCURRENT_JOBS", 1)
    monkeypatch.setattr(worker_module, "disk_guard_triggered", lambda **kwargs: False)

    first = render_jobs.enqueue_render_job(
        user_id=user_a,
        payload=_payload(1, "global-a", 1),
        input_hash=_input_hash("global-a", 1),
    )
    second = render_jobs.enqueue_render_job(
        user_id=user_b,
        payload=_payload(2, "global-b", 1),
        input_hash=_input_hash("global-b", 1),
    )
    claimed = render_jobs.claim_next_job("worker-global-cap")
    assert claimed["id"] == first["job"]["id"]

    app = create_app()
    app.secret_key = "test-secret"
    processed = process_next_job(app, "worker-global-cap")
    queued_job = render_jobs.get_job(second["job"]["id"], user_id=user_b)

    assert processed is False
    assert queued_job["status"] == "queued"
    assert queued_job["attempts"] == 0


def test_worker_loop_uses_stale_job_timeout_config(monkeypatch):
    calls = []

    monkeypatch.setattr(worker_module, "STALE_JOB_TIMEOUT_SECONDS", 5400)
    monkeypatch.setattr(worker_module, "WORKER_CONCURRENCY", 1)
    monkeypatch.setattr(worker_module, "create_app", lambda: create_app())
    monkeypatch.setattr(
        worker_module,
        "requeue_timed_out_jobs",
        lambda *, timeout_seconds: calls.append(timeout_seconds),
    )
    monkeypatch.setattr(worker_module, "process_next_job", lambda app, worker_id: False)

    def _stop(_seconds):
        raise RuntimeError("stop-loop")

    monkeypatch.setattr(worker_module.time, "sleep", _stop)

    try:
        worker_module.run_worker_loop()
    except RuntimeError as exc:
        assert str(exc) == "stop-loop"

    assert calls == [5400]


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


def test_transcribe_start_refuses_over_quota_before_source_resolution(monkeypatch, tmp_path):
    _configure_duckdb(monkeypatch, tmp_path, "transcribe_quota_guard.duckdb")
    user_id = str(uuid4())
    video_pk = 303
    source_video_id = "transcribe-quota-video"

    _insert_user(user_id, "plan_free")
    _insert_video(video_pk, source_video_id, user_id)
    usage_metering.add_transcription_minutes(user_id, 45, video_id=source_video_id, video_title="Quota Video")

    conn = db_service.get_db()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS youtube_transcripts (
                id BIGINT,
                video_id VARCHAR,
                full_text VARCHAR,
                segments_json VARCHAR,
                whisper_segments_json VARCHAR
            )
            """
        )
        conn.execute(
            "UPDATE youtube_videos SET duration_seconds = ? WHERE id = ?",
            [42 * 60, video_pk],
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(generation, "disk_guard_triggered", lambda **kwargs: False)
    monkeypatch.setattr(
        generation,
        "_resolve_source_video",
        lambda current_video_id: (_ for _ in ()).throw(AssertionError("source resolution should not run when transcription quota is exhausted")),
    )

    app = create_app()
    app.secret_key = "test-secret"
    client = app.test_client()
    with client.session_transaction() as session:
        session["vs_user_id"] = user_id

    response = client.post(f"/video_shorts/generate/{video_pk}/transcribe/start")
    payload = response.get_json()

    assert response.status_code == 403
    assert payload["ok"] is False
    assert payload["message"] == "This video is 42 minutes, but you have 15 minutes of transcription left this month."


def test_autoclip_refuses_over_export_quota_before_source_resolution(monkeypatch, tmp_path):
    _configure_duckdb(monkeypatch, tmp_path, "render_quota_guard.duckdb")
    user_id = str(uuid4())
    video_pk = 404
    source_video_id = "render-quota-video"
    shorts_dir = tmp_path / "shorts"

    _insert_user(user_id, "plan_free")
    _insert_video(video_pk, source_video_id, user_id)
    _write_plan(shorts_dir, source_video_id, plan_index=1)
    for _ in range(10):
        result = usage_metering.reserve_export(user_id)
        assert result["allowed"] is True

    monkeypatch.setattr(generation, "SHORTS_DIR", shorts_dir)
    monkeypatch.setattr(generation, "_get_user_storage_usage", lambda conn, current_user_id: {"used_bytes": 0, "limit_bytes": 10 * 1024**3})
    monkeypatch.setattr(generation, "_fetch_video_with_transcript", lambda current_video_pk: (source_video_id, "Render Quota Video", 180.0, "", []))
    monkeypatch.setattr(generation, "_resolve_brand_subscribe_overlay_path", lambda brand_id: None)
    monkeypatch.setattr(generation, "load_background_preference", lambda owner_user_id, brand_id=None: None)
    monkeypatch.setattr(generation, "disk_guard_triggered", lambda **kwargs: False)
    monkeypatch.setattr(
        generation,
        "_resolve_source_video",
        lambda current_video_id: (_ for _ in ()).throw(AssertionError("source resolution should not run when export quota is exhausted")),
    )

    app = create_app()
    app.secret_key = "test-secret"
    client = app.test_client()
    with client.session_transaction() as session:
        session["vs_user_id"] = user_id

    response = client.post(
        f"/video_shorts/generate/{video_pk}/autoclip",
        data={"plan_index": "1", "title": "Clip 1"},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    payload = response.get_json()

    assert response.status_code == 403
    assert payload["success"] is False
    assert payload["code"] == "export_limit_reached"
    assert payload["message"] == "Monthly export limit reached for your plan."


def test_worker_marks_render_job_failed_when_media_timeout_raises(monkeypatch, tmp_path, caplog):
    _configure_duckdb(monkeypatch, tmp_path, "worker_timeout_failure.duckdb")
    user_id = str(uuid4())
    video_pk = 101
    source_video_id = "timeout-video"
    plan_index = 1
    shorts_dir = tmp_path / "shorts"
    source_path = tmp_path / "timeout-video.mp4"
    source_path.write_bytes(b"not-a-real-video")

    _insert_user(user_id, "plan_free")
    _insert_video(video_pk, source_video_id, user_id)
    _write_plan(shorts_dir, source_video_id, plan_index=plan_index)

    monkeypatch.setattr(generation, "SHORTS_DIR", shorts_dir)
    monkeypatch.setattr(generation, "_get_user_storage_usage", lambda conn, current_user_id: {"used_bytes": 0, "limit_bytes": 10 * 1024**3})
    monkeypatch.setattr(generation, "_fetch_video_with_transcript", lambda current_video_pk: (source_video_id, "Timeout Video", 180.0, "", []))
    monkeypatch.setattr(generation, "_resolve_source_video", lambda current_video_id: (source_path, False))
    monkeypatch.setattr(generation, "_cleanup_resolved_source_video", lambda path, is_temp: None)
    monkeypatch.setattr(generation, "_resolve_brand_subscribe_overlay_path", lambda brand_id: None)
    monkeypatch.setattr(generation, "load_background_preference", lambda owner_user_id, brand_id=None: None)
    monkeypatch.setattr(generation, "disk_guard_triggered", lambda **kwargs: False)
    monkeypatch.setattr(worker_module, "disk_guard_triggered", lambda **kwargs: False)
    monkeypatch.setattr(compositor, "_resolve_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(compositor, "_resolve_ffprobe", lambda: "ffprobe")

    def _raise_timeout(*args, **kwargs):
        logging.getLogger("app.video_shorts.services.media_utils").error(
            "Media subprocess timeout binary=ffmpeg timeout=3600s operation=compose_with_background context=output=1_timeout-video.mp4"
        )
        raise MediaSubprocessTimeoutError(
            binary="ffmpeg",
            timeout_seconds=3600,
            operation="compose_with_background",
            context="output=1_timeout-video.mp4",
        )

    monkeypatch.setattr(generation, "_compose_trimmed_with_background", _raise_timeout)
    monkeypatch.setattr(generation, "_cut_clip", _raise_timeout)

    payload = _payload(video_pk, source_video_id, plan_index)
    payload["brand_id"] = None
    payload["options"]["brand_id"] = None

    queued = render_jobs.enqueue_render_job(
        user_id=user_id,
        payload=payload,
        input_hash=_input_hash(source_video_id, plan_index),
    )
    job_id = queued["job"]["id"]

    before_snapshot = usage_metering.get_usage_snapshot(user_id)
    reserve_result = usage_metering.reserve_export(user_id)
    assert reserve_result["allowed"] is True
    reserved_snapshot = usage_metering.get_usage_snapshot(user_id)
    assert reserved_snapshot["exports"]["used"] == before_snapshot["exports"]["used"] + 1

    app = create_app()
    app.secret_key = "test-secret"

    with caplog.at_level(logging.ERROR):
        processed = process_next_job(app, "worker-timeout-test")

    assert processed is True

    failed_job = render_jobs.get_job(job_id, user_id=user_id)
    after_snapshot = usage_metering.get_usage_snapshot(user_id)
    plan_entries = generation._load_plan_entries(source_video_id)
    expected_error = (
        "This video took too long to process. Please try again. "
        "If it keeps happening, contact support."
    )

    assert failed_job["status"] == "failed"
    assert failed_job["error"] == expected_error
    assert failed_job["attempts"] == 1
    assert after_snapshot["exports"]["used"] == before_snapshot["exports"]["used"]
    assert plan_entries[0]["status"] == "failed"
    assert plan_entries[0]["render_error"] == expected_error
    assert any(
        record.levelno == logging.ERROR
        and "Media subprocess timeout" in record.getMessage()
        and "binary=ffmpeg" in record.getMessage()
        for record in caplog.records
    )


def test_worker_marks_render_job_failed_without_retry_when_disk_is_full(monkeypatch, tmp_path, caplog):
    _configure_duckdb(monkeypatch, tmp_path, "worker_enospc_failure.duckdb")
    user_id = str(uuid4())
    video_pk = 202
    source_video_id = "disk-full-video"
    plan_index = 1
    shorts_dir = tmp_path / "shorts"
    source_path = tmp_path / "disk-full-video.mp4"
    source_path.write_bytes(b"not-a-real-video")

    _insert_user(user_id, "plan_free")
    _insert_video(video_pk, source_video_id, user_id)
    _write_plan(shorts_dir, source_video_id, plan_index=plan_index)

    monkeypatch.setattr(generation, "SHORTS_DIR", shorts_dir)
    monkeypatch.setattr(generation, "_get_user_storage_usage", lambda conn, current_user_id: {"used_bytes": 0, "limit_bytes": 10 * 1024**3})
    monkeypatch.setattr(generation, "_fetch_video_with_transcript", lambda current_video_pk: (source_video_id, "Disk Full Video", 180.0, "", []))
    monkeypatch.setattr(generation, "_resolve_source_video", lambda current_video_id: (source_path, False))
    monkeypatch.setattr(generation, "_cleanup_resolved_source_video", lambda path, is_temp: None)
    monkeypatch.setattr(generation, "_resolve_brand_subscribe_overlay_path", lambda brand_id: None)
    monkeypatch.setattr(generation, "load_background_preference", lambda owner_user_id, brand_id=None: None)
    monkeypatch.setattr(generation, "disk_guard_triggered", lambda **kwargs: False)
    monkeypatch.setattr(worker_module, "disk_guard_triggered", lambda **kwargs: False)
    monkeypatch.setattr(compositor, "_resolve_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(compositor, "_resolve_ffprobe", lambda: "ffprobe")

    def _raise_enospc(*args, **kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(generation, "_compose_trimmed_with_background", _raise_enospc)
    monkeypatch.setattr(generation, "_cut_clip", _raise_enospc)

    payload = _payload(video_pk, source_video_id, plan_index)
    payload["brand_id"] = None
    payload["options"]["brand_id"] = None

    queued = render_jobs.enqueue_render_job(
        user_id=user_id,
        payload=payload,
        input_hash=_input_hash(source_video_id, plan_index),
    )
    job_id = queued["job"]["id"]

    before_snapshot = usage_metering.get_usage_snapshot(user_id)
    reserve_result = usage_metering.reserve_export(user_id)
    assert reserve_result["allowed"] is True

    app = create_app()
    app.secret_key = "test-secret"

    with caplog.at_level(logging.ERROR):
        processed = process_next_job(app, "worker-enospc-test")

    assert processed is True

    failed_job = render_jobs.get_job(job_id, user_id=user_id)
    after_snapshot = usage_metering.get_usage_snapshot(user_id)
    plan_entries = generation._load_plan_entries(source_video_id)
    expected_error = "The system is busy right now. Please try again in a few minutes."

    assert failed_job["status"] == "failed"
    assert failed_job["error"] == expected_error
    assert failed_job["attempts"] == 1
    assert after_snapshot["exports"]["used"] == before_snapshot["exports"]["used"]
    assert plan_entries[0]["status"] == "failed"
    assert plan_entries[0]["render_error"] == expected_error
    assert any(
        record.levelno == logging.ERROR
        and "Job failed without retry" in record.getMessage()
        for record in caplog.records
    )
