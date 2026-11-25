#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
load_dotenv(REPO_ROOT / ".env")

from app.video_shorts.services.discovery_backlog_enrichment import (
    backlog_runtime_status,
    run_backlog_enrichment_batch,
)
from app.video_shorts.services.youtube_quota import youtube_quota_runtime_budgets


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="Run one batch outside the hourly slot guard.")
    parser.add_argument("--status", action="store_true", help="Print runtime budgets and next run without enriching.")
    args = parser.parse_args()
    status = {
        "youtube_quota": youtube_quota_runtime_budgets(),
        "backlog_enrichment": backlog_runtime_status(),
    }
    if not args.status:
        status["result"] = run_backlog_enrichment_batch(force=args.force)
    print(json.dumps(status, default=str, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
