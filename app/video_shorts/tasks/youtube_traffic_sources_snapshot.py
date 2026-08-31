#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import date

from dotenv import load_dotenv

load_dotenv()

from app import create_app


def _parse_args():
    parser = argparse.ArgumentParser(
        description="Capture YouTube traffic source snapshots."
    )
    parser.add_argument(
        "--start-date",
        dest="start_date",
        help="Backfill start date (YYYY-MM-DD). Defaults to the rolling 4-day window.",
    )
    return parser.parse_args()


def _parse_start_date(value: str | None) -> date | None:
    if not value:
        return None
    return date.fromisoformat(value)


def main() -> int:
    args = _parse_args()
    app = create_app()
    with app.app_context():
        from app.video_shorts.services.youtube_traffic_sources import ingest_traffic_sources

        print("youtube_traffic_sources_snapshot starting", flush=True)
        try:
            result = ingest_traffic_sources(start_date=_parse_start_date(args.start_date))
            print(f"youtube_traffic_sources_snapshot result={result}", flush=True)
            return 0
        except Exception as exc:
            print(f"youtube_traffic_sources_snapshot failed: {exc}", flush=True)
            raise


if __name__ == "__main__":
    raise SystemExit(main())
