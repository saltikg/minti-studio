#!/home/ubuntu/apps/minti_studio/.venv/bin/python
from __future__ import annotations

import json
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from app.video_shorts.services.autopilot_channel_watcher import run_watcher  # noqa: E402


def main() -> int:
    result = run_watcher()
    for customer in result["customers"]:
        print(json.dumps(customer, sort_keys=True))
    print(json.dumps({"new_videos": result["new_videos"], "email_sent": result["email_sent"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
