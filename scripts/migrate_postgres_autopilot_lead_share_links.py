#!/usr/bin/env python3
"""Add the explicit autopilot lead association to recipient share links."""

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


TABLE = "short_share_links"
REQUIRED_COLUMN = "autopilot_lead_id"
INDEX = "idx_short_share_links_autopilot_lead_generated"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Apply the migration.")
    args = parser.parse_args()
    conn = get_db()
    try:
        if getattr(conn, "backend_name", "") != "postgres":
            raise RuntimeError("This migration must run against Postgres.")
        before = table_columns(conn, TABLE)
        print(f"column_exists_before={'yes' if REQUIRED_COLUMN in before else 'no'}")
        if not args.apply:
            print("dry_run=yes")
            return 0
        conn.execute(f"ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS {REQUIRED_COLUMN} VARCHAR")
        conn.execute(
            f"""
            CREATE INDEX IF NOT EXISTS {INDEX}
            ON {TABLE}({REQUIRED_COLUMN}, generated_video_id)
            """
        )
        conn.commit()
        columns = table_columns(conn, TABLE)
        index_exists = conn.execute(
            "SELECT 1 FROM pg_indexes WHERE schemaname = 'main' AND indexname = ? LIMIT 1",
            [INDEX],
        ).fetchone()
        print(f"column_exists_after={'yes' if REQUIRED_COLUMN in columns else 'no'}")
        print(f"index_exists={'yes' if index_exists else 'no'}")
        if REQUIRED_COLUMN not in columns or not index_exists:
            raise RuntimeError("Autopilot lead share-link migration verification failed.")
        print("migration_ok=yes")
        return 0
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
