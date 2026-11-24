#!/usr/bin/env python3
"""Strictly reclassify the women-hormones niche from cached research data only."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Any

import requests
from dotenv import load_dotenv


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
load_dotenv(REPO_ROOT / ".env")

from app.video_shorts.config import OPENAI_MODEL, _openai_client
from app.video_shorts.services.db import get_db, table_columns
from app.video_shorts.services.youtube_quota import record_youtube_data_api_call


CLASSIFIER_VERSION = "strict-menopause-v2"
PRIMARY_NICHES = {
    "menopause_hormones",
    "fertility_obgyn",
    "womens_fitness",
    "womens_nutrition_fasting",
    "weight_glp1",
    "general_gyn",
    "sexual_health",
    "other",
}
ENTITIES = {"individual", "company_clinic"}
OUTPUT_FIELDS = [
    "primary_niche",
    "on_topic_share",
    "on_topic_count",
    "entity",
    "reclass_reason",
    "views_90d",
]


class ReclassifyError(RuntimeError):
    pass


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    lock: Lock = field(default_factory=Lock, repr=False)

    def add(self, response: Any) -> None:
        usage = getattr(response, "usage", None)
        input_tokens = int(getattr(usage, "prompt_tokens", 0) or getattr(usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "completion_tokens", 0) or getattr(usage, "output_tokens", 0) or 0)
        with self.lock:
            self.input_tokens += input_tokens
            self.output_tokens += output_tokens

    def estimated_cost(self) -> float | None:
        prices = {"gpt-4.1-mini": (0.40, 1.60), "gpt-4o-mini": (0.15, 0.60)}
        selected = prices.get(OPENAI_MODEL)
        if not selected:
            return None
        return (self.input_tokens * selected[0] + self.output_tokens * selected[1]) / 1_000_000


def _parse_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _cache_payload(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    payload = raw.get("payload") if isinstance(raw, dict) else None
    return payload if isinstance(payload, dict) else {}


def load_video_views(cache_root: Path) -> dict[str, int]:
    views: dict[str, int] = {}
    for path in sorted((cache_root / "video_batches").glob("*.json")):
        for item in _cache_payload(path).get("items") or []:
            video_id = str(item.get("id") or "").strip()
            raw_views = ((item.get("statistics") or {}).get("viewCount"))
            try:
                if video_id and raw_views is not None:
                    views[video_id] = int(raw_views)
            except (TypeError, ValueError):
                continue
    return views


def load_cached_channel_activity_ids(
    cache_root: Path,
    channel_id: str,
    cutoff: datetime,
) -> tuple[list[str], set[str]]:
    titles: list[str] = []
    recent_ids: set[str] = set()
    upload_dir = cache_root / "uploads" / channel_id
    for path in sorted(upload_dir.glob("page_*.json")):
        for item in _cache_payload(path).get("items") or []:
            snippet = item.get("snippet") or {}
            details = item.get("contentDetails") or {}
            title = " ".join(str(snippet.get("title") or "").split())
            if title and len(titles) < 15:
                titles.append(title)
            video_id = str(details.get("videoId") or (snippet.get("resourceId") or {}).get("videoId") or "").strip()
            published_at = _parse_timestamp(details.get("videoPublishedAt") or snippet.get("publishedAt"))
            if video_id and published_at and published_at >= cutoff:
                recent_ids.add(video_id)
    return titles, recent_ids


def load_cached_channel_activity(
    cache_root: Path,
    channel_id: str,
    cutoff: datetime,
    video_views: dict[str, int],
) -> tuple[list[str], int, int]:
    titles, recent_ids = load_cached_channel_activity_ids(cache_root, channel_id, cutoff)
    missing_view_count = 0
    views_90d = 0
    for video_id in recent_ids:
        if video_id in video_views:
            views_90d += video_views[video_id]
        else:
            missing_view_count += 1
    return titles, views_90d, missing_view_count


def _store_youtube_cache(path: Path, params: dict[str, Any], payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps({"params": params, "payload": payload}, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(path)


def backfill_missing_video_stats(
    *,
    source_rows: list[dict[str, Any]],
    cache_root: Path,
    cutoff: datetime,
) -> dict[str, int]:
    existing_views = load_video_views(cache_root)
    missing_by_channel: dict[str, set[str]] = {}
    for row in source_rows:
        channel_id = str(row.get("youtube_channel_id") or "").strip()
        _titles, recent_ids = load_cached_channel_activity_ids(cache_root, channel_id, cutoff)
        missing = {video_id for video_id in recent_ids if video_id not in existing_views}
        if missing:
            missing_by_channel[channel_id] = missing
    missing_ids = sorted(set().union(*missing_by_channel.values())) if missing_by_channel else []
    api_key = str(os.getenv("YOUTUBE_API_KEY") or "").strip()
    if missing_ids and not api_key:
        raise ReclassifyError("YOUTUBE_API_KEY is required to fill missing video statistics")

    units_used = 0
    session = requests.Session()
    for offset in range(0, len(missing_ids), 50):
        chunk = missing_ids[offset : offset + 50]
        params = {"part": "contentDetails,statistics", "id": ",".join(chunk), "maxResults": 50}
        units_used += 1
        response = session.get(
            "https://www.googleapis.com/youtube/v3/videos",
            params={**params, "key": api_key},
            timeout=30,
        )
        try:
            response.raise_for_status()
            payload = response.json() or {}
        except (requests.RequestException, ValueError) as exc:
            raise ReclassifyError(
                f"videos.list backfill failed at batch {offset // 50 + 1}: HTTP {response.status_code}"
            ) from exc
        if not isinstance(payload, dict):
            raise ReclassifyError("videos.list backfill returned a non-object response")
        record_youtube_data_api_call(feature="women_hormones_research", method="videos.list")
        batch_key = hashlib.sha256(",".join(chunk).encode("utf-8")).hexdigest()[:16]
        _store_youtube_cache(cache_root / "video_batches" / f"{batch_key}.json", params, payload)

    refreshed_views = load_video_views(cache_root)
    unresolved_ids = {video_id for video_id in missing_ids if video_id not in refreshed_views}
    affected_channels = sum(
        1 for ids in missing_by_channel.values() if any(video_id in refreshed_views for video_id in ids)
    )
    return {
        "missing_before": len(missing_ids),
        "resolved": len(missing_ids) - len(unresolved_ids),
        "unresolved": len(unresolved_ids),
        "affected_channels": affected_channels,
        "units_used": units_used,
    }


def _fallback_titles(raw: str) -> list[str]:
    return [part.strip() for part in str(raw or "").split(" | ") if part.strip()][:15]


def _extract_json(text: str) -> dict[str, Any]:
    clean = str(text or "").strip()
    if clean.startswith("```"):
        clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", clean, flags=re.IGNORECASE)
    payload = json.loads(clean)
    if not isinstance(payload, dict):
        raise ReclassifyError("LLM result is not an object")
    return payload


def _validate_result(payload: dict[str, Any], titles: list[str]) -> dict[str, Any]:
    primary_niche = str(payload.get("primary_niche") or "").strip().lower()
    entity = str(payload.get("entity") or "").strip().lower()
    reason = " ".join(str(payload.get("reason") or "").split())[:500]
    title_flags = payload.get("titles")
    if primary_niche not in PRIMARY_NICHES:
        raise ReclassifyError(f"invalid primary_niche: {primary_niche!r}")
    if entity not in ENTITIES:
        raise ReclassifyError(f"invalid entity: {entity!r}")
    if not isinstance(title_flags, list) or len(title_flags) != len(titles):
        raise ReclassifyError(
            f"expected {len(titles)} title results, got "
            f"{len(title_flags) if isinstance(title_flags, list) else 'non-list'}"
        )
    flags: list[bool] = []
    for item in title_flags:
        if not isinstance(item, dict) or type(item.get("on_topic")) is not bool:
            raise ReclassifyError("each title result must contain an on_topic boolean")
        flags.append(item["on_topic"])
    if not reason:
        raise ReclassifyError("reason is required")
    on_topic_count = sum(1 for flag in flags if flag)
    return {
        "primary_niche": primary_niche,
        "titles": [{"title": title, "on_topic": flag} for title, flag in zip(titles, flags)],
        "on_topic_count": on_topic_count,
        "on_topic_share": on_topic_count / len(titles) if titles else 0.0,
        "entity": entity,
        "reclass_reason": reason,
    }


def classify(
    *,
    channel_id: str,
    description: str,
    titles: list[str],
    cache_dir: Path,
    usage: Usage,
    cached_only: bool = False,
) -> tuple[dict[str, Any], bool]:
    source = {
        "description": str(description or "")[:5000],
        "titles": titles[:15],
    }
    input_hash = hashlib.sha256(json.dumps(source, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    cache_path = cache_dir / f"{channel_id}.json"
    if cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if (
                cached.get("version") == CLASSIFIER_VERSION
                and cached.get("model") == OPENAI_MODEL
                and cached.get("input_hash") == input_hash
            ):
                return _validate_result(cached.get("result") or {}, titles), True
        except (OSError, ValueError, json.JSONDecodeError, ReclassifyError):
            pass

    if cached_only:
        raise ReclassifyError(f"strict classification cache is missing or stale for {channel_id}")
    if not _openai_client:
        raise ReclassifyError("OPENAI_API_KEY is required")
    prompt = (
        "Classify a YouTube channel using only the channel description and the ordered list of its latest video titles. "
        "Judge what the channel is MAINLY about, using the description first. Mentioning menopause in some titles is not enough.\n\n"
        "Return JSON only with exactly these fields:\n"
        '{"primary_niche":"menopause_hormones|fertility_obgyn|womens_fitness|womens_nutrition_fasting|weight_glp1|general_gyn|sexual_health|other",'
        '"titles":[{"title":"exact supplied title","on_topic":true}],'
        '"entity":"individual|company_clinic","reason":"one line"}\n\n'
        "The titles array must contain exactly one object for each supplied title, in the same order, and copy each title exactly. "
        "A title is on_topic true only when "
        "the video is mainly about perimenopause, menopause, HRT, or female sex hormones (estrogen, progesterone, or testosterone "
        "in women). Diet, workouts, GLP-1, fertility, pregnancy, thyroid alone, and general wellness are false, even when the title "
        "says women over 40. Telehealth brands, clinics, hospitals, and product companies are company_clinic.\n\n"
        f"Channel description:\n{source['description']}\n\n"
        f"Ordered titles ({len(titles)}):\n{json.dumps(titles, ensure_ascii=False)}"
    )
    last_error: Exception | None = None
    for _attempt in range(3):
        try:
            response = _openai_client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": "You are a strict channel taxonomy classifier. Return valid JSON only."},
                    {"role": "user", "content": prompt},
                ],
                response_format={"type": "json_object"},
                temperature=0,
            )
            usage.add(response)
            raw = _extract_json(response.choices[0].message.content or "")
            result = _validate_result(raw, titles)
            cache_dir.mkdir(parents=True, exist_ok=True)
            temporary = cache_path.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(
                    {
                        "version": CLASSIFIER_VERSION,
                        "model": OPENAI_MODEL,
                        "input_hash": input_hash,
                        "result": {
                            "primary_niche": result["primary_niche"],
                            "titles": result["titles"],
                            "entity": result["entity"],
                            "reason": result["reclass_reason"],
                        },
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            temporary.replace(cache_path)
            return result, False
        except Exception as exc:
            last_error = exc
    raise ReclassifyError(f"classification failed for {channel_id}: {last_error}")


def write_checkpoint(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def apply_to_database(rows: list[dict[str, Any]], niche_slug: str, snapshot_date: str) -> None:
    conn = get_db()
    try:
        required = set(OUTPUT_FIELDS) | {"admin_exclude", "admin_include", "exclude_reason"}
        missing = required - table_columns(conn, "niche_channels")
        if missing:
            raise ReclassifyError(f"niche_channels schema is missing: {', '.join(sorted(missing))}")
        niche = conn.execute("SELECT id FROM niches WHERE slug=? LIMIT 1", [niche_slug]).fetchone()
        if not niche:
            raise ReclassifyError(f"unknown niche: {niche_slug}")
        updated = 0
        for row in rows:
            result = conn.execute(
                """
                UPDATE niche_channels
                SET primary_niche=?, on_topic_share=?, on_topic_count=?, entity=?,
                    reclass_reason=?, views_90d=?, updated_at=now()
                WHERE niche_id=? AND youtube_channel_id=? AND snapshot_date=?
                """,
                [
                    row["primary_niche"],
                    float(row["on_topic_share"]),
                    int(row["on_topic_count"]),
                    row["entity"],
                    row["reclass_reason"],
                    int(row["views_90d"]),
                    niche[0],
                    row["youtube_channel_id"],
                    snapshot_date,
                ],
            )
            updated += int(result.rowcount or 0)
        if updated != len(rows):
            raise ReclassifyError(f"database update guard expected {len(rows)} rows, updated {updated}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_path", type=Path)
    parser.add_argument("niche_slug")
    parser.add_argument("--snapshot-date", required=True)
    parser.add_argument("--cache-root", type=Path, default=REPO_ROOT / "data" / "research" / "cache")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--expected-rows", type=int, default=680)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--cached-classifications-only", action="store_true")
    parser.add_argument("--fill-missing-video-stats", action="store_true")
    args = parser.parse_args()

    csv_path = args.csv_path.expanduser().resolve()
    output_path = (args.output or csv_path.with_name(f"{csv_path.stem}_reclassified.csv")).resolve()
    snapshot = date.fromisoformat(args.snapshot_date)
    cutoff = datetime.combine(snapshot + timedelta(days=1), time.min, tzinfo=timezone.utc) - timedelta(days=90)
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        source_fields = list(reader.fieldnames or [])
        source_rows = list(reader)
    if len(source_rows) != args.expected_rows:
        raise ReclassifyError(f"row guard expected {args.expected_rows}, found {len(source_rows)}")
    if len({str(row.get('youtube_channel_id') or '').strip() for row in source_rows}) != len(source_rows):
        raise ReclassifyError("CSV channel IDs are missing or duplicated")

    backfill_result = {
        "missing_before": 0,
        "resolved": 0,
        "unresolved": 0,
        "affected_channels": 0,
        "units_used": 0,
    }
    if args.fill_missing_video_stats:
        backfill_result = backfill_missing_video_stats(
            source_rows=source_rows,
            cache_root=args.cache_root,
            cutoff=cutoff,
        )
        print(
            "video_stats_backfill "
            + " ".join(f"{key}={value}" for key, value in backfill_result.items()),
            flush=True,
        )

    video_views = load_video_views(args.cache_root)
    usage = Usage()
    output_rows: list[dict[str, Any]] = []
    cache_hits = 0
    missing_video_stats = 0

    def process_row(row: dict[str, Any]) -> tuple[dict[str, Any], bool, int]:
        channel_id = str(row.get("youtube_channel_id") or "").strip()
        titles, views_90d, missing = load_cached_channel_activity(args.cache_root, channel_id, cutoff, video_views)
        if not titles:
            titles = _fallback_titles(str(row.get("last_15_video_titles") or ""))
        result, cached = classify(
            channel_id=channel_id,
            description=str(row.get("description") or ""),
            titles=titles,
            cache_dir=args.cache_root / "strict_reclassifications",
            usage=usage,
            cached_only=args.cached_classifications_only,
        )
        return (
            {
                **row,
                "primary_niche": result["primary_niche"],
                "on_topic_share": f"{result['on_topic_share']:.6f}",
                "on_topic_count": result["on_topic_count"],
                "entity": result["entity"],
                "reclass_reason": result["reclass_reason"],
                "views_90d": views_90d,
            },
            cached,
            missing,
        )

    worker_count = max(1, min(args.workers, 12))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        classified_rows = executor.map(process_row, source_rows)
        for index, (output_row, cached, missing) in enumerate(classified_rows, start=1):
            output_rows.append(output_row)
            cache_hits += int(cached)
            missing_video_stats += missing
            if index % 10 == 0 or index == len(source_rows):
                write_checkpoint(output_path, source_fields + OUTPUT_FIELDS, output_rows)
                print(f"classified {index}/{len(source_rows)} cache_hits={cache_hits}", flush=True)

    apply_to_database(output_rows, args.niche_slug, args.snapshot_date)
    niche_counts = Counter(row["primary_niche"] for row in output_rows)
    entity_counts = Counter(row["entity"] for row in output_rows)
    cost = usage.estimated_cost()
    print("RECLASSIFICATION_COMPLETE")
    print(f"rows={len(output_rows)} cache_hits={cache_hits} new_llm_calls={len(output_rows)-cache_hits}")
    print("primary_niche=" + ",".join(f"{key}:{niche_counts[key]}" for key in sorted(niche_counts)))
    print("entity=" + ",".join(f"{key}:{entity_counts[key]}" for key in sorted(entity_counts)))
    print(f"missing_cached_video_stats={missing_video_stats}")
    print(f"youtube_units_used={backfill_result['units_used']}")
    print(f"llm_model={OPENAI_MODEL} input_tokens={usage.input_tokens} output_tokens={usage.output_tokens}")
    print(f"llm_cost_usd={cost:.6f}" if cost is not None else "llm_cost_usd=unknown")
    print(f"output={output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
