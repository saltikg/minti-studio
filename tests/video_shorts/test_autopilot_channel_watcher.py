from datetime import datetime, timedelta
from types import SimpleNamespace

import duckdb

from app.video_shorts.services import autopilot_channel_watcher as watcher


class _ReusableConnection:
    def __init__(self, conn):
        self.conn = conn

    def execute(self, *args, **kwargs):
        return self.conn.execute(*args, **kwargs)

    def commit(self):
        return self.conn.commit()

    def rollback(self):
        return self.conn.rollback()

    def close(self):
        pass


def _database():
    conn = duckdb.connect(":memory:")
    statements = (
        "CREATE TABLE shorts_users (id VARCHAR, email VARCHAR, username VARCHAR, service_mode VARCHAR)",
        "CREATE TABLE autopilot_leads (id VARCHAR, user_id VARCHAR, brand_id VARCHAR, youtube_channel_id VARCHAR, converted_at TIMESTAMP)",
        "CREATE TABLE youtube_oauth_tokens_v2 (user_id VARCHAR, youtube_channel_id VARCHAR)",
        "CREATE TABLE shorts_generated_videos (brand_id VARCHAR, youtube_video_id VARCHAR)",
        "CREATE TABLE shorts_youtube_schedules (brand_id VARCHAR, youtube_video_id VARCHAR)",
        "CREATE SEQUENCE autopilot_channel_videos_id_seq",
        """CREATE TABLE autopilot_channel_videos (
            id BIGINT DEFAULT nextval('autopilot_channel_videos_id_seq'), user_id VARCHAR,
            brand_id VARCHAR, channel_id VARCHAR, youtube_video_id VARCHAR, title VARCHAR,
            published_at TIMESTAMP, duration_s INTEGER, detected_at TIMESTAMP, status VARCHAR,
            UNIQUE(brand_id, youtube_video_id))""",
    )
    for statement in statements:
        conn.execute(statement)
    conn.execute("INSERT INTO shorts_users VALUES ('u1', 'customer@example.com', 'customer', 'autopilot')")
    conn.execute("INSERT INTO autopilot_leads VALUES ('l1', 'u1', 'b1', 'UC0000000000000000000000', CURRENT_TIMESTAMP)")
    conn.execute("INSERT INTO youtube_oauth_tokens_v2 VALUES ('u1::b1', 'UC1234567890123456789012')")
    return conn


def test_first_run_baselines_and_second_run_sends_one_digest(monkeypatch):
    conn = _database()
    monkeypatch.setattr(watcher, "get_db", lambda: _ReusableConnection(conn))
    feed = [
        {"video_id": "long-1", "title": "Existing long", "published_at": datetime(2026, 11, 1)},
        {"video_id": "short-1", "title": "Existing short", "published_at": datetime(2026, 11, 1)},
    ]
    metadata = {
        "long-1": {"video_id": "long-1", "title": "Existing long", "duration_seconds": 900},
        "short-1": {"video_id": "short-1", "title": "Existing short", "duration_seconds": 180},
        "long-2": {"video_id": "long-2", "title": "New long", "duration_seconds": 1200},
    }
    digests = []

    def channel_loader(_channel_id):
        return [{**item, **metadata[item["video_id"]]} for item in feed], "rss"

    first = watcher.run_watcher(
        channel_loader=channel_loader,
        email_sender=lambda rows: digests.append(rows),
    )

    assert first["customers"][0]["channel_id"] == "UC1234567890123456789012"
    assert first["customers"][0]["baseline"] is True
    assert first["new_videos"] == 0
    assert conn.execute(
        "SELECT youtube_video_id, status FROM autopilot_channel_videos ORDER BY youtube_video_id"
    ).fetchall() == [("long-1", "seen"), ("short-1", "skipped")]

    feed.append({"video_id": "long-2", "title": "New long", "published_at": datetime(2026, 11, 2)})
    second = watcher.run_watcher(
        channel_loader=channel_loader,
        email_sender=lambda rows: digests.append(rows),
    )
    third = watcher.run_watcher(
        channel_loader=channel_loader,
        email_sender=lambda rows: digests.append(rows),
    )

    assert second["new_videos"] == 1
    assert second["email_sent"] is True
    assert third["new_videos"] == 0
    assert len(digests) == 1
    assert digests[0][0]["youtube_video_id"] == "long-2"
    assert conn.execute(
        "SELECT status FROM autopilot_channel_videos WHERE youtube_video_id='long-2'"
    ).fetchone()[0] == "new"


def test_live_and_minti_published_videos_are_skipped(monkeypatch):
    conn = _database()
    conn.execute("INSERT INTO shorts_generated_videos VALUES ('b1', 'minti-1')")
    monkeypatch.setattr(watcher, "get_db", lambda: _ReusableConnection(conn))
    feed = [
        {"video_id": "live-1", "title": "Live", "published_at": datetime.now() - timedelta(hours=1)},
        {"video_id": "minti-1", "title": "Minti", "published_at": datetime.now()},
    ]

    watcher.run_watcher(
        channel_loader=lambda channel_id: (
            [
                {
                    **item,
                    "duration_seconds": 1000,
                    "live_broadcast_content": "live" if item["video_id"] == "live-1" else "none",
                }
                for item in feed
            ],
            "rss",
        ),
        email_sender=lambda rows: None,
    )

    assert conn.execute("SELECT DISTINCT status FROM autopilot_channel_videos").fetchall() == [("skipped",)]


def test_first_successful_fetch_marks_post_deploy_upload_new(monkeypatch):
    conn = _database()
    monkeypatch.setattr(watcher, "get_db", lambda: _ReusableConnection(conn))
    digests = []
    result = watcher.run_watcher(
        channel_loader=lambda channel_id: (
            [
                {
                    "video_id": "after-deploy",
                    "title": "After deploy",
                    "published_at": datetime(2026, 11, 3, 7, 0),
                    "duration_seconds": 600,
                }
            ],
            "data_api",
        ),
        email_sender=lambda rows: digests.append(rows),
    )

    assert result["customers"][0]["baseline"] is True
    assert result["customers"][0]["fetch_source"] == "data_api"
    assert result["customers"][0]["videos_found"] == 1
    assert result["new_videos"] == 1
    assert len(digests) == 1
    assert conn.execute("SELECT status FROM autopilot_channel_videos").fetchone()[0] == "new"


def test_fetch_chain_falls_back_to_api_and_batches_metadata(monkeypatch):
    calls = []
    monkeypatch.setattr(
        watcher,
        "fetch_channel_feed",
        lambda channel_id: (_ for _ in ()).throw(RuntimeError("rss down")),
    )
    monkeypatch.setattr(
        watcher,
        "fetch_channel_data_api",
        lambda channel_id: [
            {"video_id": "v1", "title": "One", "published_at": datetime(2026, 11, 3)}
        ],
    )
    monkeypatch.setattr(
        watcher,
        "fetch_video_stats",
        lambda video_ids: calls.append(list(video_ids)) or {"v1": {"duration_seconds": 500}},
    )
    monkeypatch.setattr(
        watcher,
        "fetch_channel_yt_dlp",
        lambda channel_id: (_ for _ in ()).throw(AssertionError("yt-dlp should not run")),
    )

    rows, source = watcher.fetch_channel_videos("UC1234567890123456789012")

    assert source == "data_api"
    assert calls == [["v1"]]
    assert rows[0]["duration_seconds"] == 500


def test_yt_dlp_fallback_is_flat_and_never_downloads(monkeypatch):
    captured = []

    def fake_run(command, **kwargs):
        captured.append(command)
        return SimpleNamespace(
            returncode=0,
            stdout='{"entries":[{"id":"v1","title":"One","duration":700,"timestamp":1793689200}]}',
        )

    monkeypatch.setattr(watcher.subprocess, "run", fake_run)
    monkeypatch.setattr(watcher, "YT_DLP_COOKIES", None)

    rows = watcher.fetch_channel_yt_dlp("UC1234567890123456789012")

    assert "--flat-playlist" in captured[0]
    assert captured[0][captured[0].index("--playlist-end") + 1] == "15"
    assert "--skip-download" in captured[0]
    assert rows[0]["video_id"] == "v1"
    assert rows[0]["duration_seconds"] == 700
