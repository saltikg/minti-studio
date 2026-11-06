from __future__ import annotations

from datetime import date

from app.video_shorts.services.dashboard_metrics import (
    _audience_net_by_date,
    _period_totals,
    _summarize_youtube_insight_rows,
    load_long_form_effect_metrics,
)


class _Result:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class _LongFormConnection:
    def __init__(self):
        self.calls = []

    def execute(self, query, params):
        self.calls.append((query, params))
        if "MIN(LEAST" in query:
            return _Result((date(2026, 11, 2),))
        return _Result((1193, 31, 36797, 31, date(2026, 10, 31)))


def test_period_totals_require_complete_connected_platform_data():
    daily = {
        "youtube": {
            "2026-11-01": {
                "views": 12,
                "views_rows": 2,
                "likes": 3,
                "likes_rows": 2,
                "comments": 1,
                "comments_rows": 2,
                "short_rows": 2,
            }
        },
        "instagram": {
            "2026-11-01": {
                "views": 8,
                "views_rows": 1,
                "likes": 2,
                "likes_rows": 1,
                "comments": None,
                "comments_rows": 0,
                "short_rows": 1,
            }
        },
    }

    totals = _period_totals(
        daily,
        start_date=date(2026, 11, 1),
        end_date=date(2026, 11, 1),
        connections={"youtube": True, "instagram": True, "facebook": False},
    )

    assert totals["youtube"] == {"views": 12, "likes": 3, "comments": 1}
    assert totals["instagram"] == {"views": 8, "likes": 2, "comments": None}
    assert totals["all"] == {"views": 20, "likes": 5, "comments": 1}
    assert totals["facebook"] == {"views": None, "likes": None, "comments": None}


def test_partial_short_rows_sum_known_gains_without_coercing_missing_values():
    daily = {
        "youtube": {
            "2026-11-01": {
                "views": 12,
                "views_rows": 1,
                "likes": 3,
                "likes_rows": 2,
                "comments": 1,
                "comments_rows": 2,
                "short_rows": 2,
            }
        }
    }

    totals = _period_totals(
        daily,
        start_date=date(2026, 11, 1),
        end_date=date(2026, 11, 1),
        connections={"youtube": True, "instagram": False, "facebook": False},
    )

    assert totals["youtube"]["views"] == 12
    assert totals["all"]["views"] == 12
    assert totals["all"]["likes"] == 3


def test_connected_active_platform_with_only_missing_metrics_is_pending():
    daily = {
        "youtube": {
            "2026-11-02": {
                "views": None,
                "views_rows": 0,
                "likes": None,
                "likes_rows": 0,
                "comments": None,
                "comments_rows": 0,
                "short_rows": 1,
            }
        }
    }

    totals = _period_totals(
        daily,
        start_date=date(2026, 11, 1),
        end_date=date(2026, 11, 2),
        connections={"youtube": True, "instagram": False, "facebook": False},
    )

    assert totals["youtube"] == {"views": None, "likes": None, "comments": None}
    assert totals["all"] == {"views": None, "likes": None, "comments": None}


def test_instagram_audience_net_is_derived_from_consecutive_totals():
    rows = {
        "2026-11-01": {"audience_total": 1620, "audience_gain": None, "gain_rows": 0},
        "2026-11-02": {"audience_total": 1623, "audience_gain": None, "gain_rows": 0},
        "2026-11-03": {"audience_total": 1625, "audience_gain": None, "gain_rows": 0},
    }

    assert _audience_net_by_date("instagram", rows) == {
        "2026-11-01": None,
        "2026-11-02": 3,
        "2026-11-03": 2,
    }


def test_youtube_audience_net_keeps_reported_gain_and_pending_days():
    rows = {
        "2026-11-01": {"audience_total": 100, "audience_gain": 2, "gain_rows": 1},
        "2026-11-02": {"audience_total": 100, "audience_gain": None, "gain_rows": 0},
    }

    assert _audience_net_by_date("youtube", rows) == {
        "2026-11-01": 2,
        "2026-11-02": None,
    }


def test_youtube_insights_use_attributed_denominator_and_weighted_retention():
    rows = [
        {
            "metric_date": date(2026, 11, 2),
            "generated_video_id": 5078,
            "generated_title": "Low Libido",
            "views": 51,
            "shorts_feed_views": 10,
            "search_views": 30,
            "subscriber_views": 1,
            "notification_views": 8,
            "channel_page_views": 1,
            "external_views": 1,
            "other_views": 0,
            "source_attributed_views": 51,
            "subscribers_gained": 0,
            "average_view_percentage": 40.0,
            "average_view_duration_seconds": 25.0,
        },
        {
            "metric_date": date(2026, 11, 3),
            "generated_video_id": 5078,
            "generated_title": "Low Libido",
            "views": 44,
            "shorts_feed_views": 4,
            "search_views": 22,
            "subscriber_views": 1,
            "notification_views": 13,
            "channel_page_views": 1,
            "external_views": 3,
            "other_views": 0,
            "source_attributed_views": 44,
            "subscribers_gained": 0,
            "average_view_percentage": 96.9,
            "average_view_duration_seconds": 62.0,
        },
    ]

    result = _summarize_youtube_insight_rows(
        rows,
        start_date=date(2026, 10, 7),
        end_date=date(2026, 11, 5),
        current_totals={"all": {"views": 334}},
        previous_totals={"all": {"views": 0}},
    )

    assert result["analytics_views"] == 95
    assert result["coverage_percent"] == 100.0
    assert result["traffic_sources"][1]["percent"] == 54.7
    assert result["average_view_percentage"] == 66.4
    assert result["average_view_duration_seconds"] == 42.1
    assert result["subscribers_gained"] == 0
    assert result["latest_date"] == date(2026, 11, 3)


def test_long_form_effect_uses_explicit_tenant_and_pre_publication_month():
    conn = _LongFormConnection()

    result = load_long_form_effect_metrics(
        conn,
        owner_user_id="owner-a",
        brand_id="brand-a",
        start_date=date(2026, 11, 1),
        end_date=date(2026, 11, 5),
    )

    assert result["long_form_views_per_day"] == 1193 / 31
    assert result["baseline_start"] == date(2026, 10, 1)
    assert result["baseline_end"] == date(2026, 10, 31)
    assert result["baseline_views_per_day"] == 36797 / 31
    assert result["data_through_date"] == date(2026, 10, 31)
    assert "user_id" in conn.calls[0][0] and "brand_id" in conn.calls[0][0]
    assert "owner_user_id" in conn.calls[1][0] and "brand_id" in conn.calls[1][0]
    assert conn.calls[0][1] == ["owner-a", "brand-a"]
