#!/usr/bin/env python3
from __future__ import annotations

import argparse
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

from app.video_shorts.config import _openai_client
from app.video_shorts.services.db import get_db, get_db_readonly
from app.video_shorts.services.niche_definitions import load_niche_definition
from app.video_shorts.services.niches import (
    final_niche_pool,
    is_final_niche_pool_row,
    load_niche_snapshot,
)
from scripts.research import reclassify_women_hormones as legacy


NICHE_SLUG = "menopause_hormones"
CACHE_ROOT = REPO_ROOT / "data" / "research" / "cache"
PRIMARY_FOCUSES = {
    "hormones_midlife_health",
    "fitness_training",
    "nutrition_diet",
    "general_health_wellness",
    "fertility_pregnancy",
    "other",
}


def _channel_description(channel_id: str) -> str:
    payload = legacy._cache_payload(CACHE_ROOT / "channels" / f"{channel_id}.json")
    item = payload.get("item") or {}
    return " ".join(str((item.get("snippet") or {}).get("description") or "").split())[:5000]


def _json_result(response) -> dict[str, Any]:
    value = json.loads(response.choices[0].message.content or "{}")
    if not isinstance(value, dict):
        raise RuntimeError("LLM response is not an object")
    return value


def _usage(response) -> dict[str, Any]:
    usage = response.usage
    input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": (input_tokens * 0.4 + output_tokens * 1.6) / 1_000_000,
    }


def _normalize_result(raw: dict[str, Any], titles: list[str]) -> dict[str, Any]:
    primary_focus = str(raw.get("primary_focus") or "").strip().casefold()
    if primary_focus not in PRIMARY_FOCUSES:
        primary_focus = "other"

    raw_entity = str(raw.get("entity") or "").strip().casefold()
    entity = "individual" if raw_entity == "individual" else "company_clinic"

    title_results = raw.get("title_results")
    flags: list[bool] = []
    if isinstance(title_results, list) and len(title_results) == len(titles):
        for item in title_results:
            if isinstance(item, dict):
                flags.append(item.get("core_topic") is True)
            else:
                flags.append(item is True)
    else:
        by_title = {}
        for item in title_results if isinstance(title_results, list) else []:
            if isinstance(item, dict):
                by_title[" ".join(str(item.get("title") or "").split()).casefold()] = (
                    item.get("core_topic") is True
                )
        flags = [by_title.get(" ".join(title.split()).casefold(), False) for title in titles]

    core_count = sum(flags)
    core_share = core_count / len(titles) if titles else 0.0
    reason = " ".join(str(raw.get("reason") or "").split())[:500]
    if not reason:
        reason = (
            f"Primary focus: {primary_focus}; {core_count}/{len(titles)} cached long-form "
            "titles are core-topic titles."
        )
    return {
        "primary_focus": primary_focus,
        "entity": entity,
        "reason": reason,
        "core_count": core_count,
        "core_share": core_share,
        "title_results": [
            {"title": title, "core_topic": flag}
            for title, flag in zip(titles, flags)
        ],
    }


def _classify_channel(core_topics: list[str], row: dict[str, Any], video_details):
    channel_id = str(row.get("youtube_channel_id") or "")
    titles = legacy.load_cached_longform_titles(CACHE_ROOT, channel_id, video_details)
    response = _openai_client.chat.completions.create(
        model="gpt-4.1-mini",
        temperature=0,
        max_tokens=1200,
        response_format={"type": "json_object"},
        messages=[
            {
                "role": "system",
                "content": (
                    "Classify the channel's MAIN focus, judging the channel description first. "
                    "Return JSON only with exactly: primary_focus, title_results, entity, reason. "
                    "primary_focus must be exactly one of hormones_midlife_health, fitness_training, "
                    "nutrition_diet, general_health_wellness, fertility_pregnancy, other. entity must "
                    "be individual or company_clinic. title_results must contain one object per input "
                    "title in the same order: {\"title\":\"exact input title\",\"core_topic\":true}. "
                    "A title is core_topic=true ONLY when it is mainly about perimenopause/menopause, "
                    "HRT, estrogen/progesterone/testosterone in women, menopause symptoms, hormone "
                    "labs, bladder/UTI/genitourinary symptoms, breast cancer and HRT safety, or HRT "
                    "news/access/supply. Adjacent topics are false. Workouts for women 50+ are "
                    "fitness_training; foods for menopause on a diet channel are nutrition_diet. "
                    "reason must be one line."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Channel description: {_channel_description(channel_id)}\n"
                    f"Last 15 long-form titles: {json.dumps(titles, ensure_ascii=False)}\n"
                    f"Stored core_topics: {json.dumps(core_topics, ensure_ascii=False)}"
                ),
            },
        ],
    )
    return _normalize_result(_json_result(response), titles), _usage(response)


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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    video_details = legacy.load_video_details(CACHE_ROOT)
    conn = get_db_readonly()
    try:
        niche, rows = load_niche_snapshot(conn, NICHE_SLUG)
        definition = load_niche_definition(conn, niche_id=niche["id"])
        previous_ids = _previous_pool_ids(conn, niche["id"])
    finally:
        conn.close()
    if not definition:
        raise RuntimeError("Stored niche definition is required")

    channel_results: dict[str, dict[str, Any]] = {}
    total_usage = {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}

    def classify_row(row):
        return row, *_classify_channel(definition["core_topics"], row, video_details)

    with ThreadPoolExecutor(max_workers=max(1, min(args.workers, 12))) as executor:
        classifications = executor.map(classify_row, rows)
        for index, (row, result, usage) in enumerate(classifications, start=1):
            channel_id = str(row.get("youtube_channel_id") or "")
            channel_results[channel_id] = result
            for key in total_usage:
                total_usage[key] += usage[key]
            print(f"classified {index}/{len(rows)} {channel_id}", file=sys.stderr)

    conn = get_db()
    try:
        for channel_id, result in channel_results.items():
            conn.execute(
                """
                UPDATE niche_channels
                SET primary_focus=?, on_topic_share=?, on_topic_count=?, entity=?,
                    reclass_reason=?, updated_at=CURRENT_TIMESTAMP
                WHERE niche_id=? AND youtube_channel_id=? AND snapshot_date=?
                """,
                [
                    result["primary_focus"], result["core_share"], result["core_count"],
                    result["entity"], result["reason"], niche["id"], channel_id,
                    niche["snapshot_date"],
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
            [niche["id"], "strict-channel-focus-v3", "v3 strict channel focus"],
        ).fetchone()[0]
        for row in current_rows:
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

    current_pool = sorted(
        final_niche_pool(current_rows, clinicians_only=False),
        key=lambda row: (
            -int(row.get("subscriber_count") or 0),
            str(row.get("title") or "").casefold(),
        ),
    )
    current_ids = {str(row["youtube_channel_id"]) for row in current_pool}
    by_id = {str(row["youtube_channel_id"]): row for row in current_rows}
    shelley_id = "UCx-TX9EAE0NOL9hhG1wnvwg"
    shelley_index = next(
        (index for index, row in enumerate(current_pool) if row["youtube_channel_id"] == shelley_id),
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

    def channel_report(channel_id: str) -> dict[str, Any]:
        row = by_id[channel_id]
        return {
            "title": row.get("title"),
            "primary_focus": row.get("primary_focus"),
            "core_share": float(row.get("on_topic_share") or 0),
            "entity": row.get("entity"),
            "in_pool": is_final_niche_pool_row(row),
            "admin_include": bool(row.get("admin_include")),
            "admin_exclude": bool(row.get("admin_exclude")),
            "reason": row.get("reclass_reason"),
        }

    status_rows = [
        row for row in current_rows
        if "frances pitsilis" in str(row.get("title") or "").casefold()
        or "dardur gynecology" in str(row.get("title") or "").casefold()
    ]
    print(
        json.dumps(
            {
                "pool_size": len(current_pool),
                "left": [channel_report(item) for item in sorted(previous_ids - current_ids)],
                "joined": [channel_report(item) for item in sorted(current_ids - previous_ids)],
                "requested_statuses": [
                    channel_report(str(row["youtube_channel_id"])) for row in status_rows
                ],
                "shelley_rank": shelley_index + 1 if shelley_index is not None else None,
                "shelley_neighbors": neighbors,
                "usage": total_usage,
                "run_id": run_id,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
