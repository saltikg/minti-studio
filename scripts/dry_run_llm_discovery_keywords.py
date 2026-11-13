#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
load_dotenv(REPO_ROOT / ".env")

from app import create_app
from app.video_shorts.routes.api import generate_seed_keywords_into_queue


def main() -> int:
    app = create_app()
    with app.app_context():
        result = generate_seed_keywords_into_queue(persist=False, record_dry_run=True)
    items = list(result.get("keyword_items") or [])[:5]
    if len(items) != 5:
        raise RuntimeError(f"Expected 5 dry-run keywords, got {len(items)}")
    print(json.dumps({"dry_run": True, "keywords": items}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
