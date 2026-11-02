from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest

from app.video_shorts.services.publish_target_guard import PublishTargetMismatch
from app.video_shorts.services import social_oauth_state
from app.video_shorts.tasks import process_facebook_queue as facebook_worker
from app.video_shorts.tasks import process_instagram_queue as instagram_worker


class Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"status={self.status_code}")


def instagram_job():
    return {
        "id": "ig-schedule",
        "user_id": "owner",
        "brand_id": "brand",
        "clip_filename": "clip.mp4",
        "plan_index": "1",
        "caption_text": "caption",
        "media_type": "reel",
        "instagram_business_account_id": "ig-a",
    }


def facebook_job():
    return {
        "id": "fb-schedule",
        "user_id": "owner",
        "brand_id": "brand",
        "clip_filename": "clip.mp4",
        "caption_text": "caption",
        "media_type": "reel",
        "page_id": "page-a",
    }


def test_instagram_matching_target_calls_provider(monkeypatch, tmp_path):
    job = instagram_job()
    credentials = {
        "user_id": "owner::brand",
        "page_access_token": "token",
        "instagram_business_account_id": "ig-a",
    }
    monkeypatch.setattr(instagram_worker, "get_instagram_credentials", lambda user_id: credentials)
    monkeypatch.setattr(instagram_worker, "authorize_queue_clip", lambda job: {"storage_key": "key"})
    monkeypatch.setattr(instagram_worker.tenant_media, "short_path", lambda filename: tmp_path / filename)
    monkeypatch.setattr(
        instagram_worker,
        "get_media_storage",
        lambda: SimpleNamespace(exists=lambda key: True),
    )
    monkeypatch.setattr(instagram_worker, "_current_instagram_account_id", lambda creds: "ig-a")
    monkeypatch.setattr(instagram_worker, "_proxy_video_url", lambda job: "https://media.invalid/clip")
    monkeypatch.setattr(instagram_worker, "_wait_for_creation_ready", lambda *args: (True, "FINISHED", 0))
    monkeypatch.setattr(instagram_worker, "update_instagram_job_status", lambda *args, **kwargs: None)
    posts = []
    post_responses = iter([Response({"id": "container"}), Response({"id": "media"})])
    monkeypatch.setattr(
        instagram_worker.requests,
        "post",
        lambda *args, **kwargs: posts.append(args[0]) or next(post_responses),
    )
    monkeypatch.setattr(
        instagram_worker.requests,
        "get",
        lambda *args, **kwargs: Response({"permalink": "https://instagram.invalid/media"}),
    )
    assert instagram_worker._upload_reel(job) == "media"
    assert len(posts) == 2


def test_instagram_reconnected_target_blocks_and_alerts(monkeypatch):
    job = instagram_job()
    statuses = []
    alerts = []
    posts = []
    monkeypatch.setattr(instagram_worker, "_log_queue_state", lambda: None)
    monkeypatch.setattr(instagram_worker, "fetch_due_instagram_jobs", lambda limit: [job])
    monkeypatch.setattr(
        instagram_worker,
        "update_instagram_job_status",
        lambda queue_id, **kwargs: statuses.append(kwargs["status"]),
    )
    monkeypatch.setattr(
        instagram_worker,
        "get_instagram_credentials",
        lambda user_id: {
            "user_id": "owner::brand",
            "page_access_token": "token",
            "instagram_business_account_id": "ig-b",
        },
    )
    monkeypatch.setattr(instagram_worker, "authorize_queue_clip", lambda job: {"storage_key": "key"})
    monkeypatch.setattr(instagram_worker, "send_target_mismatch_alert", alerts.append)
    monkeypatch.setattr(instagram_worker.requests, "post", lambda *args, **kwargs: posts.append(args))
    instagram_worker.process_queue(1)
    assert statuses == ["uploading", "blocked_target_mismatch"]
    assert posts == []
    assert len(alerts) == 1


@pytest.mark.parametrize("actual,expected_status,provider_calls", [
    ("page-a", "published", 1),
    ("page-b", "blocked_target_mismatch", 0),
])
def test_facebook_target_gate(monkeypatch, tmp_path, actual, expected_status, provider_calls):
    job = facebook_job()
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"video")
    statuses = []
    calls = []
    alerts = []
    monkeypatch.setattr(facebook_worker, "fetch_due_facebook_jobs", lambda limit: [job])
    monkeypatch.setattr(
        facebook_worker,
        "update_facebook_job_status",
        lambda queue_id, **kwargs: statuses.append(kwargs["status"]),
    )
    monkeypatch.setattr(facebook_worker, "worker_clip_path", lambda job: media)
    monkeypatch.setattr(
        facebook_worker,
        "get_facebook_page_data",
        lambda user_id: {
            "user_id": "owner::brand",
            "page_access_token": "token",
            "page_id": "page-a",
        },
    )
    monkeypatch.setattr(facebook_worker, "_current_facebook_page_id", lambda creds: actual)
    monkeypatch.setattr(
        facebook_worker,
        "_publish_facebook_video",
        lambda *args: calls.append(args) or {"id": "video"},
    )
    monkeypatch.setattr(facebook_worker, "_fetch_facebook_metrics", lambda *args: {})
    monkeypatch.setattr(facebook_worker, "send_target_mismatch_alert", alerts.append)
    facebook_worker.process_queue(1)
    assert statuses[-1] == expected_status
    assert len(calls) == provider_calls
    assert len(alerts) == (1 if actual == "page-b" else 0)


@pytest.mark.parametrize("platform", ["youtube", "instagram", "facebook"])
def test_oauth_state_is_tenant_bound_tamper_proof_and_one_time(monkeypatch, tmp_path, platform):
    path = str(tmp_path / f"{platform}.duckdb")
    with duckdb.connect(path) as conn:
        conn.execute("CREATE TABLE shorts_brands (id VARCHAR, owner_user_id VARCHAR)")
        conn.execute("INSERT INTO shorts_brands VALUES ('customer-brand', 'customer')")
    monkeypatch.setattr(social_oauth_state, "get_db", lambda: duckdb.connect(path))
    monkeypatch.setattr(social_oauth_state, "get_db_readonly", lambda: duckdb.connect(path, read_only=True))
    monkeypatch.setattr(
        social_oauth_state,
        "request_admin_operation_scope",
        lambda: {
            "owner_user_id": "customer",
            "brand_id": "customer-brand",
            "workspace_kind": "autopilot",
        },
    )
    monkeypatch.setattr(social_oauth_state, "current_brand_id", lambda: "admin-brand")
    actor = {"id": "admin", "role": "admin"}
    nonce = social_oauth_state.create_oauth_state(platform=platform, current_user=actor)
    with pytest.raises(social_oauth_state.OAuthStateError):
        social_oauth_state.consume_oauth_state(
            platform=platform, nonce=f"{nonce}-tampered", current_user=actor
        )
    state = social_oauth_state.consume_oauth_state(
        platform=platform, nonce=nonce, current_user=actor
    )
    assert state["owner_user_id"] == "customer"
    assert state["brand_id"] == "customer-brand"
    with pytest.raises(social_oauth_state.OAuthStateError):
        social_oauth_state.consume_oauth_state(
            platform=platform, nonce=nonce, current_user=actor
        )
