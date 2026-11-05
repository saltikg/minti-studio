from __future__ import annotations

import pytest

from app.video_shorts.services import instagram_api
from app.video_shorts.tasks import sync_instagram_metrics


def test_instagram_token_lookup_is_explicitly_brand_scoped(monkeypatch):
    entry = {
        "id": "queue-1",
        "user_id": "owner-1",
        "brand_id": "brand-1",
        "instagram_business_account_id": "ig-1",
    }
    observed = []
    monkeypatch.setattr(instagram_api, "get_instagram_queue_entry", lambda _queue_id: entry)
    monkeypatch.setattr(
        instagram_api,
        "get_instagram_credentials",
        lambda identity: observed.append(identity)
        or {
            "user_id": identity,
            "instagram_business_account_id": "ig-1",
            "page_access_token": "secret",
        },
    )

    resolved_entry, token = instagram_api._get_entry_and_token("queue-1")

    assert resolved_entry == entry
    assert token == "secret"
    assert observed == ["owner-1::brand-1"]


def test_refresh_instagram_media_stores_views_without_overwriting_impressions(monkeypatch):
    monkeypatch.setattr(
        instagram_api,
        "_get_entry_and_token",
        lambda _queue_id: (
            {
                "instagram_media_id": "media-1",
                "user_id": "owner-1",
                "instagram_business_account_id": "ig-1",
            },
            "secret",
        ),
    )
    requests = []

    def fake_graph_request(_method, path, _token, *, params=None, data=None):
        requests.append((path, params))
        if path == "media-1":
            return {
                "media_type": "REELS",
                "permalink": "https://instagram.test/reel/one",
                "like_count": 3,
                "comments_count": 1,
            }
        return {
            "data": [
                {"name": "views", "values": [{"value": 488}]},
                {"name": "reach", "values": [{"value": 397}]},
                {"name": "likes", "values": [{"value": 4}]},
                {"name": "comments", "values": [{"value": 2}]},
                {"name": "saved", "values": [{"value": 3}]},
                {"name": "shares", "values": [{"value": 0}]},
            ]
        }

    updates = []
    monkeypatch.setattr(instagram_api, "_graph_request", fake_graph_request)
    monkeypatch.setattr(instagram_api, "update_instagram_metrics", lambda queue_id, **values: updates.append((queue_id, values)))
    monkeypatch.setattr(instagram_api, "upsert_instagram_comment_cache", lambda *_args: None)

    result = instagram_api.refresh_instagram_media("queue-1", comments_limit=0)

    assert result["views"] == 488
    assert result["reach"] == 397
    assert updates == [
        (
            "queue-1",
            {
                "like_count": 4,
                "comment_count": 2,
                "permalink": "https://instagram.test/reel/one",
                "views": 488,
                "reach": 397,
                "saved": 3,
                "shares": 0,
            },
        )
    ]
    assert "views" in requests[1][1]["metric"]
    assert "impressions" not in updates[0][1]


def test_refresh_instagram_media_fails_loudly_when_views_missing(monkeypatch):
    monkeypatch.setattr(
        instagram_api,
        "_get_entry_and_token",
        lambda _queue_id: ({"instagram_media_id": "media-1"}, "secret"),
    )
    monkeypatch.setattr(
        instagram_api,
        "_graph_request",
        lambda _method, path, _token, **_kwargs: (
            {"media_type": "REELS"} if path == "media-1" else {"data": []}
        ),
    )
    updates = []
    monkeypatch.setattr(instagram_api, "update_instagram_metrics", lambda *_args, **_kwargs: updates.append(True))

    with pytest.raises(instagram_api.InstagramActionError, match="did not include views"):
        instagram_api.refresh_instagram_media("queue-1", comments_limit=0)

    assert updates == []


def test_sync_summary_alerts_above_twenty_percent(monkeypatch):
    jobs = [
        {"id": "1", "brand_id": "brand-a", "instagram_media_id": "media-1"},
        {"id": "2", "brand_id": "brand-a", "instagram_media_id": "media-2"},
        {"id": "3", "brand_id": "brand-b", "instagram_media_id": "media-3"},
    ]
    monkeypatch.setattr(sync_instagram_metrics, "fetch_instagram_media_jobs", lambda *_args, **_kwargs: jobs)

    def fake_refresh(queue_id, comments_limit):
        if queue_id == "3":
            raise instagram_api.InstagramActionError("provider error")
        return {"views": 10, "reach": 8}

    alerts = []
    monkeypatch.setattr(sync_instagram_metrics, "refresh_instagram_media", fake_refresh)
    monkeypatch.setattr(sync_instagram_metrics, "_send_failure_alert", alerts.append)

    summary = sync_instagram_metrics.sync_instagram_metrics(None, 0)

    assert summary["total"] == 3
    assert summary["succeeded"] == 2
    assert summary["failed"] == 1
    assert summary["by_brand"]["brand-b"]["failed"] == 1
    assert alerts == [summary]


def test_versioned_instagram_graph_base(monkeypatch):
    monkeypatch.setattr(instagram_api, "IG_GRAPH_API_BASE", "https://graph.instagram.com")
    monkeypatch.setattr(instagram_api, "IG_GRAPH_API_VERSION", "v24.0")
    assert instagram_api._versioned_graph_base() == "https://graph.instagram.com/v24.0"
