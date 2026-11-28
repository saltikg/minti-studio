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
            "hit": {"duration_s": 90, "views": 10000, "likes": 400, "comments": 100},
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
    assert result["rows"][0]["outlier_ratio"] == 100
    assert result["rows"][0]["engagement"] == 0.05
    assert result["rows"][0]["baseline_engagement"] == 0.01
    assert result["cohort_start"] == "2026-11-09"
    assert result["cohort_end"] == "2026-11-15"
    assert result["rows"][0]["signal_type"] == "weekly"


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


def test_daily_early_refresh_runs_once_after_four_pacific():
    before = datetime(2026, 11, 28, 11, 59, tzinfo=timezone.utc)
    due = datetime(2026, 11, 28, 12, 0, tzinfo=timezone.utc)

    assert not niche_outliers.daily_early_refresh_due(before)
    assert niche_outliers.daily_early_refresh_due(due)
    assert not niche_outliers.daily_early_refresh_due(due, "2026-11-28T12:01:00Z")


def test_pattern_generation_uses_customer_top_six_and_builds_sentence(monkeypatch):
    monkeypatch.setattr(niche_outliers, "OPENAI_MODEL", "gpt-4.1-mini")
    calls = []

    class Completions:
        def create(self, **kwargs):
            calls.append(kwargs)
            message = type(
                "Message",
                (),
                {"content": json.dumps({"items": [
                    {"video_id": str(index), "theme": "HRT safety" if index in {5, 6, 7} else f"theme {index}"}
                    for index in range(2, 8)
                ]})},
            )()
            choice = type("Choice", (), {"message": message})()
            usage = type("Usage", (), {"prompt_tokens": 100, "completion_tokens": 20})()
            return type("Response", (), {"choices": [choice], "usage": usage})()

    client = type("Client", (), {"chat": type("Chat", (), {"completions": Completions()})()})()
    result = {
        "niche_id": 1,
        "as_of": "2026-11-25",
        "language": "en",
        "rows": [
            {
                "youtube_channel_id": f"channel-{index}",
                "video_id": str(index),
                "title": f"Title {index}",
                "duration_s": 20 + index,
                "views": 10_000 + index,
                "engagement": 0.02,
                "baseline_engagement": 0.02,
                "in_category": True,
                "format_label": "story" if index in {6, 7} else "explanation",
            }
            for index in range(8)
        ],
    }

    pattern = niche_outliers.generate_niche_outlier_pattern(result, client=client)

    assert len(calls) == 1
    assert calls[0]["model"] == "gpt-4.1-mini"
    assert len(json.loads(calls[0]["messages"][1]["content"])["titles"]) == 6
    assert pattern["source_count"] == 6
    assert pattern["pattern_sentence"] == (
        "Last week's pattern: 3 of 6 winners were about HRT safety; "
        "2 were stories. Most ran 23–26s."
    )
    assert pattern["estimated_cost_usd"] == 0.000072


def test_customer_visible_rows_preserve_niche_floor_screened_results():
    rows = [{
        "youtube_channel_id": "channel-1",
        "video_id": "low-floor-winner",
        "views": 3_500,
        "outlier_ratio": 4.2,
        "in_category": True,
        "engagement": 0.03,
        "baseline_engagement": 0.03,
    }]

    assert niche_outliers.customer_visible_outlier_rows(rows, views_floor=3_000) == rows


def test_topic_classification_uses_one_call_per_short(monkeypatch, tmp_path):
    monkeypatch.setattr(niche_outliers, "OPENAI_MODEL", "gpt-4.1-mini")
    channel_path = tmp_path / "channels" / "UC1.json"
    channel_path.parent.mkdir(parents=True)
    channel_path.write_text(
        json.dumps({"payload": {"item": {"snippet": {"description": "Menopause clinic"}}}}),
        encoding="utf-8",
    )
    calls = []
    video_path = tmp_path / "niche_expansion" / "pool_video_details" / "batch.json"
    video_path.parent.mkdir(parents=True)
    video_path.write_text(
        json.dumps(
            {
                "payload": {
                    "items": [
                        {
                            "id": "a",
                            "snippet": {
                                "description": "Estrogen cream details #menopause" * 30,
                                "tags": ["HRT"],
                            },
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    class Completions:
        def create(self, **kwargs):
            calls.append(kwargs)
            in_category = "First" in kwargs["messages"][1]["content"]
            message = type(
                "Message", (), {"content": json.dumps({"in_category": in_category})}
            )()
            choice = type("Choice", (), {"message": message})()
            usage = type("Usage", (), {"prompt_tokens": 50, "completion_tokens": 5})()
            return type("Response", (), {"choices": [choice], "usage": usage})()

    client = type("Client", (), {"chat": type("Chat", (), {"completions": Completions()})()})()
    result = {
        "definition": {
            "name": "Women's Hormones & Midlife Health",
            "description": "A stored category definition.",
            "core_topics": ["menopause"],
            "adjacent_topics": ["midlife mood"],
            "excluded_topics": ["male health"],
            "definition_version": 2,
        },
        "rows": [
            {"youtube_channel_id": "UC1", "video_id": "a", "title": "First title"},
            {"youtube_channel_id": "UC1", "video_id": "b", "title": "Second title"},
        ]
    }

    usage = niche_outliers.classify_niche_outlier_topics(
        result, cache_root=tmp_path, client=client
    )

    assert len(calls) == 2
    assert [row["in_category"] for row in result["rows"]] == [True, False]
    assert "Stored category definition:" in calls[0]["messages"][1]["content"]
    assert "Menopause clinic" not in calls[0]["messages"][1]["content"]
    assert "Video description: " in calls[0]["messages"][1]["content"]
    assert "Hashtags: HRT, #menopause" in calls[0]["messages"][1]["content"]
    description_line = next(
        line for line in calls[0]["messages"][1]["content"].splitlines()
        if line.startswith("Video description:")
    )
    assert len(description_line.removeprefix("Video description: ")) == 500
    assert usage["input_tokens"] == 100
    assert usage["output_tokens"] == 10


def test_whole_percentile_interpolates():
    durations = [33, 39, 58, 80, 95, 134]

    assert niche_outliers._whole_percentile(durations, 0.25) == 44
    assert niche_outliers._whole_percentile(durations, 0.75) == 91
