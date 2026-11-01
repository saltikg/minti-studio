from __future__ import annotations

import pytest
from flask import Flask, g, session

from app.video_shorts import video_shorts_bp
from app.video_shorts.routes import generation
from app.video_shorts.services.feature_flags import tiktok_enabled


def test_tiktok_flag_defaults_to_disabled(monkeypatch) -> None:
    monkeypatch.delenv("TIKTOK_ENABLED", raising=False)
    assert tiktok_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
def test_tiktok_flag_accepts_explicit_true_values(monkeypatch, value: str) -> None:
    monkeypatch.setenv("TIKTOK_ENABLED", value)
    assert tiktok_enabled() is True


def _render_overview(*, enabled: bool) -> str:
    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.register_blueprint(video_shorts_bp)
    entry = {
        "video_id": "source-video",
        "plan_index": 1,
        "plan_title": "Test short",
        "channel_name": "Test channel",
        "video_url": "https://example.com/video",
        "publish_status": "scheduled",
        "publish_status_label": "Scheduled",
        "publish_label": "2026-11-02 03:40 PST",
        "instagram_entries": [],
        "facebook_entries": [],
        "tiktok_entries": [
            {
                "status": "failed",
                "status_label": "Failed",
                "badge_class": "bg-danger",
                "publish_display": "2026-11-02 03:40 PST",
            }
        ],
    }
    with app.test_request_context("/video_shorts/shorts/overview"):
        return app.jinja_env.get_template("shorts_overview.html").render(
            comments_enabled=False,
            tiktok_enabled=enabled,
            entries=[entry],
            total_scheduled=1,
            total_published=0,
            entries_count=1,
            status_filter="",
            comments_filter="",
            search_q="",
            channel_filter="",
            sort_key="publish",
            sort_dir="desc",
            channel_options=[],
            page=1,
            total_pages=1,
            showing_from=1,
            showing_to=1,
            pending_only=False,
            pending_videos_total=0,
            calendar_summary={},
            day_filter="",
            unread_videos_total=0,
            last_comment_check_display=None,
            vs_current_user=None,
            guest_mode=False,
        )


def test_disabled_publishing_render_has_no_tiktok_ui() -> None:
    html = _render_overview(enabled=False)

    assert "TikTok" not in html
    assert "tiktokErrorModal" not in html
    assert 'key: "tiktok"' not in html
    assert "View error" not in html


def test_enabled_publishing_render_keeps_existing_tiktok_ui() -> None:
    html = _render_overview(enabled=True)

    assert "<th>TikTok</th>" in html
    assert "TikTok Publish Error" in html
    assert 'key: "tiktok"' in html
    assert "View error" in html


def _render_template(name: str, **context) -> str:
    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.register_blueprint(video_shorts_bp)
    with app.test_request_context("/video_shorts/test"):
        return app.jinja_env.get_template(name).render(vs_current_user=None, **context)


def test_disabled_remaining_publish_controls_are_not_rendered() -> None:
    ai_html = _render_template(
        "ai_video_create.html",
        items=[],
        tiktok_enabled=False,
        instagram_connected=False,
        facebook_connected=False,
        tiktok_connected=False,
        youtube_connected=False,
        user_time_zone="UTC",
        user_time_zone_label="UTC",
    )
    workflow_html = _render_template(
        "workflow_monthly_top_video.html",
        tiktok_enabled=False,
        channel_options=[{"value": "all", "label": "All channels"}],
        top_n_options=[],
        top_rows=[],
        generated_videos=[],
        render_order=[],
        youtube_connected=False,
        instagram_connected=False,
        facebook_connected=False,
        tiktok_connected=False,
    )
    logs_html = _render_template(
        "social_logs.html",
        rows=[{"timestamp": "now", "tiktok": "TIKTOK_ONLY_ERROR_MARKER"}],
        lines=[],
        log_path="test.log",
        last_run=None,
        limit=1,
        tiktok_enabled=False,
    )

    assert 'id="aiTiktokSettingsCard"' not in ai_html
    assert 'id="tiktokSettingsCard"' not in workflow_html
    assert "bi-tiktok" not in logs_html
    assert "TIKTOK_ONLY_ERROR_MARKER" not in logs_html


def test_disabled_tiktok_authorize_does_not_prepare_oauth_state(monkeypatch) -> None:
    monkeypatch.delenv("TIKTOK_ENABLED", raising=False)
    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.register_blueprint(video_shorts_bp)

    with app.test_request_context("/video_shorts/tiktok/authorize"):
        g.vs_current_user = {"id": "test-user"}
        session["tt_oauth_state"] = {"nonce": "stale"}
        response = generation.tiktok_authorize()

        assert response.status_code == 302
        assert response.location.endswith("/video_shorts/social/connect")
        assert "tt_oauth_state" not in session
