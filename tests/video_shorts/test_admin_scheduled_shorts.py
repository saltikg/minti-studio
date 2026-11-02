from datetime import datetime, timezone

import duckdb
import pytest
from flask import Flask, g
from werkzeug.exceptions import Forbidden

from app.video_shorts import video_shorts_bp
from app.video_shorts.routes import generation
from app.video_shorts.services.admin_scheduled_shorts import (
    filter_scheduled_shorts,
    load_scheduled_shorts,
)


NOW = datetime(2026, 11, 2, 16, 0, tzinfo=timezone.utc)


def _database():
    conn = duckdb.connect(":memory:")
    statements = (
        "CREATE TABLE shorts_users (id VARCHAR, service_mode VARCHAR, email VARCHAR)",
        "CREATE TABLE shorts_brands (id VARCHAR, name VARCHAR, owner_user_id VARCHAR)",
        "CREATE TABLE autopilot_leads (user_id VARCHAR, brand_id VARCHAR, converted_at TIMESTAMP)",
        "CREATE TABLE youtube_videos (id INTEGER, owner_user_id VARCHAR, brand_id VARCHAR, video_id VARCHAR, title VARCHAR, thumbnail_url VARCHAR)",
        "CREATE TABLE youtube_oauth_tokens_v2 (user_id VARCHAR, youtube_channel_id VARCHAR, channel_title VARCHAR)",
        "CREATE TABLE instagram_oauth_tokens (user_id VARCHAR, instagram_business_account_id VARCHAR, instagram_username VARCHAR)",
        "CREATE TABLE facebook_page_tokens (user_id VARCHAR, page_id VARCHAR, page_name VARCHAR)",
        "CREATE TABLE shorts_youtube_schedules (id VARCHAR, user_id VARCHAR, brand_id VARCHAR, expected_youtube_channel_id VARCHAR, payload_json VARCHAR, status VARCHAR, publish_at TIMESTAMP, youtube_video_id VARCHAR, updated_at TIMESTAMP)",
        "CREATE TABLE shorts_instagram_queue (id VARCHAR, user_id VARCHAR, brand_id VARCHAR, video_id VARCHAR, plan_index VARCHAR, clip_filename VARCHAR, plan_title VARCHAR, instagram_business_account_id VARCHAR, instagram_username VARCHAR, status VARCHAR, publish_at TIMESTAMP, published_at TIMESTAMP, instagram_media_id VARCHAR, permalink VARCHAR, updated_at TIMESTAMP)",
        "CREATE TABLE shorts_facebook_queue (id VARCHAR, user_id VARCHAR, brand_id VARCHAR, video_id VARCHAR, plan_index VARCHAR, clip_filename VARCHAR, plan_title VARCHAR, page_id VARCHAR, page_name VARCHAR, status VARCHAR, publish_at TIMESTAMP, published_at TIMESTAMP, facebook_video_id VARCHAR, permalink VARCHAR, updated_at TIMESTAMP)",
    )
    for statement in statements:
        conn.execute(statement)
    conn.execute("INSERT INTO shorts_users VALUES ('customer', 'autopilot', 'customer@example.com'), ('admin', 'studio', 'admin@example.com')")
    conn.execute("INSERT INTO shorts_brands VALUES ('customer-brand', 'Customer Brand', 'customer'), ('admin-brand', 'Hoca', 'admin')")
    conn.execute("INSERT INTO autopilot_leads VALUES ('customer', 'customer-brand', '2026-10-01')")
    conn.execute("INSERT INTO youtube_videos VALUES (1, 'customer', 'customer-brand', 'source-1', 'Source one', 'https://img/one.jpg'), (2, 'admin', 'admin-brand', 'source-2', 'Source two', '')")
    conn.execute("INSERT INTO youtube_oauth_tokens_v2 VALUES ('customer::customer-brand', 'yt-customer', 'Customer YouTube'), ('admin::admin-brand', 'yt-admin', 'Hoca YouTube')")
    conn.execute("INSERT INTO instagram_oauth_tokens VALUES ('customer::customer-brand', 'ig-current', 'customer.ig')")
    conn.execute("INSERT INTO facebook_page_tokens VALUES ('customer::customer-brand', 'fb-customer', 'Customer Facebook')")
    conn.execute(
        "INSERT INTO shorts_youtube_schedules VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            'yt-upcoming', 'customer', 'customer-brand', 'yt-customer',
            '{"video_pk": 1, "title": "YouTube short"}', 'pending',
            datetime(2026, 11, 2, 18, 0), None, datetime(2026, 11, 2, 15, 0),
        ],
    )
    conn.execute(
        "INSERT INTO shorts_youtube_schedules VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            'yt-admin', 'admin', 'admin-brand', 'yt-admin',
            '{"video_pk": 2, "title": "Admin short"}', 'pending',
            datetime(2026, 11, 2, 19, 0), None, datetime(2026, 11, 2, 15, 0),
        ],
    )
    conn.execute(
        "INSERT INTO shorts_instagram_queue VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            'ig-mismatch', 'customer', 'customer-brand', 'source-1', '1', 'clip.mp4',
            'Instagram short', 'ig-old', 'customer.ig', 'pending',
            datetime(2026, 11, 2, 17, 0), None, None, None, datetime(2026, 11, 2, 15, 0),
        ],
    )
    conn.execute(
        "INSERT INTO shorts_facebook_queue VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            'fb-published', 'customer', 'customer-brand', 'source-1', '1', 'clip.mp4',
            'Facebook short', 'fb-customer', 'Customer Facebook', 'published',
            datetime(2026, 10, 30, 17, 0), datetime(2026, 10, 30, 17, 5),
            'fb-video', 'https://facebook.example/post', datetime(2026, 10, 30, 17, 5),
        ],
    )
    return conn


def test_admin_schedule_data_is_read_only_filtered_and_sorted():
    conn = _database()
    items = load_scheduled_shorts(conn, now=NOW)
    default = filter_scheduled_shorts(items, include_all_brands=False)
    all_brands = filter_scheduled_shorts(items, include_all_brands=True)

    assert [row["id"] for row in default["upcoming"]] == ["ig-mismatch", "yt-upcoming"]
    assert [row["id"] for row in default["published"]] == ["fb-published"]
    assert default["problem_count"] == 1
    assert default["upcoming"][0]["is_mismatch"] is True
    assert default["upcoming"][1]["channel_name"] == "Customer YouTube"
    assert default["upcoming"][0]["publish_time_label"] == "Mon Nov 2, 9:00 AM"
    assert all_brands["upcoming_count"] == 3
    assert {row["brand_name"] for row in all_brands["upcoming"]} == {"Customer Brand", "Hoca"}


def test_admin_schedule_template_marks_target_mismatch_red():
    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.register_blueprint(video_shorts_bp)
    conn = _database()
    page_data = filter_scheduled_shorts(
        load_scheduled_shorts(conn, now=NOW),
        include_all_brands=False,
    )
    for row in [*page_data["upcoming"], *page_data["published"]]:
        row["editor_url"] = "/video_shorts/generate/1"
    with app.test_request_context("/video_shorts/admin/scheduled-shorts"):
        html = app.jinja_env.get_template("shorts_admin_scheduled_shorts.html").render(
            vs_current_user={"id": "admin", "role": "admin"},
            vs_admin_new_users_badge_count=0,
            **page_data,
        )

    assert 'class="scheduled-row is-mismatch"' in html
    assert ">MISMATCH<" in html
    assert "Published ✓" in html


class _ReadOnlyConnection:
    def close(self):
        pass


def test_admin_schedule_route_is_200_for_admin_and_403_for_non_admin(monkeypatch):
    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.register_blueprint(video_shorts_bp)
    monkeypatch.setattr(generation, "get_db_readonly", lambda: _ReadOnlyConnection())
    monkeypatch.setattr(
        "app.video_shorts.services.admin_scheduled_shorts.load_scheduled_shorts",
        lambda conn: [],
    )
    monkeypatch.setattr(generation, "render_template", lambda *args, **kwargs: "ok")

    with app.test_request_context("/video_shorts/admin/scheduled-shorts"):
        g.vs_current_user = {"id": "admin", "role": "admin"}
        assert app.make_response(generation.admin_scheduled_shorts()).status_code == 200

    with app.test_request_context("/video_shorts/admin/scheduled-shorts"):
        g.vs_current_user = {"id": "member", "role": "member"}
        with pytest.raises(Forbidden) as error:
            generation.admin_scheduled_shorts()
        assert error.value.code == 403
