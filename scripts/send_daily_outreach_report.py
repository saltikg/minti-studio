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
from app.video_shorts.services.outreach_reporting import (
    collect_outreach_report,
    next_report_run_pt,
    render_outreach_report,
    send_outreach_report,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Render or test-send the daily outreach report.")
    parser.add_argument("--send-test", action="store_true", help="Send one test report to configured admin recipients.")
    args = parser.parse_args()
    app = create_app()
    with app.app_context():
        report = collect_outreach_report()
        rendered = render_outreach_report(report)
        result = {
            "subject": rendered["subject"],
            "dates": report["dates"],
            "table": {row["label"]: [*row["values"], row["total"]] for row in report["rows"]},
            "status": report["status"],
            "clicked_today": report["clicked_today"],
            "warnings": report["warnings"],
            "next_run_pt": next_report_run_pt().isoformat(),
        }
        if args.send_test:
            result["test_send"] = send_outreach_report(report, test=True)
    print(json.dumps(result, indent=2, default=str, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
