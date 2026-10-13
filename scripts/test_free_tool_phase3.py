#!/usr/bin/env python3
"""Technical checks for free-tool guest Phase 3."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

from werkzeug.security import generate_password_hash

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

tmp_dir = tempfile.TemporaryDirectory()
db_path = Path(tmp_dir.name) / "free_tool_phase3.duckdb"
os.environ["VIDEO_SHORTS_DB_BACKEND"] = "duckdb"
os.environ["VIDEO_SHORTS_DB"] = str(db_path)
os.environ["SECRET_KEY"] = "test-secret"
os.environ["SIGNUPS_ENABLED"] = "1"

from app import create_app
from app.video_shorts.routes import auth
from app.video_shorts.routes import generation
from app.video_shorts.services.brands import create_brand, ensure_brand_for_user, ensure_brand_schema
from app.video_shorts.services.db import ensure_auth_user_schema, ensure_storage_user_schema, get_db
from app.video_shorts.services.render_jobs import JOB_TYPE_RENDER_SHORT, enqueue_render_job, ensure_render_jobs_schema, get_job
from app.video_shorts.services.usage_metering import USAGE_TABLE, ensure_usage_metering_schema


def _bootstrap() -> tuple[str, str, str, str]:
    conn = get_db()
    try:
        ensure_storage_user_schema(conn)
        ensure_auth_user_schema(conn)
        ensure_brand_schema(conn)
        ensure_render_jobs_schema(conn)
        conn.execute("CREATE SEQUENCE IF NOT EXISTS youtube_videos_id_seq START 1")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS youtube_videos (
                id INTEGER PRIMARY KEY DEFAULT nextval('youtube_videos_id_seq'),
                video_id VARCHAR,
                title VARCHAR,
                channel_id INTEGER,
                owner_user_id VARCHAR,
                brand_id VARCHAR
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS youtube_channels (
                channel_id INTEGER PRIMARY KEY,
                channel_name VARCHAR,
                owner_user_id VARCHAR,
                brand_id VARCHAR
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS shorts_generated_videos (
                id VARCHAR PRIMARY KEY,
                source_video_id VARCHAR,
                plan_index INTEGER,
                user_id VARCHAR,
                brand_id VARCHAR,
                video_filename VARCHAR
            )
            """
        )
        guest_id = str(uuid4())
        target_id = str(uuid4())
        conn.execute(
            """
            INSERT INTO shorts_users (
                id, username, email, password_hash, name, role, email_verified,
                is_guest, guest_created_at, guest_render_count, guest_video_count
            )
            VALUES (?, NULL, NULL, NULL, 'Guest', 'member', FALSE, TRUE, now(), 2, 1)
            """,
            [guest_id],
        )
        conn.execute(
            """
            INSERT INTO shorts_users (
                id, username, email, password_hash, name, role, email_verified,
                is_guest, guest_render_count, guest_video_count
            )
            VALUES (?, 'real@example.com', 'real@example.com', ?, 'Real User', 'member', TRUE, FALSE, 0, 0)
            """,
            [target_id, generate_password_hash("correct horse battery staple")],
        )
        guest_brand = create_brand(conn, user_id=guest_id, name="Guest Brand", make_default=True)
        target_brand = create_brand(conn, user_id=target_id, name="Real Brand", make_default=True)
        conn.execute(
            "INSERT INTO youtube_channels (channel_id, channel_name, owner_user_id, brand_id) VALUES (101, 'Claimed Channel', ?, ?)",
            [guest_id, guest_brand["id"]],
        )
        conn.execute(
            "INSERT INTO youtube_videos (video_id, title, channel_id, owner_user_id, brand_id) VALUES ('abc123', 'Guest Video', 101, ?, ?)",
            [guest_id, guest_brand["id"]],
        )
        conn.execute(
            "INSERT INTO shorts_generated_videos (id, source_video_id, plan_index, user_id, brand_id, video_filename) VALUES (?, 'abc123', 4, ?, ?, 'x.mp4')",
            [str(uuid4()), guest_id, guest_brand["id"]],
        )
        conn.commit()
        return guest_id, target_id, guest_brand["id"], target_brand["id"]
    finally:
        conn.close()


def _assert_move_claim(guest_id: str, target_id: str, target_brand_id: str) -> None:
    job = enqueue_render_job(
        user_id=guest_id,
        payload={"owner_user_id": guest_id, "brand_id": "old-brand", "source_video_id": "abc123"},
        input_hash="phase3-guest-job",
    )["job"]
    conn = get_db()
    try:
        result = auth.claim_guest_into(conn, target_user_id=target_id, guest_user_id=guest_id, target_brand_id=target_brand_id)
        conn.commit()
        assert result["claimed"] is True, result
        assert conn.execute("SELECT COUNT(*) FROM shorts_users WHERE id = ?", [guest_id]).fetchone()[0] == 0
        video_owner = conn.execute("SELECT owner_user_id, brand_id FROM youtube_videos WHERE video_id = 'abc123'").fetchone()
        assert str(video_owner[0]) == target_id and str(video_owner[1]) == target_brand_id, video_owner
    finally:
        conn.close()
    moved_job = get_job(job["id"], user_id=target_id)
    assert moved_job, "render job was not re-pointed"
    assert moved_job["payload"].get("owner_user_id") == target_id, moved_job["payload"]
    conn = get_db()
    try:
        second = auth.claim_guest_into(conn, target_user_id=target_id, guest_user_id=guest_id, target_brand_id=target_brand_id)
        conn.commit()
        assert second["claimed"] is False, second
        target_row = conn.execute(
            """
            SELECT service_mode, COALESCE(onboarding_dismissed, FALSE), COALESCE(guest_video_count, 0)
            FROM shorts_users
            WHERE id = ?
            """,
            [target_id],
        ).fetchone()
        assert target_row and target_row[0] == "self" and target_row[1] is True and int(target_row[2] or 0) >= 1, target_row
    finally:
        conn.close()


def _assert_in_place_register(app) -> None:
    guest_id = str(uuid4())
    conn = get_db()
    try:
        ensure_brand_schema(conn)
        conn.execute(
            """
            INSERT INTO shorts_users (
                id, username, email, password_hash, name, role, email_verified,
                is_guest, guest_created_at, guest_render_count, guest_video_count
            )
            VALUES (?, NULL, NULL, NULL, 'Guest', 'member', FALSE, TRUE, now(), 1, 1)
            """,
            [guest_id],
        )
        brand = ensure_brand_for_user(conn, user_id=guest_id, user_name="Guest")
        conn.execute(
            "INSERT INTO youtube_channels (channel_id, channel_name, owner_user_id, brand_id) VALUES (202, 'In Place Channel', ?, ?)",
            [guest_id, brand["id"]],
        )
        conn.execute(
            "INSERT INTO youtube_videos (video_id, title, channel_id, owner_user_id, brand_id) VALUES ('inplace123', 'Guest Video', 202, ?, ?)",
            [guest_id, brand["id"]],
        )
        ensure_usage_metering_schema(conn)
        conn.execute(
            f"UPDATE {USAGE_TABLE} SET exports_used = 2 WHERE user_id = ?",
            [guest_id],
        )
        conn.commit()
    finally:
        conn.close()
    auth._verify_turnstile_or_fail = lambda: True
    auth.send_verification_email = lambda **kwargs: None
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["vs_user_id"] = guest_id
        sess["vs_brand_id"] = brand["id"]
    response = client.post(
        "/video_shorts/register?next=/video_shorts/generate/99&action=download&clip=4",
        data={
            "email": "guest-claim@example.com",
            "password": "correct horse battery staple",
            "password_confirm": "correct horse battery staple",
            "next": "/video_shorts/generate/99",
            "action": "download",
            "clip": "4",
        },
        follow_redirects=False,
    )
    assert response.status_code in {302, 303}, response.status_code
    conn = get_db()
    try:
        row = conn.execute(
            """
            SELECT COALESCE(is_guest, FALSE), email, guest_resume_next, guest_resume_action,
                   guest_resume_clip, service_mode, COALESCE(onboarding_dismissed, FALSE),
                   COALESCE(guest_video_count, 0)
            FROM shorts_users
            WHERE id = ?
            """,
            [guest_id],
        ).fetchone()
        assert row and row[0] is False and row[1] == "guest-claim@example.com", row
        assert row[2] == "/video_shorts/generate/99" and row[3] == "download" and row[4] == "4", row
        assert row[5] == "self" and row[6] is True and int(row[7] or 0) == 1, row
        brand_row = conn.execute("SELECT name FROM shorts_brands WHERE id = ?", [brand["id"]]).fetchone()
        assert brand_row and brand_row[0] == "In Place Channel", brand_row
        usage_row = conn.execute(f"SELECT exports_used FROM {USAGE_TABLE} WHERE user_id = ?", [guest_id]).fetchone()
        assert usage_row and int(usage_row[0] or 0) == 0, usage_row
    finally:
        conn.close()


def _assert_auth_resume_and_open_redirect(app) -> None:
    with app.test_request_context("/video_shorts/login?next=https://evil.example/x&action=download&clip=7"):
        assert auth._normalize_next_url("https://evil.example/x") is None
        assert auth._normalize_next_url("//evil.example/x") is None
        assert auth._guest_resume_route_kwargs()["action"] == "download"
        assert auth._guest_resume_route_kwargs()["clip"] == "7"
    with app.test_request_context("/video_shorts/login/google?next=/video_shorts/generate/99&action=schedule&clip=2"):
        assert auth._guest_resume_route_kwargs()["next"] == "/video_shorts/generate/99"


def _assert_gate_handler_wired() -> None:
    template = (ROOT / "app/video_shorts/templates/generate_short.html").read_text()
    assert "window.handleGuestGatePayload" in template
    assert "payload.gate === \"signup\"" in template
    assert "free-tool-gate-modal" in template
    assert "runGuestResumeAction" in template


def _assert_guest_generate_v2_and_no_modal(app) -> None:
    guest_id = str(uuid4())
    conn = get_db()
    try:
        ensure_brand_schema(conn)
        conn.execute(
            """
            INSERT INTO shorts_users (
                id, username, email, password_hash, name, role, email_verified,
                is_guest, guest_created_at, guest_render_count, guest_video_count
            )
            VALUES (?, NULL, NULL, NULL, 'Guest', 'member', FALSE, TRUE, now(), 1, 1)
            """,
            [guest_id],
        )
        brand = ensure_brand_for_user(conn, user_id=guest_id, user_name="Guest")
        conn.commit()
    finally:
        conn.close()
    assert generation._owner_is_free_tool_user(guest_id) is True
    with app.test_request_context("/video_shorts/generate/123"):
        from flask import session

        session["vs_user_id"] = guest_id
        session["vs_brand_id"] = brand["id"]
        context = auth._build_service_mode_context()
        assert context["show_modal"] is False, context

    template = (ROOT / "app/video_shorts/templates/generate_short.html").read_text()
    assert 'data-generate-v2="1"' in template
    assert "Finding the best moments…" in template
    assert "Preparing your transcript…" in template


def main() -> None:
    app = create_app()
    guest_id, target_id, _guest_brand_id, target_brand_id = _bootstrap()
    with app.app_context():
        _assert_move_claim(guest_id, target_id, target_brand_id)
        _assert_in_place_register(app)
        _assert_auth_resume_and_open_redirect(app)
        _assert_gate_handler_wired()
        _assert_guest_generate_v2_and_no_modal(app)
    print("FREE_TOOL_PHASE3_OK")


if __name__ == "__main__":
    main()
