from __future__ import annotations

import pytest
from flask import Flask

from app.video_shorts import video_shorts_bp
from app.video_shorts.services.feature_flags import comments_enabled
from app.video_shorts.tasks import run_social_jobs
from app.video_shorts.tasks import sync_short_comments


def test_comments_flag_defaults_to_disabled(monkeypatch) -> None:
    monkeypatch.delenv("COMMENTS_ENABLED", raising=False)
    assert comments_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
def test_comments_flag_accepts_explicit_true_values(monkeypatch, value: str) -> None:
    monkeypatch.setenv("COMMENTS_ENABLED", value)
    assert comments_enabled() is True


def test_disabled_comment_sync_exits_before_app_or_api_work(monkeypatch, capsys) -> None:
    monkeypatch.setenv("COMMENTS_ENABLED", "false")
    monkeypatch.setattr(
        sync_short_comments,
        "create_app",
        lambda: pytest.fail("disabled comments must exit before app creation"),
    )

    assert sync_short_comments.main() == 0
    assert "comments disabled" in capsys.readouterr().out


def test_disabled_dedicated_runner_exits_before_locked_work(monkeypatch, capsys) -> None:
    monkeypatch.setenv("COMMENTS_ENABLED", "false")
    monkeypatch.setattr(
        run_social_jobs,
        "_run_locked_step",
        lambda *args, **kwargs: pytest.fail("disabled comments must exit before locked work"),
    )
    monkeypatch.setattr(
        "sys.argv",
        ["run_social_jobs.py", "--only-youtube-comments"],
    )

    assert run_social_jobs.main() == 0
    assert "comments disabled" in capsys.readouterr().out


@pytest.mark.parametrize(
    "path",
    [
        "/video_shorts/shorts/comments",
        "/video_shorts/shorts/comments/feed",
        "/video_shorts/shorts/comments/video-1",
        "/video_shorts/instagram/media/queue-1/comments",
        "/video_shorts/facebook/media/queue-1/comments",
    ],
)
def test_disabled_comment_routes_redirect_to_my_videos(monkeypatch, path: str) -> None:
    monkeypatch.setenv("COMMENTS_ENABLED", "false")
    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.register_blueprint(video_shorts_bp)

    response = app.test_client().get(path)

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/video_shorts/my-videos")


def test_disabled_publishing_render_has_no_comment_ui_or_fetches() -> None:
    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.register_blueprint(video_shorts_bp)
    with app.test_request_context("/video_shorts/shorts/overview"):
        html = app.jinja_env.get_template("shorts_overview.html").render(
            comments_enabled=False,
            entries=[],
            total_scheduled=0,
            total_published=0,
            entries_count=0,
            status_filter="",
            comments_filter="",
            search_q="",
            channel_filter="",
            sort_key="publish",
            sort_dir="desc",
            channel_options=[],
            page=1,
            total_pages=1,
            showing_from=0,
            showing_to=0,
            pending_only=False,
            pending_videos_total=0,
            calendar_summary={},
            day_filter="",
            unread_videos_total=0,
            last_comment_check_display=None,
            vs_current_user=None,
            guest_mode=False,
        )

    assert 'id="shortCommentsModal"' not in html
    assert "data-comments-url=" not in html
    assert "instagram-comments-btn" not in html
    assert "facebook-comments-btn" not in html
    assert "/shorts/comments/" not in html
    assert "/instagram/media/${" not in html
    assert "/facebook/media/${" not in html
    assert ">Comments</span>" not in html
