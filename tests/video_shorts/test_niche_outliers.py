import json
from datetime import datetime, timezone

import pytest

from app.video_shorts.services import niche_outliers


def test_compute_niche_outliers_uses_settled_window_median(monkeypatch, tmp_path):
    monkeypatch.setattr(
        niche_outliers,
        "_current_pool",
        lambda _slug: (
            {"id": 7, "slug": "menopause_hormones"},
            [
                {"youtube_channel_id": "channel-a"},
                {"youtube_channel_id": "channel-b"},
            ],
        ),
    )
    manifest = {
        "version": "niche-outliers-v1",
        "uploads": {
            "channel-a": [
                {"video_id": "a1", "title": "Normal one", "published_at": "2026-11-01T18:00:00Z"},
                {"video_id": "a2", "title": "Normal two", "published_at": "2026-11-05T18:00:00Z"},
                {"video_id": "a3", "title": "Normal three", "published_at": "2026-11-10T18:00:00Z"},
                {"video_id": "hit", "title": "Working now", "published_at": "2026-11-15T18:00:00Z"},
                {"video_id": "duet", "title": "#Duet with someone", "published_at": "2026-11-16T18:00:00Z"},
                {"video_id": "tiny", "title": "A six second clip", "published_at": "2026-11-17T18:00:00Z"},
                {"video_id": "young", "title": "Too young", "published_at": "2026-11-23T18:00:00Z"},
            ],
            "channel-b": [
                {"video_id": "b1", "title": "One", "published_at": "2026-11-01T18:00:00Z"},
                {"video_id": "b2", "title": "Two", "published_at": "2026-11-02T18:00:00Z"},
            ],
        },
        "videos": {
            "a1": {"duration_s": 60, "views": 100, "likes": 1, "comments": 0},
            "a2": {"duration_s": 70, "views": 100, "likes": 1, "comments": 0},
            "a3": {"duration_s": 80, "views": 100, "likes": 1, "comments": 0},
            "hit": {"duration_s": 90, "views": 1000, "likes": 40, "comments": 10},
            "duet": {"duration_s": 60, "views": 100000, "likes": 100, "comments": 10},
            "tiny": {"duration_s": 6, "views": 100000, "likes": 100, "comments": 10},
            "young": {"duration_s": 30, "views": 50000, "likes": 500, "comments": 50},
            "b1": {"duration_s": 30, "views": 100, "likes": 0, "comments": 0},
            "b2": {"duration_s": 30, "views": 5000, "likes": 0, "comments": 0},
        },
    }
    path = tmp_path / "niche_outliers" / "menopause_hormones" / "2026-11-25.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(manifest), encoding="utf-8")

    result = niche_outliers.compute_niche_outlier_shorts(
        "menopause_hormones",
        "2026-11-25",
        cache_root=tmp_path,
    )

    assert result["channels_with_3_shorts"] == 1
    assert result["outlier_count"] == 1
    assert result["rows"][0]["video_id"] == "hit"
    assert result["rows"][0]["baseline"] == 100
    assert result["rows"][0]["outlier_ratio"] == 10
    assert result["rows"][0]["engagement"] == 0.05


@pytest.mark.parametrize(
    "title",
    ["#DUET reply", "Duet With creator", "#Stitch this", "STITCH WITH me", "Quick repost"],
)
def test_title_exclusions_are_case_insensitive(title):
    assert niche_outliers._is_excluded_short(title, 60)


def test_shorts_under_fifteen_seconds_are_excluded():
    assert niche_outliers._is_excluded_short("Ordinary title", 14)
    assert not niche_outliers._is_excluded_short("Ordinary title", 15)


def test_run_quota_stops_before_call_over_cap(monkeypatch):
    monkeypatch.setattr(niche_outliers, "require_youtube_quota_available", lambda **_kwargs: None)
    quota = niche_outliers.RunQuota(max_units=1)
    quota.reserve("videos.list")

    with pytest.raises(niche_outliers.NicheOutlierError, match="cap reached"):
        quota.reserve("playlistItems.list")


def test_weekly_refresh_due_monday_after_four_pacific():
    before = datetime(2026, 11, 30, 11, 59, tzinfo=timezone.utc)
    due = datetime(2026, 11, 30, 12, 0, tzinfo=timezone.utc)

    assert not niche_outliers.weekly_refresh_due(before)
    assert niche_outliers.weekly_refresh_due(due)
    assert not niche_outliers.weekly_refresh_due(due, "2026-11-30T12:01:00Z")
