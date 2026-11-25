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
from app.video_shorts.services.discovery_frame_review import process_next_discovery_frame_review


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=1)
    args = parser.parse_args()
    app = create_app()
    results = []
    with app.app_context():
        for _ in range(max(1, min(100, args.count))):
            result = process_next_discovery_frame_review()
            if not result:
                break
            results.append(result)
            print(json.dumps(result, default=str, sort_keys=True), flush=True)
    print(json.dumps({"processed": len(results), "results": results}, default=str, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
