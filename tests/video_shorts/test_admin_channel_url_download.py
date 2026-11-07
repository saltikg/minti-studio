from flask import Flask, g, get_flashed_messages

from app.video_shorts import video_shorts_bp
from app.video_shorts.routes import generation, quick_short, videos


VIDEO_ID = "abc123DEF45"
YOUTUBE_CHANNEL_ID = "UC1234567890123456789012"


class _ChannelDownloadConnection:
    backend_name = "duckdb"

    def __init__(self):
        self.inserted = False

    def execute(self, sql, params=None):
        normalized = " ".join(str(sql).split())
        if "FROM youtube_channels" in normalized:
            return _Result(
                (
                    4,
                    "HerkulNagme",
                    "https://www.youtube.com/@HerkulNagme",
                    "owner-1",
                    "brand-1",
                    YOUTUBE_CHANNEL_ID,
                )
            )
        if "SELECT id FROM youtube_videos" in normalized and "(channel_id = ? OR local_bucket_channel_id = ?)" in normalized:
            return _Result(None)
        if "SELECT id, owner_user_id, brand_id, local_bucket_channel_id" in normalized:
            return _Result(None)
        if normalized.startswith("INSERT INTO youtube_videos"):
            self.inserted = True
            return _Result(None)
        if "SELECT id FROM youtube_videos" in normalized and "ORDER BY id DESC" in normalized:
            return _Result((99,))
        raise AssertionError(f"Unexpected SQL: {normalized}")

    def commit(self):
        return None

    def close(self):
        return None


class _Result:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


def _app():
    app = Flask(__name__)
    app.secret_key = "test"
    app.register_blueprint(video_shorts_bp)
    return app


def _patch_common(monkeypatch, conn):
    monkeypatch.setattr(videos, "get_db", lambda: conn)
    monkeypatch.setattr(videos, "ensure_brand_schema", lambda _conn: None)
    monkeypatch.setattr(videos, "ensure_channel_owner_schema", lambda _conn: None)
    monkeypatch.setattr(videos, "ensure_youtube_video_local_bucket_schema", lambda _conn: None)
    monkeypatch.setattr(videos, "_ensure_video_crop_schema", lambda _conn: None)
    monkeypatch.setattr(videos, "current_brand_id", lambda: "brand-1")
    monkeypatch.setattr(videos, "_preferred_brand_channel_ids", lambda *_args: set())

    def _skip_oembed(*_args, **_kwargs):
        raise RuntimeError("oEmbed disabled in unit test")

    monkeypatch.setattr(videos.requests, "get", _skip_oembed)


def test_admin_can_add_channel_video_by_url_and_queue_shared_ingest(monkeypatch):
    app = _app()
    conn = _ChannelDownloadConnection()
    _patch_common(monkeypatch, conn)
    monkeypatch.setattr(
        videos,
        "fetch_video_metadata",
        lambda _video_id: {
            "video_id": VIDEO_ID,
            "title": "A lecture",
            "thumbnail_url": "https://example.com/thumb.jpg",
            "channel_id": YOUTUBE_CHANNEL_ID,
            "duration_seconds": 3600,
        },
    )
    monkeypatch.setattr(videos, "_enrich_real_youtube_channel_meta", lambda meta: meta)
    monkeypatch.setattr(videos, "_load_admin_global_outreach_match", lambda *_args: None)
    monkeypatch.setattr(generation, "apply_autopilot_design_to_video", lambda **_kwargs: None)
    captured = {}

    def _queue(**kwargs):
        captured.update(kwargs)
        return {"cached": False}

    monkeypatch.setattr(quick_short, "create_youtube_ingest_session_for_user", _queue)

    with app.test_request_context(
        "/video_shorts/videos/4/add-by-url",
        method="POST",
        data={"video_url": f"https://www.youtube.com/watch?v={VIDEO_ID}"},
    ):
        g.vs_current_user = {"id": "admin-1", "role": "admin"}
        response = videos.add_video_by_url(4)
        messages = get_flashed_messages(with_categories=True)

    assert response.status_code == 302
    assert conn.inserted is True
    assert captured["user_id"] == "owner-1"
    assert captured["brand_id"] == "brand-1"
    assert captured["existing_video_pk"] == 99
    assert captured["source"] == "admin_channel_url_download"
    assert captured["job_payload_extra"] == {
        "quota_exempt": True,
        "made_by_admin": True,
        "acting_admin_id": "admin-1",
        "admin_operation_kind": "channel",
    }
    assert ("success", "Video added to the channel and queued for download.") in messages


def test_non_admin_cannot_download_by_url_into_youtube_channel(monkeypatch):
    app = _app()
    conn = _ChannelDownloadConnection()
    _patch_common(monkeypatch, conn)

    with app.test_request_context(
        "/video_shorts/videos/4/add-by-url",
        method="POST",
        data={"video_url": f"https://www.youtube.com/watch?v={VIDEO_ID}"},
    ):
        g.vs_current_user = {"id": "owner-1", "role": "member"}
        response = videos.add_video_by_url(4)
        messages = get_flashed_messages(with_categories=True)

    assert response.status_code == 302
    assert conn.inserted is False
    assert ("warning", "Only admins can download a video by URL into a YouTube channel.") in messages


def test_admin_url_download_rejects_video_from_another_channel(monkeypatch):
    app = _app()
    conn = _ChannelDownloadConnection()
    _patch_common(monkeypatch, conn)
    monkeypatch.setattr(
        videos,
        "fetch_video_metadata",
        lambda _video_id: {
            "video_id": VIDEO_ID,
            "title": "Wrong channel video",
            "channel_id": "UC9999999999999999999999",
            "duration_seconds": 600,
        },
    )
    monkeypatch.setattr(videos, "_enrich_real_youtube_channel_meta", lambda meta: meta)
    monkeypatch.setattr(
        quick_short,
        "create_youtube_ingest_session_for_user",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("download must not be queued")),
    )

    with app.test_request_context(
        "/video_shorts/videos/4/add-by-url",
        method="POST",
        data={"video_url": f"https://www.youtube.com/watch?v={VIDEO_ID}"},
    ):
        g.vs_current_user = {"id": "admin-1", "role": "admin"}
        response = videos.add_video_by_url(4)
        messages = get_flashed_messages(with_categories=True)

    assert response.status_code == 302
    assert conn.inserted is False
    assert ("warning", "That video does not belong to this YouTube channel.") in messages
