#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.video_shorts.services.niche_caption_archive import enqueue_brand_caption_archive
from app.video_shorts.services.niche_weekly_ideas import compute_brand_weekly_ideas


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("brand_id")
    parser.add_argument("--enqueue-caption", action="store_true")
    parser.add_argument("--max-videos", type=int)
    parser.add_argument("--run-key")
    args = parser.parse_args()
    if args.enqueue_caption:
        run_key = args.run_key or datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d-%H%M%S")
        result = enqueue_brand_caption_archive(
            args.brand_id, run_key=run_key, max_videos=args.max_videos,
        )
    else:
        result = compute_brand_weekly_ideas(args.brand_id)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
