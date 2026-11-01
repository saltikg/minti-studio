from __future__ import annotations

import json
from datetime import datetime

from flask import Flask

from app.video_shorts.routes import generation


class _Cursor:
    def __init__(self, rows):
        self._rows = list(rows)

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _ReadConnection:
    def __init__(self, *, already_projected: bool):
        self.already_projected = already_projected

    def execute(self, sql, _params=None):
        normalized = " ".join(sql.split())
        if "ORDER BY planned_publish_at" in normalized:
            return _Cursor([])
        if "FROM shorts_youtube_schedules" in normalized:
            payload = json.dumps(
                {
                    "video_pk": "42",
                    "plan_index": "7",
                    "filename": "7_source_v42.mp4",
                    "publish_at_local": "2026-10-28T00:00",
                    "title": "Test Short",
                    "description": "Description",
                }
            )
            return _Cursor(
                [
                    (
                        "schedule-1",
                        "owner",
                        "brand",
                        payload,
                        "youtube-1",
                        datetime(2026, 10, 28, 7, 0),
                    )
                ]
            )
        if "FROM youtube_videos" in normalized:
            return _Cursor([("source",)])
        if "FROM shorts_generated_videos" in normalized:
            if self.already_projected:
                return _Cursor([("youtube-1", datetime(2026, 10, 28, 7, 0), "published")])
            return _Cursor([(None, None, "published")])
        raise AssertionError(normalized)

    def close(self):
        return None


class _WriteConnection:
    def commit(self):
        return None

    def rollback(self):
        return None

    def close(self):
        return None


def _run_reconcile(monkeypatch, *, already_projected: bool):
    projected = []
    fetched = []
    monkeypatch.setattr(
        generation,
        "get_db_readonly",
        lambda: _ReadConnection(already_projected=already_projected),
    )
    monkeypatch.setattr(generation, "get_db", lambda: _WriteConnection())
    monkeypatch.setattr(generation, "has_refresh_token", lambda **_kwargs: True)
    monkeypatch.setattr(
        generation,
        "fetch_video_statuses",
        lambda ids, **_kwargs: fetched.append(ids) or {
            "youtube-1": {"privacyStatus": "public", "uploadStatus": "processed"}
        },
    )
    monkeypatch.setattr(
        generation,
        "_update_plan_entry_publish_state",
        lambda **kwargs: projected.append(kwargs) or True,
    )
    app = Flask(__name__)
    with app.app_context():
        result = generation.reconcile_overdue_youtube_publish_statuses(limit=50)
    return result, fetched, projected


def test_uploaded_schedule_repairs_missing_projection(monkeypatch) -> None:
    result, fetched, projected = _run_reconcile(monkeypatch, already_projected=False)

    assert fetched == [["youtube-1"]]
    assert result["checked"] == 1
    assert result["updated"] == 1
    assert result["projection_failed"] == 0
    assert projected[0]["video_pk"] == "42"
    assert projected[0]["plan_index"] == "7"
    assert projected[0]["youtube_id"] == "youtube-1"
    assert projected[0]["owner_user_id"] == "owner"
    assert projected[0]["brand_id"] == "brand"


def test_already_projected_schedule_is_untouched(monkeypatch) -> None:
    result, fetched, projected = _run_reconcile(monkeypatch, already_projected=True)

    assert fetched == []
    assert projected == []
    assert result["checked"] == 0
    assert result["updated"] == 0


def test_worker_projection_uses_explicit_scope_without_request_context(monkeypatch) -> None:
    calls = []
    entry = {
        "plan_index": 7,
        "clip_filename": "7_source_v42.mp4",
        "status": "created",
    }
    monkeypatch.setattr(
        generation,
        "_resolve_video_id_from_pk",
        lambda video_pk, **scope: calls.append(("resolve", video_pk, scope)) or "source",
    )
    monkeypatch.setattr(
        generation,
        "_load_plan_entries",
        lambda video_id, **scope: calls.append(("load", video_id, scope)) or [entry],
    )
    monkeypatch.setattr(
        generation,
        "_write_plan_entries",
        lambda video_id, entries, **scope: calls.append(("write", video_id, scope)),
    )
    monkeypatch.setattr(
        generation,
        "upsert_generated_video_record",
        lambda **kwargs: calls.append(("lifecycle", kwargs)),
    )
    app = Flask(__name__)
    with app.app_context():
        assert generation._update_plan_entry_publish_state(
            video_pk="42",
            plan_index="7",
            filename="7_source_v42.mp4",
            publish_status="scheduled",
            publish_at_local="2026-10-28T00:00",
            publish_at_iso="2026-10-28T07:00:00Z",
            title="Test Short",
            description="Description",
            youtube_id="youtube-1",
            owner_user_id="owner",
            brand_id="brand",
        )

    for call in calls[:3]:
        assert call[2]["owner_user_id"] == "owner"
        assert call[2]["brand_id"] == "brand"
    assert calls[3][1]["user_id"] == "owner"
    assert calls[3][1]["brand_id"] == "brand"
