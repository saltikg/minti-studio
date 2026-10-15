#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

import duckdb
from flask import Flask

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.video_shorts.routes import channels, generation  # noqa: E402


def _test_successful_render_record(app: Flask) -> None:
    calls = []
    original_asset_upsert = generation._upsert_storage_asset
    original_lifecycle_sync = generation._sync_generated_video_from_plan_entry
    generation._upsert_storage_asset = lambda *args, **kwargs: calls.append(("asset", args, kwargs))
    generation._sync_generated_video_from_plan_entry = lambda **kwargs: calls.append(("lifecycle", (), kwargs))
    try:
        with app.app_context():
            generation._record_successful_short_render(
                source_video_id="source-1",
                clip_filename="rendered.mp4",
                file_path=Path("/tmp/rendered.mp4"),
                size_bytes=1024,
                owner_user_id="test-user",
                brand_id="test-brand",
                plan_entry={"status": "created"},
                render_settings_hash="hash-1",
            )
        assert [call[0] for call in calls] == ["asset", "lifecycle"]
        assert calls[0][1][3] == 1024
        assert calls[1][2]["owner_user_id"] == "test-user"
        assert calls[1][2]["brand_id"] == "test-brand"

        generation._sync_generated_video_from_plan_entry = lambda **kwargs: (_ for _ in ()).throw(
            RuntimeError("lifecycle write failed")
        )
        try:
            with app.app_context():
                generation._record_successful_short_render(
                    source_video_id="source-1",
                    clip_filename="rendered.mp4",
                    file_path=Path("/tmp/rendered.mp4"),
                    size_bytes=1024,
                    owner_user_id="test-user",
                    brand_id="test-brand",
                    plan_entry={"status": "created"},
                    render_settings_hash="hash-1",
                )
        except RuntimeError as exc:
            assert str(exc) == "lifecycle write failed"
        else:
            raise AssertionError("Lifecycle write failure must fail the render response")
    finally:
        generation._upsert_storage_asset = original_asset_upsert
        generation._sync_generated_video_from_plan_entry = original_lifecycle_sync
    print("PASS successful render records asset then lifecycle before completion")
    print("PASS lifecycle write failure propagates to the render job")


def main() -> int:
    conn = duckdb.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE shorts_generated_videos (
            clip_filename VARCHAR,
            generated_title VARCHAR,
            generation_status VARCHAR,
            publish_status VARCHAR,
            youtube_video_id VARCHAR,
            created_at TIMESTAMP,
            youtube_published_at TIMESTAMP,
            published_at TIMESTAMP,
            user_id VARCHAR,
            brand_id VARCHAR,
            source_video_id VARCHAR,
            storage_file_key VARCHAR
        );
        CREATE TABLE shorts_storage_assets (
            file_key VARCHAR,
            status VARCHAR,
            size_bytes BIGINT
        );
        CREATE TABLE youtube_videos (
            id BIGINT,
            video_id VARCHAR,
            owner_user_id VARCHAR,
            brand_id VARCHAR,
            view_count BIGINT,
            like_count BIGINT,
            comment_count BIGINT
        );
        """
    )
    rows = [
        ("rendered.mp4", "Rendered", "created", "ready", None, "short:rendered.mp4"),
        ("plan-only.mp4", "Plan only", "pending", "not_ready", None, None),
        ("deleted.mp4", "Deleted", "created", "ready", None, "short:deleted.mp4"),
        ("published-missing.mp4", "Published missing", "created", "published", "yt1", "short:published-missing.mp4"),
    ]
    for filename, title, generation_status, publish_status, youtube_id, storage_key in rows:
        conn.execute(
            """
            INSERT INTO shorts_generated_videos (
                clip_filename, generated_title, generation_status, publish_status,
                youtube_video_id, created_at, user_id, brand_id, source_video_id,
                storage_file_key
            ) VALUES (?, ?, ?, ?, ?, now(), 'test-user', 'test-brand', ?, ?)
            """,
            [filename, title, generation_status, publish_status, youtube_id, filename, storage_key],
        )
    conn.execute("INSERT INTO shorts_storage_assets VALUES ('short:rendered.mp4', 'active', 1024)")
    conn.execute("INSERT INTO shorts_storage_assets VALUES ('short:deleted.mp4', 'deleted', 1024)")
    conn.execute("INSERT INTO shorts_storage_assets VALUES ('short:published-missing.mp4', 'missing', 1024)")

    original_media_helper = channels._short_card_media_urls
    channels._short_card_media_urls = lambda filename, storage_key="": (f"/shorts/{filename}", "")
    app = Flask(__name__)
    app.add_url_rule("/generate/<int:video_pk>", endpoint="video_shorts_bp.generate_short")
    try:
        with app.test_request_context("/"):
            cards, has_more = channels._load_recent_short_cards(
                conn,
                "test-user",
                "test-brand",
                "UTC",
                limit=20,
            )
    finally:
        channels._short_card_media_urls = original_media_helper
        conn.close()

    assert not has_more
    assert [card["title"] for card in cards] == ["Rendered"]
    assert cards[0]["video_url"] == "/shorts/rendered.mp4"
    assert "thumbnail_url" not in cards[0]
    _test_successful_render_record(app)
    print("PASS before_cards=4 after_cards=1 visible=Rendered")
    print("PASS plan-only/deleted/published-missing hidden")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
