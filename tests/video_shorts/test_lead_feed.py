from flask import Flask, g

from app.video_shorts.routes import channels


class _LeadFeedConnection:
    def execute(self, sql, params):
        assert params == ["user-1", "brand-1"]
        assert "LEFT JOIN shorts_storage_assets poster_asset" in sql
        return self

    def fetchall(self):
        return [("short-1", "clip.mp4", "Test Short", None, "clip-poster.jpg")]

    def close(self):
        pass


def test_lead_feed_scopes_media_urls_to_current_tenant(monkeypatch):
    app = Flask(__name__)
    app.add_url_rule("/my-videos", "video_shorts_bp.my_videos_page", lambda: "")
    app.add_url_rule(
        "/autopilot-confirmation",
        "video_shorts_bp.autopilot_confirmation_page",
        lambda: "",
    )
    app.add_url_rule("/lead-feed/event", "video_shorts_bp.lead_feed_event", lambda: "")

    media_calls = []
    monkeypatch.setattr(channels, "current_brand_id", lambda: "brand-1")
    monkeypatch.setattr(channels, "_current_user_is_pending_autopilot_lead", lambda *_args: True)
    monkeypatch.setattr(channels, "_load_current_lead_feed_tracking_context", lambda *_args: {})
    monkeypatch.setattr(channels, "get_db_readonly", _LeadFeedConnection)
    monkeypatch.setattr(
        channels,
        "_short_card_media_urls",
        lambda filename, user_id, brand_id, poster_filename: (
            media_calls.append((filename, user_id, brand_id, poster_filename)) or "/media/clip.mp4",
            "/media/poster.jpg",
        ),
    )
    monkeypatch.setattr(channels, "stripe_is_configured", lambda: False)
    monkeypatch.setattr(channels, "render_template", lambda _template, **context: context)

    with app.test_request_context("/lead-feed"):
        g.vs_current_user = {"id": "user-1", "pending_service_intent": "autopilot"}
        context = channels.lead_feed_page()

    assert media_calls == [("clip.mp4", "user-1", "brand-1", "clip-poster.jpg")]
    assert context["shorts"][0]["video_url"] == "/media/clip.mp4"
    assert context["first_short"]["poster_url"] == "/media/poster.jpg"
