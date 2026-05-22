#!/usr/bin/env python3
"""
Runs YouTube short comment sync + Instagram metrics sync in one cron job.
"""
from __future__ import annotations

import logging
import pathlib
import sys

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app import create_app
from app.video_shorts.tasks.sync_short_comments import main as sync_short_comments_main
from app.video_shorts.tasks.sync_instagram_metrics import sync_instagram_metrics

logger = logging.getLogger(__name__)


def main() -> int:
    app = create_app()
    exit_code = 0
    with app.app_context():
        try:
            sync_short_comments_main()
        except Exception:
            logger.exception("sync_short_comments failed")
            exit_code = 1
        try:
            sync_instagram_metrics(max_items=None, comments_limit=25)
        except Exception:
            logger.exception("sync_instagram_metrics failed")
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
