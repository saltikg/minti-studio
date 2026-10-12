#!/usr/bin/env python3
"""Technical checks for free-tool guest Phase 1."""

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
db_path = Path(tmp_dir.name) / "free_tool_phase1.duckdb"
os.environ["VIDEO_SHORTS_DB_BACKEND"] = "duckdb"
os.environ["VIDEO_SHORTS_DB"] = str(db_path)
os.environ["SECRET_KEY"] = "test-secret"
os.environ["FREE_TOOL_ENABLED"] = "0"
os.environ["FREE_TOOL_PREVIEW_TOKEN"] = "preview-secret"
os.environ.setdefault("TURNSTILE_SITE_KEY", "test-site-key")
os.environ.setdefault("TURNSTILE_SECRET_KEY", "test-secret-key")

from werkzeug.security import generate_password_hash

from app import create_app
from app.video_shorts.routes import auth as auth_routes
from app.video_shorts.routes import free_tool
from app.video_shorts.services.db import get_db


def _ensure_minimal_video_tables() -> None:
    conn = get_db()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS youtube_channels (
                channel_id INTEGER PRIMARY KEY,
                channel_name VARCHAR,
                channel_url VARCHAR,
                notes VARCHAR,
                owner_user_id VARCHAR,
                youtube_channel_id VARCHAR,
                is_active BOOLEAN,
                brand_id VARCHAR
            )
            """
        )
        conn.execute(
            """
            CREATE SEQUENCE IF NOT EXISTS youtube_videos_id_seq START 1
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS youtube_videos (
                id INTEGER PRIMARY KEY DEFAULT nextval('youtube_videos_id_seq'),
                channel_id INTEGER,
                video_id VARCHAR,
                title VARCHAR,
                published_at TIMESTAMP,
                thumbnail_url VARCHAR,
                fetch_transcript BOOLEAN,
                duration_seconds DOUBLE,
                view_count BIGINT,
                like_count BIGINT,
                comment_count BIGINT,
                video_url VARCHAR,
                owner_user_id VARCHAR,
                brand_id VARCHAR,
                download_status VARCHAR,
                transcript_status VARCHAR,
                subtitle_style VARCHAR
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def _patch_free_tool() -> list[dict]:
    enqueued: list[dict] = []
    free_tool._verify_turnstile_or_fail = lambda: True
    auth_routes._verify_turnstile_or_fail = lambda: True
    free_tool.fetch_video_metadata = lambda video_id: {
        "video_id": video_id,
        "title": f"Video {video_id}",
        "thumbnail_url": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
        "duration_seconds": 600,
        "channel_title": "Test Channel",
        "channel_id": "channel-1",
    }

    def _fake_enqueue_job(**kwargs):
        enqueued.append(kwargs)
        return {"kind": "created", "job": {"id": f"job-{len(enqueued)}", "status": "queued"}}

    free_tool.enqueue_job = _fake_enqueue_job
    return enqueued


def _post_video(client, url: str = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"):
    return client.post(
        "/tools/youtube-to-shorts",
        data={"video_url": url, "cf-turnstile-response": "ok"},
        follow_redirects=False,
    )


def main() -> None:
    app = create_app()
    app.config["TESTING"] = True
    app.secret_key = "test-secret"
    enqueued = _patch_free_tool()
    _ensure_minimal_video_tables()
    client = app.test_client()

    disabled = client.get("/tools/youtube-to-shorts")
    assert disabled.status_code == 404, disabled.status_code

    preview = client.get("/tools/youtube-to-shorts?preview=preview-secret")
    assert preview.status_code == 200, preview.status_code
    assert b"YouTube to Shorts" in preview.data

    free_tool.fetch_video_metadata = lambda video_id: {
        "video_id": video_id,
        "title": "Long Video",
        "duration_seconds": 31 * 60,
        "channel_id": "channel-long",
    }
    too_long = _post_video(client)
    assert too_long.status_code == 200
    assert b"30 min" in too_long.data
    assert not enqueued, enqueued

    free_tool.fetch_video_metadata = lambda video_id: {
        "video_id": video_id,
        "title": "Short Video",
        "duration_seconds": 600,
        "channel_id": "channel-short",
        "thumbnail_url": "https://example.com/thumb.jpg",
    }
    first = _post_video(client)
    assert first.status_code in {302, 303}, first.status_code
    assert "/video_shorts/generate/" in first.headers["Location"]
    assert len(enqueued) == 1

    second = _post_video(client, "https://www.youtube.com/watch?v=ScMzIvxBSi4")
    assert second.status_code in {302, 303}, second.status_code
    assert "/video_shorts/register" in second.headers["Location"]
    assert "action=new_video" in second.headers["Location"]

    my_videos = client.get("/video_shorts/my-videos")
    assert my_videos.status_code in {302, 303}
    assert my_videos.headers["Location"].endswith("/tools/youtube-to-shorts")

    json_forbidden = client.post("/video_shorts/api/generated-shorts/share-link", json={})
    assert json_forbidden.status_code == 403
    assert json_forbidden.get_json()["success"] is False

    conn = get_db()
    try:
        guest_id = str(uuid4())
        conn.execute(
            """
            INSERT INTO shorts_users (
                id, username, email, password_hash, name, role, email_verified,
                is_guest, guest_created_at, guest_video_count, guest_render_count
            )
            VALUES (?, ?, ?, ?, 'Guest Login', 'member', TRUE, TRUE, now(), 0, 0)
            """,
            [guest_id, "guest@example.com", "guest@example.com", generate_password_hash("password123")],
        )
        conn.commit()
    finally:
        conn.close()

    login_attempt = client.post(
        "/video_shorts/login",
        data={"username": "guest@example.com", "password": "password123", "cf-turnstile-response": "ok"},
    )
    assert login_attempt.status_code == 200
    assert b"Account not found" in login_attempt.data

    print("free_tool_phase1: PASS")
    print("landing_disabled=404 preview=200 long_video_rejected_before_enqueue=ok")
    print("second_video_redirect=ok guest_allowlist=ok guest_login_blocked=ok")


if __name__ == "__main__":
    try:
        main()
    finally:
        tmp_dir.cleanup()
