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

from app.video_shorts.services.niche_outliers import (
    classify_and_persist_latest_topics,
    generate_niche_outlier_pattern,
    persist_niche_outlier_pattern,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--niche", default="menopause_hormones")
    args = parser.parse_args()
    classified = classify_and_persist_latest_topics(args.niche)
    if not classified:
        print(json.dumps({"niche": args.niche, "status": "no outliers"}))
        return 0
    result, topic_usage = classified
    pattern = generate_niche_outlier_pattern(result)
    if pattern:
        persist_niche_outlier_pattern(pattern)
    report = {
        "niche": args.niche,
        "as_of": result["as_of"],
        "classified": len(topic_usage["decisions"]),
        "status_counts": {
            status: sum(
                1 for item in topic_usage["decisions"]
                if item["in_category"] is (status == "in_category")
            )
            for status in ("in_category", "out_of_category")
        },
        "changed": [
            item for item in topic_usage["decisions"]
            if item["previous_in_category"] != item["in_category"]
        ],
        "topic_usage": topic_usage,
        "pattern": pattern,
        "total_estimated_cost_usd": (
            topic_usage["estimated_cost_usd"]
            + float((pattern or {}).get("estimated_cost_usd") or 0)
        ),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
