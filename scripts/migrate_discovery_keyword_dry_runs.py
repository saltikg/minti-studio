#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
load_dotenv(REPO_ROOT / ".env")

from app.video_shorts.services.db import get_db, table_columns


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    conn = get_db()
    try:
        exists = bool(table_columns(conn, "discovery_keyword_dry_runs"))
        if not args.apply:
            print(f"table_exists={int(exists)} dry_run=1")
            return 0
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS discovery_keyword_dry_runs (
                id BIGSERIAL PRIMARY KEY,
                keyword VARCHAR NOT NULL,
                category VARCHAR NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_discovery_keyword_dry_runs_created
            ON discovery_keyword_dry_runs(created_at)
            """
        )
        conn.commit()
        print("table_ready=1 applied=1")
        return 0
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
