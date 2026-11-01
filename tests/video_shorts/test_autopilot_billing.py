from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

from flask import Flask, g

from app.video_shorts.routes import billing as billing_routes
from app.video_shorts.services import billing
from app.video_shorts.services import db as db_service
from app.video_shorts.services import email_verification
from app.video_shorts.services import instagram_queue


def _configure_duckdb(monkeypatch, tmp_path, filename: str) -> None:
    monkeypatch.setattr(db_service, "VIDEO_SHORTS_DB_BACKEND", "duckdb")
    monkeypatch.setattr(db_service, "VIDEO_SHORTS_DB", str(tmp_path / filename))


def _create_user_table() -> None:
    conn = db_service.get_db()
    try:
        conn.execute(
            """
            CREATE TABLE shorts_users (
                id UUID PRIMARY KEY,
                email VARCHAR,
                name VARCHAR,
                service_mode VARCHAR,
                service_tier INTEGER,
                service_mode_chosen_at TIMESTAMP,
                stripe_subscription_id VARCHAR,
                subscription_status VARCHAR,
                autopilot_billing_reminder_sent_at TIMESTAMP,
                autopilot_billing_paused_at TIMESTAMP,
                updated_at TIMESTAMP DEFAULT now()
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def test_autopilot_trial_end_is_anchored_to_service_choice() -> None:
    chosen_at = datetime(2026, 10, 27, 15, 4, 51)
    assert billing.autopilot_trial_end(chosen_at) == datetime(
        2026, 11, 26, 15, 4, 51, tzinfo=timezone.utc
    )


def test_embedded_checkout_uses_exact_trial_end_and_collects_card(monkeypatch) -> None:
    captured = {}
    monkeypatch.setattr(
        billing.stripe.checkout.Session,
        "create",
        lambda **kwargs: captured.update(kwargs) or SimpleNamespace(client_secret="secret"),
    )
    trial_end = datetime(2026, 11, 26, 15, 4, 51, tzinfo=timezone.utc)

    billing.create_embedded_checkout_session(
        customer_id="cus_test",
        price_id="price_autopilot_15",
        shorts_user_id="user-1",
        plan_id="",
        interval="month",
        return_url="https://example.com/complete",
        trial_end=trial_end,
        billing_mode="autopilot",
        service_tier=15,
    )

    assert captured["payment_method_collection"] == "always"
    assert captured["line_items"] == [{"price": "price_autopilot_15", "quantity": 1}]
    assert captured["subscription_data"]["trial_end"] == int(trial_end.timestamp())
    assert captured["metadata"]["billing_mode"] == "autopilot"
    assert captured["metadata"]["service_tier"] == "15"


def test_autopilot_checkout_route_anchors_trial_and_leaves_diy_unchanged(monkeypatch) -> None:
    app = Flask(__name__)
    captured = []
    chosen_at = datetime(2026, 10, 27, 15, 4, 51)
    user = {
        "id": "user-1",
        "email": "autopilot@example.com",
        "stripe_customer_id": "cus_test",
        "stripe_subscription_id": None,
        "subscription_status": None,
        "service_mode": "autopilot",
        "service_tier": 15,
        "service_mode_chosen_at": chosen_at,
    }
    monkeypatch.setattr(billing_routes, "stripe_is_configured", lambda: True)
    monkeypatch.setattr(billing_routes, "_load_billing_user", lambda _user_id: dict(user))
    monkeypatch.setattr(billing_routes, "get_price_id_for_autopilot_tier", lambda tier: f"price_auto_{tier}")
    monkeypatch.setattr(
        billing_routes,
        "create_embedded_checkout_session",
        lambda **kwargs: captured.append(kwargs) or SimpleNamespace(client_secret="secret"),
    )
    monkeypatch.setattr(
        billing_routes,
        "datetime",
        SimpleNamespace(now=lambda _tz: datetime(2026, 10, 31, tzinfo=timezone.utc)),
    )

    with app.test_request_context(
        "/billing/create-checkout-session",
        method="POST",
        json={"billing_mode": "autopilot", "service_tier": 15, "interval": "year"},
    ):
        g.vs_current_user = {"id": "user-1", "email": user["email"]}
        response = billing_routes.create_checkout_session_route()
        assert response.get_json() == {"client_secret": "secret"}

    assert captured[0]["price_id"] == "price_auto_15"
    assert captured[0]["interval"] == "month"
    assert captured[0]["trial_end"] == datetime(2026, 11, 26, 15, 4, 51, tzinfo=timezone.utc)
    assert captured[0]["trial_days"] is None

    user.update({"service_mode": "self", "service_tier": None, "service_mode_chosen_at": None})
    monkeypatch.setattr(billing_routes, "get_price_id_for_plan", lambda plan_id, interval: "price_diy_19")
    with app.test_request_context(
        "/billing/create-checkout-session",
        method="POST",
        json={"billing_mode": "self", "plan_id": "plan_10gb", "interval": "month"},
    ):
        g.vs_current_user = {"id": "user-1", "email": user["email"]}
        response = billing_routes.create_checkout_session_route()
        assert response.get_json() == {"client_secret": "secret"}

    assert captured[1]["price_id"] == "price_diy_19"
    assert captured[1]["trial_end"] is None
    assert captured[1]["billing_mode"] == "self"


def test_autopilot_price_normalizes_without_changing_diy_mapping(monkeypatch) -> None:
    monkeypatch.setattr(billing, "PRICE_ID_TO_AUTOPILOT_TIER", {"price_auto_30": 30})
    monkeypatch.setattr(
        billing,
        "PRICE_ID_TO_PLAN_INTERVAL",
        {"price_diy_19": ("plan_10gb", "month")},
    )

    auto = billing.normalize_subscription_payload(
        {
            "id": "sub_auto",
            "customer": "cus_auto",
            "status": "trialing",
            "items": {"data": [{"price": {"id": "price_auto_30"}}]},
        }
    )
    diy = billing.normalize_subscription_payload(
        {
            "id": "sub_diy",
            "customer": "cus_diy",
            "status": "active",
            "items": {"data": [{"price": {"id": "price_diy_19"}}]},
        }
    )

    assert auto["billing_mode"] == "autopilot"
    assert auto["service_tier"] == 30
    assert auto["plan_id"] is None
    assert diy["billing_mode"] == "self"
    assert diy["plan_id"] == "plan_10gb"
    assert diy["service_tier"] is None


def test_diy_checkout_keeps_existing_payment_collection_behavior(monkeypatch) -> None:
    captured = {}
    monkeypatch.setattr(
        billing.stripe.checkout.Session,
        "create",
        lambda **kwargs: captured.update(kwargs) or SimpleNamespace(client_secret="secret"),
    )
    billing.create_embedded_checkout_session(
        customer_id="cus_test",
        price_id="price_diy_19",
        shorts_user_id="user-1",
        plan_id="plan_10gb",
        interval="month",
        return_url="https://example.com/complete",
    )
    assert "payment_method_collection" not in captured
    assert captured["subscription_data"] == {
        "metadata": {
            "shorts_user_id": "user-1",
            "plan_id": "plan_10gb",
            "interval": "month",
            "billing_mode": "self",
        }
    }


def test_deadline_sends_day_25_reminder_then_pauses_at_day_30(monkeypatch, tmp_path) -> None:
    _configure_duckdb(monkeypatch, tmp_path, "autopilot_deadline.duckdb")
    _create_user_table()
    user_id = str(uuid4())
    chosen_at = datetime(2026, 10, 1, 12, 0, 0)
    now = datetime(2026, 10, 26, 12, 0, 0, tzinfo=timezone.utc)
    conn = db_service.get_db()
    try:
        conn.execute(
            """
            INSERT INTO shorts_users (
                id, email, name, service_mode, service_tier, service_mode_chosen_at
            ) VALUES (?, 'autopilot@example.com', 'Auto Pilot', 'autopilot', 15, ?)
            """,
            [user_id, chosen_at],
        )
        conn.commit()
    finally:
        conn.close()

    sent = []
    monkeypatch.setattr(
        email_verification,
        "send_autopilot_billing_reminder_email",
        lambda **kwargs: sent.append(kwargs) or {"ok": True},
    )
    assert billing.process_autopilot_billing_deadlines(now=now) is True
    assert sent[0]["to_email"] == "autopilot@example.com"
    assert sent[0]["trial_end_label"] == "October 31, 2026"
    assert sent[0]["billing_url"].endswith(
        "/video_shorts/shorts/storage/plans?checkout=current"
    )

    conn = db_service.get_db_readonly()
    try:
        reminder_sent_at, status = conn.execute(
            "SELECT autopilot_billing_reminder_sent_at, subscription_status FROM shorts_users WHERE id = ?",
            [user_id],
        ).fetchone()
    finally:
        conn.close()
    assert reminder_sent_at is not None
    assert status is None

    assert billing.process_autopilot_billing_deadlines(now=now + timedelta(days=5)) is True
    conn = db_service.get_db_readonly()
    try:
        status, paused_at = conn.execute(
            "SELECT subscription_status, autopilot_billing_paused_at FROM shorts_users WHERE id = ?",
            [user_id],
        ).fetchone()
    finally:
        conn.close()
    assert status == "paused"
    assert paused_at is not None


def test_paused_autopilot_is_excluded_from_instagram_publish_queue(monkeypatch, tmp_path) -> None:
    _configure_duckdb(monkeypatch, tmp_path, "autopilot_publish_pause.duckdb")
    _create_user_table()
    paused_user = str(uuid4())
    active_user = str(uuid4())
    conn = db_service.get_db()
    try:
        conn.execute(
            """
            INSERT INTO shorts_users (id, email, service_mode, service_tier, subscription_status)
            VALUES (?, 'paused@example.com', 'autopilot', 15, 'paused'),
                   (?, 'active@example.com', 'autopilot', 15, 'active')
            """,
            [paused_user, active_user],
        )
        conn.execute(
            """
            CREATE TABLE shorts_instagram_queue (
                id VARCHAR PRIMARY KEY, user_id VARCHAR, brand_id VARCHAR, video_id VARCHAR,
                plan_index VARCHAR, clip_filename VARCHAR, caption_text VARCHAR, publish_at VARCHAR,
                status VARCHAR, status_detail VARCHAR, created_at VARCHAR, updated_at VARCHAR,
                instagram_business_account_id VARCHAR, instagram_username VARCHAR,
                instagram_media_id VARCHAR, published_at VARCHAR, youtube_video_id VARCHAR,
                youtube_short_id VARCHAR, plan_title VARCHAR, permalink VARCHAR,
                like_count INTEGER, comment_count INTEGER, impressions INTEGER, reach INTEGER,
                saved INTEGER, shares INTEGER, media_type VARCHAR
            )
            """
        )
        for queue_id, user_id in (("paused-job", paused_user), ("active-job", active_user)):
            conn.execute(
                """
                INSERT INTO shorts_instagram_queue (
                    id, user_id, video_id, plan_index, clip_filename, status, created_at, updated_at, media_type
                ) VALUES (?, ?, 'video-1', '1', 'clip.mp4', 'pending', '2026-10-01T00:00:00Z', '2026-10-01T00:00:00Z', 'reel')
                """,
                [queue_id, user_id],
            )
        conn.commit()
    finally:
        conn.close()

    jobs = instagram_queue.fetch_due_instagram_jobs(limit=10)
    assert [job["id"] for job in jobs] == ["active-job"]
