from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

from flask import Flask


def _load_analytics_module():
    module_names = [
        "app",
        "app.video_shorts",
        "app.video_shorts.config",
        "app.video_shorts.services",
        "app.video_shorts.services.db",
        "app.video_shorts.services.user_events",
    ]
    previous = {name: sys.modules.get(name) for name in module_names}
    try:
        app_package = ModuleType("app")
        app_package.__path__ = []
        video_shorts_package = ModuleType("app.video_shorts")
        video_shorts_package.__path__ = []
        services_package = ModuleType("app.video_shorts.services")
        services_package.__path__ = []
        config_module = ModuleType("app.video_shorts.config")
        config_module.INTERNAL_IPS = set()
        config_module.SIGNUP_ALIAS_ALLOWLIST = {"gokhansaltik@gmail.com"}
        db_module = ModuleType("app.video_shorts.services.db")
        db_module.get_db_readonly = lambda: (_ for _ in ()).throw(AssertionError("unexpected DB lookup"))
        events_module = ModuleType("app.video_shorts.services.user_events")
        events_module.track_event = lambda *args, **kwargs: None
        sys.modules.update(
            {
                "app": app_package,
                "app.video_shorts": video_shorts_package,
                "app.video_shorts.config": config_module,
                "app.video_shorts.services": services_package,
                "app.video_shorts.services.db": db_module,
                "app.video_shorts.services.user_events": events_module,
            }
        )
        path = Path(__file__).resolve().parents[2] / "app/video_shorts/services/free_tool_analytics.py"
        spec = importlib.util.spec_from_file_location("free_tool_analytics_under_test", path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for name, original in previous.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original


analytics = _load_analytics_module()


def _app() -> Flask:
    app = Flask(__name__)
    app.secret_key = "free-tool-analytics-test"
    return app


def test_first_touch_keeps_session_utm_and_referrer() -> None:
    app = _app()
    with app.test_request_context(
        "/tools/youtube-to-shorts?utm_source=newsletter&utm_campaign=launch",
        headers={"Referer": "https://example.com/article?id=1", "User-Agent": "Mozilla/5.0"},
    ):
        analytics.capture_first_touch(lead_id="lead-1")
        first = analytics.attribution_metadata()
        analytics.capture_first_touch(lead_id="lead-2")
        second = analytics.attribution_metadata()

    assert first["session_id"] == second["session_id"]
    assert second["utm_source"] == "newsletter"
    assert second["utm_campaign"] == "launch"
    assert second["lead_id"] == "lead-1"
    assert second["referrer"] == "https://example.com/article?id=1"


def test_direct_first_touch_is_not_reattributed_later() -> None:
    app = _app()

    @app.get("/capture")
    def capture():
        analytics.capture_first_touch()
        return analytics.attribution_metadata()

    client = app.test_client()
    direct = client.get("/capture", headers={"User-Agent": "Mozilla/5.0"}).get_json()
    later = client.get(
        "/capture?utm_source=outreach",
        headers={"Referer": "https://example.com/later", "User-Agent": "Mozilla/5.0"},
    ).get_json()

    assert direct["session_id"] == later["session_id"]
    assert "utm_source" not in later
    assert "referrer" not in later


def test_bot_and_internal_ip_requests_are_not_tracked(monkeypatch) -> None:
    app = _app()
    calls = []
    monkeypatch.setattr(analytics, "track_event", lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setattr(analytics, "INTERNAL_IPS", {"203.0.113.0/24"})

    with app.test_request_context(
        "/tools/youtube-to-shorts/event",
        headers={"User-Agent": "Slackbot-LinkExpanding 1.0"},
        environ_base={"REMOTE_ADDR": "198.51.100.10"},
    ):
        assert analytics.track_free_tool_event("free_tool_landing_view") is False

    with app.test_request_context(
        "/tools/youtube-to-shorts/event",
        headers={"User-Agent": "Mozilla/5.0"},
        environ_base={"REMOTE_ADDR": "203.0.113.42"},
    ):
        assert analytics.track_free_tool_event("free_tool_landing_view") is False

    assert calls == []


def test_allowlisted_email_alias_is_internal(monkeypatch) -> None:
    app = _app()
    monkeypatch.setattr(analytics, "SIGNUP_ALIAS_ALLOWLIST", {"first.last@gmail.com"})
    with app.test_request_context(
        "/tools/youtube-to-shorts/event",
        headers={"User-Agent": "Mozilla/5.0"},
        environ_base={"REMOTE_ADDR": "198.51.100.10"},
    ):
        assert analytics.is_internal_request(
            {"id": "admin-alias", "role": "member", "email": "first.last+qa@googlemail.com"}
        ) is True


def test_clean_client_event_records_session_and_referrer(monkeypatch) -> None:
    app = _app()
    calls = []
    monkeypatch.setattr(analytics, "track_event", lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setattr(analytics, "INTERNAL_IPS", set())

    with app.test_request_context(
        "/tools/youtube-to-shorts?utm_source=organic-test",
        headers={"Referer": "https://search.example/results", "User-Agent": "Mozilla/5.0"},
        environ_base={"REMOTE_ADDR": "198.51.100.10"},
    ):
        analytics.capture_first_touch()
        assert analytics.track_free_tool_event(
            "free_tool_landing_view",
            status="en",
            metadata={"path": "/tools/youtube-to-shorts", "client_tracked": True},
        ) is True

    assert len(calls) == 1
    metadata = calls[0][1]["metadata"]
    assert metadata["session_id"]
    assert metadata["utm_source"] == "organic-test"
    assert metadata["referrer"] == "https://search.example/results"
    assert metadata["client_tracked"] is True
