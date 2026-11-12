from datetime import datetime, timedelta, timezone

import duckdb
from flask import Flask, g

from app.video_shorts import video_shorts_bp
from app.video_shorts.routes import generation


class CountingConnection:
    def __init__(self, conn):
        self.conn = conn
        self.execute_count = 0

    def execute(self, sql, params=None):
        self.execute_count += 1
        return self.conn.execute(sql, params or [])

    def commit(self):
        return self.conn.commit()

    def rollback(self):
        return self.conn.rollback()

    def close(self):
        pass


def _database():
    conn = duckdb.connect(":memory:")
    statements = (
        """CREATE TABLE shorts_users (
            id VARCHAR, email VARCHAR, username VARCHAR, service_mode VARCHAR, service_tier INTEGER,
            service_mode_chosen_at TIMESTAMP, password_hash VARCHAR, google_sub VARCHAR,
            updated_at TIMESTAMP, created_at TIMESTAMP)""",
        "CREATE TABLE shorts_brands (id VARCHAR, name VARCHAR, owner_user_id VARCHAR)",
        "CREATE TABLE shorts_user_preferences (user_id VARCHAR, preference_key VARCHAR)",
        """CREATE TABLE autopilot_leads (
            id VARCHAR, user_id VARCHAR, brand_id VARCHAR, creator_name VARCHAR,
            youtube_channel_id VARCHAR, converted_at TIMESTAMP)""",
        "CREATE TABLE youtube_videos (owner_user_id VARCHAR)",
        """CREATE TABLE youtube_oauth_tokens_v2 (
            user_id VARCHAR, youtube_channel_id VARCHAR, reauth_required INTEGER)""",
        """CREATE TABLE shorts_generated_videos (
            brand_id VARCHAR, publish_status VARCHAR, published_at TIMESTAMP, planned_publish_at TIMESTAMP)""",
        """CREATE TABLE shorts_youtube_schedules (
            id VARCHAR, brand_id VARCHAR, expected_youtube_channel_id VARCHAR, status VARCHAR,
            publish_at TIMESTAMP, updated_at TIMESTAMP)""",
        """CREATE TABLE autopilot_channel_videos (
            id BIGINT, user_id VARCHAR, brand_id VARCHAR, channel_id VARCHAR,
            youtube_video_id VARCHAR, title VARCHAR, published_at TIMESTAMP,
            duration_s INTEGER, detected_at TIMESTAMP, status VARCHAR)""",
    )
    for statement in statements:
        conn.execute(statement)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    conn.execute(
        "INSERT INTO shorts_users VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ["u1", "customer@example.com", "customer", "autopilot", 15, now, "hash", "", now, now],
    )
    conn.execute("INSERT INTO shorts_brands VALUES ('b1', 'Brand', 'u1')")
    conn.execute("INSERT INTO autopilot_leads VALUES ('l1', 'u1', 'b1', 'Creator', 'UC0000000000000000000000', ?)", [now])
    conn.execute("INSERT INTO youtube_oauth_tokens_v2 VALUES ('u1::b1', 'UC1234567890123456789012', 1)")
    conn.execute("INSERT INTO shorts_generated_videos VALUES ('b1', 'published', ?, NULL)", [now])
    conn.execute(
        "INSERT INTO shorts_youtube_schedules VALUES ('s1', 'b1', 'UC1234567890123456789012', 'pending', ?, ?)",
        [now + timedelta(days=2), now],
    )
    conn.execute(
        "INSERT INTO autopilot_channel_videos VALUES (1, 'u1', 'b1', 'UC1234567890123456789012', 'v1', 'New upload', ?, 600, ?, 'new')",
        [now, now - timedelta(hours=49)],
    )
    return conn


def test_customer_delivery_health_is_loaded_in_one_query(monkeypatch):
    app = Flask(__name__)
    app.secret_key = "test"
    app.register_blueprint(video_shorts_bp)
    counting = CountingConnection(_database())
    monkeypatch.setattr(generation, "ensure_storage_user_schema", lambda conn: None)
    monkeypatch.setattr(generation, "ensure_auth_user_schema", lambda conn: None)

    with app.test_request_context("/video_shorts/admin/autopilot-leads"):
        customers, total = generation._load_admin_autopilot_customers(counting)

    assert counting.execute_count == 1
    assert total == 1
    assert customers[0]["channel_id"] == "UC1234567890123456789012"
    assert customers[0]["monthly_delivered"] == 1
    assert customers[0]["runway_count"] == 1
    assert customers[0]["runway_low"] is True
    assert customers[0]["channel_health"] == "Token error"
    assert customers[0]["new_video_count"] == 1
    assert customers[0]["overdue"] is True


def test_customer_workspace_channel_videos_keep_saved_decisions():
    conn = _database()
    conn.execute(
        "INSERT INTO autopilot_channel_videos VALUES (2, 'u1', 'b1', 'UC1234567890123456789012', 'v2', 'Handled upload', CURRENT_TIMESTAMP, 500, CURRENT_TIMESTAMP, 'seen')"
    )

    videos = generation._load_autopilot_channel_videos(
        conn,
        owner_user_id="u1",
        brand_id="b1",
    )

    assert {video["id"] for video in videos} == {1, 2}
    assert {video["status"] for video in videos} == {"new", "seen"}


def test_process_revalidates_customer_scope_and_uses_shared_ingest(monkeypatch):
    from app.video_shorts.routes import quick_short

    app = Flask(__name__)
    app.secret_key = "test"
    app.register_blueprint(video_shorts_bp)
    raw_conn = _database()
    conn = CountingConnection(raw_conn)
    ingest_calls = []
    monkeypatch.setattr(generation, "get_db_readonly", lambda: conn)
    monkeypatch.setattr(generation, "get_db", lambda: conn)
    monkeypatch.setattr(
        quick_short,
        "create_youtube_ingest_session_for_user",
        lambda **kwargs: ingest_calls.append(kwargs) or {"job_id": "job-1"},
    )

    with app.test_request_context("/video_shorts/admin/autopilot-channel-videos/1/process", method="POST"):
        g.vs_current_user = {"id": "admin", "role": "admin"}
        response = generation.admin_autopilot_channel_video_action(1, "process")

    assert response.status_code == 302
    assert response.location.endswith("/video_shorts/admin/operation/customer/b1/workspace")
    assert ingest_calls[0]["user_id"] == "u1"
    assert ingest_calls[0]["brand_id"] == "b1"
    assert ingest_calls[0]["source"] == "autopilot_channel_watcher"
    assert raw_conn.execute("SELECT status FROM autopilot_channel_videos WHERE id=1").fetchone()[0] == "processed"


def test_ajax_decision_saves_without_redirect_and_keeps_history(monkeypatch):
    app = Flask(__name__)
    app.secret_key = "test"
    app.register_blueprint(video_shorts_bp)
    raw_conn = _database()
    conn = CountingConnection(raw_conn)
    monkeypatch.setattr(generation, "get_db_readonly", lambda: conn)
    monkeypatch.setattr(generation, "get_db", lambda: conn)

    with app.test_request_context(
        "/video_shorts/admin/autopilot-channel-videos/1/skip",
        method="POST",
        headers={"X-Requested-With": "XMLHttpRequest"},
    ):
        g.vs_current_user = {"id": "admin", "role": "admin"}
        response = generation.admin_autopilot_channel_video_action(1, "skip")

    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "status": "skipped", "status_label": "Skipped"}
    assert raw_conn.execute("SELECT status FROM autopilot_channel_videos WHERE id=1").fetchone()[0] == "skipped"


def test_set_channel_updates_only_empty_lead_record(monkeypatch):
    app = Flask(__name__)
    app.secret_key = "test"
    app.register_blueprint(video_shorts_bp)
    raw_conn = _database()
    raw_conn.execute("DELETE FROM youtube_oauth_tokens_v2")
    raw_conn.execute("UPDATE autopilot_leads SET youtube_channel_id='manual_activation:u1:b1'")
    conn = CountingConnection(raw_conn)
    monkeypatch.setattr(generation, "get_db_readonly", lambda: conn)
    monkeypatch.setattr(generation, "get_db", lambda: conn)
    monkeypatch.setattr(
        generation,
        "get_channel_metadata",
        lambda value: {"youtube_channel_id": "UC9999999999999999999999"},
    )

    with app.test_request_context(
        "/video_shorts/admin/autopilot-leads/l1/channel",
        method="POST",
        data={"channel": "@customer"},
    ):
        g.vs_current_user = {"id": "admin", "role": "admin"}
        response = generation.admin_autopilot_set_channel("l1")

    assert response.status_code == 302
    assert raw_conn.execute("SELECT youtube_channel_id FROM autopilot_leads").fetchone()[0] == "UC9999999999999999999999"
    assert raw_conn.execute("SELECT COUNT(*) FROM youtube_oauth_tokens_v2").fetchone()[0] == 0


def test_set_channel_cannot_override_brand_connection(monkeypatch):
    app = Flask(__name__)
    app.secret_key = "test"
    app.register_blueprint(video_shorts_bp)
    raw_conn = _database()
    raw_conn.execute("UPDATE autopilot_leads SET youtube_channel_id='manual_activation:u1:b1'")
    conn = CountingConnection(raw_conn)
    monkeypatch.setattr(generation, "get_db_readonly", lambda: conn)
    monkeypatch.setattr(
        generation,
        "get_channel_metadata",
        lambda value: (_ for _ in ()).throw(AssertionError("resolver must not run")),
    )

    with app.test_request_context(
        "/video_shorts/admin/autopilot-leads/l1/channel",
        method="POST",
        data={"channel": "@other"},
    ):
        g.vs_current_user = {"id": "admin", "role": "admin"}
        response = generation.admin_autopilot_set_channel("l1")

    assert response.status_code == 302
    assert raw_conn.execute("SELECT youtube_channel_id FROM autopilot_leads").fetchone()[0] == "manual_activation:u1:b1"
