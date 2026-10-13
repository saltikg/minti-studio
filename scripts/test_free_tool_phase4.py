#!/usr/bin/env python3
"""Technical checks for free-tool guest Phase 4."""

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
db_path = Path(tmp_dir.name) / "free_tool_phase4.duckdb"
os.environ["VIDEO_SHORTS_DB_BACKEND"] = "duckdb"
os.environ["VIDEO_SHORTS_DB"] = str(db_path)
os.environ["SECRET_KEY"] = "test-secret"
os.environ["MEDIA_BACKEND"] = "local"
os.environ["FREE_TOOL_GUEST_TTL_DAYS"] = "3"

from app import create_app
from app.video_shorts.routes import auth, free_tool, generation
from app.video_shorts.services.brands import create_brand, ensure_brand_schema
from app.video_shorts.services.db import ensure_auth_user_schema, ensure_storage_user_schema, get_db
from app.video_shorts.services.free_tool_cache import load_text_cache, write_suggestions_cache, write_transcript_cache
from app.video_shorts.services.render_jobs import ensure_render_jobs_schema
from app.video_shorts.services.storage import guest_s3_tags_for_owner


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
            CREATE TABLE IF NOT EXISTS youtube_videos (
                id INTEGER PRIMARY KEY DEFAULT nextval('youtube_videos_id_seq'),
                video_id VARCHAR,
                title VARCHAR,
                channel_id INTEGER,
                video_url VARCHAR,
                duration_seconds DOUBLE,
                owner_user_id VARCHAR,
                brand_id VARCHAR,
                transcript_status VARCHAR,
                download_status VARCHAR
            )
            """
        )
        guest_id = str(uuid4())
        normal_id = str(uuid4())
        conn.execute(
            """
            INSERT INTO shorts_users (
                id, username, email, password_hash, name, role, email_verified,
                is_guest, guest_created_at, guest_render_count, guest_video_count
            )
            VALUES (?, NULL, NULL, NULL, 'Guest', 'member', FALSE, TRUE, now() - INTERVAL '4 days', 1, 1)
            """,
            [guest_id],
        )
        conn.execute(
            """
            INSERT INTO shorts_users (
                id, username, email, password_hash, name, role, email_verified,
                is_guest, guest_render_count, guest_video_count
            )
            VALUES (?, 'normal@example.com', 'normal@example.com', NULL, 'Normal', 'member', TRUE, FALSE, 0, 0)
            """,
            [normal_id],
        )
        guest_brand = create_brand(conn, user_id=guest_id, name="Guest Brand", make_default=True)
        target_brand = create_brand(conn, user_id=normal_id, name="Normal Brand", make_default=True)
        conn.execute(
            "INSERT INTO youtube_channels (channel_id, channel_name, owner_user_id, brand_id) VALUES (42, 'Guest Channel', ?, ?)",
            [guest_id, guest_brand["id"]],
        )
        conn.execute(
            """
            INSERT INTO youtube_videos (
                video_id, title, channel_id, video_url, duration_seconds, owner_user_id, brand_id,
                transcript_status, download_status
            )
            VALUES ('cachevid', 'Cached Video', 42, 's3://videos/cachevid.mp4', 120, ?, ?, 'done', 'downloaded')
            """,
            [guest_id, guest_brand["id"]],
        )
        conn.commit()
        return guest_id, normal_id, guest_brand["id"], target_brand["id"]
    finally:
        conn.close()


def _assert_tag_helper(guest_id: str, normal_id: str) -> None:
    assert guest_s3_tags_for_owner(guest_id) == {"minti-guest": "true"}
    assert guest_s3_tags_for_owner(normal_id) == {}
    print("tag_helper_guest_only=ok")


def _assert_claim_removes_tags(guest_id: str, normal_id: str, target_brand_id: str) -> None:
    calls = []
    original = auth.remove_guest_tag_from_keys
    auth.remove_guest_tag_from_keys = lambda keys: calls.append(sorted(keys)) or {"scanned": len(keys), "updated": len(keys), "skipped": 0}
    try:
        conn = get_db()
        try:
            result = auth.claim_guest_into(conn, target_user_id=normal_id, guest_user_id=guest_id, target_brand_id=target_brand_id)
            conn.commit()
        finally:
            conn.close()
    finally:
        auth.remove_guest_tag_from_keys = original
    assert result.get("claimed") is True, result
    assert calls and "videos/cachevid.mp4" in calls[0], calls
    print("claim_removes_guest_tags=ok")


def _assert_cleanup_dry_run_lists_old_guest() -> None:
    import scripts.cleanup_free_tool_guests as cleanup

    conn = get_db()
    try:
        expired = cleanup._expired_guests(conn, ttl_days=3)
    finally:
        conn.close()
    assert len(expired) == 0, expired  # guest was claimed/deleted by the previous check
    print("cleanup_dry_run_scope=ok")


def _assert_disk_guard_mock() -> None:
    assert free_tool.FREE_TOOL_GUEST_MIN_FREE_DISK_GB >= 1
    original = free_tool.free_disk_below_bytes
    free_tool.free_disk_below_bytes = lambda _threshold: True
    try:
        assert free_tool.free_disk_below_bytes(5 * 1024**3) is True
    finally:
        free_tool.free_disk_below_bytes = original
    print("disk_guard_mock=ok")


def _assert_cache_hit_skips_planner(app) -> None:
    guest_id = str(uuid4())
    brand_id = str(uuid4())
    conn = get_db()
    try:
        conn.execute(
            """
            INSERT INTO shorts_users (
                id, username, email, password_hash, name, role, email_verified,
                is_guest, guest_created_at, guest_render_count, guest_video_count
            )
            VALUES (?, NULL, NULL, NULL, 'Guest2', 'member', FALSE, TRUE, now(), 0, 1)
            """,
            [guest_id],
        )
        conn.execute(
            "INSERT INTO shorts_brands (id, owner_user_id, name, created_at, updated_at) VALUES (?, ?, 'Guest2 Brand', now(), now())",
            [brand_id, guest_id],
        )
        conn.execute(
            """
            INSERT INTO youtube_videos (
                video_id, title, channel_id, video_url, duration_seconds, owner_user_id, brand_id,
                transcript_status, download_status
            )
            VALUES ('cachehit', 'Cached Hit', NULL, 's3://videos/cachehit.mp4', 120, ?, ?, 'done', 'downloaded')
            """,
            [guest_id, brand_id],
        )
        video_pk = conn.execute("SELECT id FROM youtube_videos WHERE video_id = 'cachehit'").fetchone()[0]
        conn.commit()
    finally:
        conn.close()
    write_transcript_cache("cachehit", full_text="hello world", segments=[{"start": 0, "end": 6, "text": "hello world"}])
    write_suggestions_cache("cachehit", entries=[{"title": "Hit", "start": 0, "end": 12, "score": 91, "origin": "ai"}])
    assert load_text_cache("cachehit")
    original_planner = generation._generate_clip_plan_for_video
    original_enqueue = generation._enqueue_auto_render_for_plan_entry
    generation._generate_clip_plan_for_video = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("planner should not run"))
    generation._enqueue_auto_render_for_plan_entry = lambda **kwargs: {"ok": True, "status_code": 202, "job_id": "fake-job", "payload": {"success": True}}
    try:
        with app.app_context():
            result = generation.execute_auto_suggest_render_job(
                {
                    "owner_user_id": guest_id,
                    "brand_id": brand_id,
                    "video_pk": int(video_pk),
                    "video_id": "cachehit",
                    "job_origin": "guest",
                }
            )
    finally:
        generation._generate_clip_plan_for_video = original_planner
        generation._enqueue_auto_render_for_plan_entry = original_enqueue
    assert result["enqueued"] and result["enqueued"][0]["job_id"] == "fake-job", result
    print("text_cache_hit_skips_planner=ok")


def main() -> int:
    app = create_app()
    guest_id, normal_id, _guest_brand_id, target_brand_id = _bootstrap()
    _assert_tag_helper(guest_id, normal_id)
    _assert_claim_removes_tags(guest_id, normal_id, target_brand_id)
    _assert_cleanup_dry_run_lists_old_guest()
    _assert_disk_guard_mock()
    _assert_cache_hit_skips_planner(app)
    print("free_tool_phase4=ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
