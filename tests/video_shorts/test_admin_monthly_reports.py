from __future__ import annotations

from datetime import date, datetime, timezone
from email import policy
from email.parser import BytesParser

import pytest
from flask import Flask, g

from app.video_shorts import video_shorts_bp
from app.video_shorts.routes import admin_reports
from app.video_shorts.services import autopilot_monthly_reports as reports
from app.video_shorts.services import outreach_email_send


class _Result:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row


class _WriteConnection:
    def __init__(self):
        self.statements = []

    def execute(self, sql, params=None):
        self.statements.append((sql, params or []))
        return _Result(("report-1",))

    def commit(self):
        return None

    def rollback(self):
        return None

    def close(self):
        return None


class _ReadConnection:
    def close(self):
        return None


class _Storage:
    def __init__(self):
        self.objects = {}

    def put_bytes(self, data, key, content_type=None):
        self.objects[key] = bytes(data)

    def read_bytes(self, key):
        return self.objects[key]

    def delete(self, key):
        self.objects.pop(key, None)


@pytest.fixture()
def app():
    test_app = Flask(__name__)
    test_app.secret_key = "reports-test"
    test_app.config["BASE_URL"] = "https://mintistudio.com"
    actor = {"role": "admin"}

    @test_app.before_request
    def _actor():
        g.vs_current_user = {"id": "admin-1", "role": actor["role"]}

    test_app.register_blueprint(video_shorts_bp, url_prefix="/video_shorts")
    return test_app, actor


def _scope(brand_id="brand-1"):
    return {
        "owner_user_id": "owner-1",
        "brand_id": brand_id,
        "brand_name": "Shelley Brand",
        "recipient": "shelley@example.com",
        "account_name": "Dr. Shelley Meyer",
    }


def _record(brand_id="brand-1", status="draft"):
    return {
        "id": "report-1",
        "owner_user_id": "owner-1",
        "brand_id": brand_id,
        "month": date(2026, 11, 1),
        "status": status,
        "s3_key": f"private/owner-1/{brand_id}/monthly-reports/2026-11/frozen.pdf",
        "recipient": "shelley@example.com",
        "headline": "Search is bringing new viewers to your Shorts.",
        "tracking_token": "tracking-token",
    }


@pytest.mark.parametrize("brand_id", ["hoca-brand", "shelley-brand"])
def test_generate_draft_through_real_route(app, monkeypatch, brand_id):
    test_app, _actor = app
    storage = _Storage()
    write_conn = _WriteConnection()
    monkeypatch.setattr(admin_reports, "get_db_readonly", lambda: _ReadConnection())
    monkeypatch.setattr(admin_reports, "resolve_converted_customer", lambda _conn, *, brand_id: _scope(brand_id))
    monkeypatch.setattr(admin_reports, "load_report_record", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        admin_reports,
        "load_monthly_report_metrics",
        lambda *_args, **_kwargs: {"report_summary": {"headline": "A strong month."}},
    )
    monkeypatch.setattr(admin_reports, "issue_report_grant", lambda **_kwargs: "grant")
    monkeypatch.setattr(admin_reports, "render_report_pdf", lambda **_kwargs: (b"%PDF-frozen", 0.42))
    monkeypatch.setattr(admin_reports, "get_media_storage", lambda: storage)
    monkeypatch.setattr(admin_reports, "get_db", lambda: write_conn)
    response = test_app.test_client().post(f"/video_shorts/admin/reports/{brand_id}/2026-11/generate", json={})
    assert response.status_code == 200
    assert response.get_json()["ok"] is True
    assert list(storage.objects.values()) == [b"%PDF-frozen"]
    insert = next(item for item in write_conn.statements if "INSERT INTO autopilot_monthly_reports" in item[0])
    assert insert[1][1:4] == ["owner-1", brand_id, date(2026, 11, 1)]


def test_preview_returns_frozen_pdf_and_brand_mismatch_is_403(app, monkeypatch):
    test_app, _actor = app
    storage = _Storage()
    storage.objects[_record()["s3_key"]] = b"%PDF-preview"
    monkeypatch.setattr(admin_reports, "get_db_readonly", lambda: _ReadConnection())
    monkeypatch.setattr(admin_reports, "resolve_converted_customer", lambda _conn, *, brand_id: _scope("brand-1"))
    monkeypatch.setattr(admin_reports, "load_report_record", lambda *_args, **_kwargs: _record())
    monkeypatch.setattr(admin_reports, "get_media_storage", lambda: storage)
    client = test_app.test_client()
    preview = client.get("/video_shorts/admin/reports/brand-1/2026-11/preview")
    assert preview.status_code == 200
    assert preview.data == b"%PDF-preview"
    assert preview.mimetype == "application/pdf"
    mismatch = client.get("/video_shorts/admin/reports/other-brand/2026-11/preview")
    assert mismatch.status_code == 403


def test_reports_non_admin_is_403(app):
    test_app, actor = app
    actor["role"] = "member"
    response = test_app.test_client().get("/video_shorts/admin/reports")
    assert response.status_code == 403


def test_mocked_zoho_message_has_report_attachment_and_headers(monkeypatch):
    smtp_calls = {}
    monkeypatch.setattr(
        outreach_email_send,
        "_zoho_smtp_settings",
        lambda: {"host": "smtp.zoho.com", "port": 465, "user": "info@mintistudio.com", "password": "app-password"},
    )

    class FakeSmtp:
        def __init__(self, host, port, **kwargs):
            smtp_calls.update(host=host, port=port)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def login(self, user, password):
            smtp_calls["login_user"] = user

        def sendmail(self, from_addr, to_addrs, raw_message):
            smtp_calls.update(from_addr=from_addr, to_addrs=to_addrs, raw_message=raw_message)
            return {}

    monkeypatch.setattr(outreach_email_send.smtplib, "SMTP_SSL", FakeSmtp)
    email = reports.build_monthly_report_email(
        first_name="Shelley",
        month=date(2026, 11, 1),
        headline="Search is bringing new viewers.",
        dashboard_url="https://mintistudio.com/video_shorts/r/token",
    )
    result = reports.send_monthly_report_email(
        to_email="shelley@example.com",
        subject=email["subject"],
        html_body=email["html"],
        text_body=email["text"],
        pdf_bytes=b"%PDF-attachment",
        filename="Minti-Shelley-2026-11.pdf",
    )
    message = BytesParser(policy=policy.default).parsebytes(smtp_calls["raw_message"])
    attachment = next(message.iter_attachments())
    assert message["From"].addresses[0].display_name == "Minti Studio"
    assert message["From"].addresses[0].addr_spec == "info@mintistudio.com"
    assert message["Reply-To"].addresses[0].addr_spec == "info@mintistudio.com"
    assert str(message["Subject"]) == "Your Shorts report — November 2026"
    assert attachment.get_filename() == "Minti-Shelley-2026-11.pdf"
    assert attachment.get_content_type() == "application/pdf"
    assert attachment.get_payload(decode=True) == b"%PDF-attachment"
    assert result["message_id"] == str(message["Message-ID"])


def test_mocked_zoho_send_through_real_route_uses_frozen_attachment(app, monkeypatch):
    test_app, _actor = app
    storage = _Storage()
    storage.objects[_record()["s3_key"]] = b"%PDF-frozen-route"
    write_conn = _WriteConnection()
    smtp_calls = {}
    monkeypatch.setattr(admin_reports, "get_db_readonly", lambda: _ReadConnection())
    monkeypatch.setattr(admin_reports, "resolve_converted_customer", lambda _conn, *, brand_id: _scope(brand_id))
    monkeypatch.setattr(admin_reports, "load_report_record", lambda *_args, **_kwargs: _record())
    monkeypatch.setattr(admin_reports, "get_media_storage", lambda: storage)
    monkeypatch.setattr(admin_reports, "get_db", lambda: write_conn)
    monkeypatch.setattr(
        outreach_email_send,
        "_zoho_smtp_settings",
        lambda: {"host": "smtp.zoho.com", "port": 465, "user": "info@mintistudio.com", "password": "app-password"},
    )

    class FakeSmtp:
        def __init__(self, host, port, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def login(self, user, password):
            pass

        def sendmail(self, from_addr, to_addrs, raw_message):
            smtp_calls["raw_message"] = raw_message
            return {}

    monkeypatch.setattr(outreach_email_send.smtplib, "SMTP_SSL", FakeSmtp)
    response = test_app.test_client().post(
        "/video_shorts/admin/reports/brand-1/2026-11/send",
        json={"confirm_resend": False},
    )
    assert response.status_code == 200
    message = BytesParser(policy=policy.default).parsebytes(smtp_calls["raw_message"])
    attachment = next(message.iter_attachments())
    assert attachment.get_payload(decode=True) == b"%PDF-frozen-route"
    assert message["From"].addresses[0].display_name == "Minti Studio"
    assert "/video_shorts/r/tracking-token" in message.get_body(preferencelist=("html",)).get_content()
    assert any("status = 'sent'" in sql for sql, _params in write_conn.statements)


def test_tracking_redirect_records_click_and_range(app, monkeypatch):
    test_app, _actor = app

    class ClickConnection(_WriteConnection):
        def execute(self, sql, params=None):
            self.statements.append((sql, params or []))
            if "SELECT month" in sql:
                return _Result((date(2026, 11, 1),))
            return _Result()

    conn = ClickConnection()
    monkeypatch.setattr(admin_reports, "get_db", lambda: conn)
    response = test_app.test_client().get("/video_shorts/r/test-token")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/video_shorts/stats?start=2026-11-01&end=2026-11-30")
    assert any("first_clicked_at = COALESCE" in sql for sql, _params in conn.statements)
