#!/usr/bin/env python3
"""Build a one-off YouTube peer report for women's hormone health.

This script reads application data and uses the normal YouTube quota tracker; it
does not mutate lead or outreach records. It combines selected discovery leads
with YouTube search results, enriches every channel, classifies the channel with
the discovery ICP model, and checkpoints under the git-ignored data/research
directory.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
load_dotenv(REPO_ROOT / ".env")

from app.video_shorts import youtube_api as discovery_youtube
from app.video_shorts.config import OPENAI_MODEL, _openai_client
from app.video_shorts.services.db import get_db_readonly, table_columns
from app.video_shorts.services.youtube_quota import record_youtube_data_api_call


PACIFIC = ZoneInfo("America/Los_Angeles")
YOUTUBE_QUOTA_COSTS = {
    "channels.list": 1,
    "playlistItems.list": 1,
    "search.list": 100,
    "videos.list": 1,
}
SEARCH_CALL_LIMIT = 40
OTHER_UNIT_LIMIT = 3_000
SHELLEY_MEYER_CHANNEL_ID = "UCx-TX9EAE0NOL9hhG1wnvwg"

SEARCH_QUERIES = [
    "menopause doctor",
    "perimenopause symptoms",
    "HRT menopause",
    "menopause weight gain",
    "perimenopause anxiety",
    "menopause brain fog",
    "hot flashes treatment",
    "testosterone for women",
    "menopause specialist",
    "bioidentical hormones",
    "menopause gynecologist",
    "women's hormone health",
]

WOMEN_HORMONE_TERMS = (
    "hormone", "menopause", "perimenopause", "estrogen", "progesterone",
    "hot flash", "pcos", "testosterone",
)
OBGYN_FERTILITY_TERMS = (
    "fertility", "ivf", "conceiv", "obgyn", "gynecolog", "irregular period",
    "fibroid", "endometri", "ovulat", "vaginal", "pregnan",
)

CSV_FIELDS = [
    "youtube_channel_id",
    "channel_url",
    "source",
    "matched_queries",
    "discovery_lead_id",
    "discovery_lead_status",
    "outreach_status",
    "creator_email",
    "title",
    "handle",
    "description",
    "country",
    "published_at",
    "subscriber_count",
    "view_count",
    "video_count",
    "longform_90d",
    "shorts_90d",
    "avg_views_long_90d",
    "avg_views_short_90d",
    "last_upload_at",
    "in_niche",
    "presenter",
    "credential",
    "language",
    "classification_reason",
    "last_15_video_titles",
]


class ResearchError(RuntimeError):
    pass


@dataclass
class QuotaBudget:
    search_limit: int = SEARCH_CALL_LIMIT
    other_limit: int = OTHER_UNIT_LIMIT
    search_calls: int = 0
    other_units: int = 0
    calls: Counter = field(default_factory=Counter)

    def charge(self, method: str) -> None:
        cost = YOUTUBE_QUOTA_COSTS[method]
        if method == "search.list":
            if self.search_calls + 1 > self.search_limit:
                raise ResearchError(
                    f"YouTube search-call guard stopped before {method}: "
                    f"{self.search_calls} used of {self.search_limit}."
                )
            self.search_calls += 1
        elif self.other_units + cost > self.other_limit:
            raise ResearchError(
                f"YouTube non-search quota guard stopped before {method}: "
                f"{self.other_units} used and {cost} more would exceed {self.other_limit}."
            )
        else:
            self.other_units += cost
        self.calls[method] += 1


@dataclass
class LLMUsage:
    input_tokens: int = 0
    output_tokens: int = 0

    def add(self, response: Any) -> None:
        usage = getattr(response, "usage", None)
        self.input_tokens += int(
            getattr(usage, "prompt_tokens", None)
            or getattr(usage, "input_tokens", None)
            or 0
        )
        self.output_tokens += int(
            getattr(usage, "completion_tokens", None)
            or getattr(usage, "output_tokens", None)
            or 0
        )

    @property
    def estimated_cost_usd(self) -> float | None:
        # Keep parity with the existing discovery backfill cost report.
        prices = {"gpt-4.1-mini": (0.40, 1.60), "gpt-4o-mini": (0.15, 0.60)}
        model_prices = prices.get(OPENAI_MODEL)
        if model_prices is None:
            return None
        input_price, output_price = model_prices
        return (self.input_tokens * input_price + self.output_tokens * output_price) / 1_000_000


@dataclass
class Candidate:
    channel_id: str
    source: str
    matched_queries: list[str] = field(default_factory=list)
    discovery_lead_id: str = ""
    discovery_lead_status: str = ""
    creator_email: str = ""
    autopilot_lead_id: str = ""
    db_row: dict[str, Any] = field(default_factory=dict, repr=False)

    def add_query(self, query: str) -> None:
        clean = " ".join(str(query or "").split())
        if clean and clean not in self.matched_queries:
            self.matched_queries.append(clean)


def batched(values: Sequence[str], size: int = 50) -> Iterator[Sequence[str]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _lead_bucket_matches(row: dict[str, Any]) -> list[str]:
    raw = _json_object(row.get("raw_json"))
    recent_titles = raw.get("recent_video_titles") or raw.get("recent_titles") or []
    if not isinstance(recent_titles, list):
        recent_titles = []
    haystack = " ".join(
        [str(row.get("matched_keyword") or "")] + [str(title) for title in recent_titles[:15]]
    ).lower()
    matches = []
    if any(term in haystack for term in WOMEN_HORMONE_TERMS):
        matches.append("women_hormones")
    if any(term in haystack for term in OBGYN_FERTILITY_TERMS):
        matches.append("obgyn_fertility")
    return matches


def _english_signal(text: str) -> bool:
    words = re.findall(r"[a-z']+", str(text or "").lower())
    if not words:
        return False
    common = {
        "a", "about", "and", "are", "for", "from", "health", "how", "in",
        "is", "of", "on", "the", "this", "to", "what", "with", "women", "you", "your",
    }
    return sum(word in common for word in words) >= 2


def _lead_is_estimated_english(row: dict[str, Any]) -> bool:
    raw = _json_object(row.get("raw_json"))
    values = []
    for source in (row, raw):
        for key in (
            "estimated_language", "language_estimate", "language", "default_language",
            "default_audio_language", "is_english", "estimated_english",
        ):
            if key in source and source.get(key) not in (None, ""):
                values.append(source.get(key))
    for value in values:
        if isinstance(value, bool):
            return value
        normalized = str(value).strip().lower().replace("_", "-")
        if normalized in {"en", "en-us", "en-gb", "english", "true", "yes", "likely-english"}:
            return True
        if normalized in {"false", "no", "non-english"} or re.fullmatch(r"[a-z]{2}(?:-[a-z]{2})?", normalized):
            return False
    # The existing discovery ICP classifier makes English a hard requirement.
    if row.get("icp_fit") is True:
        return True
    recent_titles = raw.get("recent_video_titles") or raw.get("recent_titles") or []
    if not isinstance(recent_titles, list):
        recent_titles = []
    sample = " ".join(
        [str(row.get("channel_title") or ""), str(row.get("channel_description") or "")]
        + [str(title) for title in recent_titles[:15]]
    )
    return _english_signal(sample)


def _rows_as_dicts(result: Any) -> list[dict[str, Any]]:
    names = [str(item[0]) for item in (result.description or [])]
    return [dict(zip(names, row)) for row in result.fetchall()]


def _load_cached_payload(path: Path, params: dict[str, Any]) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(cached, dict) or cached.get("params") != params:
        return None
    payload = cached.get("payload")
    return payload if isinstance(payload, dict) else None


def _store_cached_payload(path: Path, params: dict[str, Any], payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps({"params": params, "payload": payload}, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(path)


def _cache_slug(value: str) -> str:
    words = "-".join(re.findall(r"[a-z0-9]+", str(value or "").lower()))[:80]
    digest = hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:10]
    return f"{words or 'item'}-{digest}"


def load_discovery_leads() -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Load lead metadata and return the two requested bucket subsets."""
    conn = get_db_readonly()
    try:
        lead_columns = table_columns(conn, "discovery_leads")
        if not lead_columns or "youtube_channel_id" not in lead_columns:
            raise ResearchError("discovery_leads.youtube_channel_id is unavailable")
        wanted = [
            "id", "youtube_channel_id", "status", "creator_email", "autopilot_lead_id",
            "is_clinician", "face_check_verdict", "icp_fit", "matched_keyword",
            "channel_title", "channel_description", "raw_json",
        ]
        selections = [f"dl.{name}" for name in wanted if name in lead_columns]
        result = conn.execute(
            f"""
            SELECT {', '.join(selections)}
            FROM discovery_leads dl
            WHERE COALESCE(dl.is_clinician, false)
              AND dl.face_check_verdict IS DISTINCT FROM 'face_fail'
            """
        )
        rows = _rows_as_dicts(result)
    finally:
        conn.close()

    by_channel: dict[str, dict[str, Any]] = {}
    bucket_rows: list[dict[str, Any]] = []
    seen_bucket_ids: set[str] = set()
    for row in rows:
        channel_id = str(row.get("youtube_channel_id") or "").strip()
        if not channel_id:
            continue
        existing = by_channel.get(channel_id)
        if existing is None:
            by_channel[channel_id] = row
        if (
            _lead_bucket_matches(row)
            and _lead_is_estimated_english(row)
            and channel_id not in seen_bucket_ids
        ):
            bucket_rows.append(row)
            seen_bucket_ids.add(channel_id)
    return by_channel, bucket_rows


def youtube_get(
    session: requests.Session,
    quota: QuotaBudget,
    endpoint: str,
    method: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    quota.charge(method)
    api_key = discovery_youtube._youtube_api_key()
    if not api_key:
        raise ResearchError("YOUTUBE_API_KEY is required; OAuth fallback is disabled")
    try:
        response = session.get(
            f"{discovery_youtube.YOUTUBE_API_BASE}/{endpoint}",
            params={**params, "key": api_key},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json() or {}
    except requests.RequestException as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        detail = f"HTTP {status}" if status is not None else exc.__class__.__name__
        raise ResearchError(f"YouTube {method} key-only request failed: {detail}") from exc
    record_youtube_data_api_call(
        feature="women_hormones_research",
        method=method,
    )
    return payload if isinstance(payload, dict) else {}


def cached_youtube_get(
    session: requests.Session,
    quota: QuotaBudget,
    endpoint: str,
    method: str,
    params: dict[str, Any],
    cache_path: Path,
    cache_only: bool = False,
) -> dict[str, Any]:
    cached = _load_cached_payload(cache_path, params)
    if cached is not None:
        return cached
    if cache_only:
        raise ResearchError(f"Required cached YouTube response is missing: {cache_path}")
    payload = youtube_get(session, quota, endpoint, method, params)
    _store_cached_payload(cache_path, params, payload)
    return payload


def search_channels(
    session: requests.Session,
    quota: QuotaBudget,
    cache_root: Path,
    cache_only: bool = False,
) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for index, query in enumerate(SEARCH_QUERIES, start=1):
        params = {
            "part": "snippet",
            "q": query,
            "type": "video",
            "maxResults": 50,
            "order": "relevance",
            "regionCode": "US",
            "relevanceLanguage": "en",
        }
        payload = cached_youtube_get(
            session,
            quota,
            "search",
            "search.list",
            params,
            cache_root / "search" / f"{_cache_slug(query)}.json",
            cache_only=cache_only,
        )
        for item in payload.get("items") or []:
            snippet = item.get("snippet") or {}
            channel_id = str(snippet.get("channelId") or "").strip()
            if not channel_id:
                continue
            found.setdefault(channel_id, [])
            if query not in found[channel_id]:
                found[channel_id].append(query)
        print(f"search {index:02d}/{len(SEARCH_QUERIES)}: {query} ({len(found)} unique channels)")
    return found


def build_candidates(
    all_leads: dict[str, dict[str, Any]],
    bucket_rows: Iterable[dict[str, Any]],
    search_results: dict[str, list[str]],
) -> dict[str, Candidate]:
    candidates: dict[str, Candidate] = {}

    def ensure(channel_id: str) -> Candidate:
        lead = all_leads.get(channel_id) or {}
        source = "db" if lead else "search_new"
        candidate = candidates.get(channel_id)
        if candidate is None:
            candidate = Candidate(
                channel_id=channel_id,
                source=source,
                discovery_lead_id=str(lead.get("id") or ""),
                discovery_lead_status=str(lead.get("status") or ""),
                creator_email=str(lead.get("creator_email") or ""),
                autopilot_lead_id=str(lead.get("autopilot_lead_id") or ""),
                db_row=lead,
            )
            candidates[channel_id] = candidate
        return candidate

    for row in bucket_rows:
        channel_id = str(row.get("youtube_channel_id") or "").strip()
        if channel_id:
            for bucket in _lead_bucket_matches(row):
                ensure(channel_id).add_query(f"db:{bucket}")
    for channel_id, queries in search_results.items():
        candidate = ensure(channel_id)
        for query in queries:
            candidate.add_query(query)
    ensure(SHELLEY_MEYER_CHANNEL_ID).add_query("required:Dr. Shelley Meyer")
    return candidates


def fetch_channel_metadata(
    session: requests.Session,
    quota: QuotaBudget,
    channel_ids: Sequence[str],
    cache_root: Path,
) -> dict[str, dict[str, Any]]:
    metadata: dict[str, dict[str, Any]] = {}
    missing_ids: list[str] = []
    item_params = {"part": "snippet,statistics,contentDetails"}
    for channel_id in channel_ids:
        cached = _load_cached_payload(cache_root / "channels" / f"{channel_id}.json", item_params)
        item = (cached or {}).get("item") if cached is not None else None
        if isinstance(item, dict):
            metadata[channel_id] = _channel_metadata_from_item(item)
        elif cached is None:
            missing_ids.append(channel_id)
    for chunk in batched(missing_ids):
        params = {
            "part": "snippet,statistics,contentDetails",
            "id": ",".join(chunk),
            "maxResults": 50,
        }
        batch_key = hashlib.sha256(",".join(chunk).encode("utf-8")).hexdigest()[:16]
        payload = cached_youtube_get(
            session,
            quota,
            "channels",
            "channels.list",
            params,
            cache_root / "channel_batches" / f"{batch_key}.json",
        )
        returned: dict[str, dict[str, Any]] = {}
        for item in payload.get("items") or []:
            channel_id = str(item.get("id") or "").strip()
            if channel_id:
                returned[channel_id] = item
                metadata[channel_id] = _channel_metadata_from_item(item)
        for channel_id in chunk:
            _store_cached_payload(
                cache_root / "channels" / f"{channel_id}.json",
                item_params,
                {"item": returned.get(channel_id)},
            )
    return metadata


def _channel_metadata_from_item(item: dict[str, Any]) -> dict[str, Any]:
    snippet = item.get("snippet") or {}
    stats = item.get("statistics") or {}
    playlists = ((item.get("contentDetails") or {}).get("relatedPlaylists") or {})
    return {
        "title": str(snippet.get("title") or ""),
        "handle": str(snippet.get("customUrl") or ""),
        "description": str(snippet.get("description") or ""),
        "country": str(snippet.get("country") or ""),
        "published_at": str(snippet.get("publishedAt") or ""),
        "subscriber_count": _optional_int(stats.get("subscriberCount")),
        "view_count": _optional_int(stats.get("viewCount")),
        "video_count": _optional_int(stats.get("videoCount")),
        "uploads_playlist_id": str(playlists.get("uploads") or ""),
    }


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


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def fetch_uploads(
    session: requests.Session,
    quota: QuotaBudget,
    channels: dict[str, dict[str, Any]],
    cutoff: datetime,
    cache_root: Path,
    show_progress: bool = True,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[str]]]:
    uploads: dict[str, list[dict[str, Any]]] = {channel_id: [] for channel_id in channels}
    titles: dict[str, list[str]] = {channel_id: [] for channel_id in channels}
    for index, (channel_id, channel) in enumerate(channels.items(), start=1):
        playlist_id = str(channel.get("uploads_playlist_id") or "").strip()
        if not playlist_id:
            continue
        page_token = ""
        page_index = 0
        crossed_cutoff = False
        while not crossed_cutoff:
            params: dict[str, Any] = {
                "part": "snippet,contentDetails",
                "playlistId": playlist_id,
                "maxResults": 50,
            }
            if page_token:
                params["pageToken"] = page_token
            payload = cached_youtube_get(
                session,
                quota,
                "playlistItems",
                "playlistItems.list",
                params,
                cache_root / "uploads" / channel_id / f"page_{page_index:04d}.json",
            )
            items = payload.get("items") or []
            for item in items:
                snippet = item.get("snippet") or {}
                details = item.get("contentDetails") or {}
                title = str(snippet.get("title") or "").strip()
                if title and len(titles[channel_id]) < 15:
                    titles[channel_id].append(title)
                video_id = str(details.get("videoId") or (snippet.get("resourceId") or {}).get("videoId") or "").strip()
                published_text = str(details.get("videoPublishedAt") or snippet.get("publishedAt") or "")
                published_at = _parse_timestamp(published_text)
                if published_at and published_at < cutoff:
                    crossed_cutoff = True
                    continue
                if video_id and published_at:
                    uploads[channel_id].append(
                        {"video_id": video_id, "title": title, "published_at": published_at}
                    )
            page_token = str(payload.get("nextPageToken") or "")
            page_index += 1
            if not page_token or (crossed_cutoff and len(titles[channel_id]) >= 15):
                break
        if show_progress and (index % 50 == 0 or index == len(channels)):
            print(f"uploads: {index}/{len(channels)} channels")
    return uploads, titles


_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)


def duration_seconds(value: Any) -> int | None:
    match = _DURATION_RE.fullmatch(str(value or "").strip())
    if not match:
        return None
    parts = {name: int(number or 0) for name, number in match.groupdict().items()}
    return parts["days"] * 86_400 + parts["hours"] * 3_600 + parts["minutes"] * 60 + parts["seconds"]


def fetch_video_details(
    session: requests.Session,
    quota: QuotaBudget,
    uploads: dict[str, list[dict[str, Any]]],
    cache_root: Path,
) -> dict[str, dict[str, Any]]:
    video_ids = list(dict.fromkeys(item["video_id"] for values in uploads.values() for item in values))
    details: dict[str, dict[str, Any]] = {}
    for chunk in batched(video_ids):
        params = {"part": "contentDetails,statistics", "id": ",".join(chunk), "maxResults": 50}
        batch_key = hashlib.sha256(",".join(chunk).encode("utf-8")).hexdigest()[:16]
        payload = cached_youtube_get(
            session,
            quota,
            "videos",
            "videos.list",
            params,
            cache_root / "video_batches" / f"{batch_key}.json",
        )
        for item in payload.get("items") or []:
            content = item.get("contentDetails") or {}
            stats = item.get("statistics") or {}
            details[str(item.get("id") or "")] = {
                "duration_seconds": duration_seconds(content.get("duration")),
                "views": _optional_int(stats.get("viewCount")),
            }
    return details


def compute_activity(
    uploads: list[dict[str, Any]], video_details: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    long_views: list[int] = []
    short_views: list[int] = []
    last_upload: datetime | None = None
    long_count = 0
    short_count = 0
    for upload in uploads:
        published_at = upload.get("published_at")
        if isinstance(published_at, datetime) and (last_upload is None or published_at > last_upload):
            last_upload = published_at
        details = video_details.get(str(upload.get("video_id") or "")) or {}
        duration = details.get("duration_seconds")
        if duration is None:
            continue
        views = details.get("views")
        if duration <= 180:
            short_count += 1
            if views is not None:
                short_views.append(int(views))
        else:
            long_count += 1
            if views is not None:
                long_views.append(int(views))
    return {
        "longform_90d": long_count,
        "shorts_90d": short_count,
        "avg_views_long_90d": round(sum(long_views) / len(long_views)) if long_views else "",
        "avg_views_short_90d": round(sum(short_views) / len(short_views)) if short_views else "",
        "last_upload_at": last_upload.isoformat().replace("+00:00", "Z") if last_upload else "",
    }


def _extract_json(text: str) -> dict[str, Any]:
    clean = str(text or "").strip()
    if clean.startswith("```"):
        clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", clean, flags=re.IGNORECASE)
    try:
        payload = json.loads(clean)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", clean, flags=re.DOTALL)
        if not match:
            raise
        payload = json.loads(match.group(0))
    if not isinstance(payload, dict):
        raise ValueError("classification was not a JSON object")
    return payload


def classify_channel(
    channel: dict[str, Any], video_titles: Sequence[str], llm_usage: LLMUsage | None = None
) -> dict[str, str]:
    if not _openai_client:
        raise ResearchError("OPENAI_API_KEY is required for classification")
    prompt = (
        "Classify this YouTube channel for a niche peer-ranking report. The niche is women's hormone health, "
        "perimenopause, or menopause; the target is English-language health professionals who personally teach "
        "on camera. Related topics such as PCOS, thyroid, osteoporosis, fertility, nutrition, and gynecology count "
        "as partial unless the channel substantially covers women's hormones/perimenopause/menopause.\n\n"
        "Return JSON only with exactly these fields:\n"
        '{"in_niche":"yes|no|partial","presenter":"real_person|org|faceless",'
        '"credential":"MD_DO|NP|PA|ND|RD|other|none","language":"...",'
        '"reason":"one line"}\n\n'
        "Use real_person only when an identifiable individual is the primary on-camera teacher. Use org for "
        "hospitals, clinics with multiple presenters, companies, media brands, or organizations. Use faceless for "
        "voiceover/animation/compilation channels without a primary visible teacher. Infer credentials conservatively.\n\n"
        f"Channel title: {str(channel.get('title') or '')[:300]}\n"
        f"Description: {str(channel.get('description') or '')[:3000]}\n"
        f"Last 15 video titles: {json.dumps(list(video_titles)[:15], ensure_ascii=False)}"
    )
    response = _openai_client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {
                "role": "system",
                "content": (
                    "You classify YouTube channels for B2B lead discovery. Be strict, concise, and return valid JSON only."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        temperature=0.1,
    )
    if llm_usage is not None:
        llm_usage.add(response)
    payload = _extract_json(response.choices[0].message.content or "")
    result = {
        "in_niche": str(payload.get("in_niche") or "").strip().lower(),
        "presenter": str(payload.get("presenter") or "").strip().lower(),
        "credential": str(payload.get("credential") or "").strip(),
        "language": str(payload.get("language") or "").strip(),
        "classification_reason": " ".join(str(payload.get("reason") or "").split())[:500],
    }
    credential_aliases = {
        "md": "MD_DO", "do": "MD_DO", "md/do": "MD_DO", "md_do": "MD_DO",
        "np": "NP", "pa": "PA", "nd": "ND", "rd": "RD",
        "other": "other", "none": "none",
    }
    result["credential"] = credential_aliases.get(result["credential"].lower(), result["credential"])
    allowed = {
        "in_niche": {"yes", "no", "partial"},
        "presenter": {"real_person", "org", "faceless"},
        "credential": {"MD_DO", "NP", "PA", "ND", "RD", "other", "none"},
    }
    for key, choices in allowed.items():
        if result[key] not in choices:
            raise ResearchError(f"Invalid {key} classification: {result[key]!r}")
    if not result["language"] or not result["classification_reason"]:
        raise ResearchError("Classifier omitted language or reason")
    return result


def load_outreach_status(candidates: dict[str, Candidate]) -> dict[str, str]:
    status = {
        channel_id: (
            "emailed"
            if candidate.discovery_lead_status.lower() in {"contacted", "converted"}
            else "has email not emailed"
            if candidate.creator_email
            else "no email"
        )
        for channel_id, candidate in candidates.items()
    }
    conn = get_db_readonly()
    try:
        autopilot_columns = table_columns(conn, "autopilot_leads")
        share_columns = table_columns(conn, "short_share_links")
        scheduled_columns = table_columns(conn, "outreach_scheduled_emails")
        if not autopilot_columns or "youtube_channel_id" not in autopilot_columns:
            return status
        channel_ids = list(candidates)
        for chunk in batched(channel_ids, 500):
            placeholders = ",".join(["?"] * len(chunk))
            email_expr = "COALESCE(l.creator_email, '')" if "creator_email" in autopilot_columns else "''"
            if share_columns and "autopilot_lead_id" in share_columns:
                emailed_expr = "sl.emailed_at" if "emailed_at" in share_columns else "NULL"
                scheduled_join = ""
                scheduled_sent_expr = "NULL"
                scheduled_status_expr = "NULL"
                if scheduled_columns and "share_link_id" in scheduled_columns:
                    scheduled_join = "LEFT JOIN outreach_scheduled_emails ose ON ose.share_link_id = sl.id"
                    if "sent_at" in scheduled_columns:
                        scheduled_sent_expr = "ose.sent_at"
                    if "status" in scheduled_columns:
                        scheduled_status_expr = "ose.status"
                query = f"""
                    SELECT l.youtube_channel_id, {email_expr} AS creator_email,
                           {emailed_expr} AS emailed_at,
                           {scheduled_sent_expr} AS scheduled_sent_at,
                           {scheduled_status_expr} AS scheduled_status
                    FROM autopilot_leads l
                    LEFT JOIN short_share_links sl
                      ON CAST(sl.autopilot_lead_id AS VARCHAR) = CAST(l.id AS VARCHAR)
                    {scheduled_join}
                    WHERE l.youtube_channel_id IN ({placeholders})
                """
            else:
                query = f"""
                    SELECT l.youtube_channel_id, {email_expr} AS creator_email,
                           NULL AS emailed_at, NULL AS scheduled_sent_at, NULL AS scheduled_status
                    FROM autopilot_leads l
                    WHERE l.youtube_channel_id IN ({placeholders})
                """
            for row in _rows_as_dicts(conn.execute(query, list(chunk))):
                channel_id = str(row.get("youtube_channel_id") or "").strip()
                if not channel_id:
                    continue
                email = str(row.get("creator_email") or "").strip()
                if email and not candidates[channel_id].creator_email:
                    candidates[channel_id].creator_email = email
                sent = bool(row.get("emailed_at") or row.get("scheduled_sent_at")) or str(
                    row.get("scheduled_status") or ""
                ).lower() == "sent"
                if sent:
                    status[channel_id] = "emailed"
                elif status[channel_id] != "emailed" and candidates[channel_id].creator_email:
                    status[channel_id] = "has email not emailed"
    finally:
        conn.close()
    return status


def write_csv(rows: list[dict[str, Any]], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def load_partial_csv(output_path: Path, candidate_ids: set[str]) -> list[dict[str, Any]]:
    if not output_path.exists():
        return []
    try:
        with output_path.open("r", encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
    except (OSError, csv.Error):
        return []
    return [
        row
        for row in rows
        if str(row.get("youtube_channel_id") or "") in candidate_ids
        and str(row.get("in_niche") or "") in {"yes", "partial", "no"}
    ]


def load_checkpoint_rows(
    output_path: Path,
    resume_from: Path | None,
    candidate_ids: set[str],
) -> list[dict[str, Any]]:
    """Merge completed rows from the destination and an earlier checkpoint."""
    merged: dict[str, dict[str, Any]] = {}
    for path in (resume_from, output_path):
        if path is None:
            continue
        for row in load_partial_csv(path, candidate_ids):
            merged[str(row["youtube_channel_id"])] = row
    return list(merged.values())


def print_summary(
    rows: list[dict[str, Any]], quota: QuotaBudget, llm_usage: LLMUsage, output_path: Path
) -> None:
    source_counts = Counter(row["source"] for row in rows)
    niche_counts = Counter(row["in_niche"] for row in rows)
    presenter_counts = Counter(row["presenter"] for row in rows)
    credential_counts = Counter(row["credential"] for row in rows)
    print("\nSummary")
    print(f"  CSV: {output_path}")
    print(f"  Total candidates: {len(rows)}")
    print(f"  Source: db={source_counts['db']}, search_new={source_counts['search_new']}")
    print(
        "  In niche: "
        f"yes={niche_counts['yes']}, partial={niche_counts['partial']}, no={niche_counts['no']}"
    )
    print(f"  Real person: {presenter_counts['real_person']}")
    print("  By credential: " + ", ".join(f"{key}={credential_counts[key]}" for key in sorted(credential_counts)))
    call_summary = ", ".join(f"{key}={quota.calls[key]}" for key in sorted(quota.calls))
    print(f"  YouTube search calls: {quota.search_calls}/{quota.search_limit}")
    print(f"  YouTube other units: {quota.other_units}/{quota.other_limit} ({call_summary})")
    if llm_usage.estimated_cost_usd is None:
        cost_text = "unavailable for configured model"
    else:
        cost_text = f"${llm_usage.estimated_cost_usd:.6f} estimated"
    print(
        f"  LLM: model={OPENAI_MODEL}, input_tokens={llm_usage.input_tokens}, "
        f"output_tokens={llm_usage.output_tokens}, cost={cost_text}"
    )


def run(
    output_path: Path,
    *,
    resume_from: Path | None = None,
    cached_search_only: bool = False,
    expected_completed: int | None = None,
    expected_total: int | None = None,
) -> list[dict[str, Any]]:
    if not _openai_client:
        raise ResearchError("OPENAI_API_KEY is required")

    quota = QuotaBudget()
    llm_usage = LLMUsage()
    cache_root = output_path.parent / "cache"
    session = requests.Session()
    session.headers.update({"User-Agent": "minti-studio-women-hormones-research/1.0"})

    all_leads, bucket_rows = load_discovery_leads()
    print(f"database candidates: {len(bucket_rows)}")
    if not discovery_youtube._youtube_api_key():
        raise ResearchError("YOUTUBE_API_KEY is required; OAuth fallback is disabled")
    print("YouTube auth: API key only (OAuth fallback disabled)")
    search_results = search_channels(
        session,
        quota,
        cache_root,
        cache_only=cached_search_only,
    )
    candidates = build_candidates(all_leads, bucket_rows, search_results)
    print(f"deduplicated candidates: {len(candidates)}")
    if expected_total is not None and len(candidates) != expected_total:
        raise ResearchError(
            f"Candidate guard expected {expected_total} total rows, found {len(candidates)}"
        )

    channel_ids = list(candidates)
    rows = load_checkpoint_rows(output_path, resume_from, set(channel_ids))
    completed_ids = {str(row.get("youtube_channel_id") or "") for row in rows}
    if expected_completed is not None and len(completed_ids) != expected_completed:
        raise ResearchError(
            f"Checkpoint guard expected {expected_completed} completed rows, found {len(completed_ids)}"
        )
    write_csv(rows, output_path)
    if rows:
        print(f"resuming completed CSV rows: {len(rows)}")
    print(f"remaining candidates: {len(channel_ids) - len(completed_ids)}")

    channels = fetch_channel_metadata(session, quota, channel_ids, cache_root)
    missing = [channel_id for channel_id in channel_ids if channel_id not in channels]
    if missing:
        print(
            f"warning: channels.list returned no metadata for {len(missing)} candidate(s); "
            "keeping them with database metadata"
        )
        for channel_id in missing:
            lead = candidates[channel_id].db_row
            channels[channel_id] = {
                "title": str(lead.get("channel_title") or ""),
                "handle": "",
                "description": str(lead.get("channel_description") or ""),
                "country": "",
                "published_at": "",
                "subscriber_count": "",
                "view_count": "",
                "video_count": "",
                "uploads_playlist_id": "",
            }
    if SHELLEY_MEYER_CHANNEL_ID not in channels:
        raise ResearchError("Required Dr. Shelley Meyer channel was not returned by channels.list")

    outreach = load_outreach_status(candidates)
    cutoff = datetime.now(timezone.utc) - timedelta(days=90)
    for index, channel_id in enumerate(channel_ids, start=1):
        if channel_id in completed_ids:
            continue
        candidate = candidates[channel_id]
        channel = channels[channel_id]
        channel_uploads, channel_titles = fetch_uploads(
            session,
            quota,
            {channel_id: channel},
            cutoff,
            cache_root,
            show_progress=False,
        )
        video_details = fetch_video_details(
            session,
            quota,
            channel_uploads,
            cache_root,
        )
        classification = classify_channel(
            channel, channel_titles.get(channel_id) or [], llm_usage
        )
        activity = compute_activity(channel_uploads.get(channel_id) or [], video_details)
        rows.append(
            {
                "youtube_channel_id": channel_id,
                "channel_url": f"https://www.youtube.com/channel/{channel_id}",
                "source": candidate.source,
                "matched_queries": " | ".join(candidate.matched_queries),
                "discovery_lead_id": candidate.discovery_lead_id,
                "discovery_lead_status": candidate.discovery_lead_status,
                "outreach_status": outreach[channel_id],
                "creator_email": candidate.creator_email,
                **{key: channel.get(key, "") for key in (
                    "title", "handle", "description", "country", "published_at",
                    "subscriber_count", "view_count", "video_count",
                )},
                **activity,
                **classification,
                "last_15_video_titles": " | ".join(channel_titles.get(channel_id) or []),
            }
        )
        write_csv(rows, output_path)
        if index % 25 == 0 or index == len(channel_ids):
            print(
                f"completed: {len(rows)}/{len(channel_ids)} "
                f"(search_calls={quota.search_calls}, other_units={quota.other_units})"
            )

    rows.sort(
        key=lambda row: (
            {"yes": 0, "partial": 1, "no": 2}.get(str(row["in_niche"]), 3),
            -int(row["subscriber_count"] or 0),
            str(row["title"]).lower(),
        )
    )
    write_csv(rows, output_path)
    print_summary(rows, quota, llm_usage, output_path)
    return rows


def main() -> int:
    default_date = datetime.now(PACIFIC).date().isoformat()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=REPO_ROOT / "data" / "research" / f"women_hormones_{default_date}.csv",
        help="CSV output path (default: data/research/women_hormones_<Pacific date>.csv)",
    )
    parser.add_argument(
        "--resume-from",
        type=Path,
        help="Earlier progressive CSV whose completed rows should seed the output",
    )
    parser.add_argument(
        "--cached-search-only",
        action="store_true",
        help="Require all search responses to be cached; never issue search.list",
    )
    parser.add_argument(
        "--expected-completed",
        type=int,
        help="Stop unless this many completed checkpoint rows were loaded",
    )
    parser.add_argument(
        "--expected-total",
        type=int,
        help="Stop unless this many deduplicated candidates were reconstructed",
    )
    args = parser.parse_args()
    try:
        run(
            args.output.resolve(),
            resume_from=args.resume_from.resolve() if args.resume_from else None,
            cached_search_only=args.cached_search_only,
            expected_completed=args.expected_completed,
            expected_total=args.expected_total,
        )
    except (ResearchError, requests.RequestException) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
