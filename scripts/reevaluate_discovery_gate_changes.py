#!/usr/bin/env python3
"""Re-run recent leads rejected only by the former subscriber/Shorts gates."""

from __future__ import annotations

import argparse
import json

from app import create_app
from app.video_shorts.routes.api import _enqueue_auto_trakk_if_needed, _evaluate_discovery_candidate, _persist_discovery_lead
from app.video_shorts.services.db import get_db_readonly


def _candidates(limit: int | None) -> list[dict]:
    conn = get_db_readonly()
    try:
        suffix = " LIMIT ?" if limit else ""
        params = ["Hard numeric gate failed:%"]
        if limit:
            params.append(limit)
        rows = conn.execute(
            f"""
            SELECT youtube_channel_id, channel_url, channel_title, channel_description,
                   subscriber_count, longform_last_60d, shorts_last_15d, matched_keyword,
                   raw_json
            FROM discovery_leads
            WHERE last_discovered_at >= CURRENT_TIMESTAMP - INTERVAL '30 days'
              AND status = 'disqualified'
              AND COALESCE(icp_reason, '') LIKE ?
              AND COALESCE(subscriber_count, 0) BETWEEN 1000 AND 150000
              AND COALESCE(longform_last_60d, 0) >= 2
              AND (COALESCE(shorts_last_15d, 0) > 8 OR COALESCE(subscriber_count, 0) < 2000)
            ORDER BY last_discovered_at ASC, id ASC{suffix}
            """,
            params,
        ).fetchall()
    finally:
        conn.close()
    candidates = []
    for values in rows:
        raw = values[8] if isinstance(values[8], dict) else json.loads(str(values[8] or "{}"))
        raw.update(
            {
                "channel_id": str(values[0] or ""),
                "channel_url": str(values[1] or ""),
                "channel_title": values[2],
                "channel_description": values[3],
                "subscriber_count": values[4],
                "longform_last_60d": values[5],
                "shorts_last_15d": values[6],
                "matched_keyword": values[7],
            }
        )
        candidates.append(raw)
    return candidates


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    app = create_app()
    counts = {"candidates": 0, "reentered": 0, "disqualified": 0, "failed": 0}
    with app.app_context():
        candidates = _candidates(args.limit)
        counts["candidates"] = len(candidates)
        reentered_rows = []
        for index, row in enumerate(candidates, start=1):
            try:
                _evaluate_discovery_candidate(
                    row,
                    niche=str(row.get("matched_keyword") or ""),
                    ai_icp=True,
                )
                row.pop("_email_candidate_descriptions", None)
                outcome = _persist_discovery_lead(row)
                if outcome == "failed":
                    counts["failed"] += 1
                elif row.get("icp_fit") is True:
                    counts["reentered"] += 1
                    reentered_rows.append(row)
                else:
                    counts["disqualified"] += 1
            except Exception:
                app.logger.exception("Gate-change re-evaluation failed channel=%s", row.get("channel_id"))
                counts["failed"] += 1
            if index % 25 == 0:
                print(json.dumps({**counts, "processed": index}), flush=True)
        counts["email_queue"] = _enqueue_auto_trakk_if_needed(reentered_rows)
    print(json.dumps(counts, sort_keys=True))
    return 0 if counts["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
