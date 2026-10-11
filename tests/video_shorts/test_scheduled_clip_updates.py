from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask

from app.video_shorts import worker
from app.video_shorts.services import youtube_oauth


class _Request:
    def __init__(self, response=None, error=None):
        self.response = response or {}
        self.error = error

    def execute(self):
        if self.error:
            raise self.error
        return self.response


class _Videos:
    def __init__(self, *, status=None, list_error=None, calls=None):
        self.status = status or {}
        self.list_error = list_error
        self.calls = calls if calls is not None else []

    def list(self, **kwargs):
        self.calls.append(("list", kwargs))
        if self.list_error:
            return _Request(error=self.list_error)
        return _Request({"items": [{"id": kwargs.get("id"), "status": self.status}]})

    def delete(self, **kwargs):
        self.calls.append(("delete", kwargs))
        return _Request({})


class _YouTube:
    def __init__(self, videos):
        self._videos = videos

    def videos(self):
        return self._videos


def test_youtube_safe_delete_blocks_unsafe_cases(monkeypatch):
    app = Flask(__name__)
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat().replace("+00:00", "Z")

    calls = []
    videos = _Videos(status={"privacyStatus": "private", "publishAt": future}, calls=calls)
    monkeypatch.setattr(youtube_oauth, "build_authenticated_youtube", lambda **kwargs: _YouTube(videos))
    monkeypatch.setattr(youtube_oauth, "record_youtube_data_api_call", lambda **kwargs: None)
    with app.app_context():
        result = youtube_oauth.delete_youtube_video_if_safe(
            video_id="yt-1",
            expected_video_ids=["other"],
            scheduled_publish_at=future,
            user_id="user",
        )
        assert result["reason"] == "id_not_in_db"
        assert calls == []

        videos.status = {"privacyStatus": "public", "publishAt": future}
        result = youtube_oauth.delete_youtube_video_if_safe(
            video_id="yt-1",
            expected_video_ids=["yt-1"],
            scheduled_publish_at=future,
            user_id="user",
        )
        assert result["reason"] == "not_private"

        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
        videos.status = {"privacyStatus": "private", "publishAt": past}
        result = youtube_oauth.delete_youtube_video_if_safe(
            video_id="yt-1",
            expected_video_ids=["yt-1"],
            scheduled_publish_at=past,
            user_id="user",
        )
        assert result["reason"] == "not_future_scheduled"

        videos.list_error = RuntimeError("boom")
        result = youtube_oauth.delete_youtube_video_if_safe(
            video_id="yt-1",
            expected_video_ids=["yt-1"],
            scheduled_publish_at=future,
            user_id="user",
        )
        assert result["reason"] == "list_error"


def test_scheduled_regenerate_uploads_new_youtube_before_old_delete(monkeypatch, tmp_path):
    app = Flask(__name__)
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"video")
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    calls = []
    entries = [
        {
            "plan_index": 2,
            "clip_filename": "clip.mp4",
            "publish_status": "scheduled",
            "publish_at_iso": future,
            "publish_at": "local",
            "yt_video_id": "old-yt",
            "yt_title": "Title",
            "yt_description": "Description",
        }
    ]

    monkeypatch.setattr(worker, "_load_user_context", lambda user_id, brand_id: ({"id": user_id}, {"id": brand_id}))
    monkeypatch.setattr(worker.generation, "_load_plan_entries", lambda video_id: entries)
    monkeypatch.setattr(worker.generation, "_resolve_short_path_for_processing", lambda filename: clip)
    monkeypatch.setattr(worker, "upload_video_with_refresh_token", lambda **kwargs: calls.append(("upload", kwargs)) or {"id": "new-yt"})
    monkeypatch.setattr(worker, "delete_youtube_video_if_safe", lambda **kwargs: calls.append(("delete", kwargs)) or {"deleted": True})
    monkeypatch.setattr(worker.generation, "_write_plan_entries", lambda *args, **kwargs: None)

    def fake_update(**kwargs):
        calls.append(("store", kwargs))
        entries[0]["yt_video_id"] = kwargs["youtube_id"]

    monkeypatch.setattr(worker.generation, "_update_plan_entry_publish_state", fake_update)

    result = worker._replace_pending_youtube_upload_after_render(
        app,
        {
            "id": "job-1",
            "user_id": "user-1",
            "payload": {
                "scheduled_replace": True,
                "video_pk": 7,
                "source_video_id": "src",
                "brand_id": "brand-1",
                "plan_index": 2,
                "previous_youtube_video_id": "old-yt",
                "previous_publish_at_iso": future,
            },
        },
        {"clip_filename": "clip.mp4"},
    )

    assert [name for name, _ in calls] == ["upload", "store", "delete"]
    assert calls[0][1]["publish_at"] == future
    assert calls[1][1]["youtube_id"] == "new-yt"
    assert calls[2][1]["video_id"] == "old-yt"
    assert result["schedule_update"]["youtube_new_id"] == "new-yt"


def test_scheduled_regenerate_keeps_old_youtube_when_new_upload_fails(monkeypatch, tmp_path):
    app = Flask(__name__)
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"video")
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    calls = []
    entries = [
        {
            "plan_index": 2,
            "clip_filename": "clip.mp4",
            "publish_status": "scheduled",
            "publish_at_iso": future,
            "yt_video_id": "old-yt",
        }
    ]

    monkeypatch.setattr(worker, "_load_user_context", lambda user_id, brand_id: ({"id": user_id}, {"id": brand_id}))
    monkeypatch.setattr(worker.generation, "_load_plan_entries", lambda video_id: entries)
    monkeypatch.setattr(worker.generation, "_resolve_short_path_for_processing", lambda filename: clip)
    monkeypatch.setattr(worker, "upload_video_with_refresh_token", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("upload failed")))
    monkeypatch.setattr(worker, "delete_youtube_video_if_safe", lambda **kwargs: calls.append(("delete", kwargs)) or {"deleted": True})
    monkeypatch.setattr(worker.generation, "_write_plan_entries", lambda *args, **kwargs: None)

    result = worker._replace_pending_youtube_upload_after_render(
        app,
        {
            "id": "job-2",
            "user_id": "user-1",
            "payload": {
                "scheduled_replace": True,
                "video_pk": 7,
                "source_video_id": "src",
                "brand_id": "brand-1",
                "plan_index": 2,
                "previous_youtube_video_id": "old-yt",
                "previous_publish_at_iso": future,
            },
        },
        {"clip_filename": "clip.mp4"},
    )

    assert calls == []
    assert entries[0]["yt_video_id"] == "old-yt"
    assert result["schedule_update_warning"] == "Schedule kept with the previous version."
