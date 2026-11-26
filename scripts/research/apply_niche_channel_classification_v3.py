#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests


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
    near_miss_niche_rows,
)
from app.video_shorts.services.youtube_quota import (
    record_youtube_data_api_call,
    research_youtube_api_key,
    require_youtube_quota_available,
    unit_cost,
)
from scripts.research import reclassify_women_hormones as legacy


NICHE_SLUG = "menopause_hormones"
CACHE_ROOT = REPO_ROOT / "data" / "research" / "cache"
FEATURE = "women_hormones_research_project"
YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"
TEACH_RULE = (
    "The channel must TEACH or EXPLAIN hormones/menopause/midlife health (education, "
    "talking to camera). Channels that mainly OFFER workouts, yoga or exercise classes, "
    "recipes/meal plans, or general lifestyle/mindset content for women in midlife are "
    "fitness_training, nutrition_diet or general_health_wellness — even if every title "
    "mentions menopause."
)


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


def _normalize_result(
    raw: dict[str, Any],
    title_inputs: list[dict[str, str]],
    focus_keys: set[str] | None = None,
) -> dict[str, Any]:
    titles = [item["title"] for item in title_inputs]
    primary_focus = str(raw.get("primary_focus") or "").strip().casefold()
    allowed_focuses = focus_keys or {"other"}
    if primary_focus not in allowed_focuses:
        primary_focus = "other" if "other" in allowed_focuses else sorted(allowed_focuses)[0]

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
        "title_count": len(titles),
        "title_results": [
            {"title": title, "core_topic": flag}
            for title, flag in zip(titles, flags)
        ],
    }


def _cached_uploads(channel_id: str) -> list[dict[str, Any]]:
    uploads = []
    for path in sorted((CACHE_ROOT / "uploads" / channel_id).glob("page_*.json")):
        for item in legacy._cache_payload(path).get("items") or []:
            snippet = item.get("snippet") or {}
            content = item.get("contentDetails") or {}
            video_id = str(
                content.get("videoId")
                or (snippet.get("resourceId") or {}).get("videoId")
                or ""
            ).strip()
            title = " ".join(str(snippet.get("title") or "").split())
            published_at = str(content.get("videoPublishedAt") or snippet.get("publishedAt") or "")
            if video_id and title:
                uploads.append({"video_id": video_id, "title": title, "published_at": published_at})
    uploads.sort(key=lambda item: item["published_at"], reverse=True)
    return uploads


def load_mixed_titles(channel_id: str, video_details) -> list[dict[str, str]]:
    uploads = _cached_uploads(channel_id)
    long_titles = []
    short_titles = []
    for upload in uploads:
        duration = (video_details.get(upload["video_id"]) or {}).get("duration_seconds")
        if duration is None:
            continue
        item = {
            "title": upload["title"],
            "format": "long" if int(duration) > 180 else "short",
        }
        (long_titles if item["format"] == "long" else short_titles).append(item)
    selected = long_titles[:15]
    if len(selected) < 15:
        selected.extend(short_titles[: 15 - len(selected)])
    return selected


class RunQuota:
    def __init__(self, cap: int):
        self.cap = int(cap)
        self.units = 0
        self.calls: dict[str, int] = {}

    def reserve(self, method: str) -> None:
        cost = unit_cost(method)
        if self.units + cost > self.cap:
            raise RuntimeError(f"YouTube run cap reached: {self.units}/{self.cap}")
        require_youtube_quota_available(feature=FEATURE, method=method)
        self.units += cost
        self.calls[method] = self.calls.get(method, 0) + 1


def _store_cache(path: Path, params: dict[str, Any], payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps({"params": params, "payload": payload}, ensure_ascii=False),
        encoding="utf-8",
    )
    temporary.replace(path)


def _youtube_get(session, quota: RunQuota, endpoint: str, method: str, params: dict[str, Any]):
    api_key = research_youtube_api_key()
    quota.reserve(method)
    response = None
    try:
        response = session.get(
            f"{YOUTUBE_API_BASE}/{endpoint}",
            params={**params, "key": api_key},
            timeout=30,
        )
        record_youtube_data_api_call(feature=FEATURE, method=method)
        response.raise_for_status()
        payload = response.json() or {}
    except (requests.RequestException, ValueError):
        status = getattr(response, "status_code", None)
        detail = f"HTTP {status}" if status is not None else "request error"
        raise RuntimeError(f"{method} failed: {detail}") from None
    return payload


def backfill_zero_upload_channels(rows: list[dict[str, Any]], cap: int) -> dict[str, Any]:
    before = [
        str(row.get("youtube_channel_id") or "")
        for row in rows
        if not _cached_uploads(str(row.get("youtube_channel_id") or ""))
    ]
    quota = RunQuota(cap)
    fetched_ids = []
    all_video_ids = []
    with requests.Session() as session:
        # One playlist call can yield 50 videos and therefore up to one videos.list call.
        # Half the cap is reserved for the detail batches.
        for channel_id in before[: cap // 2]:
            params = {
                "part": "snippet,contentDetails",
                "playlistId": "UU" + channel_id[2:],
                "maxResults": 50,
            }
            try:
                payload = _youtube_get(
                    session, quota, "playlistItems", "playlistItems.list", params
                )
            except requests.RequestException:
                continue
            _store_cache(CACHE_ROOT / "uploads" / channel_id / "page_0000.json", params, payload)
            fetched_ids.append(channel_id)
            for item in payload.get("items") or []:
                snippet = item.get("snippet") or {}
                content = item.get("contentDetails") or {}
                video_id = str(
                    content.get("videoId")
                    or (snippet.get("resourceId") or {}).get("videoId")
                    or ""
                ).strip()
                if video_id:
                    all_video_ids.append(video_id)
        unique_ids = list(dict.fromkeys(all_video_ids))
        for offset in range(0, len(unique_ids), 50):
            chunk = unique_ids[offset : offset + 50]
            params = {
                "part": "snippet,contentDetails,statistics",
                "id": ",".join(chunk),
                "maxResults": 50,
            }
            payload = _youtube_get(session, quota, "videos", "videos.list", params)
            key = hashlib.sha256(",".join(chunk).encode()).hexdigest()[:16]
            _store_cache(CACHE_ROOT / "video_batches" / f"{key}.json", params, payload)
    after = [channel_id for channel_id in before if _cached_uploads(channel_id)]
    return {
        "zero_uploads_before": before,
        "zero_uploads_now_with_data": after,
        "units_used": quota.units,
        "calls": quota.calls,
    }


def _admin_precedents(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    decided = [row for row in rows if row.get("admin_include") or row.get("admin_exclude")]
    decided.sort(key=lambda row: str(row.get("updated_at") or ""), reverse=True)
    return [
        {
            "channel": str(row.get("title") or ""),
            "description": _channel_description(str(row.get("youtube_channel_id") or ""))[:500],
            "decision": "include" if row.get("admin_include") else "exclude",
            "reason": str(row.get("exclude_reason") or "")[:500],
        }
        for row in decided[:20]
    ]


def _classify_channel(
    definition: dict[str, Any],
    admin_precedents: list[dict[str, str]],
    row: dict[str, Any],
    video_details,
):
    channel_id = str(row.get("youtube_channel_id") or "")
    title_inputs = load_mixed_titles(channel_id, video_details)
    if not title_inputs:
        return None, None
    focus_options = definition.get("focus_options") or []
    focus_keys = {str(item.get("key") or "") for item in focus_options if isinstance(item, dict)}
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
                    f"primary_focus must be exactly one of {', '.join(sorted(focus_keys))}. entity must "
                    "be individual or company_clinic. title_results must contain one object per input "
                    "title in the same order: {\"title\":\"exact input title\",\"core_topic\":true}. "
                    "A title is core_topic=true ONLY when it is mainly about one of the supplied stored "
                    "core topics. Adjacent topics are false. "
                    f"reason must be one line. {TEACH_RULE} Follow the supplied Admin decisions "
                    "as precedent for similar channels."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Channel description: {_channel_description(channel_id)}\n"
                    f"Recent titles with format: {json.dumps(title_inputs, ensure_ascii=False)}\n"
                    f"Target focus: {definition.get('target_focus')}\n"
                    f"Focus options: {json.dumps(focus_options, ensure_ascii=False)}\n"
                    f"Stored core_topics: {json.dumps(definition.get('core_topics') or [], ensure_ascii=False)}\n"
                    f"Admin decisions: {json.dumps(admin_precedents, ensure_ascii=False)}"
                ),
            },
        ],
    )
    return _normalize_result(_json_result(response), title_inputs, focus_keys), _usage(response)


def _classification_scope(conn, niche: dict[str, Any], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    run = conn.execute(
        "SELECT run_id FROM niche_classification_runs WHERE niche_id=? ORDER BY run_at DESC, run_id DESC LIMIT 1",
        [niche["id"]],
    ).fetchone()
    if not run:
        return rows
    prior_ids = {
        str(item[0]) for item in conn.execute(
            "SELECT youtube_channel_id FROM niche_channel_run_results WHERE run_id=?", [run[0]]
        ).fetchall()
    }
    pool_ids = {
        str(item[0]) for item in conn.execute(
            "SELECT youtube_channel_id FROM niche_channel_run_results WHERE run_id=? AND in_pool", [run[0]]
        ).fetchall()
    }
    near_ids = {
        str(row.get("youtube_channel_id") or "")
        for row in near_miss_niche_rows(rows, clinicians_only=False, niche=niche)
    }
    return [
        row for row in rows
        if str(row.get("youtube_channel_id") or "") in pool_ids | near_ids
        or str(row.get("youtube_channel_id") or "") not in prior_ids
    ]


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
    parser.add_argument("--youtube-cap", type=int, default=300)
    args = parser.parse_args()
    video_details = legacy.load_video_details(CACHE_ROOT)
    conn = get_db_readonly()
    try:
        niche, rows = load_niche_snapshot(conn, NICHE_SLUG)
        definition = load_niche_definition(conn, niche_id=niche["id"])
        previous_ids = _previous_pool_ids(conn, niche["id"])
        classification_rows = _classification_scope(conn, niche, rows)
    finally:
        conn.close()
    if not definition:
        raise RuntimeError("Stored niche definition is required")

    backfill = backfill_zero_upload_channels(classification_rows, args.youtube_cap)
    video_details = legacy.load_video_details(CACHE_ROOT)
    precedents = _admin_precedents(rows)

    channel_results: dict[str, dict[str, Any]] = {}
    total_usage = {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}

    def classify_row(row):
        return row, *_classify_channel(
            definition, precedents, row, video_details
        )

    with ThreadPoolExecutor(max_workers=max(1, min(args.workers, 12))) as executor:
        classifications = executor.map(classify_row, classification_rows)
        for index, (row, result, usage) in enumerate(classifications, start=1):
            channel_id = str(row.get("youtube_channel_id") or "")
            if result is None:
                channel_results[channel_id] = {"classification_status": "no_titles"}
                print(f"no_titles {index}/{len(classification_rows)} {channel_id}", file=sys.stderr)
                continue
            result["classification_status"] = "classified"
            channel_results[channel_id] = result
            for key in total_usage:
                total_usage[key] += usage[key]
            print(f"classified {index}/{len(classification_rows)} {channel_id}", file=sys.stderr)

    conn = get_db()
    try:
        description = str(definition.get("description") or "").strip()
        if TEACH_RULE not in description:
            description = f"{description} {TEACH_RULE}".strip()
            conn.execute(
                """
                UPDATE niches SET description=?, definition_version=definition_version+1
                WHERE id=?
                """,
                [description, niche["id"]],
            )
        for channel_id, result in channel_results.items():
            if result["classification_status"] == "no_titles":
                conn.execute(
                    """
                    UPDATE niche_channels SET classification_status='no_titles',
                        updated_at=CURRENT_TIMESTAMP
                    WHERE niche_id=? AND youtube_channel_id=? AND snapshot_date=?
                    """,
                    [niche["id"], channel_id, niche["snapshot_date"]],
                )
                continue
            conn.execute(
                """
                UPDATE niche_channels
                SET primary_focus=?, on_topic_share=?, on_topic_count=?,
                    classification_title_count=?, entity=?,
                    classification_status='classified',
                    reclass_reason=?, updated_at=CURRENT_TIMESTAMP
                WHERE niche_id=? AND youtube_channel_id=? AND snapshot_date=?
                """,
                [
                    result["primary_focus"], result["core_share"], result["core_count"],
                    result["title_count"], result["entity"], result["reason"], niche["id"], channel_id,
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
            [
                niche["id"], "titles-teach-admin-precedent-v3.1",
                "v3.1 titles fix + teach rule + admin precedent",
            ],
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

    current_pool = sorted(
        final_niche_pool(current_rows, clinicians_only=False, niche=niche),
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
            "in_pool": is_final_niche_pool_row(row, niche),
            "admin_include": bool(row.get("admin_include")),
            "admin_exclude": bool(row.get("admin_exclude")),
            "reason": row.get("reclass_reason"),
        }

    requested_titles = {
        "yourself yoga for menopause",
        "gorgeously green",
        "natalie jill fitness",
        "honest hrt doc",
    }
    status_rows = [
        row for row in current_rows
        if str(row.get("title") or "").strip().casefold() in requested_titles
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
                "youtube": backfill,
                "no_titles": sum(
                    result.get("classification_status") == "no_titles"
                    for result in channel_results.values()
                ),
                "run_id": run_id,
                "classified_scope": len(classification_rows),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
