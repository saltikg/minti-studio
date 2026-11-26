#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv

load_dotenv(REPO_ROOT / ".env")

from app.video_shorts.services.db import get_db, get_db_readonly
from app.video_shorts.services.niches import (
    final_niche_pool,
    is_final_niche_pool_row,
    load_niche_snapshot,
)


NICHE_SLUG = "menopause_hormones"
SHELLEY_ID = "UCx-TX9EAE0NOL9hhG1wnvwg"


def _previous_pool_ids(conn, niche_id: Any) -> set[str]:
    run = conn.execute(
        """
        SELECT run_id FROM niche_classification_runs
        WHERE niche_id=? ORDER BY run_at DESC, run_id DESC LIMIT 1
        """,
        [niche_id],
    ).fetchone()
    if not run:
        return set()
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT youtube_channel_id FROM niche_channel_run_results WHERE run_id=? AND in_pool",
            [run[0]],
        ).fetchall()
    }


def _channel_report(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": row.get("title"),
        "core_count": int(row.get("on_topic_count") or 0),
        "core_share": float(row.get("on_topic_share") or 0),
        "primary_focus": row.get("primary_focus"),
    }


def main() -> int:
    read_conn = get_db_readonly()
    try:
        niche, rows = load_niche_snapshot(read_conn, NICHE_SLUG)
        previous_ids = _previous_pool_ids(read_conn, niche["id"])
    finally:
        read_conn.close()

    current_pool = sorted(
        final_niche_pool(rows, clinicians_only=False),
        key=lambda row: (
            -int(row.get("subscriber_count") or 0),
            str(row.get("title") or "").casefold(),
        ),
    )
    current_ids = {str(row["youtube_channel_id"]) for row in current_pool}
    by_id = {str(row["youtube_channel_id"]): row for row in rows}
    joined = [_channel_report(by_id[item]) for item in sorted(current_ids - previous_ids)]

    conn = get_db()
    try:
        run_id = conn.execute(
            """
            INSERT INTO niche_classification_runs (niche_id, run_at, rule_version, notes)
            VALUES (?, CURRENT_TIMESTAMP, ?, ?) RETURNING run_id
            """,
            [niche["id"], "primary-focus-only-v3", "v3 primary focus only"],
        ).fetchone()[0]
        for row in rows:
            conn.execute(
                """
                INSERT INTO niche_channel_run_results
                    (run_id, youtube_channel_id, in_pool, primary_niche, on_topic_share, reason)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    run_id, row["youtube_channel_id"], is_final_niche_pool_row(row),
                    row.get("primary_focus"), float(row.get("on_topic_share") or 0),
                    row.get("reclass_reason"),
                ],
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    shelley_index = next(
        (index for index, row in enumerate(current_pool) if row["youtube_channel_id"] == SHELLEY_ID),
        None,
    )
    neighbors = []
    if shelley_index is not None:
        for index in range(max(0, shelley_index - 2), min(len(current_pool), shelley_index + 3)):
            row = current_pool[index]
            neighbors.append(
                {
                    "rank": index + 1,
                    "title": row.get("title"),
                    "credential": row.get("credential"),
                    "subscribers": row.get("subscriber_count"),
                    "views_90d": row.get("views_90d"),
                }
            )

    print(
        json.dumps(
            {
                "pool_size": len(current_pool),
                "joined": joined,
                "joined_below_0_15": [
                    row for row in joined if float(row["core_share"]) < 0.15
                ],
                "left": [
                    _channel_report(by_id[item])
                    for item in sorted(previous_ids - current_ids)
                ],
                "shelley_rank": shelley_index + 1 if shelley_index is not None else None,
                "shelley_neighbors": neighbors,
                "run_id": run_id,
                "llm_calls": 0,
                "youtube_api_calls": 0,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
