from __future__ import annotations

from email import policy
from email.parser import BytesParser
from datetime import datetime, timezone

import pytest
from flask import Flask, g
from werkzeug.exceptions import Forbidden

from app.video_shorts import video_shorts_bp
from app.video_shorts.routes import generation
from app.video_shorts.services import customer_short_notifications as notifications
from app.video_shorts.services import email_verification
from app.video_shorts.services import outreach_email_send


class _AuditConnection:
    def __init__(self):
        self.inserts = []

    def execute(self, sql, params=None):
        self.inserts.append((sql, params or []))
        return self

    def commit(self):
        return None

    def rollback(self):
        return None

    def close(self):
        return None


@pytest.fixture()
def app():
    test_app = Flask(__name__)
    test_app.secret_key = "test-secret"
    test_app.config["BASE_URL"] = "https://mintistudio.com"
    test_app.register_blueprint(video_shorts_bp, url_prefix="/video_shorts")
    return test_app


@pytest.mark.parametrize("connected,video_pk", [(True, 7923), (False, 7943)])
def test_mocked_smtp_through_real_handler_renders_both_variants(app, monkeypatch, connected, video_pk, capsys):
    scope = {"owner_user_id": "owner-1", "brand_id": "brand-1", "acting_admin_id": "admin-1"}
    payload = {
        "recipient": "customer@example.com",
        "first_name": "Shelley" if connected else "Matt",
        "video_title": "A useful long-form customer video",
        "short_titles": [
            {"title": "A clean opening", "state": "published", "scheduled_at": None},
            {
                "title": "The key takeaway",
                "state": "scheduled",
                "scheduled_at": datetime(2026, 11, 9, 8, tzinfo=timezone.utc),
            },
        ],
        "youtube_connected": connected,
        "last_notified_at": None,
    }
    audit = _AuditConnection()
    smtp_calls = {}
    monkeypatch.setattr(generation, "_require_customer_notification_scope", lambda _brand, _pk: scope)
    monkeypatch.setattr(generation, "_resolve_customer_notification_payload", lambda _scope, _pk: payload)
    monkeypatch.setattr(generation, "get_db", lambda: audit)

    monkeypatch.setenv("CUSTOMER_NOTIFY_MAILER", "zoho")
    monkeypatch.setattr(
        outreach_email_send,
        "_zoho_smtp_settings",
        lambda: {"host": "smtp.zoho.com", "port": 465, "user": "info@mintistudio.com", "password": "app-password"},
    )

    class FakeSmtp:
        def __init__(self, host, port, **kwargs):
            smtp_calls.update(host=host, port=port, kwargs=kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def login(self, user, password):
            smtp_calls.update(login_user=user, login_password=password)

        def sendmail(self, from_addr, to_addrs, raw_message):
            smtp_calls.update(from_addr=from_addr, to_addrs=to_addrs, raw_message=raw_message)
            return {}

    monkeypatch.setattr(outreach_email_send.smtplib, "SMTP_SSL", FakeSmtp)
    path = f"/video_shorts/admin/operation/customer/brand-1/video/{video_pk}/notify"
    with app.test_request_context(path, method="POST", json={"personal_note": "Thanks for the great source video."}):
        g.vs_current_user = {"id": "admin-1", "role": "admin"}
        response = generation.admin_operation_notify_customer("brand-1", video_pk)
    assert response.status_code == 200
    message = BytesParser(policy=policy.default).parsebytes(smtp_calls["raw_message"])
    html_body = message.get_body(preferencelist=("html",)).get_content()
    assert smtp_calls["host"] == "smtp.zoho.com"
    assert smtp_calls["port"] == 465
    assert smtp_calls["login_user"] == "info@mintistudio.com"
    assert smtp_calls["from_addr"] == "info@mintistudio.com"
    assert smtp_calls["to_addrs"] == ["customer@example.com"]
    assert message["From"].addresses[0].addr_spec == "info@mintistudio.com"
    assert message["Reply-To"].addresses[0].addr_spec == "info@mintistudio.com"
    assert message["To"].addresses[0].addr_spec == "customer@example.com"
    assert str(message["Message-ID"]).endswith("@mintistudio.com>")
    assert "See your Shorts" in html_body
    assert "admin" not in html_body.lower()
    expected = "1 are already live" if connected else "Watch them and download"
    assert expected in html_body
    assert "A clean opening · Live" in html_body
    assert "The key takeaway · Nov 9" in html_body
    assert audit.inserts
    audit_params = audit.inserts[-1][1]
    assert audit_params[-2] == str(message["Message-ID"])
    assert audit_params[-1] is None
    print(f"\n--- {'CONNECTED' if connected else 'NOT CONNECTED'} HTML ({video_pk}) ---\n{html_body}")
    assert expected in capsys.readouterr().out


def test_customer_notify_resend_is_explicit_rollback(monkeypatch):
    captured = {}
    monkeypatch.setenv("CUSTOMER_NOTIFY_MAILER", "resend")

    def fake_resend(**kwargs):
        captured.update(kwargs)
        return {"request_id": "resend-id"}

    monkeypatch.setattr(email_verification, "send_resend_email", fake_resend)
    result = notifications.send_customer_notification_email(
        to_email="customer@example.com", subject="Ready", html_body="<p>Ready</p>", text_body="Ready"
    )
    assert result["transport"] == "resend"
    assert captured["reply_to_email"] == "info@mintistudio.com"


def test_customer_notify_mailer_defaults_to_zoho(monkeypatch):
    monkeypatch.delenv("CUSTOMER_NOTIFY_MAILER", raising=False)
    assert notifications.customer_notify_mailer() == "zoho"


def test_smtp_failure_is_logged_and_returned_without_success_state(app, monkeypatch):
    scope = {"owner_user_id": "owner-1", "brand_id": "brand-1", "acting_admin_id": "admin-1"}
    payload = {
        "recipient": "customer@example.com", "first_name": "Matt", "video_title": "Source",
        "short_titles": ["Ready Short"], "youtube_connected": False, "last_notified_at": None,
    }
    audit = _AuditConnection()
    monkeypatch.setattr(generation, "_require_customer_notification_scope", lambda _brand, _pk: scope)
    monkeypatch.setattr(generation, "_resolve_customer_notification_payload", lambda _scope, _pk: payload)
    monkeypatch.setattr(generation, "get_db", lambda: audit)
    monkeypatch.setenv("CUSTOMER_NOTIFY_MAILER", "zoho")
    monkeypatch.setattr(outreach_email_send, "send_zoho_email", lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("authentication failed")))
    with app.test_request_context("/notify", method="POST", json={}):
        g.vs_current_user = {"id": "admin-1", "role": "admin"}
        response, status = generation.admin_operation_notify_customer("brand-1", 7943)
    assert status == 502
    assert "Zoho SMTP failed: authentication failed" in response.get_json()["message"]
    assert audit.inserts[-1][1][-2] is None
    assert audit.inserts[-1][1][-1] == "Zoho SMTP failed: authentication failed"


def test_notification_brand_mismatch_fails_closed_with_403(app, monkeypatch):
    monkeypatch.setattr(
        generation,
        "_require_admin_operation_scope",
        lambda **_kwargs: {"owner_user_id": "owner-1", "brand_id": "brand-1", "acting_admin_id": "admin-1"},
    )

    class _ReadConnection:
        def execute(self, *_args, **_kwargs):
            return self

        def fetchone(self):
            return None

        def close(self):
            return None

    monkeypatch.setattr(generation, "get_db_readonly", _ReadConnection)
    with app.test_request_context("/video_shorts/admin/operation/customer/brand-1/video/9/notify"):
        g.vs_current_user = {"id": "admin-1", "role": "admin"}
        with pytest.raises(Forbidden):
            generation._require_customer_notification_scope("brand-1", 9)


def test_notification_non_admin_is_403(app):
    with app.test_request_context("/video_shorts/admin/operation/customer/brand-1/video/9/notify"):
        g.vs_current_user = {"id": "member-1", "role": "member"}
        with pytest.raises(Forbidden):
            generation._require_customer_notification_scope("brand-1", 9)


def test_email_content_and_note_limit():
    email = notifications.build_customer_shorts_email(
        first_name="Matt",
        video_title="A very useful video",
        short_titles=["First", "Second"],
        youtube_connected=False,
        my_videos_url="https://mintistudio.com/video_shorts/my-videos",
        personal_note="Nice work.",
    )
    assert email["subject"] == "2 new Shorts are ready from 'A very useful video'"
    assert "Nice work." in email["html"]
    assert "Gokhan — Minti Studio" in email["text"]
    with pytest.raises(ValueError):
        notifications.normalize_personal_note("x" * 301)


@pytest.mark.parametrize(
    "states,expected",
    [
        (["published", "published"], "They're already live on your channel."),
        (["scheduled", "scheduled"], "No action needed. They'll go out on your channel on the scheduled dates."),
        (["published", "scheduled", "scheduled"], "1 are already live, and the other 2 will go out on the scheduled dates."),
        (["unscheduled", "scheduled"], "No action needed. We'll schedule them on your channel shortly."),
        (["published", "unscheduled"], "1 are already live. No action needed. We'll schedule the unscheduled Shorts on your channel shortly."),
    ],
)
def test_connected_delivery_sentence_matches_reality(states, expected):
    items = [{"title": f"Short {index}", "state": state} for index, state in enumerate(states)]
    assert notifications.connected_delivery_sentence(items) == expected


def test_delivery_loader_combines_all_platform_rows_with_tenant_scope():
    class Result:
        def __init__(self, rows):
            self.rows = rows

        def fetchall(self):
            return self.rows

    class Connection:
        def __init__(self):
            self.calls = []

        def execute(self, sql, params=None):
            self.calls.append((sql, params or []))
            if "FROM shorts_generated_videos" in sql:
                return Result([
                    ("Instagram live", "ig.mp4", '{"plan_index": 1}', "ready", None, None, None, None, None, None),
                    ("Facebook scheduled", "fb.mp4", '{"plan_index": 2}', "ready", None, None, None, None, None, None),
                    ("TikTok live", "tt.mp4", '{"plan_index": 3}', "ready", None, None, None, None, None, None),
                    ("YouTube scheduled", "yt.mp4", '{"plan_index": 4}', "ready", None, None, None, None, None, None),
                ])
            if "FROM shorts_instagram_queue" in sql:
                return Result([("ig.mp4", "1", "published", None, "2026-11-05T08:00:00Z")])
            if "FROM shorts_facebook_queue" in sql:
                return Result([("fb.mp4", "2", "pending", "2026-11-10T08:00:00Z", None)])
            if "FROM shorts_tiktok_queue" in sql:
                return Result([("tt.mp4", "3", "published", None, "2026-11-05T08:00:00Z")])
            if "FROM shorts_youtube_schedules" in sql:
                return Result([('{"filename":"yt.mp4","plan_index":4}', "pending", datetime(2026, 11, 12, 8), None)])
            raise AssertionError(sql)

    conn = Connection()
    items = notifications.load_video_short_delivery_items(
        conn, video_pk=22, source_video_id="source-22", owner_user_id="owner-1", brand_id="brand-1"
    )
    assert [item["state"] for item in items] == ["published", "scheduled", "published", "scheduled"]
    assert notifications.short_delivery_label(items[1]) == "Facebook scheduled · Nov 10"
    assert notifications.short_delivery_label(items[3]) == "YouTube scheduled · Nov 12"
    queue_calls = [params for sql, params in conn.calls if "_queue" in sql or "youtube_schedules" in sql]
    assert all("owner-1" in params and "brand-1" in params for params in queue_calls)


def test_customer_first_name_skips_honorific(app, monkeypatch):
    scope = {"owner_user_id": "owner-1", "brand_id": "brand-1"}
    monkeypatch.setattr(
        generation,
        "_require_active_customer_workspace",
        lambda _scope: {"account_email": "shelley@example.com", "account_name": "Dr. Shelley Meyer"},
    )

    class _Rows:
        def __init__(self):
            self.query = ""

        def execute(self, sql, _params=None):
            self.query = sql
            return self

        def fetchone(self):
            if "SELECT title" in self.query:
                return ("Source video", "source-id")
            if "SELECT sent_at" in self.query:
                return None
            return None

        def fetchall(self):
            return [("A ready Short",)]

        def close(self):
            return None

    monkeypatch.setattr(generation, "get_db_readonly", _Rows)
    monkeypatch.setattr(
        notifications,
        "load_video_short_delivery_items",
        lambda *_args, **_kwargs: [{"title": "A ready Short", "state": "unscheduled", "scheduled_at": None}],
    )
    from app.video_shorts.services import youtube_token_store
    monkeypatch.setattr(youtube_token_store, "get_youtube_connection", lambda *_args: {"reauth_required": False})
    with app.test_request_context("/"):
        payload = generation._resolve_customer_notification_payload(scope, 7923)
    assert payload["first_name"] == "Shelley"
