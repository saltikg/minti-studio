#!/usr/bin/env python3
import argparse
import json
import sys
from pathlib import Path
from typing import Dict, Set

from dotenv import load_dotenv


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


ROOT = _project_root()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

load_dotenv(ROOT / ".env")

from app.video_shorts.config import SHORTS_DIR, VIDEOS_DIR
from app.video_shorts.services.db import get_db_readonly
from app.video_shorts.services.storage import get_media_storage


MEDIA_SUFFIXES = {".mp4", ".mkv", ".mov", ".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}
ACTIVE_DOWNLOAD_STATUSES = {"pending", "downloading", "processing", "downloaded", "short"}
ACTIVE_TRANSCRIPT_STATUSES = {"pending", "running", "queued"}


def _load_video_rows() -> Dict[str, Dict[str, str]]:
    conn = get_db_readonly()
    try:
        rows = conn.execute(
            """
            SELECT
                video_id,
                COALESCE(download_status, ''),
                COALESCE(transcript_status, ''),
                COALESCE(title, '')
            FROM youtube_videos
            """
        ).fetchall()
    finally:
        conn.close()
    return {
        str(video_id): {
            "download_status": str(download_status or "").strip().lower(),
            "transcript_status": str(transcript_status or "").strip().lower(),
            "title": str(title or "").strip(),
        }
        for video_id, download_status, transcript_status, title in rows
    }


def _load_plan_video_ids() -> Set[str]:
    video_ids: Set[str] = set()
    if not SHORTS_DIR.exists():
        return video_ids
    for plan_path in SHORTS_DIR.glob("*_plan*.json"):
        stem = plan_path.name.split("_plan", 1)[0].strip()
        if stem:
            video_ids.add(stem)
        try:
            payload = json.loads(plan_path.read_text())
        except Exception:
            continue
        entries = payload.get("plan") or payload.get("clips") or []
        for entry in entries:
            video_id = str(entry.get("video_id") or "").strip()
            if video_id:
                video_ids.add(video_id)
    return video_ids


def _is_top_level_media(path: Path) -> bool:
    return path.is_file() and path.parent == VIDEOS_DIR and path.suffix.lower() in MEDIA_SUFFIXES


def main() -> int:
    parser = argparse.ArgumentParser(description="Classify local downloaded/source videos for cleanup planning.")
    parser.add_argument("--limit", type=int, default=0, help="Maximum number of files to print per category.")
    args = parser.parse_args()

    storage = get_media_storage("s3")
    rows = _load_video_rows()
    plan_video_ids = _load_plan_video_ids()

    categories: Dict[str, list[dict]] = {
        "tmp_files": [],
        "active_source": [],
        "orphan_local_upload": [],
        "archive_candidate": [],
        "missing_in_s3": [],
    }

    for path in sorted(VIDEOS_DIR.iterdir()):
        if not _is_top_level_media(path):
            continue
        name = path.name
        stem = path.stem
        key = f"videos/{name}"
        try:
            in_s3 = storage.exists(key)
        except Exception:
            in_s3 = False
        if name.startswith("tmp"):
            categories["tmp_files"].append({"name": name, "size": path.stat().st_size, "in_s3": in_s3})
            continue

        row = rows.get(stem)
        is_local_upload = stem.startswith("local_")
        has_plan = stem in plan_video_ids
        if not in_s3:
            categories["missing_in_s3"].append(
                {
                    "name": name,
                    "size": path.stat().st_size,
                    "download_status": row["download_status"] if row else "",
                    "transcript_status": row["transcript_status"] if row else "",
                }
            )
            continue
        if row:
            download_status = row["download_status"]
            transcript_status = row["transcript_status"]
            if (
                download_status in ACTIVE_DOWNLOAD_STATUSES
                or transcript_status in ACTIVE_TRANSCRIPT_STATUSES
                or has_plan
            ):
                categories["active_source"].append(
                    {
                        "name": name,
                        "size": path.stat().st_size,
                        "download_status": download_status,
                        "transcript_status": transcript_status,
                        "has_plan": has_plan,
                        "title": row["title"],
                    }
                )
            else:
                categories["archive_candidate"].append(
                    {
                        "name": name,
                        "size": path.stat().st_size,
                        "download_status": download_status,
                        "transcript_status": transcript_status,
                        "has_plan": has_plan,
                        "title": row["title"],
                    }
                )
            continue
        if is_local_upload:
            categories["orphan_local_upload"].append({"name": name, "size": path.stat().st_size, "in_s3": in_s3})
        else:
            categories["archive_candidate"].append({"name": name, "size": path.stat().st_size, "has_plan": has_plan})

    print("Downloaded videos classification")
    for category, entries in categories.items():
        total_bytes = sum(int(entry.get("size") or 0) for entry in entries)
        print(f"{category}_count={len(entries)}")
        print(f"{category}_bytes={total_bytes}")
        for entry in entries[: args.limit or 0]:
            print(f"{category}: {entry}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
