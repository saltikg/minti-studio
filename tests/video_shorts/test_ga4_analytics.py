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


def test_ga4_event_skips_without_ga_cookie(monkeypatch):
    app = _app()
    monkeypatch.setenv("GA4_API_SECRET", "test-secret")
    with app.test_request_context("/"):
        assert ga4_measurement.send_ga4_event("sign_up", {"method": "email"}) is False


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
