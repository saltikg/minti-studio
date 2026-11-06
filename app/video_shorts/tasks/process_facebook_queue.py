from app.video_shorts.services import tenant_media
import argparse
import os
import tempfile
from datetime import datetime

import requests

from app.video_shorts.config import FB_API_BASE, SHORTS_DIR
from app.video_shorts.services.facebook_queue import (
    fetch_due_facebook_jobs,
    update_facebook_job_status,
)
from app.video_shorts.services.facebook_metrics import (
    FacebookMetricsError,
    fetch_facebook_metrics,
)
from app.video_shorts.services.storage import get_media_storage
from requests.exceptions import ConnectionError as RequestsConnectionError
from src.trends.facebook_page_tokens import get_facebook_page_data, mark_facebook_insights_status
from app.video_shorts.services.social_queue_identity import queue_identity, queue_token_identity
from app.video_shorts.services.media_access import worker_clip_path
from app.video_shorts.services.publish_target_guard import (
    PublishTargetMismatch,
    require_target_match,
    send_target_mismatch_alert,
)


def _utc_now_iso() -> str:
    return datetime.utcnow().isoformat()


def _publish_facebook_reel(page_id: str, page_token: str, video_path: str, caption: str):
    file_size = os.path.getsize(video_path)
    start_url = f"{FB_API_BASE.rstrip('/')}/{page_id}/video_reels"
    start_payload = {
        "access_token": page_token,
        "upload_phase": "start",
        "file_size": file_size,
    }
    start_resp = requests.post(start_url, data=start_payload, timeout=30)
    if start_resp.status_code != 200:
        raise RuntimeError(
            f"Facebook reel start failed status={start_resp.status_code} body={start_resp.text}"
        )
    start_data = start_resp.json() or {}
    upload_url = start_data.get("upload_url")
    video_id = start_data.get("video_id") or start_data.get("id")
    if not upload_url or not video_id:
        raise RuntimeError(
            f"Facebook reel start missing upload_url/video_id body={start_resp.text}"
        )
    with open(video_path, "rb") as handle:
        upload_headers = {
            "Authorization": f"OAuth {page_token}",
            "offset": "0",
            "file_size": str(file_size),
        }
        upload_resp = requests.post(
            upload_url,
            headers=upload_headers,
            data=handle,
            timeout=120,
        )
    if upload_resp.status_code not in {200, 201}:
        raise RuntimeError(
            f"Facebook reel upload failed status={upload_resp.status_code} body={upload_resp.text}"
        )
    finish_payload = {
        "access_token": page_token,
        "upload_phase": "finish",
        "video_id": video_id,
        "description": caption,
        "share_to_feed": "true",
    }
    finish_resp = requests.post(start_url, data=finish_payload, timeout=30)
    if finish_resp.status_code != 200:
        raise RuntimeError(
            f"Facebook reel finish failed status={finish_resp.status_code} body={finish_resp.text}"
        )
    finish_data = finish_resp.json() or {}
    finish_data.setdefault("id", video_id)
    return finish_data



def _publish_facebook_video(
    page_id: str,
    page_token: str,
    video_path: str,
    caption: str,
    media_type: str,
):
    if media_type == "reel":
        return _publish_facebook_reel(page_id, page_token, video_path, caption)
    endpoint = "videos"
    url = f"{FB_API_BASE.rstrip('/')}/{page_id}/{endpoint}"
    payload = {
        "access_token": page_token,
        "description": caption,
    }
    with open(video_path, "rb") as handle:
        resp = requests.post(url, data=payload, files={"source": (os.path.basename(video_path), handle, "video/mp4")}, timeout=120)
    if resp.status_code != 200:
        raise RuntimeError(f"Facebook publish failed status={resp.status_code} body={resp.text}")
    return resp.json()


_fetch_facebook_metrics = fetch_facebook_metrics


def _queue_credentials(job: dict) -> dict:
    user_id = queue_token_identity(job)
    creds = get_facebook_page_data(user_id)
    require_target_match(
        platform="facebook_token_owner",
        schedule_id=job.get("id"),
        brand_id=job.get("brand_id"),
        expected=user_id,
        actual=(creds or {}).get("user_id"),
    )
    require_target_match(
        platform="facebook",
        schedule_id=job.get("id"),
        brand_id=job.get("brand_id"),
        expected=job.get("page_id"),
        actual=(creds or {}).get("page_id"),
    )
    if not creds.get("page_access_token"):
        raise RuntimeError(f"Facebook brand token missing for {user_id}.")
    return creds


def _current_facebook_page_id(creds: dict) -> str:
    response = requests.get(
        f"{FB_API_BASE.rstrip('/')}/me",
        params={"fields": "id", "access_token": creds.get("page_access_token")},
        timeout=15,
    )
    response.raise_for_status()
    return str((response.json() or {}).get("id") or "").strip()


def process_queue(max_jobs: int = 5) -> None:
    jobs = fetch_due_facebook_jobs(max_jobs)
    if not jobs:
        print("Facebook queue: no jobs due.")
        return
    for job in jobs:
        queue_id = job.get("id")
        if not queue_id:
            continue
        update_facebook_job_status(queue_id, status="uploading")
        clip_filename = job.get("clip_filename") or ""
        caption = (job.get("caption_text") or "").strip()
        media_type = (job.get("media_type") or "feed").lower()
        media_path = None
        try:
            media_path = worker_clip_path(job)
            creds = _queue_credentials(job)
            page_id = creds.get("page_id")
            require_target_match(
                platform="facebook",
                schedule_id=queue_id,
                brand_id=job.get("brand_id"),
                expected=job.get("page_id"),
                actual=_current_facebook_page_id(creds),
            )
            payload = _publish_facebook_video(page_id, creds.get("page_access_token"), str(media_path), caption, media_type)
            video_id = payload.get("id") or payload.get("video_id")
            metrics = {}
            if video_id:
                try:
                    metrics = _fetch_facebook_metrics(video_id, creds.get("page_access_token"))
                except FacebookMetricsError as exc:
                    owner_user_id, brand_id = queue_identity(
                        job.get("user_id"), job.get("brand_id")
                    )
                    mark_facebook_insights_status(
                        owner_user_id,
                        brand_id=str(brand_id),
                        status="needs_reconnect" if exc.needs_reconnect else "error",
                        error=str(exc),
                    )
                    print(
                        f"Facebook published without metrics queue_id={queue_id}: {exc}"
                    )
            update_facebook_job_status(
                queue_id,
                status="published",
                facebook_video_id=video_id,
                published_at_iso=_utc_now_iso(),
                permalink=metrics.get("permalink"),
                view_count=metrics.get("view_count"),
                reach=metrics.get("reach"),
                impressions=metrics.get("impressions"),
                reactions=metrics.get("reactions"),
                comment_count=metrics.get("comment_count"),
            )
            print(f"Facebook published queue_id={queue_id} video_id={video_id}")
        except PublishTargetMismatch as exc:
            update_facebook_job_status(
                queue_id,
                status="blocked_target_mismatch",
                status_detail="Publishing blocked because the connected target no longer matches the schedule.",
            )
            send_target_mismatch_alert(exc)
            print(f"Facebook publish target mismatch queue_id={queue_id}: {exc}")
        except RequestsConnectionError as exc:
            update_facebook_job_status(
                queue_id,
                status="retry",
                status_detail=str(exc),
            )
            print(f"Facebook publish retry queue_id={queue_id}: {exc}")
        except Exception as exc:
            update_facebook_job_status(
                queue_id,
                status="failed",
                status_detail=str(exc),
            )
            print(f"Facebook publish failed queue_id={queue_id}: {exc}")
        finally:
            cleanup = getattr(media_path, "cleanup", None)
            if callable(cleanup):
                cleanup()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max", type=int, default=5)
    args = parser.parse_args()
    process_queue(args.max)


if __name__ == "__main__":
    main()
