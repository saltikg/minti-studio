"""
Local caption worker for Video Shorts.
Run on your laptop; it pulls pending tasks and posts transcripts back.

Usage:
  MINTI_API_BASE=https://mintistudio.com/video_shorts \
  CAPTION_API_TOKEN=... \
  python scripts/video_shorts_caption_worker.py
"""

import os
import time
import requests
from youtube_transcript_api import YouTubeTranscriptApi, NoTranscriptFound

API_BASE = os.getenv("MINTI_API_BASE", "https://mintistudio.com/video_shorts")
CAPTION_API_TOKEN = os.getenv("CAPTION_API_TOKEN", "minti_caption_8273f4ac0b")


def get_tasks(limit=10):
    url = f"{API_BASE}/api/caption-tasks"
    resp = requests.get(
        url,
        params={"limit": limit},
        headers={"X-Api-Token": CAPTION_API_TOKEN},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json().get("tasks", [])


def send_result(video_db_id, caption_text, lang="en", segments=None):
    url = f"{API_BASE}/api/caption-result"
    resp = requests.post(
        url,
        json={
            "video_db_id": video_db_id,
            "caption_text": caption_text,
            "lang": lang,
            "segments": segments,
        },
        headers={"X-Api-Token": CAPTION_API_TOKEN},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_transcript(video_id: str):
    api = YouTubeTranscriptApi()
    fetched = api.fetch(video_id, languages=["en"])
    segments = []
    pieces = []
    for seg in fetched:
        text_part = (getattr(seg, "text", "") or "").strip()
        pieces.append(text_part)
        segments.append(
            {
                "start": getattr(seg, "start", None),
                "duration": getattr(seg, "duration", None),
                "text": text_part,
            }
        )
    text = " ".join(pieces).strip()
    return text, segments


def run_once(limit=5, sleep_between=1.0):
    tasks = get_tasks(limit=limit)
    print(f"Got {len(tasks)} tasks")

    for t in tasks:
        vid = t["video_id"]
        db_id = t["id"]
        title = t.get("video_title") or vid
        print(f"\nVideo {db_id}: {title} ({vid})")

        try:
            caption, segments = fetch_transcript(vid)
            if not caption:
                print("  empty transcript, skipping")
                continue

            print(f"  transcript length: {len(caption)} chars; segments: {len(segments)}")
            send_result(db_id, caption, lang="en", segments=segments)
            print("  sent back to server")
            time.sleep(sleep_between)
        except NoTranscriptFound:
            print("  transcript not found")
        except Exception as e:
            print(f"  error: {e}")


if __name__ == "__main__":
    run_once(limit=5)
