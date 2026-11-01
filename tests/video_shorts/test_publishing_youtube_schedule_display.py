import json
from types import SimpleNamespace

import pytest
from flask import Flask

from app.video_shorts.routes import videos


class _EmptyCursor:
    def fetchall(self):
        return []


class _ReadOnlyConnection:
    def execute(self, *_args, **_kwargs):
        return _EmptyCursor()

    def close(self):
        return None


@pytest.fixture
def overview_collector(monkeypatch, tmp_path):
    plan_path = tmp_path / "42_plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "plan": [
                    {
                        "plan_index": 1,
                        "status": "created",
                        "clip_filename": "1_source_v42.mp4",
                        "title": "Test short",
                    }
                ]
            }
        )
    )
    identity = SimpleNamespace(
        video_pk=42,
        youtube_id="source",
        owner_user_id="owner",
        brand_id="brand",
    )
    monkeypatch.setattr(videos, "SHORTS_DIR", tmp_path)
    monkeypatch.setattr(videos.tenant_media, "iter_plans", lambda **_kwargs: [(identity, plan_path)])
    monkeypatch.setattr(videos, "list_ai_broadcast_entries", lambda **_kwargs: [])
    monkeypatch.setattr(videos, "get_db_readonly", lambda: _ReadOnlyConnection())
    monkeypatch.setattr(
        videos,
        "load_instagram_queue_map",
        lambda _video_ids: {("source", "1"): [{"status": "pending", "publish_at": "2026-11-02T11:40:00Z"}]},
    )
    monkeypatch.setattr(videos, "load_tiktok_queue_map", lambda _video_ids: {})
    monkeypatch.setattr(videos, "load_facebook_queue_map", lambda _video_ids: {})
    monkeypatch.setattr(videos, "_short_public_url", lambda _filename: "/clip.mp4")

    app = Flask(__name__)

    def collect(overlay):
        monkeypatch.setattr(videos.youtube_scheduling, "overlay_plan", overlay)
        with app.app_context():
            return videos._collect_short_broadcast_entries(
                brand_id="brand",
                owner_user_id="owner",
            )

    return collect


def test_pending_youtube_schedule_with_social_queue_is_scheduled(overview_collector):
    def overlay(entries, **scope):
        assert scope == {"user_id": "owner", "brand_id": "brand", "video_pk": 42}
        entries[0].update(
            publish_status="pending",
            publish_at_iso="2026-11-02T11:40:00Z",
            upload_due_at="2026-11-02T09:40:00Z",
        )
        return entries

    entry = overview_collector(overlay)[0]

    assert entry["publish_status"] == "scheduled"
    assert entry["publish_status_label"] == "Scheduled"
    assert entry["publish_at_iso"] == "2026-11-02T11:40:00Z"


def test_social_queue_without_youtube_schedule_is_not_planned(overview_collector):
    entry = overview_collector(lambda entries, **_scope: entries)[0]

    assert entry["publish_status"] == "not_planned"
    assert entry["publish_status_label"] == "Not planned"
    assert entry["publish_at_iso"] is None
    assert entry["publish_label"] is None


@pytest.mark.parametrize("status", ["scheduled", "uploaded", "published"])
def test_existing_youtube_schedule_statuses_are_unchanged(overview_collector, status):
    def overlay(entries, **_scope):
        entries[0].update(
            publish_status=status,
            publish_at_iso="2026-11-02T11:40:00Z",
            upload_due_at="2026-11-02T09:40:00Z",
        )
        return entries

    entry = overview_collector(overlay)[0]

    assert entry["publish_status"] == status
    assert entry["publish_status_label"] == status.capitalize()
    assert entry["publish_at_iso"] == "2026-11-02T11:40:00Z"
