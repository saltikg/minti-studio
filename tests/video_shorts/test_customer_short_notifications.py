from __future__ import annotations

import pytest
from flask import Flask, g
from werkzeug.exceptions import Forbidden

from app.video_shorts import video_shorts_bp
from app.video_shorts.routes import generation
from app.video_shorts.services import customer_short_notifications as notifications
from app.video_shorts.services import email_verification


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
def test_mocked_send_through_real_handler_renders_both_variants(app, monkeypatch, connected, video_pk, capsys):
    scope = {"owner_user_id": "owner-1", "brand_id": "brand-1", "acting_admin_id": "admin-1"}
    payload = {
        "recipient": "customer@example.com",
        "first_name": "Shelley" if connected else "Matt",
        "video_title": "A useful long-form customer video",
        "short_titles": ["A clean opening", "The key takeaway"],
        "youtube_connected": connected,
        "last_notified_at": None,
    }
    audit = _AuditConnection()
    sent = {}
    monkeypatch.setattr(generation, "_require_customer_notification_scope", lambda _brand, _pk: scope)
    monkeypatch.setattr(generation, "_resolve_customer_notification_payload", lambda _scope, _pk: payload)
    monkeypatch.setattr(generation, "get_db", lambda: audit)

    def _mock_send(**kwargs):
        sent.update(kwargs)
        return {"request_id": f"mock-{video_pk}"}

    monkeypatch.setattr(email_verification, "send_resend_email", _mock_send)
    path = f"/video_shorts/admin/operation/customer/brand-1/video/{video_pk}/notify"
    with app.test_request_context(path, method="POST", json={"personal_note": "Thanks for the great source video."}):
        g.vs_current_user = {"id": "admin-1", "role": "admin"}
        response = generation.admin_operation_notify_customer("brand-1", video_pk)
    assert response.status_code == 200
    assert sent["to_email"] == "customer@example.com"
    assert sent["reply_to_email"] == "info@mintistudio.com"
    assert "See your Shorts" in sent["html"]
    assert "admin" not in sent["html"].lower()
    expected = "No action needed" if connected else "Watch them and download"
    assert expected in sent["html"]
    assert audit.inserts
    print(f"\n--- {'CONNECTED' if connected else 'NOT CONNECTED'} HTML ({video_pk}) ---\n{sent['html']}")
    assert expected in capsys.readouterr().out


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
                return ("Source video",)
            if "SELECT sent_at" in self.query:
                return None
            return None

        def fetchall(self):
            return [("A ready Short",)]

        def close(self):
            return None

    monkeypatch.setattr(generation, "get_db_readonly", _Rows)
    from app.video_shorts.services import youtube_token_store
    monkeypatch.setattr(youtube_token_store, "get_youtube_connection", lambda *_args: {"reauth_required": False})
    with app.test_request_context("/"):
        payload = generation._resolve_customer_notification_payload(scope, 7923)
    assert payload["first_name"] == "Shelley"
