#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from app import create_app
from app.video_shorts.services.discovery_frame_review import (
    process_next_discovery_frame_review,
    requeue_failed_frame_reviews,
    resize_cached_frame_reviews,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--requeue-failed", action="store_true")
    parser.add_argument("--resize-existing", action="store_true")
    parser.add_argument("--maintenance-limit", type=int, default=1000)
    args = parser.parse_args()
    app = create_app()
    results = []
    with app.app_context():
        if args.resize_existing:
            resized = resize_cached_frame_reviews(limit=args.maintenance_limit)
            print(json.dumps({"resize_existing": resized}, sort_keys=True), flush=True)
        if args.requeue_failed:
            requeued = requeue_failed_frame_reviews(limit=args.maintenance_limit)
            print(json.dumps({"requeued_failed": requeued}, sort_keys=True), flush=True)
        for _ in range(max(0, min(100, args.count))):
            result = process_next_discovery_frame_review()
            if not result:
                break
            results.append(result)
            print(json.dumps(result, default=str, sort_keys=True), flush=True)
    print(json.dumps({"processed": len(results), "results": results}, default=str, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
