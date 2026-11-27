#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv

load_dotenv(REPO_ROOT / ".env")

from app.video_shorts.services.db import get_db_readonly
from app.video_shorts.services.niche_rising_topics import compute_and_persist_rising_topics


def main() -> int:
    conn = get_db_readonly()
    try:
        slugs = [
            str(row[0]) for row in conn.execute(
                "SELECT slug FROM niches WHERE status = 'active' ORDER BY id"
            ).fetchall()
        ]
    finally:
        conn.close()
    results = [compute_and_persist_rising_topics(slug) for slug in slugs]
    print(json.dumps(results, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
