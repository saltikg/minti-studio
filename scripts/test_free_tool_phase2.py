#!/usr/bin/env python3
"""Technical checks for free-tool guest Phase 2."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

tmp_dir = tempfile.TemporaryDirectory()
db_path = Path(tmp_dir.name) / "free_tool_phase2.duckdb"
os.environ["VIDEO_SHORTS_DB_BACKEND"] = "duckdb"
os.environ["VIDEO_SHORTS_DB"] = str(db_path)
os.environ["SECRET_KEY"] = "test-secret"
os.environ["FREE_TOOL_GUEST_RENDER_LIMIT"] = "3"
os.environ["FREE_TOOL_DAILY_RENDER_CAP"] = "10"

from app import create_app
from app.video_shorts.routes import free_tool
from app.video_shorts.routes import generation
from app.video_shorts.services.brands import ensure_brand_schema
from app.video_shorts.services.db import ensure_auth_user_schema, ensure_storage_user_schema, get_db
from app.video_shorts.services.render_jobs import (
    AUTO_RENDER_JOB_ORIGIN,
    DISCOVERY_JOB_ORIGIN,
    GUEST_JOB_ORIGIN,
    JOB_STATUS_DONE,
    JOB_TYPE_RENDER_SHORT,
    cancel_job,
    claim_next_job,
    enqueue_job,
    enqueue_render_job,
    ensure_render_jobs_schema,
    get_job,
    mark_job_done,
    mark_job_failed,
)


def _create_user(conn, *, guest: bool = False) -> str:
    user_id = str(uuid4())
    conn.execute(
        """
        INSERT INTO shorts_users (
            id, username, email, password_hash, name, role, email_verified,
            is_guest, guest_created_at, guest_video_count, guest_render_count
        )
        VALUES (?, ?, ?, NULL, ?, 'member', TRUE, ?, now(), 0, 0)
        """,
        [
            user_id,
            f"{user_id}@example.com",
            f"{user_id}@example.com",
            "Guest" if guest else "User",
            bool(guest),
        ],
    )
    return user_id


def _bootstrap_db() -> tuple[str, str, str]:
    conn = get_db()
    try:
        ensure_storage_user_schema(conn)
        ensure_auth_user_schema(conn)
        ensure_brand_schema(conn)
        ensure_render_jobs_schema(conn)
        conn.execute(
            """
            CREATE SEQUENCE IF NOT EXISTS youtube_videos_id_seq START 1
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS youtube_videos (
                id INTEGER PRIMARY KEY DEFAULT nextval('youtube_videos_id_seq'),
                video_id VARCHAR,
                title VARCHAR,
                duration_seconds DOUBLE,
                owner_user_id VARCHAR,
                brand_id VARCHAR,
                download_status VARCHAR,
                transcript_status VARCHAR
            )
            """
        )
        guest_user_id = _create_user(conn, guest=True)
        normal_user_id = _create_user(conn, guest=False)
        brand_id = str(uuid4())
        conn.execute(
            """
            INSERT INTO shorts_brands (id, owner_user_id, name, created_at, updated_at)
            VALUES (?, ?, 'Guest Brand', now(), now())
            """,
            [brand_id, guest_user_id],
        )
        conn.commit()
        return guest_user_id, normal_user_id, brand_id
    finally:
        conn.close()


def _enqueue_for_order(user_id: str, suffix: str, origin: str | None = None):
    payload = {"source_video_id": suffix}
    if origin:
        payload["job_origin"] = origin
    return enqueue_job(
        user_id=user_id,
        job_type=JOB_TYPE_RENDER_SHORT,
        payload=payload,
        input_hash=f"order:{suffix}",
    )["job"]["id"]


def _assert_claim_order(customer_user: str, guest_user: str) -> None:
    customer_id = _enqueue_for_order(customer_user, "customer")
    auto_id = _enqueue_for_order(customer_user, "auto", AUTO_RENDER_JOB_ORIGIN)
    guest_id = _enqueue_for_order(guest_user, "guest")
    discovery_id = _enqueue_for_order(customer_user, "discovery", DISCOVERY_JOB_ORIGIN)

    expected = [customer_id, auto_id, guest_id, discovery_id]
    actual: list[str] = []
    for index in range(4):
        job = claim_next_job(f"phase2-test-{index}")
        assert job, "claim_next_job returned no job"
        actual.append(job["id"])
        mark_job_done(job["id"], {"test": True})
    assert actual == expected, actual


def _assert_guest_origin(guest_user: str) -> None:
    job = enqueue_job(
        user_id=guest_user,
        job_type="ingest_youtube",
        payload={"video_pk": 1},
        input_hash="guest-origin-ingest",
    )["job"]
    payload = (get_job(job["id"]) or {}).get("payload") or {}
    assert payload.get("job_origin") == GUEST_JOB_ORIGIN, payload


def _assert_guest_auto_top_one() -> None:
    entries = [
        {"plan_index": 1, "origin": "ai", "score": 78, "status": "pending"},
        {"plan_index": 2, "origin": "ai", "score": 82, "status": "pending"},
        {"plan_index": 3, "origin": "ai", "score": 65, "status": "pending"},
    ]
    selected = generation._select_auto_render_entries(entries, min_score=50, limit=1)
    assert [entry["plan_index"] for entry in selected] == [2], selected


def _assert_render_limit_and_refund(app, guest_user: str) -> None:
    with app.test_request_context("/video_shorts/generate/1/autoclip", method="POST", headers={"X-Requested-With": "XMLHttpRequest"}):
        from flask import g

        g.vs_current_user = {"id": guest_user, "is_guest": True}
        for expected_left in [2, 1, 0]:
            result = generation._reserve_guest_render(guest_user)
            assert result["allowed"] is True, result
            assert result["renders_left"] == expected_left, result
        gate = generation._reserve_guest_render(guest_user)
        assert gate["allowed"] is False and gate["reason"] == "render_limit", gate
        response, status = generation._guest_gate_response("create_clip", video_pk=1, clip=7, reason=gate["reason"], json_only=True)
        body = response.get_json()
        assert status == 403 and body["gate"] == "signup" and body["renders_left"] == 0 and body["action"] == "create_clip", body

    conn = get_db()
    try:
        conn.execute(
            """
            UPDATE shorts_users
            SET guest_render_count = 1
            WHERE id = ?
            """,
            [guest_user],
        )
        conn.commit()
    finally:
        conn.close()
    job = enqueue_render_job(
        user_id=guest_user,
        payload={"guest_render_reserved": True, "source_video_id": "refund"},
        input_hash="guest-refund-render",
    )["job"]
    mark_job_failed(job["id"], "synthetic failure")
    assert generation._guest_renders_left_for_user(guest_user) == 3


def _assert_daily_cap(app, guest_user: str) -> None:
    old_cap = generation.FREE_TOOL_DAILY_RENDER_CAP
    conn = get_db()
    try:
        existing_count = generation._guest_daily_render_count(conn)
        conn.execute("UPDATE shorts_users SET guest_render_count = 0 WHERE id = ?", [guest_user])
        conn.commit()
    finally:
        conn.close()
    generation.FREE_TOOL_DAILY_RENDER_CAP = existing_count + 1
    try:
        enqueue_render_job(
            user_id=guest_user,
            payload={"source_video_id": "daily-1"},
            input_hash="daily-1",
        )
        assert generation._reserve_guest_render(guest_user)["allowed"] is True
        enqueue_render_job(
            user_id=guest_user,
            payload={"source_video_id": "daily-2"},
            input_hash="daily-2",
        )
        blocked = generation._reserve_guest_render(guest_user)
        assert blocked["allowed"] is False and blocked["reason"] == "daily_cap", blocked
    finally:
        generation.FREE_TOOL_DAILY_RENDER_CAP = old_cap


def _assert_daily_video_count() -> None:
    conn = get_db()
    try:
        before = free_tool._count_recent_guest_videos(conn)
        conn.execute(
            """
            INSERT INTO shorts_users (
                id, username, email, password_hash, name, role, email_verified,
                is_guest, guest_created_at, guest_video_count, guest_render_count
            )
            VALUES (?, ?, ?, NULL, 'Guest Video Cap', 'member', TRUE, TRUE, now(), 2, 0)
            """,
            [str(uuid4()), "video-cap@example.com", "video-cap@example.com"],
        )
        conn.commit()
        after = free_tool._count_recent_guest_videos(conn)
        assert after == before + 2, (before, after)
    finally:
        conn.close()


def _assert_guest_gates(app, guest_user: str, brand_id: str) -> None:
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["vs_user_id"] = guest_user
        sess["vs_brand_id"] = brand_id

    ai = client.post("/video_shorts/generate/999/create_plan/start", headers={"X-Requested-With": "XMLHttpRequest"})
    assert ai.status_code == 403 and ai.get_json()["action"] == "ai_suggest"

    schedule = client.post(
        "/video_shorts/youtube/upload_clip",
        data={"video_pk": "999", "plan_index": "3"},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert schedule.status_code == 403 and schedule.get_json()["action"] == "schedule"
    with app.test_request_context("/video_shorts/generate/999", method="POST", headers={"X-Requested-With": "XMLHttpRequest"}):
        from flask import g

        g.vs_current_user = {"id": guest_user, "is_guest": True}
        response, status = generation._guest_gate_response("schedule_all", video_pk=999, reason="guest_action", json_only=True)
        assert status == 403 and response.get_json()["action"] == "schedule_all"

    download = client.get("/video_shorts/generate/999/clip/3/download", follow_redirects=False)
    assert download.status_code in {302, 303}, download.status_code
    assert "/video_shorts/register" in download.headers["Location"]
    assert "action=download" in download.headers["Location"]
    assert "clip=3" in download.headers["Location"]

    plain_save = client.post(
        "/video_shorts/generate/999/clip/3/edit-title-time",
        data={"title": "Plain save", "start": "0", "end": "10"},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert plain_save.status_code == 404, plain_save.status_code


def _assert_non_guest_untouched(normal_user: str) -> None:
    assert generation._reserve_guest_render(normal_user)["guest"] is False


def main() -> None:
    app = create_app()
    app.config["TESTING"] = True
    app.secret_key = "test-secret"
    guest_user, normal_user, brand_id = _bootstrap_db()

    _assert_claim_order(normal_user, guest_user)
    _assert_guest_origin(guest_user)
    _assert_guest_auto_top_one()
    _assert_render_limit_and_refund(app, guest_user)
    _assert_daily_cap(app, guest_user)
    _assert_daily_video_count()
    _assert_guest_gates(app, guest_user, brand_id)
    _assert_non_guest_untouched(normal_user)

    print("free_tool_phase2: PASS")
    print("claim_order=customer>auto>guest>discovery")
    print("guest_origin=ok auto_top_one=ok render_limit_gate=ok failed_refund=ok daily_cap=ok")
    print("ai_suggest/download/schedule_gates=ok non_guest_unchanged=ok")


if __name__ == "__main__":
    try:
        main()
    finally:
        tmp_dir.cleanup()
