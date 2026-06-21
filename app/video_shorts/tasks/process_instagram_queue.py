import argparse
import os
import time
from datetime import datetime, timezone
from typing import Optional

import requests

from app.video_shorts.config import IG_API_BASE, IG_GRAPH_API_BASE, SHORTS_DIR
from app.video_shorts.services.db import get_db_readonly
from app.video_shorts.services.instagram_queue import (
    fetch_due_instagram_jobs,
    mark_job_retry,
    retry_instagram_job_as_reel,
    update_instagram_job_status,
)
from app.video_shorts.services.storage import get_media_storage
from src.trends.instagram_tokens import get_instagram_credentials
from app.video_shorts.tasks.process_facebook_queue import process_queue as process_facebook_queue

BASE_URL = os.getenv("BASE_URL", "https://mintiproduct.com").rstrip("/")


def _resolve_clip(filename: str):
    key = f"shorts/{filename}"
    local_path = SHORTS_DIR / filename
    return get_media_storage().resolve_local_or_s3(key, fallback_local_paths=[local_path])


def _public_clip_url(filename: str) -> str:
    resolved = _resolve_clip(filename)
    if resolved.public_url:
        return resolved.public_url
    return f"{BASE_URL}/video_shorts/static/shorts/{filename}"


def _now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


class InstagramMediaError(RuntimeError):
    def __init__(self, message: str, *, error_subcode: Optional[int] = None):
        super().__init__(message)
        self.error_subcode = error_subcode


def _payload_for_log(payload: dict) -> dict:
    sanitized = {}
    for key, value in payload.items():
        if key == "access_token":
            continue
        sanitized[key] = value
    return sanitized


def _extract_error_subcode(response: requests.Response) -> Optional[int]:
    try:
        data = response.json()
        return data.get("error", {}).get("error_subcode")
    except Exception:
        return None


def _wait_for_creation_ready(business_id: str, creation_id: str, access_token: str) -> tuple[bool, Optional[str], int]:
    wait_schedule = [0, 5, 10, 20, 30, 30, 30, 30]  # seconds, total ~155s
    total_wait = 0
    last_status = None
    endpoint = f"{IG_GRAPH_API_BASE.rstrip('/')}/{creation_id}"
    for delay in wait_schedule:
        if delay:
            time.sleep(delay)
            total_wait += delay
        try:
            resp = requests.get(
                endpoint,
                params={"fields": "status_code", "access_token": access_token},
                timeout=15,
            )
        except Exception as exc:
            print(f"   ↳ creation status check failed: {exc}")
            last_status = None
            continue
        if resp.status_code != 200:
            print(f"   ↳ creation status error ({resp.status_code}): {resp.text}")
            last_status = None
            continue
        last_status = (resp.json() or {}).get("status_code")
        print(f"   ↳ creation status={last_status} waited={total_wait}s")
        if last_status == "FINISHED":
            return True, last_status, total_wait
        if last_status in {"ERROR", "CANCELED"}:
            break
    return False, last_status, total_wait


def _upload_reel(job: dict) -> Optional[str]:
    clip_filename = job.get("clip_filename") or ""
    resolved = _resolve_clip(clip_filename)
    if not resolved.exists:
        raise RuntimeError(f"Clip dosyası bulunamadı: {SHORTS_DIR / clip_filename}")
    user_id = job.get("user_id")
    creds = get_instagram_credentials(user_id)
    if not creds:
        raise RuntimeError("Instagram bağlantısı bulunamadı veya geçersiz.")
    access_token = creds.get("page_access_token")
    business_id = creds.get("instagram_business_account_id")
    if not access_token or not business_id:
        raise RuntimeError("Instagram token veya Business ID eksik.")
    video_url = _public_clip_url(clip_filename)
    caption = (job.get("caption_text") or "").strip()
    media_type = (job.get("media_type") or "reel").lower()
    if media_type not in {"reel", "feed"}:
        media_type = "reel"

    payload = {
        "media_type": "REELS",
        "video_url": video_url,
        "caption": caption,
        "access_token": access_token,
    }
    if media_type == "feed":
        payload["share_to_feed"] = "true"
        payload["is_feed"] = "true"

    print(f"   ↳ IG media create payload={_payload_for_log(payload)}")
    create_resp = requests.post(
        f"{IG_GRAPH_API_BASE.rstrip('/')}/{business_id}/media",
        data=payload,
        timeout=30,
    )
    if create_resp.status_code != 200:
        print(f"   ↳ media create error ({create_resp.status_code}): {create_resp.text}")
        raise InstagramMediaError(
            f"media create failed: {create_resp.text}",
            error_subcode=_extract_error_subcode(create_resp),
        )
    creation_id = (create_resp.json() or {}).get("id")
    if not creation_id:
        raise InstagramMediaError("Instagram creation_id alınamadı.")
    print(f"   ↳ creation_id={creation_id}")
    ready, last_status, waited = _wait_for_creation_ready(business_id, creation_id, access_token)
    if not ready:
        print(f"   ↳ creation not ready (status={last_status}, waited={waited}s)")
        raise InstagramMediaError(
            f"media not ready for publish (status={last_status}, waited={waited}s)",
            error_subcode=2207027,
        )
    print(f"   ↳ creation ready after {waited}s")
    publish_payload = {"creation_id": creation_id, "access_token": access_token}
    publish_resp = requests.post(
        f"{IG_GRAPH_API_BASE.rstrip('/')}/{business_id}/media_publish",
        data=publish_payload,
        timeout=30,
    )
    if publish_resp.status_code != 200:
        print(f"   ↳ media publish error ({publish_resp.status_code}): {publish_resp.text}")
        raise InstagramMediaError(
            f"media publish failed: {publish_resp.text}",
            error_subcode=_extract_error_subcode(publish_resp),
        )
    media_id = (publish_resp.json() or {}).get("id")
    if not media_id:
        raise InstagramMediaError("Instagram media_id alınamadı.")
    print(f"   ↳ publish_id={media_id}")
    details_resp = requests.get(
        f"{IG_GRAPH_API_BASE.rstrip('/')}/{media_id}",
        params={"fields": "permalink,like_count,comments_count", "access_token": access_token},
        timeout=15,
    )
    if details_resp.status_code != 200:
        print(f"   ↳ media details error ({details_resp.status_code}): {details_resp.text}")
    details = details_resp.json() if details_resp.status_code == 200 else {}
    update_instagram_job_status(
        job["id"],
        status="published",
        instagram_media_id=media_id,
        published_at_iso=_now_iso(),
        permalink=details.get("permalink"),
        like_count=details.get("like_count"),
        comment_count=details.get("comments_count"),
    )
    return media_id


def process_queue(max_jobs: int):
    _log_queue_state()
    jobs = fetch_due_instagram_jobs(max_jobs)
    if not jobs:
        print("Instagram kuyruğunda iş yok.")
        return
    for job in jobs:
        print(f"→ İşleniyor: {job.get('clip_filename')} (plan {job.get('plan_index')})")
        update_instagram_job_status(job["id"], status="uploading")
        try:
            media_id = _upload_reel(job)
            print(f"   ✓ Yayınlandı. media_id={media_id}")
        except InstagramMediaError as exc:
            print(f"   ✗ Hata: {exc}")
            media_type_value = (job.get("media_type") or "").lower()
            if exc.error_subcode == 2207067 and media_type_value == "feed":
                print("   ↻ VIDEO media_type reddedildi; job REELS + share_to_feed olarak yeniden denenecek.")
                retry_instagram_job_as_reel(job["id"])
                continue
            if exc.error_subcode == 2207027 or exc.error_subcode == 9007:
                print("   ↻ Media hazır değil; job retry kuyruğuna alınacak.")
                mark_job_retry(job["id"], str(exc))
                continue
            update_instagram_job_status(
                job["id"],
                status="failed",
                status_detail=str(exc),
            )
        except Exception as exc:
            print(f"   ✗ Hata: {exc}")
            update_instagram_job_status(
                job["id"],
                status="failed",
                status_detail=str(exc),
            )


def main():
    ap = argparse.ArgumentParser(description="Instagram Reels kuyruğunu işle")
    ap.add_argument("--max", type=int, default=3, help="Bu çalıştırmada işlenecek maksimum kayıt")
    args = ap.parse_args()
    SHORTS_DIR.mkdir(parents=True, exist_ok=True)
    print("== Instagram publish run ==")
    process_queue(args.max)
    print("== Facebook publish run ==")
    try:
        process_facebook_queue(args.max)
    except Exception as exc:
        print(f"Facebook queue failed: {exc}")


def _log_queue_state():
    try:
        conn = get_db_readonly()
        try:
            pending_count = conn.execute(
                "SELECT COUNT(*) FROM shorts_instagram_queue WHERE status IN ('pending','retry')"
            ).fetchone()[0]
        finally:
            conn.close()
    except Exception as exc:
        print(f"Instagram queue DB info unavailable: {exc}")
    else:
        print(f"Instagram queue pending_count={pending_count}")


if __name__ == "__main__":
    main()
