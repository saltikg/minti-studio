from __future__ import annotations

from datetime import date

from app.video_shorts.services.dashboard_metrics import _audience_net_by_date, _period_totals


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
