from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.video_shorts.services import niche_caption_archive, niche_weekly_ideas


PACIFIC = ZoneInfo("America/Los_Angeles")


def _row(video_id, channel_id, days_old, *, views=10_000, ratio=4.0):
    as_of = datetime(2026, 11, 26, 12, tzinfo=PACIFIC)
    return {
        "video_id": video_id,
        "youtube_channel_id": channel_id,
        "published_at": as_of - timedelta(days=days_old),
        "views": views,
        "outlier_ratio": ratio,
        "in_category": True,
        "engagement": 0.02,
        "baseline_engagement": 0.02,
    }


def test_candidates_use_seven_days_when_six_channels_qualify():
    rows = [_row(str(index), f"channel-{index}", index) for index in range(1, 7)]
    selected, days = niche_weekly_ideas._candidate_rows(rows, "2026-11-26")
    assert days == 7
    assert len(selected) == 6


def test_candidates_fall_back_to_fourteen_days_and_cap_one_per_channel():
    rows = [
        _row("best", "same", 2, ratio=9),
        _row("duplicate", "same", 3, ratio=8),
        _row("older", "other", 10, ratio=7),
    ]
    selected, days = niche_weekly_ideas._candidate_rows(rows, "2026-11-26")
    assert days == 14
    assert [item["video_id"] for item in selected] == ["best", "older"]


def test_caption_windows_are_roughly_thirty_seconds():
    segments = [
        {"start": index * 10, "end": (index + 1) * 10, "text": f"part {index}"}
        for index in range(6)
    ]
    windows = niche_caption_archive._caption_windows(segments)
    assert [(item["start_seconds"], item["end_seconds"]) for item in windows] == [(0.0, 30.0), (30.0, 60.0)]


def test_vtt_parser_preserves_timestamps(tmp_path):
    path = tmp_path / "sample.vtt"
    path.write_text(
        "WEBVTT\n\n00:00:01.000 --> 00:00:04.000\nFirst line\n\n"
        "00:00:04.000 --> 00:00:07.500\nSecond line\n",
        encoding="utf-8",
    )
    assert niche_caption_archive._parse_vtt(path) == [
        {"start": 1.0, "end": 4.0, "text": "First line"},
        {"start": 4.0, "end": 7.5, "text": "Second line"},
    ]


def test_caption_archive_uses_six_month_scope_and_audio_by_default(monkeypatch):
    monkeypatch.delenv("NICHE_CAPTION_SUBTITLES_ENABLED", raising=False)
    assert niche_caption_archive.CAPTION_LOOKBACK_DAYS == 183
    assert niche_caption_archive._subtitles_enabled() is False


def test_caption_subtitles_can_be_explicitly_enabled(monkeypatch):
    monkeypatch.setenv("NICHE_CAPTION_SUBTITLES_ENABLED", "true")
    assert niche_caption_archive._subtitles_enabled() is True


def test_archive_candidate_datetime_is_json_safe():
    value = datetime(2026, 11, 26, 12, 30, tzinfo=PACIFIC)
    assert niche_weekly_ideas._json_safe(value) == value.isoformat()
