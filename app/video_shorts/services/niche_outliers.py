from __future__ import annotations

import hashlib
import json
import os
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

import requests

from app.video_shorts.services.db import get_db, get_db_readonly, table_columns
from app.video_shorts.services.niches import final_niche_pool, load_niche_snapshot
from app.video_shorts.services.youtube_quota import (
    record_youtube_data_api_call,
    require_youtube_quota_available,
    unit_cost,
)


PACIFIC = ZoneInfo("America/Los_Angeles")
YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"
QUOTA_FEATURE = "niche_outlier_research"
DEFAULT_MAX_UNITS = 500
DEFAULT_NICHE_SLUG = "menopause_hormones"
SCHEDULER_STATE_NAME = "niche_outliers:menopause_hormones"
EXCLUDED_TITLE_TERMS = ("#duet", "duet with", "#stitch", "stitch with", "repost")
MIN_SHORT_DURATION_SECONDS = 15
REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CACHE_ROOT = REPO_ROOT / "data" / "research" / "cache"


class NicheOutlierError(RuntimeError):
    pass


class RunQuota:
    def __init__(self, max_units: int = DEFAULT_MAX_UNITS):
        self.max_units = max(0, int(max_units))
        self.units_used = 0
        self.calls: dict[str, int] = defaultdict(int)

    def reserve(self, method: str) -> None:
        cost = unit_cost(method)
        if self.units_used + cost > self.max_units:
            raise NicheOutlierError(
                f"niche outlier YouTube cap reached: used={self.units_used} "
                f"requested={cost} cap={self.max_units}"
            )
        require_youtube_quota_available(feature=QUOTA_FEATURE, method=method)
        self.units_used += cost
        self.calls[method] += 1


def _cache_root(path: Path | None = None) -> Path:
    configured = str(os.getenv("NICHE_RESEARCH_CACHE_ROOT") or "").strip()
    return path or (Path(configured).expanduser() if configured else DEFAULT_CACHE_ROOT)


def _as_of_date(value: date | datetime | str | None) -> date:
    if value is None:
        return datetime.now(PACIFIC).date()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=PACIFIC)
        return value.astimezone(PACIFIC).date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value).strip())


def _parse_timestamp(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _duration_seconds(value: Any) -> int | None:
    text = str(value or "").strip()
    if not text.startswith("P") or "T" not in text:
        return None
    days_text, clock = text[1:].split("T", 1)
    days = int(days_text[:-1]) if days_text.endswith("D") and days_text[:-1].isdigit() else 0
    number = ""
    values = {"H": 0, "M": 0, "S": 0}
    for char in clock:
        if char.isdigit():
            number += char
        elif char in values and number:
            values[char] = int(number)
            number = ""
        else:
            return None
    return days * 86400 + values["H"] * 3600 + values["M"] * 60 + values["S"]


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _is_excluded_short(title: Any, duration_s: Any) -> bool:
    try:
        duration = int(duration_s)
    except (TypeError, ValueError):
        return True
    normalized_title = str(title or "").casefold()
    return duration < MIN_SHORT_DURATION_SECONDS or any(
        term in normalized_title for term in EXCLUDED_TITLE_TERMS
    )


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def _store_api_cache(path: Path, params: dict[str, Any], payload: dict[str, Any]) -> None:
    _atomic_json(path, {"params": params, "payload": payload})


def _youtube_get(
    session: requests.Session,
    quota: RunQuota,
    endpoint: str,
    method: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    api_key = str(os.getenv("YOUTUBE_API_KEY") or "").strip()
    if not api_key:
        raise NicheOutlierError("YOUTUBE_API_KEY is required")
    quota.reserve(method)
    try:
        response = session.get(
            f"{YOUTUBE_API_BASE}/{endpoint}",
            params={**params, "key": api_key},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json() or {}
    except (requests.RequestException, ValueError) as exc:
        raise NicheOutlierError(f"{method} failed: {exc}") from exc
    record_youtube_data_api_call(feature=QUOTA_FEATURE, method=method)
    return payload if isinstance(payload, dict) else {}


def _current_pool(niche_slug: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    conn = get_db_readonly()
    try:
        niche, rows = load_niche_snapshot(conn, niche_slug)
        if not niche:
            raise NicheOutlierError(f"unknown niche: {niche_slug}")
        return niche, final_niche_pool(rows, clinicians_only=False)
    finally:
        conn.close()


def refresh_niche_outlier_cache(
    niche: str = DEFAULT_NICHE_SLUG,
    as_of: date | datetime | str | None = None,
    *,
    cache_root: Path | None = None,
    max_units: int = DEFAULT_MAX_UNITS,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    as_of_date = _as_of_date(as_of)
    root = _cache_root(cache_root)
    niche_row, pool = _current_pool(str(niche))
    start_pt = datetime.combine(as_of_date - timedelta(days=30), time.min, tzinfo=PACIFIC)
    quota = RunQuota(max_units)
    owns_session = session is None
    session = session or requests.Session()
    uploads: dict[str, list[dict[str, Any]]] = {}
    try:
        for channel in pool:
            channel_id = str(channel.get("youtube_channel_id") or "").strip()
            if not channel_id.startswith("UC"):
                continue
            playlist_id = "UU" + channel_id[2:]
            page_token = ""
            page_index = 0
            channel_uploads: list[dict[str, Any]] = []
            while True:
                params: dict[str, Any] = {
                    "part": "snippet,contentDetails",
                    "playlistId": playlist_id,
                    "maxResults": 50,
                }
                if page_token:
                    params["pageToken"] = page_token
                payload = _youtube_get(
                    session,
                    quota,
                    "playlistItems",
                    "playlistItems.list",
                    params,
                )
                _store_api_cache(
                    root / "uploads" / channel_id / f"page_{page_index:04d}.json",
                    params,
                    payload,
                )
                crossed_window = False
                for item in payload.get("items") or []:
                    snippet = item.get("snippet") or {}
                    details = item.get("contentDetails") or {}
                    video_id = str(
                        details.get("videoId")
                        or (snippet.get("resourceId") or {}).get("videoId")
                        or ""
                    ).strip()
                    published_at = _parse_timestamp(
                        details.get("videoPublishedAt") or snippet.get("publishedAt")
                    )
                    if published_at and published_at.astimezone(PACIFIC) < start_pt:
                        crossed_window = True
                    if video_id and published_at and published_at.astimezone(PACIFIC) >= start_pt:
                        channel_uploads.append(
                            {
                                "video_id": video_id,
                                "title": " ".join(str(snippet.get("title") or "").split()),
                                "published_at": published_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                            }
                        )
                page_token = str(payload.get("nextPageToken") or "")
                page_index += 1
                if not page_token or crossed_window:
                    break
            uploads[channel_id] = list(
                {item["video_id"]: item for item in channel_uploads}.values()
            )

        video_ids = sorted(
            {item["video_id"] for channel_uploads in uploads.values() for item in channel_uploads}
        )
        videos: dict[str, dict[str, Any]] = {}
        for offset in range(0, len(video_ids), 50):
            chunk = video_ids[offset : offset + 50]
            params = {
                "part": "contentDetails,statistics",
                "id": ",".join(chunk),
                "maxResults": 50,
            }
            payload = _youtube_get(session, quota, "videos", "videos.list", params)
            batch_key = hashlib.sha256(",".join(chunk).encode("utf-8")).hexdigest()[:16]
            _store_api_cache(root / "video_batches" / f"{batch_key}.json", params, payload)
            for item in payload.get("items") or []:
                statistics = item.get("statistics") or {}
                videos[str(item.get("id") or "")] = {
                    "duration_s": _duration_seconds((item.get("contentDetails") or {}).get("duration")),
                    "views": _optional_int(statistics.get("viewCount")),
                    "likes": _optional_int(statistics.get("likeCount")),
                    "comments": _optional_int(statistics.get("commentCount")),
                }
    finally:
        if owns_session:
            session.close()

    manifest = {
        "version": "niche-outliers-v1",
        "niche_id": niche_row["id"],
        "niche_slug": niche_row["slug"],
        "as_of": as_of_date.isoformat(),
        "refreshed_at": datetime.now(timezone.utc).isoformat(),
        "pool_channel_ids": sorted(uploads),
        "uploads": uploads,
        "videos": videos,
        "quota": {"units_used": quota.units_used, "calls": dict(quota.calls), "cap": quota.max_units},
    }
    manifest_path = root / "niche_outliers" / str(niche) / f"{as_of_date.isoformat()}.json"
    _atomic_json(manifest_path, manifest)
    return {
        "niche": str(niche),
        "as_of": as_of_date.isoformat(),
        "channels": len(uploads),
        "videos": len(videos),
        "units_used": quota.units_used,
        "calls": dict(quota.calls),
        "cache_path": str(manifest_path),
    }


def _load_manifest(niche: str, as_of: date, cache_root: Path | None = None) -> dict[str, Any]:
    path = _cache_root(cache_root) / "niche_outliers" / niche / f"{as_of.isoformat()}.json"
    if not path.is_file():
        raise NicheOutlierError(f"fresh niche outlier cache is missing: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("version") != "niche-outliers-v1":
        raise NicheOutlierError(f"unsupported niche outlier cache: {path}")
    return payload


def compute_niche_outlier_shorts(
    niche: str = DEFAULT_NICHE_SLUG,
    as_of: date | datetime | str | None = None,
    *,
    cache_root: Path | None = None,
) -> dict[str, Any]:
    as_of_date = _as_of_date(as_of)
    manifest = _load_manifest(str(niche), as_of_date, cache_root)
    niche_row, pool = _current_pool(str(niche))
    allowed_ids = {str(row.get("youtube_channel_id") or "") for row in pool}
    window_start = datetime.combine(as_of_date - timedelta(days=30), time.min, tzinfo=PACIFIC)
    settled_through = datetime.combine(as_of_date - timedelta(days=3), time.min, tzinfo=PACIFIC)
    videos = manifest.get("videos") or {}
    eligible_channels = 0
    output: list[dict[str, Any]] = []
    for channel_id, channel_uploads in (manifest.get("uploads") or {}).items():
        if channel_id not in allowed_ids:
            continue
        shorts: list[dict[str, Any]] = []
        for upload in channel_uploads or []:
            published_at = _parse_timestamp(upload.get("published_at"))
            details = videos.get(upload.get("video_id")) or {}
            duration = details.get("duration_s")
            views = details.get("views")
            if published_at is None or duration is None or views is None:
                continue
            published_pt = published_at.astimezone(PACIFIC)
            if (
                window_start <= published_pt <= settled_through
                and int(duration) <= 180
                and not _is_excluded_short(upload.get("title"), duration)
            ):
                shorts.append({**upload, **details, "published_at_value": published_at})
        if len(shorts) < 3:
            continue
        eligible_channels += 1
        baseline = float(median(int(item["views"]) for item in shorts))
        if baseline <= 0:
            continue
        engagement_values = [
            (int(item.get("likes") or 0) + int(item.get("comments") or 0))
            / int(item["views"])
            for item in shorts
            if int(item["views"]) > 0
        ]
        baseline_engagement = (
            float(median(engagement_values)) if engagement_values else 0.0
        )
        for item in shorts:
            views = int(item["views"])
            ratio = views / baseline
            if views < 1000 or ratio < 3:
                continue
            likes = int(item.get("likes") or 0)
            comments = int(item.get("comments") or 0)
            output.append(
                {
                    "niche_id": niche_row["id"],
                    "as_of": as_of_date.isoformat(),
                    "youtube_channel_id": channel_id,
                    "video_id": item["video_id"],
                    "title": item.get("title") or "",
                    "published_at": item["published_at_value"],
                    "duration_s": int(item["duration_s"]),
                    "views": views,
                    "baseline": baseline,
                    "outlier_ratio": ratio,
                    "engagement": (likes + comments) / views,
                    "baseline_engagement": baseline_engagement,
                }
            )
    output.sort(key=lambda row: float(row["outlier_ratio"]), reverse=True)
    return {
        "niche": str(niche),
        "niche_id": niche_row["id"],
        "as_of": as_of_date.isoformat(),
        "channels_with_3_shorts": eligible_channels,
        "outlier_count": len(output),
        "rows": output,
    }


def persist_niche_outlier_shorts(result: dict[str, Any]) -> None:
    conn = get_db()
    try:
        if not table_columns(conn, "niche_outlier_shorts"):
            raise NicheOutlierError("niche_outlier_shorts migration has not been applied")
        conn.execute(
            "DELETE FROM niche_outlier_shorts WHERE niche_id = ? AND as_of = ?",
            [result["niche_id"], result["as_of"]],
        )
        for row in result.get("rows") or []:
            conn.execute(
                """
                INSERT INTO niche_outlier_shorts (
                    niche_id, as_of, youtube_channel_id, video_id, title,
                    published_at, duration_s, views, baseline, outlier_ratio,
                    engagement, baseline_engagement, computed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                [
                    row["niche_id"], row["as_of"], row["youtube_channel_id"],
                    row["video_id"], row["title"], row["published_at"],
                    row["duration_s"], row["views"], row["baseline"],
                    row["outlier_ratio"], row["engagement"],
                    row["baseline_engagement"],
                ],
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def refresh_compute_and_persist(
    niche: str = DEFAULT_NICHE_SLUG,
    as_of: date | datetime | str | None = None,
    *,
    cache_root: Path | None = None,
    max_units: int = DEFAULT_MAX_UNITS,
) -> dict[str, Any]:
    refresh = refresh_niche_outlier_cache(
        niche,
        as_of,
        cache_root=cache_root,
        max_units=max_units,
    )
    computed = compute_niche_outlier_shorts(niche, as_of, cache_root=cache_root)
    persist_niche_outlier_shorts(computed)
    return {**refresh, **computed, "rows": computed["rows"]}


def load_latest_niche_outliers(conn, niche_id: Any) -> tuple[str, list[dict[str, Any]]]:
    if not table_columns(conn, "niche_outlier_shorts"):
        return "", []
    latest = conn.execute(
        "SELECT MAX(as_of) FROM niche_outlier_shorts WHERE niche_id = ?",
        [niche_id],
    ).fetchone()
    as_of = str(latest[0]) if latest and latest[0] else ""
    if not as_of:
        return "", []
    result = conn.execute(
        """
        SELECT youtube_channel_id, video_id, title, published_at, duration_s,
               views, baseline, outlier_ratio, engagement, baseline_engagement,
               computed_at
        FROM niche_outlier_shorts
        WHERE niche_id = ? AND as_of = ?
        ORDER BY outlier_ratio DESC, views DESC, video_id ASC
        """,
        [niche_id, as_of],
    )
    columns = [str(item[0]) for item in (result.description or [])]
    return as_of, [dict(zip(columns, row)) for row in result.fetchall()]


def weekly_refresh_due(now: datetime | None = None, last_completed_at: Any = None) -> bool:
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    now_pt = instant.astimezone(PACIFIC)
    if now_pt.weekday() != 0 or now_pt.hour < 4:
        return False
    if last_completed_at is None:
        return True
    completed = _parse_timestamp(last_completed_at)
    return completed is None or completed.astimezone(PACIFIC).date() != now_pt.date()


def run_weekly_refresh_if_due(now: datetime | None = None) -> dict[str, Any] | None:
    instant = now or datetime.now(timezone.utc)
    conn = get_db_readonly()
    try:
        state = conn.execute(
            "SELECT last_tick_at FROM shorts_scheduler_state WHERE name = ? LIMIT 1",
            [SCHEDULER_STATE_NAME],
        ).fetchone()
    finally:
        conn.close()
    if not weekly_refresh_due(instant, state[0] if state else None):
        return None
    as_of = instant.astimezone(PACIFIC).date()
    # Claim the weekly attempt before making API calls. A failed run must not
    # retry every scheduler minute and consume another 500-unit allowance.
    conn = get_db()
    try:
        conn.execute(
            """
            INSERT INTO shorts_scheduler_state (name, last_tick_at, last_worker_id, updated_at)
            VALUES (?, CURRENT_TIMESTAMP, 'niche_outlier_weekly_started', CURRENT_TIMESTAMP)
            ON CONFLICT (name) DO UPDATE SET
                last_tick_at = EXCLUDED.last_tick_at,
                last_worker_id = EXCLUDED.last_worker_id,
                updated_at = CURRENT_TIMESTAMP
            """,
            [SCHEDULER_STATE_NAME],
        )
        conn.commit()
    finally:
        conn.close()
    result = refresh_compute_and_persist(DEFAULT_NICHE_SLUG, as_of)
    conn = get_db()
    try:
        conn.execute(
            """
            INSERT INTO shorts_scheduler_state (name, last_tick_at, last_worker_id, updated_at)
            VALUES (?, CURRENT_TIMESTAMP, 'niche_outlier_weekly_complete', CURRENT_TIMESTAMP)
            ON CONFLICT (name) DO UPDATE SET
                last_tick_at = EXCLUDED.last_tick_at,
                last_worker_id = EXCLUDED.last_worker_id,
                updated_at = CURRENT_TIMESTAMP
            """,
            [SCHEDULER_STATE_NAME],
        )
        conn.commit()
    finally:
        conn.close()
    return result
