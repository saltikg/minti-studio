#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv

load_dotenv(REPO_ROOT / ".env")

from app.video_shorts.services.niche_outliers import refresh_compute_and_persist


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--niche", default="menopause_hormones")
    parser.add_argument("--as-of", default="")
    parser.add_argument("--max-units", type=int, default=500)
    args = parser.parse_args()
    result = refresh_compute_and_persist(
        args.niche,
        args.as_of or None,
        max_units=args.max_units,
    )
    report = {
        "niche": result["niche"],
        "as_of": result["as_of"],
        "units_used": result["units_used"],
        "calls": result["calls"],
        "channels": result["channels"],
        "videos": result["videos"],
        "channels_with_3_shorts": result["channels_with_3_shorts"],
        "outlier_count": result["outlier_count"],
        "top_10": result["rows"][:10],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
