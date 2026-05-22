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
from app.video_shorts.services.storage import get_media_storage
from requests.exceptions import ConnectionError as RequestsConnectionError
from src.trends.facebook_page_tokens import get_facebook_page_data


def _utc_now_iso() -> str:
    return datetime.utcnow().isoformat()


def _resolve_clip_url(filename: str) -> str:
    key = f"shorts/{filename}"
    local_path = SHORTS_DIR / filename
    resolved = get_media_storage().resolve_local_or_s3(key, fallback_local_paths=[local_path])
    if resolved.public_url:
        return resolved.public_url
    return f"{os.getenv('BASE_URL', '').rstrip('/')}/video_shorts/static/shorts/{filename}"


def _download_video(video_url: str) -> tuple[str, int, str]:
    resp = requests.get(video_url, stream=True, timeout=60)
    resp.raise_for_status()
    content_type = resp.headers.get("Content-Type") or "application/octet-stream"
    file_size = resp.headers.get("Content-Length")
    temp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
    size = 0
    try:
        for chunk in resp.iter_content(chunk_size=1024 * 1024):
            if not chunk:
                continue
            temp.write(chunk)
            size += len(chunk)
    finally:
        temp.flush()
        temp.close()
    return temp.name, int(file_size or size), content_type


def _publish_facebook_reel(page_id: str, page_token: str, video_url: str, caption: str):
    temp_path = None
    try:
        temp_path, file_size, _content_type = _download_video(video_url)
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
        with open(temp_path, "rb") as handle:
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
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass


def _publish_facebook_video(
    page_id: str,
    page_token: str,
    video_url: str,
    caption: str,
    media_type: str,
):
    if media_type == "reel":
        return _publish_facebook_reel(page_id, page_token, video_url, caption)
    endpoint = "videos"
    url = f"{FB_API_BASE.rstrip('/')}/{page_id}/{endpoint}"
    payload = {
        "access_token": page_token,
        "description": caption,
        "file_url": video_url,
    }
    resp = requests.post(url, data=payload, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"Facebook publish failed status={resp.status_code} body={resp.text}")
    return resp.json()


def _fetch_facebook_metrics(video_id: str, page_token: str):
    insights_url = f"{FB_API_BASE.rstrip('/')}/{video_id}/video_insights"
    metrics = "total_video_impressions,total_video_impressions_unique"
    view_count = None
    reach = None
    impressions = None
    reactions = None
    comment_count = None
    try:
        resp = requests.get(
            insights_url,
            params={"access_token": page_token, "metric": metrics},
            timeout=30,
        )
        if resp.status_code == 200:
            data = resp.json().get("data") or []
            for item in data:
                name = (item.get("name") or "").lower()
                values = item.get("values") or []
                if not values:
                    continue
                value = values[-1].get("value")
                if name == "total_video_impressions":
                    impressions = value
                elif name == "total_video_impressions_unique":
                    reach = value
    except Exception:
        pass
    try:
        meta_url = f"{FB_API_BASE.rstrip('/')}/{video_id}"
        resp = requests.get(
            meta_url,
            params={
                "access_token": page_token,
                "fields": "views,likes.summary(true).limit(0),comments.summary(true).limit(0),permalink_url",
            },
            timeout=30,
        )
        if resp.status_code == 200:
            payload = resp.json()
            view_count = payload.get("views") or view_count
            reactions = (payload.get("likes") or {}).get("summary", {}).get("total_count")
            comment_count = (payload.get("comments") or {}).get("summary", {}).get("total_count")
            permalink = payload.get("permalink_url")
        else:
            permalink = None
    except Exception:
        permalink = None
    return {
        "view_count": view_count,
        "reach": reach,
        "impressions": impressions,
        "reactions": reactions,
        "comment_count": comment_count,
        "permalink": permalink,
    }


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
        user_id = job.get("user_id")
        creds = get_facebook_page_data(user_id)
        if not creds or not creds.get("page_access_token") or not creds.get("page_id"):
            update_facebook_job_status(queue_id, status="failed", status_detail="Facebook Page token missing.")
            continue
        page_id = creds.get("page_id")
        clip_filename = job.get("clip_filename") or ""
        video_url = _resolve_clip_url(clip_filename)
        caption = (job.get("caption_text") or "").strip()
        media_type = (job.get("media_type") or "feed").lower()
        try:
            payload = _publish_facebook_video(page_id, creds.get("page_access_token"), video_url, caption, media_type)
            video_id = payload.get("id") or payload.get("video_id")
            metrics = {}
            if video_id:
                metrics = _fetch_facebook_metrics(video_id, creds.get("page_access_token"))
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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max", type=int, default=5)
    args = parser.parse_args()
    process_queue(args.max)


if __name__ == "__main__":
    main()
