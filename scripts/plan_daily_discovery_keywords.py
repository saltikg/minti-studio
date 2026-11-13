#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
load_dotenv(REPO_ROOT / ".env")

from app import create_app
from app.video_shorts.services.keyword_daily_plan import plan_daily_keywords


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tomorrow", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--no-generate", action="store_true")
    args = parser.parse_args()
    plan_date = datetime.now(ZoneInfo("America/Los_Angeles")).date()
    if args.tomorrow:
        plan_date += timedelta(days=1)
    app = create_app()
    with app.app_context():
        result = plan_daily_keywords(
            plan_date=plan_date, generate_llm=not args.no_generate, apply=args.apply
        )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
