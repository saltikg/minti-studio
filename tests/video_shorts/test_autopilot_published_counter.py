from datetime import datetime, timezone

import duckdb

from app.video_shorts.routes.generation import (
    _load_monthly_autopilot_published_clips,
    _pacific_calendar_month_bounds_utc,
)


def _connection():
    conn = duckdb.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE shorts_generated_videos (
            id BIGINT,
            user_id VARCHAR,
            source_channel_type VARCHAR,
            source_video_id VARCHAR,
            clip_filename VARCHAR,
            output_filename VARCHAR,
            generated_title VARCHAR,
            publish_status VARCHAR,
            youtube_video_id VARCHAR,
            youtube_published_at TIMESTAMP,
            instagram_media_id VARCHAR,
            instagram_published_at TIMESTAMP,
            facebook_video_id VARCHAR,
            facebook_published_at TIMESTAMP,
            tiktok_video_id VARCHAR,
            tiktok_published_at TIMESTAMP
        )
        """
    )
    return conn


def _insert(conn, row):
    conn.execute(
        "INSERT INTO shorts_generated_videos VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        row,
    )


def test_youtube_and_instagram_publication_counts_one_distinct_clip():
    conn = _connection()
    _insert(
        conn,
        (
            1, "user-1", "youtube", "source-1", "1_source.mp4", "1_source.mp4",
            "Published twice", "published", "yt-1", datetime(2026, 11, 5, 17),
            "ig-1", datetime(2026, 11, 5, 17, 1), None, None, None, None,
        ),
    )

    clips = _load_monthly_autopilot_published_clips(
        conn, "user-1", now=datetime(2026, 11, 10, tzinfo=timezone.utc)
    )

    assert len(clips) == 1
    assert [item["platform"] for item in clips[0]["platforms"]] == ["youtube", "instagram"]


def test_unpublished_cancelled_and_prepared_clips_do_not_count():
    conn = _connection()
    for clip_id, status in ((1, "ready"), (2, "cancelled"), (3, "unscheduled")):
        _insert(
            conn,
            (
                clip_id, "user-1", "youtube", "source-1", f"{clip_id}_source.mp4",
                f"{clip_id}_source.mp4", status, status, None, None, None, None,
                None, None, None, None,
            ),
        )
    _insert(
        conn,
        (
            4, "user-1", "youtube", "source-1", "4_source.mp4", "4_source.mp4",
            "No successful provider ID", "cancelled", "", datetime(2026, 11, 5, 17),
            None, None, None, None, None, None,
        ),
    )

    clips = _load_monthly_autopilot_published_clips(
        conn, "user-1", now=datetime(2026, 11, 10, tzinfo=timezone.utc)
    )

    assert clips == []


def test_month_boundary_is_pacific_time_including_dst_offset_change():
    conn = _connection()
    # November 1 starts at 07:00 UTC (PDT); December 1 starts at 08:00 UTC (PST).
    for clip_id, published_at in (
        (1, datetime(2026, 11, 1, 6, 59, 59)),
        (2, datetime(2026, 11, 1, 7, 0, 0)),
        (3, datetime(2026, 12, 1, 7, 59, 59)),
        (4, datetime(2026, 12, 1, 8, 0, 0)),
    ):
        _insert(
            conn,
            (
                clip_id, "user-1", "youtube", "source-1", f"{clip_id}_source.mp4",
                f"{clip_id}_source.mp4", f"Clip {clip_id}", "published", "yt", published_at,
                None, None, None, None, None, None,
            ),
        )

    bounds = _pacific_calendar_month_bounds_utc(
        datetime(2026, 11, 10, tzinfo=timezone.utc)
    )
    clips = _load_monthly_autopilot_published_clips(
        conn, "user-1", now=datetime(2026, 11, 10, tzinfo=timezone.utc)
    )

    assert bounds == (datetime(2026, 11, 1, 7), datetime(2026, 12, 1, 8))
    assert [clip["id"] for clip in clips] == [2, 3]
