from flask import Flask

from app import create_app
from app.video_shorts.routes import quick_short as quick_short_routes
from app.video_shorts.services import error_capture
from app.video_shorts.youtube_api import (
    YOUTUBE_INPUT_MESSAGES,
    extract_video_id,
    parse_youtube_video_input,
    sanitize_rejected_youtube_input,
)


def test_extracts_supported_youtube_video_urls():
    cases = {
        "https://www.youtube.com/watch?v=watch123&list=PL1&t=9": "watch123",
        "https://m.youtube.com/watch?v=mobile123&si=x": "mobile123",
        "https://music.youtube.com/watch?v=music123": "music123",
        "https://youtu.be/short123?si=x": "short123",
        "https://youtube.com/shorts/shorts123?feature=share": "shorts123",
        "https://youtube.com/live/live123?si=x": "live123",
        "https://youtube.com/embed/embed123": "embed123",
        "https://www.youtube-nocookie.com/embed/private123": "private123",
        "Some title https://youtu.be/abc123?si=x More text": "abc123",
    }
    for value, expected in cases.items():
        assert extract_video_id(value) == expected


def test_uses_the_first_youtube_url_in_pasted_text():
    value = "Article https://example.com/x then https://youtu.be/first123 and https://youtu.be/second456"
    assert extract_video_id(value) == "first123"


def test_rejection_messages_are_specific():
    cases = {
        "https://youtube.com/@doctor": "channel_link",
        "https://youtube.com/channel/UC123": "channel_link",
        "https://youtube.com/c/creator": "channel_link",
        "https://youtube.com/playlist?list=PL123": "playlist_link",
        "not a youtube link": "unreadable_link",
    }
    for value, reason in cases.items():
        result = parse_youtube_video_input(value)
        assert result.video_id is None
        assert result.rejection_reason == reason
        assert result.message == YOUTUBE_INPUT_MESSAGES[reason]


def test_rejected_input_sanitizer_keeps_only_allowed_query_fields():
    value = "Title https://youtube.com/playlist?list=PL123&t=5&si=secret&email=a%40b.com trailing"
    assert sanitize_rejected_youtube_input(value) == "https://youtube.com/playlist?list=PL123&t=5"
    assert len(sanitize_rejected_youtube_input("x" * 500)) == 200


def test_http_error_event_names_keep_status_code(monkeypatch):
    recorded = []
    monkeypatch.setattr(error_capture, "_record_event", lambda **kwargs: recorded.append(kwargs))
    app = Flask(__name__)

    with app.test_request_context("/example"):
        error_capture.capture_server_error(status_code=400)
        assert recorded[-1]["event_name"] == "client_error"
        assert recorded[-1]["status"] == "400"

    with app.test_request_context("/example"):
        error_capture.capture_server_error(status_code=500)
        assert recorded[-1]["event_name"] == "server_error"
        assert recorded[-1]["status"] == "500"


def test_ingest_logs_sanitized_rejection_but_preview_does_not(monkeypatch):
    events = []
    monkeypatch.setattr(quick_short_routes, "_require_quick_user", lambda: ({"id": "user-1"}, None))
    monkeypatch.setattr(quick_short_routes, "_ensure_quick_schema", lambda: None)
    monkeypatch.setattr(quick_short_routes, "current_brand_id", lambda: "brand-1")
    monkeypatch.setattr(quick_short_routes, "track_event", lambda *args, **kwargs: events.append((args, kwargs)))
    app = create_app()

    with app.test_request_context(
        "/video_shorts/shorts/quick/api/youtube-metadata",
        method="POST",
        json={"video_url": "https://youtube.com/@doctor?email=private"},
    ):
        response, status = quick_short_routes.quick_short_youtube_metadata()
        assert status == 400
        assert response.get_json()["code"] == "url_rejected"
        assert events == []

    with app.test_request_context(
        "/video_shorts/shorts/quick/api/ingest-youtube",
        method="POST",
        json={"video_url": "https://youtube.com/playlist?list=PL123&email=private"},
    ):
        response, status = quick_short_routes.quick_short_ingest_youtube()
        assert status == 400
        assert response.get_json()["rejection_reason"] == "playlist_link"

    assert len(events) == 1
    _, kwargs = events[0]
    assert kwargs["status"] == "url_rejected"
    assert kwargs["metadata"]["sanitized_input"] == "https://youtube.com/playlist?list=PL123"
