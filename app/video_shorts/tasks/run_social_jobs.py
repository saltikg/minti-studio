#!/usr/bin/env python3
"""
Run social queues + metrics in a single cron job.
"""
from __future__ import annotations

import argparse
import pathlib
import sys
import time
from datetime import datetime, timezone
from typing import Callable

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.video_shorts.config import SHORTS_DIR
from app.video_shorts.services.db import get_db
from app.video_shorts.tasks.process_facebook_queue import process_queue as process_facebook_queue
from app.video_shorts.tasks.process_instagram_queue import process_queue as process_instagram_queue
from app.video_shorts.tasks.process_tiktok_queue import process_queue as process_tiktok_queue
from app.video_shorts.tasks.sync_instagram_metrics import sync_instagram_metrics
from app.video_shorts.tasks.sync_short_comments import main as sync_short_comments_main

ERROR_LOG_PATH = PROJECT_ROOT / "logs" / "social_all_errors.log"
PUBLISH_LOCK_TIMEOUT_SECONDS = 30
METRICS_LOCK_TIMEOUT_SECONDS = 5


def _log_error(step: str, exc: Exception) -> None:
    ERROR_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    message = f"[{timestamp}] {step} failed: {exc}"
    with ERROR_LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(message + "\n")
    print(message)


def _wait_for_db_unlock(timeout_seconds: int = 300, step: str = "db") -> bool:
    start = time.time()
    while True:
        try:
            conn = get_db()
            conn.close()
            return True
        except Exception as exc:
            if time.time() - start >= timeout_seconds:
                _log_error(f"{step} lock wait", exc)
                return False
            time.sleep(5)


def _run_locked_step(
    label: str,
    func: Callable[[], None],
    *,
    lock_timeout_seconds: int,
    required: bool,
) -> bool:
    print(f"== {label} ==")
    if not _wait_for_db_unlock(timeout_seconds=lock_timeout_seconds, step=label):
        return not required
    try:
        func()
    except Exception as exc:
        _log_error(label, exc)
        return not required
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="Run social queues + metrics.")
    ap.add_argument("--max-instagram", type=int, default=5, help="Max Instagram jobs per run")
    ap.add_argument("--max-tiktok", type=int, default=5, help="Max TikTok jobs per run")
    ap.add_argument("--comments-limit", type=int, default=25, help="Max Instagram comments per media")
    ap.add_argument("--skip-youtube-comments", action="store_true", help="Skip YouTube comment sync")
    ap.add_argument("--only-youtube-comments", action="store_true", help="Run only YouTube comment sync")
    ap.add_argument(
        "--comment-scan-scope",
        choices=("recent50", "all"),
        default="recent50",
        help="YouTube comment scan scope for --only-youtube-comments runs",
    )
    args = ap.parse_args()

    SHORTS_DIR.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now(timezone.utc)
    print(f"[{started_at.strftime('%Y-%m-%d %H:%M:%S UTC')}] == Social jobs start ==")

    # Hourly YouTube-only run: keep the high-frequency job focused on publish queues.
    if args.only_youtube_comments:
        if not _run_locked_step(
            "YouTube comment sync (only)",
            lambda: sync_short_comments_main(comment_scan_scope=args.comment_scan_scope),
            lock_timeout_seconds=PUBLISH_LOCK_TIMEOUT_SECONDS,
            required=True,
        ):
            return 1
        finished_at = datetime.now(timezone.utc)
        print(f"[{finished_at.strftime('%Y-%m-%d %H:%M:%S UTC')}] == Social jobs done ==")
        return 0

    # 10-minute cron uses this flag to skip YouTube comment sync.
    if not args.skip_youtube_comments:
        if not _run_locked_step(
            "YouTube comment sync",
            lambda: sync_short_comments_main(comment_scan_scope=args.comment_scan_scope),
            lock_timeout_seconds=PUBLISH_LOCK_TIMEOUT_SECONDS,
            required=True,
        ):
            return 1

    if not _run_locked_step(
        "Instagram publish run",
        lambda: process_instagram_queue(args.max_instagram),
        lock_timeout_seconds=PUBLISH_LOCK_TIMEOUT_SECONDS,
        required=True,
    ):
        return 1

    if not _run_locked_step(
        "Facebook publish run",
        lambda: process_facebook_queue(args.max_instagram),
        lock_timeout_seconds=PUBLISH_LOCK_TIMEOUT_SECONDS,
        required=True,
    ):
        return 1

    if not _run_locked_step(
        "TikTok publish run",
        lambda: process_tiktok_queue(args.max_tiktok),
        lock_timeout_seconds=PUBLISH_LOCK_TIMEOUT_SECONDS,
        required=True,
    ):
        return 1

    _run_locked_step(
        "Instagram metrics sync",
        lambda: sync_instagram_metrics(max_items=None, comments_limit=max(0, args.comments_limit)),
        lock_timeout_seconds=METRICS_LOCK_TIMEOUT_SECONDS,
        required=False,
    )
    finished_at = datetime.now(timezone.utc)
    print(f"[{finished_at.strftime('%Y-%m-%d %H:%M:%S UTC')}] == Social jobs done ==")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
