from __future__ import annotations

from uuid import uuid4

import duckdb
from flask import Blueprint, Flask

from app.video_shorts.services import user_activity


def _database(tmp_path):
    path = tmp_path / "user_activity.duckdb"
    conn = duckdb.connect(str(path))
    conn.execute(
        """
        CREATE TABLE shorts_users (
            id UUID PRIMARY KEY,
            role VARCHAR,
            last_login_at TIMESTAMP,
            last_active_at TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE user_login_events (
            id UUID PRIMARY KEY DEFAULT uuid(),
            user_id UUID NOT NULL REFERENCES shorts_users(id),
            logged_in_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            method VARCHAR NOT NULL
        )
        """
    )
    user_id = str(uuid4())
    conn.execute("INSERT INTO shorts_users (id, role) VALUES (?, 'member')", [user_id])
    conn.close()
    return path, user_id


def _connect_factory(path):
    return lambda: duckdb.connect(str(path))


def _app():
    app = Flask(__name__)
    app.secret_key = "test-secret"
    bp = Blueprint("video_shorts_bp", __name__, url_prefix="/video_shorts")
    bp.add_url_rule("/customer", endpoint="customer_page", view_func=lambda: "ok")
    bp.add_url_rule("/n/<token>", endpoint="customer_notification_click", view_func=lambda token: "ok")
    bp.add_url_rule("/stats/report/render", endpoint="stats_report_render", view_func=lambda: "ok")
    app.register_blueprint(bp)
    return app


def test_successful_customer_login_sets_timestamp_and_event(monkeypatch, tmp_path):
    path, user_id = _database(tmp_path)
    monkeypatch.setattr(user_activity, "get_db", _connect_factory(path))

    user_activity.record_successful_login(user_id, "password")

    conn = duckdb.connect(str(path), read_only=True)
    try:
        user_row = conn.execute(
            "SELECT last_login_at, last_active_at FROM shorts_users WHERE CAST(id AS VARCHAR) = ?",
            [user_id],
        ).fetchone()
        events = conn.execute(
            "SELECT method, logged_in_at FROM user_login_events WHERE CAST(user_id AS VARCHAR) = ?",
            [user_id],
        ).fetchall()
    finally:
        conn.close()
    assert user_row[0] is not None
    assert user_row[1] is None
    assert len(events) == 1
    assert events[0][0] == "password"
    assert events[0][1] is not None


def test_customer_activity_is_throttled_for_five_minutes(monkeypatch, tmp_path):
    path, user_id = _database(tmp_path)
    monkeypatch.setattr(user_activity, "get_db", _connect_factory(path))
    app = _app()
    customer = {"id": user_id, "role": "member", "is_guest": False}

    with app.test_request_context("/video_shorts/customer"):
        user_activity.record_authenticated_activity(customer)
    conn = duckdb.connect(str(path), read_only=True)
    first_value = conn.execute(
        "SELECT last_active_at FROM shorts_users WHERE CAST(id AS VARCHAR) = ?", [user_id]
    ).fetchone()[0]
    conn.close()

    with app.test_request_context("/video_shorts/customer"):
        user_activity.record_authenticated_activity(customer)
    conn = duckdb.connect(str(path), read_only=True)
    second_value = conn.execute(
        "SELECT last_active_at FROM shorts_users WHERE CAST(id AS VARCHAR) = ?", [user_id]
    ).fetchone()[0]
    conn.close()

    assert first_value is not None
    assert second_value == first_value


def test_admin_customer_scope_does_not_update_customer(monkeypatch, tmp_path):
    path, customer_user_id = _database(tmp_path)
    monkeypatch.setattr(user_activity, "get_db", _connect_factory(path))
    app = _app()

    with app.test_request_context(
        "/video_shorts/customer?admin_operation_brand=customer-brand&admin_operation_kind=customer"
    ):
        user_activity.record_authenticated_activity(
            {"id": "admin-user", "role": "admin", "is_guest": False}
        )

    conn = duckdb.connect(str(path), read_only=True)
    value = conn.execute(
        "SELECT last_active_at FROM shorts_users WHERE CAST(id AS VARCHAR) = ?",
        [customer_user_id],
    ).fetchone()[0]
    conn.close()
    assert value is None


def test_tracking_redirect_and_signed_report_render_do_not_update_activity(monkeypatch, tmp_path):
    path, user_id = _database(tmp_path)
    monkeypatch.setattr(user_activity, "get_db", _connect_factory(path))
    app = _app()
    customer = {"id": user_id, "role": "member", "is_guest": False}

    with app.test_request_context("/video_shorts/n/click-token"):
        user_activity.record_authenticated_activity(customer)
    with app.test_request_context("/video_shorts/stats/report/render?token=signed&brand_id=b1"):
        user_activity.record_authenticated_activity(customer)

    conn = duckdb.connect(str(path), read_only=True)
    value = conn.execute(
        "SELECT last_active_at FROM shorts_users WHERE CAST(id AS VARCHAR) = ?", [user_id]
    ).fetchone()[0]
    conn.close()
    assert value is None
