import argparse
import os
import json as jsonlib
import math
import time
import shutil
import socket
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse
from pathlib import Path
from typing import Optional, Tuple

import duckdb
import requests

from app.video_shorts.config import (
    SHORTS_DIR,
    TIKTOK_API_BASE,
    TIKTOK_CLIENT_KEY,
    TIKTOK_CLIENT_SECRET,
    TIKTOK_PRIVACY_LEVEL,
)
from app.video_shorts.services.db import get_db_readonly
from app.video_shorts.services.tiktok_queue import (
    fetch_due_tiktok_jobs,
    mark_tiktok_job_retry,
    update_tiktok_job_status,
)
from src.trends.tiktok_tokens import get_tiktok_data, store_tiktok_token


def _tiktok_v2_url(path: str) -> str:
    base = (TIKTOK_API_BASE or "https://open.tiktokapis.com").rstrip("/")
    if base.endswith("/v2"):
        base = base[: -len("/v2")]
    clean_path = "/" + path.lstrip("/")
    return f"{base}/v2{clean_path}"


def _now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _parse_iso_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        if value.endswith("Z"):
            return datetime.fromisoformat(value[:-1] + "+00:00")
        return datetime.fromisoformat(value)
    except Exception:
        return None


def _is_token_expired(expires_at: Optional[str]) -> bool:
    dt = _parse_iso_datetime(expires_at)
    if not dt:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt <= datetime.now(timezone.utc)


def _extract_logid(headers: dict) -> Optional[str]:
    for key, value in headers.items():
        if not isinstance(key, str):
            continue
        lowered = key.lower()
        if "logid" in lowered or "trace" in lowered:
            return value
    return None


def _redact_payload(value):
    if isinstance(value, dict):
        redacted = {}
        for key, val in value.items():
            if isinstance(key, str) and key.lower() in {
                "access_token",
                "refresh_token",
                "client_secret",
                "authorization",
                "token",
            }:
                redacted[key] = "***"
            else:
                redacted[key] = _redact_payload(val)
        return redacted
    if isinstance(value, list):
        return [_redact_payload(item) for item in value]
    return value


def _normalize_tiktok_response(resp: requests.Response, raw_text: Optional[str] = None) -> dict:
    payload = None
    if raw_text is None:
        raw_text = resp.text
    try:
        payload = resp.json()
    except Exception:
        payload = raw_text
    error_code = None
    error_message = None
    error_logid = None
    if isinstance(payload, dict):
        error = payload.get("error") or {}
        if isinstance(error, dict):
            error_code = error.get("code")
            error_message = error.get("message")
            error_logid = error.get("log_id")
        data = payload.get("data") or {}
        if isinstance(data, dict):
            error_code = error_code or data.get("error_code")
            error_message = error_message or data.get("description")
    return {
        "http_status": resp.status_code,
        "logid": (_extract_logid(resp.headers) if hasattr(resp, "headers") else None) or error_logid,
        "error_code": error_code,
        "error_message": error_message,
        "payload": _redact_payload(payload),
    }


class TikTokRequestError(RuntimeError):
    def __init__(self, message: str, *, info: Optional[dict] = None, step: str = "") -> None:
        super().__init__(message)
        self.info = info or {}
        self.step = step


def _tiktok_request(
    method: str,
    url: str,
    *,
    headers=None,
    json_body=None,
    data=None,
    params=None,
    timeout=30,
    step="",
    queue_id: Optional[str] = None,
) -> tuple[requests.Response, dict]:
    safe_headers = dict(headers or {})
    if json_body is not None and not any(
        key.lower() == "content-type" for key in safe_headers.keys()
    ):
        safe_headers["Content-Type"] = "application/json"
    info = {
        "http_status": None,
        "logid": None,
        "error_code": None,
        "error_message": None,
        "payload": None,
    }
    try:
        response = requests.request(
            method,
            url,
            headers=safe_headers,
            json=json_body,
            data=data,
            params=params,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        error_message = str(exc)
        error_code = "network_error"
        if "NameResolutionError" in error_message or "Failed to resolve" in error_message:
            error_code = "network_dns_error"
        info = {
            "http_status": None,
            "logid": None,
            "error_code": error_code,
            "error_message": error_message,
            "payload": None,
        }
        parsed = urlparse(url)
        path = parsed.path
        print(
            f"tiktok_step={step} method={method} path={path} status=None logid=None "
            f"payload=None error={info.get('error_message')}"
        )
        raise TikTokRequestError(str(exc), info=info, step=step) from exc
    info = _normalize_tiktok_response(response)
    parsed = urlparse(url)
    path = parsed.path
    logid = info.get("logid")
    print(
        f"tiktok_step={step} method={method} path={path} status={info.get('http_status')} logid={logid} "
        f"payload={jsonlib.dumps(info.get('payload')) if info.get('payload') is not None else None}"
    )
    if queue_id:
        update_tiktok_job_status(
            queue_id,
            status="uploading",
            last_step=step,
            last_http_status=info.get("http_status"),
        )
    return response, info


def _refresh_access_token(user_id: str) -> Optional[dict]:
    info = get_tiktok_data(user_id)
    if not info or not info.get("refresh_token"):
        return None
    if not TIKTOK_CLIENT_KEY or not TIKTOK_CLIENT_SECRET:
        return None
    token_url = _tiktok_v2_url("/oauth/token/")
    resp = requests.post(
        token_url,
        data={
            "client_key": TIKTOK_CLIENT_KEY,
            "client_secret": TIKTOK_CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": info.get("refresh_token"),
        },
        timeout=12,
    )
    if resp.status_code != 200:
        print(f"   ↳ token refresh failed ({resp.status_code}): {resp.text}")
        return None
    payload = resp.json() or {}
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        data = payload if isinstance(payload, dict) else {}
    access_token = data.get("access_token")
    if not access_token:
        return None
    refresh_token = data.get("refresh_token") or info.get("refresh_token")
    open_id = data.get("open_id") or info.get("open_id")
    scopes = data.get("scope") or info.get("scopes") or ""
    expires_at = None
    refresh_expires_at = None
    expires_in = data.get("expires_in")
    refresh_expires_in = data.get("refresh_expires_in")
    if expires_in:
        try:
            expires_at = (datetime.utcnow() + timedelta(seconds=int(expires_in))).isoformat()
        except Exception:
            expires_at = info.get("expires_at")
    if refresh_expires_in:
        try:
            refresh_expires_at = (datetime.utcnow() + timedelta(seconds=int(refresh_expires_in))).isoformat()
        except Exception:
            refresh_expires_at = info.get("refresh_expires_at")
    store_tiktok_token(
        user_id=user_id,
        access_token=access_token,
        refresh_token=refresh_token,
        open_id=open_id,
        username=info.get("username"),
        display_name=info.get("display_name"),
        scopes=scopes,
        expires_at=expires_at or info.get("expires_at"),
        refresh_expires_at=refresh_expires_at or info.get("refresh_expires_at"),
    )
    return get_tiktok_data(user_id)


def _resolve_privacy_level() -> str:
    candidate = (TIKTOK_PRIVACY_LEVEL or "").strip().upper()
    allowlist = {
        "PUBLIC_TO_EVERYONE",
        "MUTUAL_FOLLOW_FRIENDS",
        "FOLLOWER_OF_CREATOR",
        "SELF_ONLY",
    }
    return candidate if candidate in allowlist else "SELF_ONLY"


def _sanitize_title(value: str, fallback: str) -> str:
    title = (value or "").strip()
    if not title:
        title = fallback
    title = title.strip()[:150]
    return title


def _fetch_creator_privacy_options(access_token: str, *, queue_id: Optional[str] = None) -> list[str]:
    resp, info = _tiktok_request(
        "POST",
        _tiktok_v2_url("/post/publish/creator_info/query/"),
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=15,
        step="creator_info",
        queue_id=queue_id,
    )
    if resp.status_code != 200:
        if queue_id:
            update_tiktok_job_status(
                queue_id,
                status="failed",
                status_detail="creator_info query failed",
                last_step="init",
                last_http_status=info.get("http_status"),
                last_error_code=str(info.get("error_code") or ""),
                last_error_message=str(info.get("error_message") or ""),
                last_error_logid=str(info.get("logid") or ""),
                last_error_payload=jsonlib.dumps(info.get("payload")),
            )
        raise RuntimeError(f"creator_info query failed: {jsonlib.dumps(info)}")
    payload = resp.json() or {}
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        data = payload if isinstance(payload, dict) else {}
    options = data.get("privacy_level_options") or []
    if not isinstance(options, list):
        options = []
    print(f"   ↳ TikTok privacy options={options}")
    return [str(item) for item in options if item]


def _pick_privacy_level(options: list[str]) -> Optional[str]:
    preferred = _resolve_privacy_level()
    if preferred in options:
        return preferred
    if "PUBLIC_TO_EVERYONE" in options:
        return "PUBLIC_TO_EVERYONE"
    if "FOLLOWER_OF_CREATOR" in options:
        return "FOLLOWER_OF_CREATOR"
    if "MUTUAL_FOLLOW_FRIENDS" in options:
        return "MUTUAL_FOLLOW_FRIENDS"
    if "SELF_ONLY" in options:
        return "SELF_ONLY"
    return None


def _calculate_chunk_plan(file_size: int) -> Tuple[int, int]:
    min_chunk = 5 * 1024 * 1024
    max_chunk = 64 * 1024 * 1024
    if file_size <= min_chunk:
        return file_size, 1
    if file_size <= max_chunk:
        return file_size, 1
    chunk_size = max_chunk
    total_chunks = int(math.ceil(file_size / chunk_size))
    return chunk_size, total_chunks


def _init_video_upload(access_token: str, caption: str, file_size: int, fallback_title: str, *, queue_id: Optional[str] = None) -> Tuple[str, Optional[str], Optional[str]]:
    privacy_options = _fetch_creator_privacy_options(access_token, queue_id=queue_id)
    privacy_level = _pick_privacy_level(privacy_options)
    if not privacy_level:
        detail = "No supported TikTok privacy level available"
        if queue_id:
            update_tiktok_job_status(
                queue_id,
                status="failed",
                status_detail=detail,
                last_step="init",
                last_error_message=detail,
            )
        raise RuntimeError(detail)
    chunk_size, total_chunk_count = _calculate_chunk_plan(file_size)
    payload = {
        "post_info": {
            "privacy_level": privacy_level,
        },
        "source_info": {
            "source": "FILE_UPLOAD",
            "video_size": file_size,
            "chunk_size": chunk_size,
            "total_chunk_count": total_chunk_count,
        },
    }
    title = _sanitize_title(caption, fallback_title)
    if title:
        payload["post_info"]["title"] = title
    print(f"   ↳ TikTok init post_info={payload['post_info']}")
    print(f"   ↳ TikTok init source_info={payload['source_info']}")
    resp, info = _tiktok_request(
        "POST",
        _tiktok_v2_url("/post/publish/video/init/"),
        json_body=payload,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30,
        step="init",
        queue_id=queue_id,
    )
    if resp.status_code != 200:
        update_tiktok_job_status(
            queue_id,
            status="failed",
            status_detail="init upload failed",
            last_step="init",
            last_http_status=info.get("http_status"),
            last_error_code=str(info.get("error_code") or ""),
            last_error_message=str(info.get("error_message") or ""),
            last_error_logid=str(info.get("logid") or ""),
            last_error_payload=jsonlib.dumps(info.get("payload")),
        )
        raise RuntimeError(f"init upload failed: {jsonlib.dumps(info)}")
    data = resp.json().get("data") or {}
    upload_url = data.get("upload_url")
    publish_id = data.get("publish_id") or data.get("publish_id_str")
    video_id = data.get("video_id")
    if not upload_url or not publish_id:
        raise RuntimeError("init upload response missing upload_url/publish_id")
    return upload_url, publish_id, video_id


def _upload_video_bytes(upload_url: str, clip_path: Path, *, queue_id: Optional[str] = None) -> None:
    file_size = clip_path.stat().st_size
    chunk_size, total_chunks = _calculate_chunk_plan(file_size)
    with clip_path.open("rb") as handle:
        for idx in range(total_chunks):
            start = idx * chunk_size
            end = min(start + chunk_size, file_size)
            length = end - start
            handle.seek(start)
            chunk = handle.read(length)
            headers = {
                "Content-Type": "video/mp4",
                "Content-Length": str(length),
                "Content-Range": f"bytes {start}-{end - 1}/{file_size}",
            }
            print(f"   ↳ TikTok upload chunk {idx + 1}/{total_chunks} bytes {start}-{end - 1}/{file_size}")
            resp, info = _tiktok_request(
                "PUT",
                upload_url,
                data=chunk,
                headers=headers,
                timeout=120,
                step="upload",
                queue_id=queue_id,
            )
            if resp.status_code not in {200, 201, 204}:
                if queue_id:
                    update_tiktok_job_status(
                        queue_id,
                        status="failed",
                        status_detail="upload failed",
                        last_step="upload",
                        last_http_status=info.get("http_status"),
                        last_error_code=str(info.get("error_code") or ""),
                        last_error_message=str(info.get("error_message") or ""),
                        last_error_logid=str(info.get("logid") or ""),
                        last_error_payload=jsonlib.dumps(info.get("payload")),
                    )
                raise RuntimeError(f"upload failed: {jsonlib.dumps(info)}")


def _fetch_publish_status(access_token: str, publish_id: str, *, queue_id: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
    endpoint = _tiktok_v2_url("/post/publish/status/fetch/")
    resp, info = _tiktok_request(
        "POST",
        endpoint,
        json_body={"publish_id": publish_id},
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8",
        },
        timeout=15,
        step="complete",
        queue_id=queue_id,
    )
    if resp.status_code == 404 and isinstance(info.get("payload"), str):
        if "<html" in info.get("payload", "").lower():
            print(f"tiktok_step=complete method=POST url={endpoint} html_404=true")
    if resp.status_code != 200:
        if queue_id:
            error_status = info.get("http_status")
            error_message = info.get("error_message") or f"publish status failed (HTTP {error_status})"
            error_code = info.get("error_code") or (f"http_{error_status}" if error_status else "http_error")
            error_payload = {
                "request": {
                    "method": "POST",
                    "url": endpoint,
                    "body": {"publish_id": publish_id},
                },
                "response": info.get("payload"),
            }
            update_tiktok_job_status(
                queue_id,
                status="failed",
                status_detail="publish status failed",
                last_step="complete",
                last_http_status=info.get("http_status"),
                last_error_code=str(error_code or ""),
                last_error_message=error_message,
                last_error_logid=str(info.get("logid") or ""),
                last_error_payload=jsonlib.dumps(error_payload),
            )
        return None, None
    payload = resp.json() or {}
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        data = payload if isinstance(payload, dict) else {}
    return data.get("status"), data.get("video_id")


def _wait_for_publish(access_token: str, publish_id: str, *, queue_id: Optional[str] = None) -> Tuple[bool, Optional[str]]:
    wait_schedule = [0, 5, 10, 20, 30, 30]
    last_status = None
    video_id = None
    for delay in wait_schedule:
        if delay:
            time.sleep(delay)
        status, vid = _fetch_publish_status(access_token, publish_id, queue_id=queue_id)
        if vid:
            video_id = vid
        last_status = status
        if status in {"SUCCESS", "PUBLISH_COMPLETE", "DONE", "PUBLISHED"}:
            return True, video_id
        if status in {"FAILED", "ERROR"}:
            break
    return False, video_id or None


def _publish_tiktok(job: dict) -> Optional[str]:
    clip_path = SHORTS_DIR / (job.get("clip_filename") or "")
    if not clip_path.exists():
        raise RuntimeError(f"Clip dosyası bulunamadı: {clip_path}")
    user_id = job.get("user_id")
    creds = get_tiktok_data(user_id)
    if not creds:
        raise RuntimeError("TikTok bağlantısı bulunamadı veya geçersiz.")
    if _is_token_expired(creds.get("expires_at")):
        refreshed = _refresh_access_token(user_id)
        if refreshed:
            creds = refreshed
    access_token = creds.get("access_token")
    if not access_token:
        raise RuntimeError("TikTok access token bulunamadı.")

    update_tiktok_job_status(
        job["id"],
        status=job.get("status") or "uploading",
        last_step="init",
        last_http_status=None,
    )
    caption = (job.get("caption_text") or "").strip()
    fallback_title = job.get("plan_title") or clip_path.stem
    file_size = clip_path.stat().st_size
    upload_url, publish_id, video_id = _init_video_upload(access_token, caption, file_size, fallback_title, queue_id=job["id"])
    update_tiktok_job_status(
        job["id"],
        status=job.get("status") or "uploading",
        last_step="upload",
        last_http_status=None,
        tiktok_publish_id=publish_id,
        tiktok_video_id=video_id,
    )
    _upload_video_bytes(upload_url, clip_path, queue_id=job["id"])
    ready, published_video_id = _wait_for_publish(access_token, publish_id, queue_id=job["id"])
    update_tiktok_job_status(
        job["id"],
        status=job.get("status") or "uploading",
        last_step="complete",
        last_http_status=None,
    )
    if not ready:
        raise RuntimeError("TikTok publish status not ready; retry later.")
    update_tiktok_job_status(
        job["id"],
        status="published",
        tiktok_video_id=published_video_id or video_id,
        tiktok_publish_id=publish_id,
        published_at_iso=_now_iso(),
        last_step="complete",
    )
    return published_video_id or video_id


def process_queue(max_jobs: int):
    _log_queue_state()
    jobs = fetch_due_tiktok_jobs(max_jobs)
    if not jobs:
        print("TikTok kuyruğunda iş yok.")
        return
    for job in jobs:
        print(f"→ İşleniyor: {job.get('clip_filename')} (plan {job.get('plan_index')})")
        update_tiktok_job_status(job["id"], status="uploading")
        try:
            video_id = _publish_tiktok(job)
            print(f"   ✓ Yayınlandı. video_id={video_id}")
        except Exception as exc:
            print(f"   ✗ Hata: {exc}")
            if "not ready" in str(exc).lower():
                mark_tiktok_job_retry(job["id"], str(exc))
                update_tiktok_job_status(
                    job["id"],
                    status="retry",
                    status_detail=str(exc),
                    last_step="complete",
                )
                continue
            error_step = _normalize_error_step(getattr(exc, "step", None) or job.get("last_step"))
            error_code = None
            error_message = str(exc)
            http_status = None
            logid = None
            if isinstance(exc, TikTokRequestError):
                info = exc.info or {}
                error_code = info.get("error_code") or "network_dns_error"
                error_message = info.get("error_message") or str(exc)
                http_status = info.get("http_status")
                logid = info.get("logid")
                if error_code == "network_dns_error":
                    _log_dns_diagnostics()
            update_tiktok_job_status(
                job["id"],
                status="failed",
                status_detail=error_message,
                last_step=error_step,
                last_http_status=http_status,
                last_error_code=str(error_code or ""),
                last_error_message=error_message,
                last_error_logid=str(logid or ""),
                last_error_payload=None,
            )


def main():
    ap = argparse.ArgumentParser(description="TikTok kuyruğunu işle")
    ap.add_argument("--max", type=int, default=3, help="Bu çalıştırmada işlenecek maksimum kayıt")
    args = ap.parse_args()
    SHORTS_DIR.mkdir(parents=True, exist_ok=True)
    _log_runtime_environment()
    process_queue(args.max)


def _log_queue_state():
    try:
        conn = get_db_readonly()
        try:
            pending_count = conn.execute(
                "SELECT COUNT(*) FROM shorts_tiktok_queue WHERE status IN ('pending','retry')"
            ).fetchone()[0]
        finally:
            conn.close()
    except Exception as exc:
        print(f"TikTok queue DB info unavailable: {exc}")
    else:
        print(f"TikTok queue pending_count={pending_count}")


def _log_dns_diagnostics() -> None:
    host = "open.tiktokapis.com"
    checks = [
        ("getent", ["hosts", host]),
        ("dig", ["+short", host]),
        ("curl", ["-I", "https://open.tiktokapis.com/v2/"]),
    ]
    print("TikTok DNS diagnostics start")
    for cmd, args in checks:
        if not shutil.which(cmd):
            print(f"  - {cmd}: not available")
            continue
        try:
            result = subprocess.run(
                [cmd, *args],
                capture_output=True,
                text=True,
                timeout=8,
            )
            stdout = (result.stdout or "").strip()
            stderr = (result.stderr or "").strip()
            print(f"  - {cmd} {' '.join(args)} -> rc={result.returncode}")
            if stdout:
                print(f"    stdout: {stdout}")
            if stderr:
                print(f"    stderr: {stderr}")
        except Exception as exc:
            print(f"  - {cmd} failed: {exc}")
    print("TikTok DNS diagnostics end")


def _log_runtime_environment() -> None:
    print(f"TikTok runner python={sys.executable} version={sys.version.splitlines()[0]}")
    print(f"TikTok API base={TIKTOK_API_BASE}")
    print(f"TikTok API v2={_tiktok_v2_url('/')}")
    try:
        socket.getaddrinfo("open.tiktokapis.com", 443)
        print("TikTok DNS smoke getaddrinfo=ok")
    except Exception as exc:
        print(f"TikTok DNS smoke getaddrinfo=failed error={exc}")
    try:
        resp = requests.get("https://open.tiktokapis.com/v2/", timeout=5)
        print(f"TikTok API smoke status={resp.status_code}")
    except Exception as exc:
        print(f"TikTok API smoke failed error={exc}")


def _normalize_error_step(step_value: Optional[str]) -> str:
    step = (step_value or "").lower()
    if step.startswith("upload"):
        return "upload"
    if step.startswith("publish") or step.startswith("complete"):
        return "complete"
    return "init"


if __name__ == "__main__":
    main()
