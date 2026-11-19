from __future__ import annotations

from flask import Flask

from app.video_shorts.services import ga4_measurement


def _app() -> Flask:
    app = Flask(__name__)
    app.secret_key = "ga4-test"
    return app


def test_ga4_client_id_parses_standard_cookie():
    assert ga4_measurement.ga4_client_id_from_cookie("GA1.1.123456789.987654321") == "123456789.987654321"
    assert ga4_measurement.ga4_client_id_from_cookie("invalid") == ""


def test_ga4_session_id_parses_current_and_legacy_cookies():
    assert ga4_measurement.ga4_session_id_from_cookie("GS2.1.s1700000000$o2$g1$t1700000123") == "1700000000"
    assert ga4_measurement.ga4_session_id_from_cookie("GS1.1.1700000000.2.1.1700000123") == "1700000000"
    assert ga4_measurement.ga4_session_id_from_cookie("invalid") == ""


def _capture_queued_event(monkeypatch, *, cookie: str = ""):
    queued = {}

    class FakeThread:
        def __init__(self, *, target, kwargs, name, daemon):
            queued.update({"target": target, "kwargs": kwargs, "name": name, "daemon": daemon})

        def start(self):
            queued["started"] = True

    monkeypatch.setenv("GA4_API_SECRET", "test-secret")
    monkeypatch.setattr(ga4_measurement, "Thread", FakeThread)
    headers = {"Cookie": cookie} if cookie else None
    with _app().test_request_context("/", headers=headers):
        assert ga4_measurement.send_ga4_event("sign_up", {"method": "email"}) is True
    return queued


def test_ga4_event_with_client_and_session_cookies(monkeypatch):
    queued = _capture_queued_event(
        monkeypatch,
        cookie=(
            "_ga=GA1.1.123456789.987654321; "
            "_ga_BFML4VLWMF=GS2.1.s1700000000$o2$g1$t1700000123"
        ),
    )
    assert queued["kwargs"]["payload"] == {
        "client_id": "123456789.987654321",
        "events": [{
            "name": "sign_up",
            "params": {
                "method": "email",
                "session_id": "1700000000",
                "engagement_time_msec": 1,
            },
        }],
    }
    assert queued["kwargs"]["success_outcome"] == "sent"


def test_ga4_event_with_client_cookie_only(monkeypatch):
    queued = _capture_queued_event(monkeypatch, cookie="_ga=GA1.1.123456789.987654321")
    assert queued["kwargs"]["payload"] == {
        "client_id": "123456789.987654321",
        "events": [{"name": "sign_up", "params": {"method": "email"}}],
    }
    assert queued["kwargs"]["success_outcome"] == "sent"


def test_ga4_event_without_cookies_generates_anonymous_client_id(monkeypatch):
    queued = _capture_queued_event(monkeypatch)
    payload = queued["kwargs"]["payload"]
    assert payload["client_id"]
    assert payload["events"] == [{"name": "sign_up", "params": {"method": "email"}}]
    assert queued["kwargs"]["success_outcome"] == "sent_generated_client_id"


def test_ga4_event_queues_pii_free_payload(monkeypatch):
    app = _app()
    queued = {}

    class FakeThread:
        def __init__(self, *, target, kwargs, name, daemon):
            queued.update({"target": target, "kwargs": kwargs, "name": name, "daemon": daemon})

        def start(self):
            queued["started"] = True

    monkeypatch.setenv("GA4_API_SECRET", "test-secret")
    monkeypatch.setattr(ga4_measurement, "Thread", FakeThread)
    with app.test_request_context(
        "/",
        headers={
            "Cookie": "_ga=GA1.1.123456789.987654321; minti_internal_traffic=true"
        },
    ):
        assert ga4_measurement.send_ga4_event(
            "sign_up",
            {"method": "email", "email": "must-not-send@example.com", "user_id": "secret"},
        ) is True

    assert queued["started"] is True
    assert queued["daemon"] is True
    payload = queued["kwargs"]["payload"]
    assert payload == {
        "client_id": "123456789.987654321",
        "events": [
            {
                "name": "sign_up",
                "params": {"method": "email", "traffic_type": "internal"},
            }
        ],
    }
    assert queued["kwargs"]["success_outcome"] == "sent"


def test_ga4_event_logs_skip_without_secret(monkeypatch, caplog):
    caplog.set_level("INFO", logger=ga4_measurement.__name__)
    monkeypatch.delenv("GA4_API_SECRET", raising=False)
    with _app().test_request_context("/"):
        assert ga4_measurement.send_ga4_event("sign_up") is False
    assert "event_name=sign_up outcome=skipped_no_secret http_status=none" in caplog.text
