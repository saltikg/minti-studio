from __future__ import annotations

from flask import Flask

from app.video_shorts import video_shorts_bp
from app.video_shorts.routes.videos import _publishing_thumbnail_url


def test_pending_row_prefers_stored_poster() -> None:
    assert _publishing_thumbnail_url(
        {
            "publish_status": "scheduled",
            "poster_url": "/media/poster.jpg",
            "thumbnail_url": "https://i.ytimg.com/source.jpg",
        }
    ) == "/media/poster.jpg"


def test_published_row_keeps_youtube_thumbnail_priority() -> None:
    assert _publishing_thumbnail_url(
        {
            "publish_status": "published",
            "short_thumbnail_url": "https://i.ytimg.com/short.jpg",
            "poster_url": "/media/poster.jpg",
            "thumbnail_url": "https://i.ytimg.com/source.jpg",
        }
    ) == "https://i.ytimg.com/short.jpg"


def test_row_without_any_thumbnail_renders_placeholder() -> None:
    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.register_blueprint(video_shorts_bp)
    entry = {
        "video_id": "source-video",
        "plan_index": 1,
        "plan_title": "No thumbnail",
        "channel_name": "Test channel",
        "video_url": "https://example.com/video",
        "publish_status": "scheduled",
        "publish_status_label": "Scheduled",
        "display_thumbnail_url": "",
        "instagram_entries": [],
        "facebook_entries": [],
        "tiktok_entries": [],
    }
    with app.test_request_context("/video_shorts/shorts/overview"):
        html = app.jinja_env.get_template("shorts_overview.html").render(
            comments_enabled=False,
            tiktok_enabled=False,
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

    assert "shorts-thumb-placeholder" in html
    assert '<img class="shorts-thumb"' not in html
