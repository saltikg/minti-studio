from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import math
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

import requests

from app.video_shorts.config import OPENAI_MODEL, _openai_client
from app.video_shorts.services.db import get_db, get_db_readonly, table_columns
from app.video_shorts.services.niches import final_niche_pool, load_niche_snapshot
from app.video_shorts.services.niche_definitions import definition_prompt, load_niche_definition
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
ON_TOPIC_DEFINITION = (
    "A Short is on-topic when it is mainly about perimenopause, menopause, HRT, female "
    "sex hormones (estrogen, progesterone, or testosterone in women), or midlife women's "
    "health that menopause/hormone clinicians commonly cover: bone density, heart health, "
    "sleep, brain or mood, skin, sexual health, or weight/metabolism in women 40+. Pregnancy, "
    "fertility, general fitness, diet or fasting programs, non-women-specific health, and "
    "unrelated specialties are off-topic."
)
TOPIC_STATUSES = {"on_topic", "adjacent", "off_topic"}
REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CACHE_ROOT = REPO_ROOT / "data" / "research" / "cache"
logger = logging.getLogger(__name__)


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
        return niche, final_niche_pool(rows, clinicians_only=False, niche=niche)
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
                "part": "snippet,contentDetails,statistics",
                "id": ",".join(chunk),
                "maxResults": 50,
            }
            payload = _youtube_get(session, quota, "videos", "videos.list", params)
            batch_key = hashlib.sha256(",".join(chunk).encode("utf-8")).hexdigest()[:16]
            _store_api_cache(root / "video_batches" / f"{batch_key}.json", params, payload)
            for item in payload.get("items") or []:
                statistics = item.get("statistics") or {}
                snippet = item.get("snippet") or {}
                videos[str(item.get("id") or "")] = {
                    "duration_s": _duration_seconds((item.get("contentDetails") or {}).get("duration")),
                    "views": _optional_int(statistics.get("viewCount")),
                    "likes": _optional_int(statistics.get("likeCount")),
                    "comments": _optional_int(statistics.get("commentCount")),
                    "description": str(snippet.get("description") or "")[:500],
                    "tags": [str(tag) for tag in (snippet.get("tags") or [])[:30]],
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
                    "video_description": str(item.get("description") or "")[:500],
                    "hashtags": list(item.get("tags") or []),
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
    outlier_columns = table_columns(conn, "niche_outlier_shorts")
    if not outlier_columns:
        return "", []
    latest = conn.execute(
        "SELECT MAX(as_of) FROM niche_outlier_shorts WHERE niche_id = ?",
        [niche_id],
    ).fetchone()
    as_of = str(latest[0]) if latest and latest[0] else ""
    if not as_of:
        return "", []
    if "in_category" in outlier_columns:
        llm_topic_column = "outlier.in_category AS llm_in_category"
    elif "topic_status" in outlier_columns:
        llm_topic_column = (
            "CASE WHEN outlier.topic_status = 'off_topic' THEN FALSE "
            "WHEN outlier.topic_status IS NOT NULL THEN TRUE END AS llm_in_category"
        )
    elif "on_topic" in outlier_columns:
        llm_topic_column = (
            "CASE WHEN outlier.on_topic IS TRUE THEN TRUE "
            "WHEN outlier.on_topic IS FALSE THEN FALSE END AS llm_in_category"
        )
    else:
        llm_topic_column = "NULL AS llm_in_category"
    has_overrides = bool(table_columns(conn, "niche_outlier_short_topic_overrides"))
    override_join = (
        "LEFT JOIN niche_outlier_short_topic_overrides topic_override "
        "ON topic_override.niche_id = outlier.niche_id "
        "AND topic_override.video_id = outlier.video_id"
        if has_overrides else ""
    )
    override_column = (
        "topic_override.topic_status AS topic_override"
        if has_overrides else "NULL AS topic_override"
    )
    result = conn.execute(
        f"""
        SELECT outlier.youtube_channel_id, outlier.video_id, outlier.title,
               outlier.published_at, outlier.duration_s,
               views, baseline, outlier_ratio, engagement, baseline_engagement,
               computed_at, {llm_topic_column}, {override_column}
        FROM niche_outlier_shorts outlier
        {override_join}
        WHERE outlier.niche_id = ? AND outlier.as_of = ?
        ORDER BY outlier_ratio DESC, views DESC, outlier.video_id ASC
        """,
        [niche_id, as_of],
    )
    columns = [str(item[0]) for item in (result.description or [])]
    rows = [dict(zip(columns, row)) for row in result.fetchall()]
    for row in rows:
        override = str(row.get("topic_override") or "")
        if override in {"on_topic", "off_topic"}:
            in_category = override == "on_topic"
        else:
            in_category = row.get("llm_in_category") is not False
        row["in_category"] = in_category
        row["on_topic"] = in_category
        row["topic_status"] = "on_topic" if in_category else "off_topic"
    return as_of, rows


def _pattern_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    prices = {
        "gpt-4.1-mini": (0.40, 1.60),
        "gpt-4o-mini": (0.15, 0.60),
    }
    input_price, output_price = prices.get(model, (0.0, 0.0))
    return (input_tokens * input_price + output_tokens * output_price) / 1_000_000


def customer_visible_outlier_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    eligible = []
    for row in rows:
        if row.get("in_category", row.get("on_topic")) is False or int(row.get("views") or 0) < 5_000:
            continue
        engagement = float(row.get("engagement") or 0)
        baseline_engagement = float(row.get("baseline_engagement") or 0)
        if baseline_engagement > 0 and engagement < baseline_engagement / 3:
            continue
        eligible.append(row)
    eligible.sort(
        key=lambda row: (-int(row.get("views") or 0), str(row.get("video_id") or ""))
    )
    channel_counts: dict[str, int] = {}
    output = []
    for row in eligible:
        channel_id = str(row.get("youtube_channel_id") or "")
        if channel_counts.get(channel_id, 0) >= 2:
            continue
        channel_counts[channel_id] = channel_counts.get(channel_id, 0) + 1
        output.append(row)
        if len(output) == 6:
            break
    return output


def _whole_percentile(values: list[int], percentile: float) -> int:
    ordered = sorted(int(value) for value in values)
    if not ordered:
        raise NicheOutlierError("cannot calculate a percentile without values")
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    interpolated = ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)
    return int(round(interpolated))


def _pattern_topics(value: Any, titles: list[str]) -> list[str]:
    text = str(value or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise NicheOutlierError("pattern generator returned invalid JSON") from exc
    topics = parsed.get("topics") if isinstance(parsed, dict) else None
    if not isinstance(topics, list) or not 2 <= len(topics) <= 3:
        raise NicheOutlierError("pattern generator must return 2–3 topic labels")
    cleaned = [" ".join(str(topic).strip().split()) for topic in topics]
    if any(topic != topic.casefold() or not 2 <= len(topic.split()) <= 4 for topic in cleaned):
        raise NicheOutlierError("pattern topic labels must be lowercase and 2–4 words")
    return cleaned


def generate_niche_outlier_pattern(
    result: dict[str, Any],
    *,
    client=None,
) -> dict[str, Any] | None:
    source_input = (
        result.get("pattern_rows") if "pattern_rows" in result else result.get("rows")
    )
    source_rows = customer_visible_outlier_rows(source_input or [])
    if not source_rows:
        return None
    selected_client = client or _openai_client
    if selected_client is None:
        raise NicheOutlierError("OpenAI client is not configured")
    titles = [str(row.get("title") or "").strip() for row in source_rows]
    title_lines = "\n".join(f"- {title}" for title in titles)
    durations = [int(row.get("duration_s") or 0) for row in source_rows]
    p25 = _whole_percentile(durations, 0.25)
    p75 = _whole_percentile(durations, 0.75)
    response = selected_client.chat.completions.create(
        model=OPENAI_MODEL,
        temperature=0,
        max_tokens=60,
        response_format={"type": "json_object"},
        messages=[
            {
                "role": "system",
                "content": (
                    "Return JSON only with exactly this shape: {\"topics\":[\"label one\","
                    "\"label two\"]}. Return 2–3 topic labels, each lowercase and 2–4 words, "
                    "taken from the supplied titles. Do not return sentences or medical advice."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Customer-page outlier Short titles:\n{title_lines}"
                ),
            },
        ],
    )
    content = response.choices[0].message.content if response.choices else ""
    usage = getattr(response, "usage", None)
    input_tokens = int(
        getattr(usage, "prompt_tokens", 0) or getattr(usage, "input_tokens", 0) or 0
    )
    output_tokens = int(
        getattr(usage, "completion_tokens", 0) or getattr(usage, "output_tokens", 0) or 0
    )
    topics = _pattern_topics(content, titles)
    return {
        "niche_id": result["niche_id"],
        "as_of": result["as_of"],
        "pattern_sentence": (
            f"Most-watched this week: {', '.join(topics)}. "
            f"Most winners ran {p25}–{p75}s."
        ),
        "model": OPENAI_MODEL,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "estimated_cost_usd": _pattern_cost_usd(OPENAI_MODEL, input_tokens, output_tokens),
        "source_count": len(source_rows),
    }


def _cached_channel_description(channel_id: str, cache_root: Path | None = None) -> str:
    path = _cache_root(cache_root) / "channels" / f"{channel_id}.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return ""
    payload = raw.get("payload") if isinstance(raw, dict) else {}
    item = (payload or {}).get("item") or {}
    return " ".join(str((item.get("snippet") or {}).get("description") or "").split())[:5000]


def _cached_video_snippets(
    video_ids: set[str], cache_root: Path | None = None
) -> dict[str, dict[str, Any]]:
    root = _cache_root(cache_root)
    paths = [
        *sorted((root / "video_batches").glob("*.json")),
        *sorted((root / "niche_expansion" / "pool_video_details").glob("*.json")),
        *sorted((root / "videos").glob("*.json")),
    ]
    snippets: dict[str, dict[str, Any]] = {}
    for path in paths:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        payload = raw.get("payload") if isinstance(raw, dict) else {}
        for item in (payload or {}).get("items") or []:
            video_id = str(item.get("id") or "")
            snippet = item.get("snippet") or {}
            if video_id in video_ids and snippet:
                snippets[video_id] = snippet
    return snippets


def _in_category(value: Any) -> bool:
    text = str(value or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise NicheOutlierError("topic classifier returned invalid JSON") from exc
    result = parsed.get("in_category") if isinstance(parsed, dict) else None
    if type(result) is not bool:
        raise NicheOutlierError("topic classifier must return an in_category boolean")
    return result


def _topic_overrides(niche_id: Any) -> dict[str, str]:
    if niche_id is None:
        return {}
    conn = get_db_readonly()
    try:
        if not table_columns(conn, "niche_outlier_short_topic_overrides"):
            return {}
        rows = conn.execute(
            """
            SELECT video_id, topic_status
            FROM niche_outlier_short_topic_overrides
            WHERE niche_id = ?
            """,
            [niche_id],
        ).fetchall()
        return {str(row[0]): str(row[1]) for row in rows}
    finally:
        conn.close()


def classify_niche_outlier_topics(
    result: dict[str, Any],
    *,
    cache_root: Path | None = None,
    client=None,
) -> dict[str, Any]:
    selected_client = client or _openai_client
    if selected_client is None:
        raise NicheOutlierError("OpenAI client is not configured")
    decisions = []
    input_tokens = 0
    output_tokens = 0
    rows = result.get("rows") or []
    snippets = _cached_video_snippets(
        {str(row.get("video_id") or "") for row in rows}, cache_root
    )
    overrides = _topic_overrides(result.get("niche_id"))
    definition = result.get("definition")
    if definition is None and result.get("niche_id") is not None:
        conn = get_db_readonly()
        try:
            definition = load_niche_definition(conn, niche_id=result["niche_id"])
        finally:
            conn.close()
    if not definition:
        raise NicheOutlierError("stored niche definition is required")
    stored_definition = definition_prompt(definition)
    for row in rows:
        channel_id = str(row.get("youtube_channel_id") or "")
        video_id = str(row.get("video_id") or "")
        snippet = snippets.get(video_id) or {}
        video_description = str(
            row.get("video_description") or snippet.get("description") or ""
        ).strip()[:500]
        tag_values = [
            str(tag).strip() for tag in (row.get("hashtags") or snippet.get("tags") or [])
            if str(tag).strip()
        ]
        extracted = re.findall(
            r"(?<!\w)#[\w-]+",
            f"{row.get('title') or ''} {video_description}",
            flags=re.UNICODE,
        )
        hashtags = list(dict.fromkeys([*tag_values, *extracted]))[:30]
        response = selected_client.chat.completions.create(
            model=OPENAI_MODEL,
            temperature=0,
            max_tokens=30,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Decide whether this Short belongs in the supplied category definition. "
                        "Return JSON only: {\"in_category\":true|false}. Use the definition as "
                        "the sole taxonomy source; core and adjacent topics count as in-category."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Short title: {str(row.get('title') or '').strip()}\n"
                        + (f"Video description: {video_description}\n" if video_description else "")
                        + (f"Hashtags: {', '.join(hashtags)}\n" if hashtags else "")
                        + f"Stored category definition: {stored_definition}"
                    ),
                },
            ],
        )
        content = response.choices[0].message.content if response.choices else ""
        llm_in_category = _in_category(content)
        previous_in_category = row.get("in_category", row.get("on_topic")) is not False
        override = str(row.get("topic_override") or overrides.get(video_id) or "")
        effective_in_category = (
            override == "on_topic"
            if override in {"on_topic", "off_topic"}
            else llm_in_category
        )
        row["llm_in_category"] = llm_in_category
        row["topic_override"] = override or None
        row["in_category"] = effective_in_category
        row["on_topic"] = effective_in_category
        row["topic_status"] = "on_topic" if effective_in_category else "off_topic"
        usage = getattr(response, "usage", None)
        input_tokens += int(
            getattr(usage, "prompt_tokens", 0) or getattr(usage, "input_tokens", 0) or 0
        )
        output_tokens += int(
            getattr(usage, "completion_tokens", 0) or getattr(usage, "output_tokens", 0) or 0
        )
        decisions.append(
            {
                "video_id": video_id,
                "title": str(row.get("title") or ""),
                "previous_in_category": previous_in_category,
                "llm_in_category": llm_in_category,
                "topic_override": override or None,
                "in_category": effective_in_category,
            }
        )
    return {
        "decisions": decisions,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "estimated_cost_usd": _pattern_cost_usd(OPENAI_MODEL, input_tokens, output_tokens),
    }


def persist_niche_outlier_topics(result: dict[str, Any]) -> None:
    conn = get_db()
    try:
        columns = table_columns(conn, "niche_outlier_shorts")
        if "in_category" not in columns:
            raise NicheOutlierError("niche_outlier_shorts.in_category migration has not been applied")
        for row in result.get("rows") or []:
            conn.execute(
                """
                UPDATE niche_outlier_shorts
                SET in_category = ?, on_topic = ?, topic_status = ?
                WHERE niche_id = ? AND as_of = ? AND video_id = ?
                """,
                [
                    row.get("llm_in_category"), row.get("on_topic"),
                    "on_topic" if row.get("llm_in_category") else "off_topic", result["niche_id"],
                    result["as_of"], row["video_id"],
                ],
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def classify_and_persist_latest_topics(
    niche: str = DEFAULT_NICHE_SLUG,
    *,
    cache_root: Path | None = None,
    client=None,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    conn = get_db_readonly()
    try:
        niche_row = conn.execute(
            "SELECT id FROM niches WHERE slug = ? LIMIT 1", [niche]
        ).fetchone()
        if not niche_row:
            raise NicheOutlierError(f"unknown niche: {niche}")
        as_of, rows = load_latest_niche_outliers(conn, niche_row[0])
    finally:
        conn.close()
    if not as_of:
        return None
    result = {"niche": niche, "niche_id": niche_row[0], "as_of": as_of, "rows": rows}
    usage = classify_niche_outlier_topics(result, cache_root=cache_root, client=client)
    persist_niche_outlier_topics(result)
    _niche_row, pool = _current_pool(niche)
    pool_ids = {str(row.get("youtube_channel_id") or "") for row in pool}
    result["pattern_rows"] = [
        row for row in rows
        if str(row.get("youtube_channel_id") or "") in pool_ids
    ]
    return result, usage


def persist_niche_outlier_pattern(pattern: dict[str, Any]) -> None:
    conn = get_db()
    try:
        if not table_columns(conn, "niche_outlier_patterns"):
            raise NicheOutlierError("niche_outlier_patterns migration has not been applied")
        conn.execute(
            """
            INSERT INTO niche_outlier_patterns (
                niche_id, as_of, pattern_sentence, model, input_tokens,
                output_tokens, estimated_cost_usd, computed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT (niche_id, as_of) DO UPDATE SET
                pattern_sentence = EXCLUDED.pattern_sentence,
                model = EXCLUDED.model,
                input_tokens = EXCLUDED.input_tokens,
                output_tokens = EXCLUDED.output_tokens,
                estimated_cost_usd = EXCLUDED.estimated_cost_usd,
                computed_at = CURRENT_TIMESTAMP
            """,
            [
                pattern["niche_id"], pattern["as_of"], pattern["pattern_sentence"],
                pattern["model"], pattern["input_tokens"], pattern["output_tokens"],
                pattern["estimated_cost_usd"],
            ],
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def generate_and_persist_latest_pattern(
    niche: str = DEFAULT_NICHE_SLUG,
    *,
    client=None,
) -> dict[str, Any] | None:
    conn = get_db_readonly()
    try:
        niche_row = conn.execute(
            "SELECT id FROM niches WHERE slug = ? LIMIT 1",
            [niche],
        ).fetchone()
        if not niche_row:
            raise NicheOutlierError(f"unknown niche: {niche}")
        as_of, rows = load_latest_niche_outliers(conn, niche_row[0])
    finally:
        conn.close()
    if not as_of:
        return None
    _niche_row, pool = _current_pool(niche)
    pool_ids = {str(row.get("youtube_channel_id") or "") for row in pool}
    pattern = generate_niche_outlier_pattern(
        {
            "niche_id": niche_row[0],
            "as_of": as_of,
            "rows": rows,
            "pattern_rows": [
                row for row in rows
                if str(row.get("youtube_channel_id") or "") in pool_ids
            ],
        },
        client=client,
    )
    if pattern:
        persist_niche_outlier_pattern(pattern)
    return pattern


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
    try:
        topic_usage = classify_niche_outlier_topics(result)
        persist_niche_outlier_topics(result)
        result["topic_classification"] = topic_usage
        pattern = generate_niche_outlier_pattern(result)
        if pattern:
            persist_niche_outlier_pattern(pattern)
            result["pattern"] = pattern
    except Exception as exc:
        logger.exception("Weekly niche outlier pattern generation failed: %s", exc)
        result["pattern_error"] = str(exc)
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
