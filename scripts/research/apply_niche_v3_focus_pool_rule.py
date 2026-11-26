#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv

load_dotenv(REPO_ROOT / ".env")

from app.video_shorts.services.db import get_db, get_db_readonly
from app.video_shorts.services.niche_definitions import load_niche_definition
from app.video_shorts.services.niches import (
    final_niche_pool,
    is_final_niche_pool_row,
    load_niche_snapshot,
)
from scripts.research import reclassify_women_hormones as legacy
from scripts.research.apply_niche_channel_classification_v3 import (
    CACHE_ROOT,
    _admin_precedents,
    _classify_channel,
)


NICHE_SLUG = "menopause_hormones"
BASELINE_RUN_ID = 10


def _run_pool_ids(conn, run_id: int) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT youtube_channel_id FROM niche_channel_run_results WHERE run_id=? AND in_pool",
            [run_id],
        ).fetchall()
    }


def main() -> int:
    conn = get_db_readonly()
    try:
        niche, rows = load_niche_snapshot(conn, NICHE_SLUG)
        if not niche:
            raise RuntimeError("menopause niche not found")
        definition = load_niche_definition(conn, niche_id=niche["id"])
        baseline_ids = _run_pool_ids(conn, BASELINE_RUN_ID)
    finally:
        conn.close()
    if not definition:
        raise RuntimeError("stored niche definition is required")

    candidates = [
        row for row in rows
        if not str(row.get("primary_focus") or "").strip()
        or str(row.get("classification_status") or "").casefold() != "classified"
    ]
    video_details = legacy.load_video_details(CACHE_ROOT)
    precedents = _admin_precedents(rows)
    results: dict[str, dict[str, Any] | None] = {}
    total_usage = {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}

    def classify(row):
        result, usage = _classify_channel(definition, precedents, row, video_details)
        return row, result, usage

    with ThreadPoolExecutor(max_workers=8) as executor:
        for index, (row, result, usage) in enumerate(executor.map(classify, candidates), start=1):
            channel_id = str(row.get("youtube_channel_id") or "")
            results[channel_id] = result
            if usage:
                for key in total_usage:
                    total_usage[key] += usage[key]
            print(
                f"{'classified' if result else 'no_titles'} {index}/{len(candidates)} {channel_id}",
                file=sys.stderr,
            )

    conn = get_db()
    try:
        for row in candidates:
            channel_id = str(row.get("youtube_channel_id") or "")
            result = results[channel_id]
            if result is None:
                conn.execute(
                    """
                    UPDATE niche_channels
                    SET classification_status='no_titles', updated_at=CURRENT_TIMESTAMP
                    WHERE niche_id=? AND snapshot_date=? AND youtube_channel_id=?
                    """,
                    [niche["id"], niche["snapshot_date"], channel_id],
                )
                continue
            conn.execute(
                """
                UPDATE niche_channels
                SET primary_focus=?, on_topic_share=?, on_topic_count=?,
                    classification_title_count=?, entity=?, classification_status='classified',
                    reclass_reason=?, updated_at=CURRENT_TIMESTAMP
                WHERE niche_id=? AND snapshot_date=? AND youtube_channel_id=?
                """,
                [
                    result["primary_focus"], result["core_share"], result["core_count"],
                    result["title_count"], result["entity"], result["reason"], niche["id"],
                    niche["snapshot_date"], channel_id,
                ],
            )

        result_set = conn.execute(
            "SELECT * FROM niche_channels WHERE niche_id=? AND snapshot_date=?",
            [niche["id"], niche["snapshot_date"]],
        )
        columns = [str(item[0]) for item in result_set.description or []]
        current_rows = [dict(zip(columns, row)) for row in result_set.fetchall()]
        run_id = conn.execute(
            """
            INSERT INTO niche_classification_runs (niche_id, run_at, rule_version, notes)
            VALUES (?, CURRENT_TIMESTAMP, ?, ?) RETURNING run_id
            """,
            [niche["id"], "v3-focus-provenance-required", "pool requires v3+ focus label"],
        ).fetchone()[0]
        for row in current_rows:
            conn.execute(
                """
                INSERT INTO niche_channel_run_results
                    (run_id, youtube_channel_id, in_pool, primary_niche, on_topic_share, reason)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    run_id, row["youtube_channel_id"], is_final_niche_pool_row(row, niche),
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

    pool = sorted(
        final_niche_pool(current_rows, clinicians_only=False, niche=niche),
        key=lambda row: (-int(row.get("subscriber_count") or 0), str(row.get("title") or "").casefold()),
    )
    current_ids = {str(row["youtube_channel_id"]) for row in pool}
    by_id = {str(row["youtube_channel_id"]): row for row in current_rows}
    shelley_id = "UCx-TX9EAE0NOL9hhG1wnvwg"
    shelley_index = next(
        (index for index, row in enumerate(pool) if str(row.get("youtube_channel_id")) == shelley_id),
        None,
    )
    neighbors = []
    if shelley_index is not None:
        for index in range(max(0, shelley_index - 2), min(len(pool), shelley_index + 3)):
            row = pool[index]
            neighbors.append({
                "rank": index + 1,
                "title": row.get("title"),
                "credential": row.get("credential"),
                "subscribers": row.get("subscriber_count"),
                "views_90d": row.get("views_90d"),
            })

    def titles(ids: set[str]) -> list[str]:
        return sorted(str(by_id[item].get("title") or item) for item in ids)

    print(json.dumps({
        "run_id": run_id,
        "baseline_run_id": BASELINE_RUN_ID,
        "pool_size": len(pool),
        "joined_vs_run_10": titles(current_ids - baseline_ids),
        "left_vs_run_10": titles(baseline_ids - current_ids),
        "classification_candidates": len(candidates),
        "classified_now": sum(result is not None for result in results.values()),
        "no_titles": sum(result is None for result in results.values()),
        "shelley_rank": shelley_index + 1 if shelley_index is not None else None,
        "shelley_neighbors": neighbors,
        "llm_usage": total_usage,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
