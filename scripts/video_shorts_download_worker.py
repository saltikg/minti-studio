"""
Local download worker for Video Shorts.
Run on your laptop/server; it pulls pending tasks, downloads the video,
optionally uploads it to S3, then posts status back to the central API.

Usage:
  MINTI_API_BASE=https://mintiproduct.com/video_shorts \
  CAPTION_API_TOKEN=... \
  S3_BUCKET_NAME=... \
  AWS_ACCESS_KEY_ID=... \
  AWS_SECRET_ACCESS_KEY=... \
  python scripts/video_shorts_download_worker.py
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from pathlib import Path

import requests

try:
    import boto3
except ImportError:  # pragma: no cover
    boto3 = None

try:
    import yt_dlp
except ImportError:  # pragma: no cover
    yt_dlp = None


API_BASE = os.getenv("MINTI_API_BASE", "https://mintiproduct.com/video_shorts").rstrip("/")
CAPTION_API_TOKEN = os.getenv("CAPTION_API_TOKEN", "minti_caption_8273f4ac0b")
DOWNLOAD_DIR = Path(os.getenv("VIDEO_SHORTS_DOWNLOAD_DIR", "/tmp/video_shorts_downloads"))
KEEP_LOCAL = (os.getenv("VIDEO_SHORTS_KEEP_LOCAL", "0") or "0").strip().lower() in {"1", "true", "yes"}
S3_BUCKET_NAME = (os.getenv("S3_BUCKET_NAME", "") or "").strip()
AWS_REGION = (os.getenv("AWS_REGION", "us-east-1") or "us-east-1").strip()


def get_tasks(limit: int = 5) -> list[dict]:
    resp = requests.get(
        f"{API_BASE}/api/download-tasks",
        params={"limit": limit},
        headers={"X-Api-Token": CAPTION_API_TOKEN},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json().get("tasks", [])


def send_status(video_db_id: int, status: str) -> dict:
    resp = requests.post(
        f"{API_BASE}/api/download-status",
        json={"video_db_id": video_db_id, "status": status},
        headers={"X-Api-Token": CAPTION_API_TOKEN},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


def _s3_client():
    if not S3_BUCKET_NAME:
        return None
    if boto3 is None:
        raise RuntimeError("boto3 is required for S3 uploads")
    session = boto3.session.Session(
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
        region_name=AWS_REGION,
    )
    return session.client("s3", region_name=AWS_REGION)


def _download_video(video_url: str, video_id: str, out_dir: Path) -> Path:
    if yt_dlp is None:
        raise RuntimeError("yt_dlp is required")
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{video_id}.mp4"
    opts = {
        "outtmpl": str(out_dir / f"{video_id}.%(ext)s"),
        "merge_output_format": "mp4",
        "format": "bestvideo*+bestaudio/best",
        "quiet": True,
        "noprogress": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([video_url])
    if target.exists():
        return target
    candidates = sorted(out_dir.glob(f"{video_id}.*"))
    if not candidates:
        raise FileNotFoundError(f"download output missing for {video_id}")
    candidates[0].rename(target)
    return target


def _upload_to_s3(local_path: Path, video_id: str) -> str:
    client = _s3_client()
    if client is None:
        return ""
    key = f"videos/{video_id}{local_path.suffix or '.mp4'}"
    extra_args = {"ContentType": "video/mp4"} if local_path.suffix.lower() == ".mp4" else {}
    kwargs = {"Filename": str(local_path), "Bucket": S3_BUCKET_NAME, "Key": key}
    if extra_args:
        kwargs["ExtraArgs"] = extra_args
    client.upload_file(**kwargs)
    return key


def process_task(task: dict) -> None:
    db_id = task["id"]
    video_id = task["video_id"]
    video_url = task["video_url"]
    title = task.get("video_title") or video_id
    print(f"\nVideo {db_id}: {title} ({video_id})")

    work_dir = Path(tempfile.mkdtemp(prefix=f"vs_dl_{video_id}_", dir=str(DOWNLOAD_DIR)))
    local_path: Path | None = None
    try:
        local_path = _download_video(video_url, video_id, work_dir)
        print(f"  downloaded: {local_path}")
        s3_key = _upload_to_s3(local_path, video_id)
        if s3_key:
            print(f"  uploaded to s3: {s3_key}")
        send_status(db_id, "downloaded")
        print("  status sent: downloaded")

        if KEEP_LOCAL and local_path.exists():
            final_dir = DOWNLOAD_DIR / "kept"
            final_dir.mkdir(parents=True, exist_ok=True)
            final_path = final_dir / local_path.name
            shutil.copyfile(local_path, final_path)
            print(f"  kept local copy: {final_path}")
    except Exception as exc:
        print(f"  error: {exc}")
        try:
            send_status(db_id, "pending")
            print("  status left as pending")
        except Exception as status_exc:
            print(f"  status callback failed: {status_exc}")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def run_once(limit: int = 5, sleep_between: float = 1.0) -> None:
    tasks = get_tasks(limit=limit)
    print(f"Got {len(tasks)} tasks")
    for task in tasks:
        process_task(task)
        time.sleep(sleep_between)


if __name__ == "__main__":
    run_once(limit=int(os.getenv("VIDEO_SHORTS_DOWNLOAD_LIMIT", "5")))
