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


def _previous_pool_ids(conn, niche_id: Any) -> set[str]:
    run = conn.execute(
        """
        SELECT run_id FROM niche_classification_runs
        WHERE niche_id = ? ORDER BY run_at DESC, run_id DESC LIMIT 1
        """,
        [niche_id],
    ).fetchone()
    if not run:
        return set()
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT youtube_channel_id FROM niche_channel_run_results WHERE run_id = ? AND in_pool",
            [run[0]],
        ).fetchall()
    }


def main() -> int:
    conn = get_db_readonly()
    try:
        niche, rows = load_niche_snapshot(conn, NICHE_SLUG)
        if not niche:
            raise RuntimeError("menopause niche not found")
        previous_ids = _previous_pool_ids(conn, niche["id"])
    finally:
        conn.close()

    missing_language_rows = [row for row in rows if not str(row.get("language") or "").strip()]
    for row in missing_language_rows:
        row["in_niche"] = str(row.get("youtube_channel_id") or "") in previous_ids
    current_pool = final_niche_pool(rows, clinicians_only=False, niche=niche)
    current_ids = {str(row["youtube_channel_id"]) for row in current_pool}
    by_id = {str(row["youtube_channel_id"]): row for row in rows}

    conn = get_db()
    try:
        for row in missing_language_rows:
            conn.execute(
                """
                UPDATE niche_channels SET in_niche = ?, updated_at = CURRENT_TIMESTAMP
                WHERE niche_id = ? AND snapshot_date = ? AND youtube_channel_id = ?
                """,
                [
                    bool(row.get("in_niche")), niche["id"], niche["snapshot_date"],
                    row["youtube_channel_id"],
                ],
            )
        run_id = conn.execute(
            """
            INSERT INTO niche_classification_runs (niche_id, run_at, rule_version, notes)
            VALUES (?, CURRENT_TIMESTAMP, ?, ?) RETURNING run_id
            """,
            [niche["id"], "generic-language-v1", "language rule replaces country"],
        ).fetchone()[0]
        for row in rows:
            conn.execute(
                """
                INSERT INTO niche_channel_run_results
                    (run_id, youtube_channel_id, in_pool, primary_niche, on_topic_share, reason)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    run_id,
                    row["youtube_channel_id"],
                    is_final_niche_pool_row(row, niche),
                    row.get("primary_focus"),
                    float(row.get("on_topic_share") or 0),
                    row.get("reclass_reason"),
                ],
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    def titles(ids: set[str]) -> list[str]:
        return sorted(str(by_id[item].get("title") or item) for item in ids)

    print(json.dumps({
        "run_id": run_id,
        "pool_before": len(previous_ids),
        "pool_after": len(current_ids),
        "joined": titles(current_ids - previous_ids),
        "left": titles(previous_ids - current_ids),
        "without_language_preserved": sorted(
            str(row.get("title") or row.get("youtube_channel_id"))
            for row in missing_language_rows
        ),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
