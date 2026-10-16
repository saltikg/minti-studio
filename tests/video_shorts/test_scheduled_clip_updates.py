from __future__ import annotations

from copy import deepcopy
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


def test_no_runtime_callers_of_remote_edit_delete_helpers():
    import ast
    root = Path(__file__).resolve().parents[2] / "app"
    banned = {"update_video_with_refresh_token", "delete_youtube_video_if_safe", "unschedule_youtube_video_if_safe"}
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = node.func.id if isinstance(node.func,ast.Name) else node.func.attr if isinstance(node.func,ast.Attribute) else ""
                assert name not in banned, f"{path}:{node.lineno}: {name}"


def test_uploaded_clip_locks_regardless_of_status():
    from app.video_shorts.routes.generation import clip_entry_publish_lock_reason
    for status in ["scheduled", "uploaded", "published", "", "pending"]:
        message = clip_entry_publish_lock_reason({"yt_video_id":"remote-id", "publish_status":status})
        assert "https://studio.youtube.com/video/remote-id/edit" in message
    assert "YouTube Studio" in clip_entry_publish_lock_reason({}, [None,"scheduled","remote-id",None,None,None])
