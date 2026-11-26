#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests


REPO_ROOT = Path(__file__).resolve().parents[2]
RESEARCH_DIR = Path(__file__).resolve().parent
for path in (REPO_ROOT, RESEARCH_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from dotenv import load_dotenv

load_dotenv(REPO_ROOT / ".env")

import build_turkish_religious_niche as build
import niche_women_hormones as base
import reclassify_women_hormones as legacy
from app.video_shorts.config import _openai_client
from app.video_shorts.services.db import get_db, get_db_readonly
from app.video_shorts.services.niche_definitions import load_niche_definition
from app.video_shorts.services.niches import (
    final_niche_pool,
    is_final_niche_pool_row,
    load_niche_snapshot,
)
from app.video_shorts.services.niche_outliers import (
    compute_niche_outlier_shorts,
    generate_niche_outlier_pattern,
    load_latest_niche_outliers,
    persist_niche_outlier_pattern,
    persist_niche_outlier_shorts,
    persist_niche_outlier_topics,
)


SLUG = "turkish_religious_talks"
MODEL = "gpt-4.1-mini"
CACHE_ROOT = REPO_ROOT / "data" / "research" / "cache"
SOURCE_CHECKPOINT = REPO_ROOT / "data" / "research" / "turkish_religious_talks_checkpoint.json"
CHECKPOINT_PATH = REPO_ROOT / "data" / "research" / "turkish_channel_types_checkpoint.json"
FEATURE = "turkish_religious_channel_types_research_project"
OTHER_UNIT_LIMIT = 200
SEED_CHANNEL_ID = "UCpZTTbddOVYPvQj02Zu8x_A"
REPORT_CHANNELS = {
    "Gündem Oldu", "Aleyna Beyazgül", "Sezin Karameşe", "Beyza Kazan Sümbül",
    "İç Sesim Diyo ki", "Dinimi Öğreniyorum", "Hayalhanem",
    "Hocaefendi'den kısa kısa",
}


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default
    return value if isinstance(value, dict) else default


def classify_channel(source: dict[str, Any], options: list[dict[str, str]]) -> tuple[str, dict[str, int]]:
    keys = {str(item["key"]) for item in options}
    response = _openai_client.chat.completions.create(
        model=MODEL,
        temperature=0,
        max_tokens=120,
        response_format={"type": "json_object"},
        messages=[
            {
                "role": "system",
                "content": (
                    "Classify this Turkish YouTube channel's channel type, not its topic. "
                    "Use the description and supplied recent titles. Return JSON only: "
                    "{\"channel_type\":\"one configured key\"}. own_speaker means the scholar "
                    "or speaker's own channel. clip_compilation republishes clips or excerpts from "
                    "one or more scholars. media_brand is a production brand with talk-format content "
                    "and multiple presenters. faceless_story is narrated stories or voiceover without "
                    "an on-camera speaker, often AI voice. news covers news/current events. "
                    "lifestyle_influencer is a personal lifestyle/vlog creator. Use other only when none fit."
                ),
            },
            {
                "role": "user",
                "content": json.dumps({
                    "channel_title": source.get("title"),
                    "channel_description": source.get("description"),
                    "recent_titles": source.get("titles"),
                    "channel_type_options": options,
                }, ensure_ascii=False),
            },
        ],
    )
    raw = json.loads(response.choices[0].message.content or "{}")
    channel_type = str(raw.get("channel_type") or "other").strip().casefold()
    if channel_type not in keys:
        channel_type = "other"
    usage = response.usage
    return channel_type, {
        "input_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
        "output_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
    }


def classify_all(definition: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    source_checkpoint = load_json(SOURCE_CHECKPOINT, {"rows": {}})
    checkpoint = load_json(CHECKPOINT_PATH, {
        "channel_types": {}, "usage": {"input_tokens": 0, "output_tokens": 0},
    })
    video_details = legacy.load_video_details(CACHE_ROOT)
    sources = {}
    for row in rows:
        channel_id = str(row.get("youtube_channel_id") or "")
        source_row = (source_checkpoint.get("rows") or {}).get(channel_id) or {}
        if not source_row.get("channel"):
            continue
        sources[channel_id] = build.classification_input(
            definition, channel_id, source_row, video_details
        )
    pending = [
        channel_id for channel_id in sources
        if channel_id not in checkpoint["channel_types"]
    ]

    def run(channel_id: str):
        channel_type, usage = classify_channel(
            sources[channel_id], definition["channel_type_options"]
        )
        return channel_id, channel_type, usage

    with ThreadPoolExecutor(max_workers=8) as executor:
        for index, (channel_id, channel_type, usage) in enumerate(
            executor.map(run, pending), start=1
        ):
            checkpoint["channel_types"][channel_id] = channel_type
            checkpoint["usage"]["input_tokens"] += usage["input_tokens"]
            checkpoint["usage"]["output_tokens"] += usage["output_tokens"]
            atomic_json(CHECKPOINT_PATH, checkpoint)
            print(f"classified channel type {index}/{len(pending)} {channel_id}", file=sys.stderr)
    return checkpoint


def refresh_zero_view_pool_channels(
    definition: dict[str, Any], rows: list[dict[str, Any]], source_rows: dict[str, Any]
) -> tuple[base.QuotaBudget, list[str], list[str]]:
    base.QUOTA_FEATURE = FEATURE
    quota = base.QuotaBudget(search_limit=0, other_limit=OTHER_UNIT_LIMIT)
    targets = [
        row for row in rows
        if is_final_niche_pool_row(row, definition) and int(row.get("views_90d") or 0) == 0
    ]
    refreshed = []
    errors = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=90)
    session = requests.Session()
    conn = get_db()
    try:
        for row in targets:
            channel_id = str(row.get("youtube_channel_id") or "")
            channel = ((source_rows.get(channel_id) or {}).get("channel") or {})
            playlist_id = str(channel.get("uploads_playlist_id") or "")
            if not playlist_id:
                errors.append(f"{channel_id}: no uploads playlist")
                continue
            uploads = []
            page_token = ""
            page_index = 0
            try:
                while True:
                    params = {
                        "part": "snippet,contentDetails", "playlistId": playlist_id,
                        "maxResults": 50,
                    }
                    if page_token:
                        params["pageToken"] = page_token
                    payload = base.youtube_get(
                        session, quota, "playlistItems", "playlistItems.list", params
                    )
                    base._store_cached_payload(
                        CACHE_ROOT / "uploads" / channel_id / f"page_{page_index:04d}.json",
                        params, payload,
                    )
                    crossed = False
                    for item in payload.get("items") or []:
                        snippet = item.get("snippet") or {}
                        details = item.get("contentDetails") or {}
                        published = base._parse_timestamp(
                            details.get("videoPublishedAt") or snippet.get("publishedAt")
                        )
                        if published and published < cutoff:
                            crossed = True
                            continue
                        video_id = str(details.get("videoId") or "")
                        if video_id and published:
                            uploads.append({
                                "video_id": video_id,
                                "title": str(snippet.get("title") or ""),
                                "published_at": published,
                            })
                    page_token = str(payload.get("nextPageToken") or "")
                    page_index += 1
                    if not page_token or crossed:
                        break
                details_by_id = {}
                for offset in range(0, len(uploads), 50):
                    chunk = uploads[offset:offset + 50]
                    params = {
                        "part": "snippet,contentDetails,statistics",
                        "id": ",".join(item["video_id"] for item in chunk),
                        "maxResults": 50,
                    }
                    payload = base.youtube_get(
                        session, quota, "videos", "videos.list", params
                    )
                    key = hashlib.sha256(params["id"].encode()).hexdigest()[:16]
                    base._store_cached_payload(
                        CACHE_ROOT / "video_batches" / f"{key}.json", params, payload
                    )
                    for item in payload.get("items") or []:
                        stats = item.get("statistics") or {}
                        details_by_id[str(item.get("id") or "")] = {
                            "duration_seconds": base.duration_seconds(
                                (item.get("contentDetails") or {}).get("duration")
                            ),
                            "views": base._optional_int(stats.get("viewCount")),
                        }
                activity = base.compute_activity(uploads, details_by_id)
                views_90d = sum(
                    int((details_by_id.get(item["video_id"]) or {}).get("views") or 0)
                    for item in uploads
                )
                conn.execute(
                    """
                    UPDATE niche_channels SET views_90d=?, longform_90d=?, shorts_90d=?,
                        avg_views_long_90d=?, avg_views_short_90d=?, updated_at=CURRENT_TIMESTAMP
                    WHERE niche_id=? AND snapshot_date=? AND youtube_channel_id=?
                    """,
                    [
                        views_90d, int(activity.get("longform_90d") or 0),
                        int(activity.get("shorts_90d") or 0),
                        build.optional_int(activity.get("avg_views_long_90d")),
                        build.optional_int(activity.get("avg_views_short_90d")),
                        definition["id"], row["snapshot_date"], channel_id,
                    ],
                )
                refreshed.append(str(row.get("title") or channel_id))
            except Exception as exc:
                errors.append(f"{row.get('title') or channel_id}: {exc}")
                if "quota" in str(exc).casefold():
                    break
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
        session.close()
    return quota, refreshed, errors


def persist_and_report(definition: dict[str, Any], rows: list[dict[str, Any]], checkpoint: dict[str, Any]) -> dict[str, Any]:
    conn = get_db()
    try:
        for row in rows:
            channel_id = str(row.get("youtube_channel_id") or "")
            channel_type = checkpoint["channel_types"].get(channel_id, "other")
            conn.execute(
                "UPDATE niche_channels SET channel_type=?, updated_at=CURRENT_TIMESTAMP "
                "WHERE niche_id=? AND snapshot_date=? AND youtube_channel_id=?",
                [channel_type, definition["id"], row["snapshot_date"], channel_id],
            )
            row["channel_type"] = channel_type
        run_id = conn.execute(
            "INSERT INTO niche_classification_runs (niche_id,run_at,rule_version,notes) "
            "VALUES (?,CURRENT_TIMESTAMP,?,?) RETURNING run_id",
            [definition["id"], "generic-v3.1-tr-channel-types", "tr channel types"],
        ).fetchone()[0]
        for row in rows:
            conn.execute(
                "INSERT INTO niche_channel_run_results "
                "(run_id,youtube_channel_id,in_pool,primary_niche,on_topic_share,reason) "
                "VALUES (?,?,?,?,?,?)",
                [
                    run_id, row["youtube_channel_id"],
                    is_final_niche_pool_row(row, definition), row.get("primary_focus"),
                    float(row.get("on_topic_share") or 0),
                    f"Channel type: {row.get('channel_type') or 'other'}",
                ],
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    pool = final_niche_pool(rows, clinicians_only=False, niche=definition)
    pool.sort(key=lambda row: (-int(row.get("subscriber_count") or 0), str(row.get("title") or "").casefold()))
    seed_rank = next((index for index, row in enumerate(pool, 1) if row.get("youtube_channel_id") == SEED_CHANNEL_ID), None)
    usage = checkpoint["usage"]
    return {
        "run_id": int(run_id),
        "channel_type_counts": dict(Counter(
            str(row.get("channel_type") or "other") for row in rows
        )),
        "pool_size": len(pool),
        "seed_rank": seed_rank,
        "requested_channels": {
            str(row.get("title") or ""): row.get("channel_type")
            for row in rows if str(row.get("title") or "").strip() in REPORT_CHANNELS
        },
        "llm_usage": {
            **usage,
            "cost_usd": (
                int(usage["input_tokens"]) * 0.4 + int(usage["output_tokens"]) * 1.6
            ) / 1_000_000,
        },
    }


def recompute_top_shorts(definition: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    conn = get_db_readonly()
    try:
        _previous_as_of, previous_rows = load_latest_niche_outliers(conn, definition["id"])
    finally:
        conn.close()
    previous_topics = {
        str(row.get("video_id") or ""): bool(row.get("llm_in_category"))
        for row in previous_rows if row.get("llm_in_category") is not None
    }
    result = compute_niche_outlier_shorts(
        SLUG, cache_root=CACHE_ROOT, include_non_pool=True
    )
    persist_niche_outlier_shorts(result)
    for row in result["rows"]:
        value = previous_topics.get(str(row.get("video_id") or ""))
        row["llm_in_category"] = value
        row["in_category"] = value is not False
        row["on_topic"] = value is not False
    persist_niche_outlier_topics(result)
    pool = final_niche_pool(rows, clinicians_only=False, niche=definition)
    pool_ids = {str(row.get("youtube_channel_id") or "") for row in pool}
    result["pattern_rows"] = [
        row for row in result["rows"]
        if str(row.get("youtube_channel_id") or "") in pool_ids
    ]
    pattern = generate_niche_outlier_pattern(result)
    if pattern:
        persist_niche_outlier_pattern(pattern)
    title_by_id = {
        str(row.get("youtube_channel_id") or ""): str(row.get("title") or "")
        for row in rows
    }
    counts = Counter()
    top = []
    visible = sorted(
        (
            row for row in result["rows"]
            if str(row.get("youtube_channel_id") or "") in pool_ids
            and row.get("in_category") is not False
        ),
        key=lambda row: -int(row.get("views") or 0),
    )
    for row in visible:
        channel_id = str(row.get("youtube_channel_id") or "")
        if counts[channel_id] >= 2:
            continue
        counts[channel_id] += 1
        top.append({
            "channel": title_by_id.get(channel_id, channel_id),
            "title": row.get("title"),
            "views": int(row.get("views") or 0),
            "ratio": round(float(row.get("outlier_ratio") or 0), 2),
            "duration_s": int(row.get("duration_s") or 0),
            "video_id": row.get("video_id"),
        })
        if len(top) == 5:
            break
    return {
        "raw_outliers": result["outlier_count"],
        "default_visible_count": len(visible),
        "top5": top,
        "pattern": pattern,
    }


def main() -> int:
    conn = get_db_readonly()
    try:
        definition = load_niche_definition(conn, slug=SLUG)
        selected, rows = load_niche_snapshot(conn, SLUG)
    finally:
        conn.close()
    if not definition or selected is None or not definition.get("channel_type_options"):
        raise RuntimeError("Turkish channel type configuration is missing")
    checkpoint = classify_all(definition, rows)
    report = persist_and_report(definition, rows, checkpoint)
    report["top_shorts"] = recompute_top_shorts(definition, rows)
    source_checkpoint = load_json(SOURCE_CHECKPOINT, {"rows": {}})
    quota, refreshed, errors = refresh_zero_view_pool_channels(
        definition, rows, source_checkpoint.get("rows") or {}
    )
    report["youtube"] = {
        "units_used": quota.other_units,
        "calls": dict(quota.calls),
        "refreshed": refreshed,
        "errors": errors,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
