from __future__ import annotations

import pytest

from app.video_shorts.services import facebook_metrics
from app.video_shorts.tasks import sync_instagram_metrics
from app.video_shorts.config import FB_OAUTH_SCOPES


class _Response:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = ""

    def json(self):
        return self._payload


def test_facebook_oauth_always_requests_read_insights():
    assert "read_insights" in FB_OAUTH_SCOPES.split(",")


def test_facebook_insights_permission_failure_is_loud(monkeypatch):
    calls = []

    def fake_get(url, **_kwargs):
        calls.append(url)
        return _Response(
            403,
            {"error": {"code": 200, "message": "read_insights permission missing"}},
        )

    monkeypatch.setattr(facebook_metrics.requests, "get", fake_get)

    with pytest.raises(facebook_metrics.FacebookMetricsError) as exc_info:
        facebook_metrics.fetch_facebook_metrics("video-1", "secret")

    assert exc_info.value.needs_reconnect is True
    assert "read_insights" in str(exc_info.value)
    assert len(calls) == 1


def test_facebook_metrics_preserve_fetched_zero_values(monkeypatch):
    responses = iter(
        [
            _Response(
                200,
                {
                    "data": [
                        {"name": "total_video_impressions", "values": [{"value": 0}]},
                        {
                            "name": "total_video_impressions_unique",
                            "values": [{"value": 0}],
                        },
                    ]
                },
            ),
            _Response(
                200,
                {
                    "views": 0,
                    "likes": {"summary": {"total_count": 0}},
                    "comments": {"summary": {"total_count": 0}},
                    "permalink_url": "/reel/one/",
                },
            ),
        ]
    )
    monkeypatch.setattr(
        facebook_metrics.requests,
        "get",
        lambda *_args, **_kwargs: next(responses),
    )

    result = facebook_metrics.fetch_facebook_metrics("video-1", "secret")

    assert result == {
        "view_count": 0,
        "reach": 0,
        "impressions": 0,
        "reactions": 0,
        "comment_count": 0,
        "permalink": "/reel/one/",
    }


def test_facebook_sync_uses_scoped_credential_and_marks_reconnect(monkeypatch):
    job = {
        "id": "queue-1",
        "user_id": "owner-1",
        "brand_id": "brand-1",
        "facebook_video_id": "video-1",
        "page_id": "page-1",
    }
    observed_keys = []
    statuses = []
    updates = []
    monkeypatch.setattr(
        sync_instagram_metrics,
        "fetch_facebook_media_jobs",
        lambda *_args, **_kwargs: [job],
    )
    monkeypatch.setattr(
        sync_instagram_metrics,
        "get_facebook_page_data",
        lambda key: observed_keys.append(key)
        or {"page_id": "page-1", "page_access_token": "secret"},
    )
    monkeypatch.setattr(
        sync_instagram_metrics,
        "fetch_facebook_metrics",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            facebook_metrics.FacebookMetricsError(
                "read_insights permission missing",
                needs_reconnect=True,
            )
        ),
    )
    monkeypatch.setattr(
        sync_instagram_metrics,
        "mark_facebook_insights_status",
        lambda owner, **values: statuses.append((owner, values)),
    )
    monkeypatch.setattr(
        sync_instagram_metrics,
        "update_facebook_queue_metrics",
        lambda *_args, **_kwargs: updates.append(True),
    )

    summary = sync_instagram_metrics.sync_facebook_metrics(None, send_alert=False)

    assert observed_keys == ["owner-1::brand-1"]
    assert statuses == [
        (
            "owner-1",
            {
                "brand_id": "brand-1",
                "status": "needs_reconnect",
                "error": "read_insights permission missing",
            },
        )
    ]
    assert updates == []
    assert summary["attempted"] == 1
    assert summary["failed"] == 1
    assert summary["needs_reconnect"] == 1
