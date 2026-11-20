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
from app.video_shorts.worker import run_discovery_hourly_maintenance


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the hourly discovery maintenance checks.")
    parser.add_argument("--dry-run", action="store_true", help="Evaluate without deleting, reconciling, or emailing.")
    parser.add_argument("--no-alerts", action="store_true", help="Run maintenance without sending alert emails.")
    args = parser.parse_args()
    app = create_app()
    with app.app_context():
        result = run_discovery_hourly_maintenance(
            app,
            force=True,
            dry_run=args.dry_run,
            send_alerts=not args.no_alerts,
        )
    print(json.dumps(result, indent=2, default=str, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
