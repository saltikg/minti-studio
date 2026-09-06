#!/usr/bin/env python3
"""Create the production autopilot_leads table.

Run with --apply on production. The application intentionally does not create this
table at request time when using Postgres.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

# Allow direct execution from the repository like the production deploy command.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
load_dotenv(REPO_ROOT / ".env")

from app.video_shorts.services.db import get_db, table_columns


TABLE = "autopilot_leads"
REQUIRED_COLUMNS = {
    "id",
    "creator_email",
    "creator_name",
    "subscriber_count",
    "youtube_channel_id",
    "channel_id",
    "first_video_id",
    "user_id",
    "brand_id",
    "created_at",
    "converted_at",
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Apply the migration.")
    args = parser.parse_args()
    conn = get_db()
    try:
        if getattr(conn, "backend_name", "") != "postgres":
            raise RuntimeError("This migration must run against Postgres.")
        before = table_columns(conn, TABLE)
        print(f"table_exists_before={'yes' if before else 'no'}")
        if not args.apply:
            print("dry_run=yes")
            return 0
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TABLE} (
                id VARCHAR PRIMARY KEY,
                creator_email VARCHAR,
                creator_name VARCHAR,
                subscriber_count BIGINT,
                youtube_channel_id VARCHAR NOT NULL,
                channel_id BIGINT,
                first_video_id BIGINT,
                user_id VARCHAR,
                brand_id VARCHAR,
                created_at TIMESTAMP NOT NULL DEFAULT now(),
                converted_at TIMESTAMP
            )
            """
        )
        conn.execute(f"ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS subscriber_count BIGINT")
        conn.execute(
            f"""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_{TABLE}_youtube_channel
            ON {TABLE}(youtube_channel_id)
            """
        )
        conn.execute(
            f"""
            CREATE INDEX IF NOT EXISTS idx_{TABLE}_user_brand
            ON {TABLE}(user_id, brand_id)
            """
        )
        conn.commit()
        columns = table_columns(conn, TABLE)
        missing = sorted(REQUIRED_COLUMNS - columns)
        print(f"table_exists_after={'yes' if columns else 'no'}")
        print(f"columns={','.join(sorted(columns))}")
        if missing:
            raise RuntimeError(f"Missing required columns: {', '.join(missing)}")
        print("migration_ok=yes")
        return 0
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
