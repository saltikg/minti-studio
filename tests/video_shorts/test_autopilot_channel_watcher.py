from datetime import datetime, timedelta

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
    conn.execute("INSERT INTO autopilot_leads VALUES ('l1', 'u1', 'b1', 'lead-channel', CURRENT_TIMESTAMP)")
    conn.execute("INSERT INTO youtube_oauth_tokens_v2 VALUES ('u1::b1', 'connected-channel')")
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

    first = watcher.run_watcher(
        feed_loader=lambda channel_id: list(feed),
        metadata_loader=lambda video_id: metadata[video_id],
        email_sender=lambda rows: digests.append(rows),
    )

    assert first["customers"][0]["channel_id"] == "connected-channel"
    assert first["customers"][0]["baseline"] is True
    assert first["new_videos"] == 0
    assert conn.execute(
        "SELECT youtube_video_id, status FROM autopilot_channel_videos ORDER BY youtube_video_id"
    ).fetchall() == [("long-1", "seen"), ("short-1", "skipped")]

    feed.append({"video_id": "long-2", "title": "New long", "published_at": datetime(2026, 11, 2)})
    second = watcher.run_watcher(
        feed_loader=lambda channel_id: list(feed),
        metadata_loader=lambda video_id: metadata[video_id],
        email_sender=lambda rows: digests.append(rows),
    )
    third = watcher.run_watcher(
        feed_loader=lambda channel_id: list(feed),
        metadata_loader=lambda video_id: metadata[video_id],
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
        feed_loader=lambda channel_id: feed,
        metadata_loader=lambda video_id: {
            "video_id": video_id,
            "title": video_id,
            "duration_seconds": 1000,
            "live_broadcast_content": "live" if video_id == "live-1" else "none",
        },
        email_sender=lambda rows: None,
    )

    assert conn.execute("SELECT DISTINCT status FROM autopilot_channel_videos").fetchall() == [("skipped",)]
