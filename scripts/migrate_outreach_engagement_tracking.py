#!/usr/bin/env python3
"""Add and backfill outreach click/engagement tracking on production Postgres."""

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


REQUIRED_COLUMNS = {
    "clicked_at",
    "clicked_user_agent",
    "clicked_ip_hash",
    "engaged_at",
}
ENGAGEMENT_EVENTS = (
    "share_play",
    "share_watch_progress",
    "share_cta_click",
    "share_yes_intent",
    "lead_feed_watch_start",
    "lead_feed_trial_cta_click",
    "free_tool_gate_click",
    "free_tool_submit",
    "channel_connected",
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Apply schema changes and backfill data.")
    args = parser.parse_args()
    conn = get_db()
    try:
        if getattr(conn, "backend_name", "") != "postgres":
            raise RuntimeError("This migration must run against Postgres.")
        before = table_columns(conn, "onboarding_magic_links")
        print(f"columns_before={','.join(sorted(before))}")
        if not args.apply:
            print("dry_run=yes")
            return 0

        conn.execute("ALTER TABLE onboarding_magic_links ADD COLUMN IF NOT EXISTS clicked_at TIMESTAMP")
        conn.execute("ALTER TABLE onboarding_magic_links ADD COLUMN IF NOT EXISTS clicked_user_agent TEXT")
        conn.execute("ALTER TABLE onboarding_magic_links ADD COLUMN IF NOT EXISTS clicked_ip_hash VARCHAR")
        conn.execute("ALTER TABLE onboarding_magic_links ADD COLUMN IF NOT EXISTS engaged_at TIMESTAMP")
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_onboarding_magic_links_clicked_engaged
            ON onboarding_magic_links(clicked_at, engaged_at)
            """
        )
        clicked_result = conn.execute(
            """
            UPDATE onboarding_magic_links
            SET clicked_at = used_at
            WHERE clicked_at IS NULL
              AND used_at IS NOT NULL
            """
        )
        placeholders = ", ".join("?" for _ in ENGAGEMENT_EVENTS)
        engaged_result = conn.execute(
            f"""
            WITH first_engagement AS (
                SELECT
                  oml.id AS magic_link_id,
                  MIN(ue.created_at) AS engaged_at
                FROM onboarding_magic_links oml
                JOIN short_share_links sl
                  ON (
                    (oml.share_link_id IS NOT NULL AND CAST(oml.share_link_id AS VARCHAR) = CAST(sl.id AS VARCHAR))
                    OR (
                      COALESCE(oml.share_link_token, '') <> ''
                      AND oml.share_link_token = sl.token
                    )
                  )
                JOIN user_events ue
                  ON ue.event_name IN ({placeholders})
                 AND ue.created_at >= oml.clicked_at
                 AND (
                   CAST(ue.user_id AS VARCHAR) = CAST(oml.user_id AS VARCHAR)
                   OR NULLIF(ue.metadata->>'share_link_id', '') = CAST(sl.id AS VARCHAR)
                   OR NULLIF(ue.metadata->>'token', '') = sl.token
                   OR (
                     NULLIF(CAST(sl.autopilot_lead_id AS VARCHAR), '') IS NOT NULL
                     AND COALESCE(
                       NULLIF(ue.metadata->>'autopilot_lead_id', ''),
                       NULLIF(ue.metadata->>'lead_id', '')
                     ) = CAST(sl.autopilot_lead_id AS VARCHAR)
                   )
                 )
                WHERE oml.clicked_at IS NOT NULL
                  AND oml.engaged_at IS NULL
                GROUP BY oml.id
            )
            UPDATE onboarding_magic_links oml
            SET engaged_at = first_engagement.engaged_at
            FROM first_engagement
            WHERE oml.id = first_engagement.magic_link_id
              AND oml.engaged_at IS NULL
            """,
            list(ENGAGEMENT_EVENTS),
        )
        conn.commit()

        columns = table_columns(conn, "onboarding_magic_links")
        missing = sorted(REQUIRED_COLUMNS - columns)
        if missing:
            raise RuntimeError(f"Missing required columns: {', '.join(missing)}")
        counts = conn.execute(
            """
            SELECT
              COUNT(*) FILTER (WHERE clicked_at IS NOT NULL),
              COUNT(*) FILTER (WHERE engaged_at IS NOT NULL)
            FROM onboarding_magic_links
            """
        ).fetchone()
        print(f"clicked_backfilled={max(0, int(getattr(clicked_result, 'rowcount', 0) or 0))}")
        print(f"engaged_backfilled={max(0, int(getattr(engaged_result, 'rowcount', 0) or 0))}")
        print(f"clicked_total={int(counts[0] or 0)} engaged_total={int(counts[1] or 0)}")
        print("migration_ok=yes")
        return 0
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
