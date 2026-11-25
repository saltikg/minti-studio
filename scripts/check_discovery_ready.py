#!/usr/bin/env python3
"""Print the ready-to-promote count and first page IDs for deploy verification."""

from __future__ import annotations

import argparse
import json
import time

from app.video_shorts.services.db import get_db_readonly
from app.video_shorts.services.discovery_blocklist import (
    discovery_blocklist_sql,
    discovery_lead_blocked_sql,
)


def _legacy_blocked_sql() -> str:
    category = """
        COALESCE((
            SELECT dc.name
            FROM keyword_queue kq
            JOIN discovery_categories dc ON dc.id = kq.category_id
            WHERE lower(kq.keyword) = lower(COALESCE(discovery_leads.matched_keyword, ''))
            ORDER BY kq.updated_at DESC NULLS LAST, kq.id DESC
            LIMIT 1
        ), '')
    """
    return discovery_blocklist_sql(["discovery_leads.matched_keyword", category])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--legacy", action="store_true")
    parser.add_argument("--limit", type=int, default=15)
    args = parser.parse_args()
    blocked = _legacy_blocked_sql() if args.legacy else discovery_lead_blocked_sql("discovery_leads")
    predicate = f"""
        WHERE icp_fit IS TRUE
          AND creator_email IS NOT NULL AND creator_email <> ''
          AND (autopilot_lead_id IS NULL OR CAST(autopilot_lead_id AS VARCHAR) = '')
          AND (status IS NULL OR status NOT IN ('already_lead', 'dismissed'))
          AND NOT ({blocked})
          AND NOT EXISTS (
              SELECT 1 FROM autopilot_leads al
              WHERE al.youtube_channel_id = discovery_leads.youtube_channel_id
          )
    """
    conn = get_db_readonly()
    started = time.perf_counter()
    try:
        count = int(conn.execute(f"SELECT COUNT(*) FROM discovery_leads {predicate}").fetchone()[0] or 0)
        ids = [
            row[0]
            for row in conn.execute(
                f"""
                SELECT id FROM discovery_leads {predicate}
                ORDER BY first_seen_at DESC NULLS LAST,
                         last_discovered_at DESC NULLS LAST,
                         last_seen_at DESC NULLS LAST, id DESC
                LIMIT ?
                """,
                [max(1, args.limit)],
            ).fetchall()
        ]
    finally:
        conn.close()
    print(json.dumps({"count": count, "ids": ids, "seconds": round(time.perf_counter() - started, 3)}))


if __name__ == "__main__":
    main()
