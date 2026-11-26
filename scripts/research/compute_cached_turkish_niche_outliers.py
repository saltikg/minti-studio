#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv

load_dotenv(REPO_ROOT / ".env")

from app.video_shorts.services.db import get_db_readonly
from app.video_shorts.services.niche_outliers import (
    _atomic_json,
    _duration_seconds,
    classify_niche_outlier_topics,
    compute_niche_outlier_shorts,
    generate_niche_outlier_pattern,
    persist_niche_outlier_pattern,
    persist_niche_outlier_shorts,
    persist_niche_outlier_topics,
)
from app.video_shorts.services.niches import final_niche_pool, load_niche_snapshot


SLUG = "turkish_religious_talks"
CACHE_ROOT = REPO_ROOT / "data" / "research" / "cache"
PACIFIC = ZoneInfo("America/Los_Angeles")


def cache_payload(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value.get("payload") or value


def optional_int(value):
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def parse_timestamp(value):
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def build_manifest() -> tuple[Path, int, int]:
    as_of = datetime.now(PACIFIC).date()
    start_pt = datetime.combine(as_of - timedelta(days=30), time.min, tzinfo=PACIFIC)
    conn = get_db_readonly()
    try:
        niche, rows = load_niche_snapshot(conn, SLUG)
        pool = final_niche_pool(rows, clinicians_only=False, niche=niche)
    finally:
        conn.close()
    uploads = {}
    video_ids = set()
    for row in pool:
        channel_id = str(row.get("youtube_channel_id") or "")
        channel_uploads = {}
        for path in sorted((CACHE_ROOT / "uploads" / channel_id).glob("page_*.json")):
            for item in cache_payload(path).get("items") or []:
                snippet = item.get("snippet") or {}
                details = item.get("contentDetails") or {}
                video_id = str(
                    details.get("videoId")
                    or (snippet.get("resourceId") or {}).get("videoId")
                    or ""
                ).strip()
                published_at = parse_timestamp(
                    details.get("videoPublishedAt") or snippet.get("publishedAt")
                )
                if not video_id or not published_at or published_at.astimezone(PACIFIC) < start_pt:
                    continue
                channel_uploads[video_id] = {
                    "video_id": video_id,
                    "title": " ".join(str(snippet.get("title") or "").split()),
                    "published_at": published_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                }
                video_ids.add(video_id)
        uploads[channel_id] = list(channel_uploads.values())
    videos = {}
    paths = sorted((CACHE_ROOT / "video_batches").glob("*.json"), key=lambda path: path.stat().st_mtime)
    for path in paths:
        for item in cache_payload(path).get("items") or []:
            video_id = str(item.get("id") or "")
            if video_id not in video_ids:
                continue
            snippet = item.get("snippet") or {}
            stats = item.get("statistics") or {}
            videos[video_id] = {
                "duration_s": _duration_seconds((item.get("contentDetails") or {}).get("duration")),
                "views": optional_int(stats.get("viewCount")),
                "likes": optional_int(stats.get("likeCount")),
                "comments": optional_int(stats.get("commentCount")),
                "description": str(snippet.get("description") or "")[:500],
                "tags": [str(tag) for tag in (snippet.get("tags") or [])[:30]],
            }
    manifest = {
        "version": "niche-outliers-v1",
        "niche_id": niche["id"],
        "niche_slug": SLUG,
        "as_of": as_of.isoformat(),
        "refreshed_at": datetime.now(timezone.utc).isoformat(),
        "pool_channel_ids": sorted(uploads),
        "uploads": uploads,
        "videos": videos,
        "quota": {"units_used": 0, "calls": {}, "cap": 0, "source": "existing cache"},
    }
    path = CACHE_ROOT / "niche_outliers" / SLUG / f"{as_of.isoformat()}.json"
    _atomic_json(path, manifest)
    return path, len(uploads), len(videos)


def main() -> int:
    path, channel_count, video_count = build_manifest()
    result = compute_niche_outlier_shorts(SLUG, cache_root=CACHE_ROOT)
    persist_niche_outlier_shorts(result)
    topic = classify_niche_outlier_topics(result, cache_root=CACHE_ROOT)
    persist_niche_outlier_topics(result)
    pattern = generate_niche_outlier_pattern(result)
    if pattern:
        persist_niche_outlier_pattern(pattern)
    print(json.dumps({
        "cache_path": str(path),
        "cache_channels": channel_count,
        "cache_videos": video_count,
        "channels_with_3_shorts": result["channels_with_3_shorts"],
        "outlier_count": result["outlier_count"],
        "rows": result["rows"],
        "topic_usage": topic,
        "pattern": pattern,
    }, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
