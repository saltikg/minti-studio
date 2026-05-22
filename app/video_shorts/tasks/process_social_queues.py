#!/usr/bin/env python3
"""
Process Instagram/Facebook + TikTok queues in one cron job.
"""
from __future__ import annotations

import argparse
import pathlib
import sys

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.video_shorts.config import SHORTS_DIR
from app.video_shorts.tasks.process_facebook_queue import process_queue as process_facebook_queue
from app.video_shorts.tasks.process_instagram_queue import process_queue as process_instagram_queue
from app.video_shorts.tasks.process_tiktok_queue import process_queue as process_tiktok_queue


def main() -> int:
    ap = argparse.ArgumentParser(description="Process Instagram/Facebook and TikTok queues.")
    ap.add_argument("--max-instagram", type=int, default=5, help="Max Instagram jobs per run")
    ap.add_argument("--max-tiktok", type=int, default=5, help="Max TikTok jobs per run")
    args = ap.parse_args()
    SHORTS_DIR.mkdir(parents=True, exist_ok=True)
    print("== Instagram publish run ==")
    process_instagram_queue(args.max_instagram)
    print("== Facebook publish run ==")
    try:
        process_facebook_queue(args.max_instagram)
    except Exception as exc:
        print(f"Facebook queue failed: {exc}")
    print("== TikTok publish run ==")
    process_tiktok_queue(args.max_tiktok)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
