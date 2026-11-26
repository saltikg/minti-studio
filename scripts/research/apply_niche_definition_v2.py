#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv

load_dotenv(REPO_ROOT / ".env")

from app.video_shorts.config import _openai_client
from app.video_shorts.services.db import get_db, get_db_readonly
from app.video_shorts.services.niche_definitions import definition_prompt
from app.video_shorts.services.niche_outliers import (
    classify_and_persist_latest_topics,
    generate_niche_outlier_pattern,
    persist_niche_outlier_pattern,
)
from app.video_shorts.services.niches import (
    final_niche_pool,
    is_final_niche_pool_row,
    load_niche_snapshot,
)
from scripts.research import reclassify_women_hormones as legacy


NICHE_SLUG = "menopause_hormones"
NICHE_NAME = "Women's Hormones & Midlife Health"
CACHE_ROOT = REPO_ROOT / "data" / "research" / "cache"
DRAFT_PATH = REPO_ROOT / "data" / "research" / "niche_definition_draft_2026-11-26.json"


def _channel_description(channel_id: str) -> str:
    payload = legacy._cache_payload(CACHE_ROOT / "channels" / f"{channel_id}.json")
    item = payload.get("item") or {}
    return " ".join(str((item.get("snippet") or {}).get("description") or "").split())[:5000]


def _json_result(response) -> dict[str, Any]:
    value = json.loads(response.choices[0].message.content or "{}")
    if not isinstance(value, dict):
        raise RuntimeError("LLM response is not an object")
    return value


def _usage(response, input_rate: float, output_rate: float) -> dict[str, Any]:
    usage = response.usage
    input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": (input_tokens * input_rate + output_tokens * output_rate) / 1_000_000,
    }


def _definition_call(pool: list[dict[str, Any]], video_details) -> tuple[dict[str, Any], dict[str, Any]]:
    draft = json.loads(DRAFT_PATH.read_text(encoding="utf-8"))
    evidence = []
    for row in pool:
        channel_id = str(row.get("youtube_channel_id") or "")
        evidence.append(
            {
                "channel": row.get("title"),
                "last_15_long_form_titles": legacy.load_cached_longform_titles(
                    CACHE_ROOT, channel_id, video_details
                ),
            }
        )
    response = _openai_client.chat.completions.create(
        model="gpt-4.1",
        temperature=0.1,
        max_tokens=1400,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": "You are a careful research taxonomist. Return JSON only."},
            {
                "role": "user",
                "content": (
                    "Rewrite the supplied draft using all channel-title evidence. Return exactly: "
                    "description, core_topics, adjacent_topics, excluded_topics. Description must "
                    "be 150–250 words. The category name is Women's Hormones & Midlife Health. "
                    "Creators are individuals teaching on camera: health professionals, coaches, "
                    "and educators—not doctors only. Include bladder/UTI and genitourinary symptoms; "
                    "heart health in women 40+; breast cancer risk and HRT safety; HRT news, access, "
                    "shortages, generic approvals and discontinued brands; genetics and functional "
                    "medicine; and ADHD and mood in midlife as adjacent. Exclude pregnancy/fertility "
                    "channels, general fitness, all-ages weight-loss programs, male health, child/family "
                    "health, and companies/clinics. Do not exclude non-health-professional creators as "
                    "a class.\nDraft:\n" + json.dumps(draft, ensure_ascii=False)
                    + "\nAll current pool evidence:\n" + json.dumps(evidence, ensure_ascii=False)
                ),
            },
        ],
    )
    result = _json_result(response)
    description = " ".join(str(result.get("description") or "").split())
    if not 150 <= len(description.split()) <= 250:
        raise RuntimeError("definition description must contain 150–250 words")
    result["description"] = description
    for key in ("core_topics", "adjacent_topics", "excluded_topics"):
        if not isinstance(result.get(key), list) or not result[key]:
            raise RuntimeError(f"definition {key} is invalid")
    return result, _usage(response, 2.0, 8.0)


def _classify_channel(definition: dict[str, Any], row: dict[str, Any], video_details):
    channel_id = str(row.get("youtube_channel_id") or "")
    titles = legacy.load_cached_longform_titles(CACHE_ROOT, channel_id, video_details)
    response = _openai_client.chat.completions.create(
        model="gpt-4.1-mini",
        temperature=0,
        max_tokens=100,
        response_format={"type": "json_object"},
        messages=[
            {
                "role": "system",
                "content": (
                    "Score channel fit against the supplied definition. Return JSON only: "
                    "{\"relevance\":0,\"entity\":\"individual|company_clinic\","
                    "\"reason\":\"one line\"}. Relevance is 0–100."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Definition: {definition_prompt(definition)}\n"
                    f"Channel description: {_channel_description(channel_id)}\n"
                    f"Last 15 long-form titles: {json.dumps(titles, ensure_ascii=False)}"
                ),
            },
        ],
    )
    result = _json_result(response)
    relevance = int(result.get("relevance"))
    entity = str(result.get("entity") or "")
    reason = " ".join(str(result.get("reason") or "").split())[:500]
    if not 0 <= relevance <= 100 or entity not in {"individual", "company_clinic"} or not reason:
        raise RuntimeError(f"invalid channel classification for {channel_id}")
    return {"relevance": relevance, "entity": entity, "reason": reason}, _usage(response, 0.4, 1.6)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.parse_args()
    video_details = legacy.load_video_details(CACHE_ROOT)
    conn = get_db_readonly()
    try:
        niche, rows = load_niche_snapshot(conn, NICHE_SLUG)
    finally:
        conn.close()
    previous_pool = final_niche_pool(rows, clinicians_only=False)
    previous_ids = {str(row["youtube_channel_id"]) for row in previous_pool}
    definition_fields, definition_usage = _definition_call(previous_pool, video_details)
    definition = {
        "id": niche["id"], "slug": NICHE_SLUG, "name": NICHE_NAME,
        "description": definition_fields["description"],
        "core_topics": definition_fields["core_topics"],
        "adjacent_topics": definition_fields["adjacent_topics"],
        "excluded_topics": definition_fields["excluded_topics"],
        "definition_version": 2,
    }
    channel_results = {}
    channel_usage = {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
    for index, row in enumerate(rows, start=1):
        channel_id = str(row.get("youtube_channel_id") or "")
        result, usage = _classify_channel(definition, row, video_details)
        channel_results[channel_id] = result
        for key in channel_usage:
            channel_usage[key] += usage[key]
        print(f"classified {index}/{len(rows)} {channel_id}", file=sys.stderr)

    conn = get_db()
    try:
        conn.execute(
            """
            UPDATE niches SET name=?, description=?, core_topics=?, adjacent_topics=?,
                excluded_topics=?, definition_version=2 WHERE id=?
            """,
            [
                NICHE_NAME, definition["description"], json.dumps(definition["core_topics"]),
                json.dumps(definition["adjacent_topics"]), json.dumps(definition["excluded_topics"]),
                niche["id"],
            ],
        )
        for channel_id, result in channel_results.items():
            conn.execute(
                """
                UPDATE niche_channels SET relevance=?, entity=?, relevance_reason=?,
                    reclass_reason=?, updated_at=CURRENT_TIMESTAMP
                WHERE niche_id=? AND youtube_channel_id=? AND snapshot_date=?
                """,
                [
                    result["relevance"], result["entity"], result["reason"], result["reason"],
                    niche["id"], channel_id, niche["snapshot_date"],
                ],
            )
        updated_rows = conn.execute(
            "SELECT * FROM niche_channels WHERE niche_id=? AND snapshot_date=?",
            [niche["id"], niche["snapshot_date"]],
        )
        columns = [str(item[0]) for item in updated_rows.description or []]
        current_rows = [dict(zip(columns, row)) for row in updated_rows.fetchall()]
        run_id = conn.execute(
            """
            INSERT INTO niche_classification_runs (niche_id, run_at, rule_version, notes)
            VALUES (?, CURRENT_TIMESTAMP, ?, ?) RETURNING run_id
            """,
            [niche["id"], "written-definition-v2", "definition v2"],
        ).fetchone()[0]
        for row in current_rows:
            conn.execute(
                """
                INSERT INTO niche_channel_run_results
                    (run_id, youtube_channel_id, in_pool, primary_niche, on_topic_share, reason)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    run_id, row["youtube_channel_id"],
                    is_final_niche_pool_row(row),
                    NICHE_SLUG, float(row.get("relevance") or 0) / 100, row.get("relevance_reason"),
                ],
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    current_pool = final_niche_pool(current_rows, clinicians_only=False)
    current_ids = {str(row["youtube_channel_id"]) for row in current_pool}
    classified = classify_and_persist_latest_topics(NICHE_SLUG)
    if not classified:
        raise RuntimeError("No current outlier rows")
    outlier_result, short_usage = classified
    pattern = generate_niche_outlier_pattern(outlier_result)
    if pattern:
        persist_niche_outlier_pattern(pattern)
    title_by_id = {str(row["youtube_channel_id"]): str(row.get("title") or "") for row in current_rows}
    distribution = {
        "0_44": sum(int(row.get("relevance") or 0) <= 44 for row in current_rows),
        "45_59": sum(45 <= int(row.get("relevance") or 0) <= 59 for row in current_rows),
        "60_79": sum(60 <= int(row.get("relevance") or 0) <= 79 for row in current_rows),
        "80_100": sum(int(row.get("relevance") or 0) >= 80 for row in current_rows),
    }
    total_cost = (
        definition_usage["cost_usd"] + channel_usage["cost_usd"]
        + short_usage["estimated_cost_usd"]
        + float((pattern or {}).get("estimated_cost_usd") or 0)
    )
    print(json.dumps({
        "definition": definition,
        "definition_usage": definition_usage,
        "channel_usage": channel_usage,
        "short_usage": short_usage,
        "pattern": pattern,
        "distribution": distribution,
        "pool_size": len(current_pool),
        "joined": [title_by_id[item] for item in sorted(current_ids - previous_ids)],
        "left": [title_by_id[item] for item in sorted(previous_ids - current_ids)],
        "total_cost_usd": total_cost,
        "run_id": run_id,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
