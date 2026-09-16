#!/usr/bin/env python3
"""Add lead pipeline state and event history for autopilot leads."""

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


LEADS_TABLE = "autopilot_leads"
EVENTS_TABLE = "lead_pipeline_events"
VALID_STATES = (
    "new",
    "downloading",
    "downloaded",
    "planning",
    "planned",
    "generating",
    "awaiting_approval",
    "approved",
    "scheduling",
    "scheduled",
    "sent",
    "failed",
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Apply the migration.")
    args = parser.parse_args()
    conn = get_db()
    try:
        if getattr(conn, "backend_name", "") != "postgres":
            raise RuntimeError("This migration must run against Postgres.")
        before_lead_cols = table_columns(conn, LEADS_TABLE)
        before_event_cols = table_columns(conn, EVENTS_TABLE)
        print(f"pipeline_state_exists_before={'yes' if 'pipeline_state' in before_lead_cols else 'no'}")
        print(f"events_table_exists_before={'yes' if before_event_cols else 'no'}")
        if not args.apply:
            print("dry_run=yes")
            return 0

        states_sql = ", ".join(f"'{state}'" for state in VALID_STATES)
        conn.execute(
            f"""
            ALTER TABLE {LEADS_TABLE}
            ADD COLUMN IF NOT EXISTS pipeline_state VARCHAR NOT NULL DEFAULT 'new'
            """
        )
        conn.execute(
            f"""
            ALTER TABLE {LEADS_TABLE}
            DROP CONSTRAINT IF EXISTS autopilot_leads_pipeline_state_check
            """
        )
        conn.execute(
            f"""
            ALTER TABLE {LEADS_TABLE}
            ADD CONSTRAINT autopilot_leads_pipeline_state_check
            CHECK (pipeline_state IN ({states_sql}))
            """
        )
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {EVENTS_TABLE} (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                lead_id VARCHAR NOT NULL,
                event_type VARCHAR NOT NULL,
                from_state VARCHAR,
                to_state VARCHAR,
                detail JSONB,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        conn.execute(
            f"""
            CREATE INDEX IF NOT EXISTS idx_{EVENTS_TABLE}_lead_created
            ON {EVENTS_TABLE}(lead_id, created_at DESC)
            """
        )
        conn.execute(
            f"""
            CREATE INDEX IF NOT EXISTS idx_{EVENTS_TABLE}_event_created
            ON {EVENTS_TABLE}(event_type, created_at DESC)
            """
        )
        share_cols = table_columns(conn, "short_share_links")
        schedule_cols = table_columns(conn, "outreach_scheduled_emails")
        video_cols = table_columns(conn, "youtube_videos")
        sent_exists_sql = (
            """
            EXISTS (
                SELECT 1
                FROM short_share_links sl
                WHERE CAST(sl.autopilot_lead_id AS VARCHAR) = CAST(l.id AS VARCHAR)
                  AND sl.emailed_at IS NOT NULL
            )
            """
            if {"autopilot_lead_id", "emailed_at"}.issubset(share_cols)
            else "FALSE"
        )
        scheduled_exists_sql = (
            """
            EXISTS (
                SELECT 1
                FROM short_share_links sl
                JOIN outreach_scheduled_emails ose ON ose.share_link_id = sl.id
                WHERE CAST(sl.autopilot_lead_id AS VARCHAR) = CAST(l.id AS VARCHAR)
                  AND ose.status IN ('scheduled', 'processing')
            )
            """
            if "autopilot_lead_id" in share_cols and {"share_link_id", "status"}.issubset(schedule_cols)
            else "FALSE"
        )
        downloaded_exists_sql = (
            """
            EXISTS (
                SELECT 1
                FROM youtube_videos v
                WHERE v.id = l.first_video_id
                  AND lower(coalesce(v.download_status, '')) IN ('downloaded', 'downloaded_deleted')
            )
            """
            if {"id", "download_status"}.issubset(video_cols)
            else "FALSE"
        )
        conn.execute(
            f"""
            UPDATE {LEADS_TABLE} l
               SET pipeline_state = CASE
                   WHEN {sent_exists_sql} THEN 'sent'
                   WHEN {scheduled_exists_sql} THEN 'scheduled'
                   WHEN {downloaded_exists_sql} THEN 'downloaded'
                   ELSE COALESCE(NULLIF(l.pipeline_state, ''), 'new')
               END
             WHERE l.pipeline_state IS NULL
                OR l.pipeline_state = ''
                OR l.pipeline_state = 'new'
            """
        )
        conn.commit()

        lead_cols = table_columns(conn, LEADS_TABLE)
        event_cols = table_columns(conn, EVENTS_TABLE)
        counts = conn.execute(
            f"""
            SELECT pipeline_state, COUNT(*)
            FROM {LEADS_TABLE}
            GROUP BY pipeline_state
            ORDER BY pipeline_state
            """
        ).fetchall()
        print(f"pipeline_state_exists_after={'yes' if 'pipeline_state' in lead_cols else 'no'}")
        print(f"events_table_exists_after={'yes' if event_cols else 'no'}")
        print("state_counts=" + ",".join(f"{row[0]}:{row[1]}" for row in counts))
        if "pipeline_state" not in lead_cols or not event_cols:
            raise RuntimeError("Lead pipeline migration verification failed.")
        print("migration_ok=yes")
        return 0
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
