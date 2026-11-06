from __future__ import annotations

from datetime import date, datetime, timezone

from app.video_shorts.services import youtube_channel_content_type as content_type


class FakeAnalytics:
    def __init__(self, rows):
        self.rows = rows
        self.queries = []

    def reports(self):
        return self

    def query(self, **kwargs):
        self.queries.append(kwargs)
        return self

    def execute(self):
        return {"rows": self.rows}


def _connection(channel_id="UC-expected"):
    return {
        "brand_id": "brand-a",
        "owner_user_id": "owner-a",
        "token_owner": "owner-a::brand-a",
        "channel_id": channel_id,
    }


def test_channel_identity_mismatch_skips_without_analytics_or_rows(monkeypatch):
    monkeypatch.setattr(
        content_type,
        "resolve_token_lookup_user_id",
        lambda owner_user_id, brand_id: ("owner-a::brand-a", "brand"),
    )
    monkeypatch.setattr(
        content_type,
        "get_connected_channel_info",
        lambda user_id: ({"id": "UC-other"}, None),
    )
    analytics_calls = []
    monkeypatch.setattr(
        content_type,
        "build_authenticated_youtube_analytics",
        lambda user_id: analytics_calls.append(user_id),
    )

    result = content_type._collect_brand_rows(
        _connection(),
        start_date=date(2026, 11, 1),
        end_date=date(2026, 11, 4),
        fetched_at=datetime(2026, 11, 5, tzinfo=timezone.utc),
    )

    assert result == {
        "status": "skipped",
        "reason": "credential_channel_mismatch",
        "rows": [],
        "requests": 0,
    }
    assert analytics_calls == []


def test_matching_channel_queries_mine_and_normalizes_content_types(monkeypatch):
    analytics = FakeAnalytics(
        [
            ["2026-11-02", "SHORTS", 12],
            ["2026-11-02", "VIDEO_ON_DEMAND", "30"],
            ["2026-11-02", "LIVE_STREAM", 2],
        ]
    )
    monkeypatch.setattr(
        content_type,
        "resolve_token_lookup_user_id",
        lambda owner_user_id, brand_id: ("owner-a::brand-a", "brand"),
    )
    monkeypatch.setattr(
        content_type,
        "get_connected_channel_info",
        lambda user_id: ({"id": "UC-expected"}, None),
    )
    monkeypatch.setattr(
        content_type,
        "build_authenticated_youtube_analytics",
        lambda user_id: analytics,
    )

    result = content_type._collect_brand_rows(
        _connection(),
        start_date=date(2026, 11, 1),
        end_date=date(2026, 11, 4),
        fetched_at=datetime(2026, 11, 5, tzinfo=timezone.utc),
    )

    assert result["status"] == "ok"
    assert result["requests"] == 1
    assert [row[4] for row in result["rows"]] == [
        "SHORTS",
        "VIDEO_ON_DEMAND",
        "LIVE_STREAM",
    ]
    assert analytics.queries == [
        {
            "ids": "channel==MINE",
            "startDate": "2026-11-01",
            "endDate": "2026-11-04",
            "metrics": "views",
            "dimensions": "day,creatorContentType",
            "sort": "day,creatorContentType",
            "maxResults": 200,
        }
    ]


def test_backfill_starts_at_first_of_month_before_first_publication():
    assert content_type._previous_calendar_month(date(2026, 11, 2)) == (
        date(2026, 10, 1),
        date(2026, 10, 31),
    )
