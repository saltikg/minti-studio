#!/usr/bin/env python3
"""Build a one-off YouTube peer report for women's hormone health.

This script is intentionally read-only with respect to the application database.
It combines selected discovery leads with YouTube search results, enriches every
channel, classifies the channel with the discovery ICP model, and writes a CSV
under the git-ignored data/research directory.
"""

from __future__ import annotations

import argparse
import csv
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

from app.video_shorts.config import OPENAI_MODEL, YOUTUBE_API_KEY, _openai_client
from app.video_shorts.services.db import get_db_readonly, table_columns


PACIFIC = ZoneInfo("America/Los_Angeles")
YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"
YOUTUBE_QUOTA_LIMIT = 5_000
YOUTUBE_QUOTA_COSTS = {
    "channels.list": 1,
    "playlistItems.list": 1,
    "search.list": 100,
    "videos.list": 1,
}
SHELLEY_MEYER_CHANNEL_ID = "UCx-TX9EAE0NOL9hhG1wnvwg"
BUCKET_NAMES = {"women_hormones", "obgyn_fertility"}

SEARCH_QUERIES = [
    "menopause doctor",
    "perimenopause symptoms",
    "menopause weight gain",
    "hormone replacement therapy",
    "HRT menopause",
    "perimenopause anxiety",
    "menopause brain fog",
    "menopause sleep",
    "estrogen women",
    "progesterone",
    "testosterone for women",
    "hot flashes treatment",
    "PCOS doctor",
    "thyroid women",
    "women's hormone health",
    "midlife women health",
    "menopause nutrition",
    "bioidentical hormones",
    "vaginal dryness menopause",
    "osteoporosis women",
    "menopause gynecologist",
    "menopause specialist",
    "perimenopause periods",
    "cortisol women",
    "hormone imbalance women",
]

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
    limit: int = YOUTUBE_QUOTA_LIMIT
    used: int = 0
    calls: Counter = field(default_factory=Counter)

    def charge(self, method: str) -> None:
        cost = YOUTUBE_QUOTA_COSTS[method]
        # The task requires strictly less than 5,000 units.
        if self.used + cost >= self.limit:
            raise ResearchError(
                f"YouTube quota guard stopped before {method}: "
                f"{self.used} used and {cost} more would reach/exceed {self.limit}."
            )
        self.used += cost
        self.calls[method] += 1


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


def _normalized_bucket(value: Any) -> str:
    return "_".join(re.findall(r"[a-z0-9]+", str(value or "").lower()))


def _lead_bucket(row: dict[str, Any]) -> str:
    values = [row.get("research_category")]
    raw = _json_object(row.get("raw_json"))
    for key in ("keyword_bucket", "bucket", "category", "keyword_category"):
        values.append(raw.get(key))
    for value in values:
        normalized = _normalized_bucket(value)
        if normalized in BUCKET_NAMES:
            return normalized
    return ""


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


def load_discovery_leads() -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Load lead metadata and return the two requested bucket subsets."""
    conn = get_db_readonly()
    try:
        lead_columns = table_columns(conn, "discovery_leads")
        if not lead_columns or "youtube_channel_id" not in lead_columns:
            raise ResearchError("discovery_leads.youtube_channel_id is unavailable")
        wanted = [
            "id", "youtube_channel_id", "status", "creator_email", "autopilot_lead_id",
            "is_clinician", "face_check_verdict", "icp_fit", "channel_title", "channel_description", "raw_json",
        ]
        selections = [f"dl.{name}" for name in wanted if name in lead_columns]
        can_join_categories = bool(table_columns(conn, "keyword_queue")) and bool(
            table_columns(conn, "discovery_categories")
        ) and "matched_keyword" in lead_columns
        if can_join_categories:
            selections.append("c.name AS research_category")
            joins = """
                LEFT JOIN keyword_queue q ON lower(q.keyword) = lower(dl.matched_keyword)
                LEFT JOIN discovery_categories c ON c.id = q.category_id
            """
        else:
            selections.append("NULL AS research_category")
            joins = ""
        result = conn.execute(f"SELECT {', '.join(selections)} FROM discovery_leads dl {joins}")
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
        if existing is None or (not existing.get("research_category") and row.get("research_category")):
            by_channel[channel_id] = row
        if (
            _lead_bucket(row)
            and bool(row.get("is_clinician"))
            and str(row.get("face_check_verdict") or "").strip().lower() != "face_fail"
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
    response = session.get(
        f"{YOUTUBE_API_BASE}/{endpoint}",
        params={**params, "key": YOUTUBE_API_KEY},
        timeout=30,
    )
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        detail = ""
        try:
            detail = str((response.json().get("error") or {}).get("message") or "")
        except (ValueError, AttributeError):
            pass
        raise ResearchError(f"YouTube {method} failed ({response.status_code}): {detail or exc}") from exc
    payload = response.json()
    return payload if isinstance(payload, dict) else {}


def search_channels(session: requests.Session, quota: QuotaBudget) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for index, query in enumerate(SEARCH_QUERIES, start=1):
        # One mixed search covers direct channel hits and video->channel hits.
        # Splitting them would cost 5,000 units before any enrichment.
        payload = youtube_get(
            session,
            quota,
            "search",
            "search.list",
            {
                "part": "snippet",
                "q": query,
                "type": "channel,video",
                "maxResults": 50,
                "order": "relevance",
                "regionCode": "US",
                "relevanceLanguage": "en",
            },
        )
        for item in payload.get("items") or []:
            item_id = item.get("id") or {}
            snippet = item.get("snippet") or {}
            channel_id = str(item_id.get("channelId") or snippet.get("channelId") or "").strip()
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
            ensure(channel_id).add_query(f"db:{_lead_bucket(row)}")
    for channel_id, queries in search_results.items():
        candidate = ensure(channel_id)
        for query in queries:
            candidate.add_query(query)
    ensure(SHELLEY_MEYER_CHANNEL_ID).add_query("required:Dr. Shelley Meyer")
    return candidates


def fetch_channel_metadata(
    session: requests.Session, quota: QuotaBudget, channel_ids: Sequence[str]
) -> dict[str, dict[str, Any]]:
    metadata: dict[str, dict[str, Any]] = {}
    for chunk in batched(channel_ids):
        payload = youtube_get(
            session,
            quota,
            "channels",
            "channels.list",
            {"part": "snippet,statistics,contentDetails", "id": ",".join(chunk), "maxResults": 50},
        )
        for item in payload.get("items") or []:
            channel_id = str(item.get("id") or "").strip()
            snippet = item.get("snippet") or {}
            stats = item.get("statistics") or {}
            playlists = ((item.get("contentDetails") or {}).get("relatedPlaylists") or {})
            metadata[channel_id] = {
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
    return metadata


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
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[str]]]:
    uploads: dict[str, list[dict[str, Any]]] = {channel_id: [] for channel_id in channels}
    titles: dict[str, list[str]] = {channel_id: [] for channel_id in channels}
    for index, (channel_id, channel) in enumerate(channels.items(), start=1):
        playlist_id = str(channel.get("uploads_playlist_id") or "").strip()
        if not playlist_id:
            continue
        page_token = ""
        crossed_cutoff = False
        while not crossed_cutoff:
            params: dict[str, Any] = {
                "part": "snippet,contentDetails",
                "playlistId": playlist_id,
                "maxResults": 50,
            }
            if page_token:
                params["pageToken"] = page_token
            payload = youtube_get(session, quota, "playlistItems", "playlistItems.list", params)
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
            if not page_token or (crossed_cutoff and len(titles[channel_id]) >= 15):
                break
        if index % 50 == 0 or index == len(channels):
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
) -> dict[str, dict[str, Any]]:
    video_ids = list(dict.fromkeys(item["video_id"] for values in uploads.values() for item in values))
    details: dict[str, dict[str, Any]] = {}
    for chunk in batched(video_ids):
        payload = youtube_get(
            session,
            quota,
            "videos",
            "videos.list",
            {"part": "contentDetails,statistics", "id": ",".join(chunk), "maxResults": 50},
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


def classify_channel(channel: dict[str, Any], video_titles: Sequence[str]) -> dict[str, str]:
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


def print_summary(rows: list[dict[str, Any]], quota: QuotaBudget, output_path: Path) -> None:
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
    print(f"  YouTube API units used: {quota.used} ({call_summary})")


def run(output_path: Path) -> list[dict[str, Any]]:
    if not YOUTUBE_API_KEY:
        raise ResearchError("YOUTUBE_API_KEY is required")
    if not _openai_client:
        raise ResearchError("OPENAI_API_KEY is required")

    quota = QuotaBudget()
    session = requests.Session()
    session.headers.update({"User-Agent": "minti-studio-women-hormones-research/1.0"})

    all_leads, bucket_rows = load_discovery_leads()
    print(f"database candidates: {len(bucket_rows)}")
    search_results = search_channels(session, quota)
    candidates = build_candidates(all_leads, bucket_rows, search_results)
    print(f"deduplicated candidates: {len(candidates)}")

    channel_ids = list(candidates)
    channels = fetch_channel_metadata(session, quota, channel_ids)
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

    cutoff = datetime.now(timezone.utc) - timedelta(days=90)
    uploads, titles = fetch_uploads(session, quota, channels, cutoff)
    video_details = fetch_video_details(session, quota, uploads)
    outreach = load_outreach_status(candidates)

    rows: list[dict[str, Any]] = []
    for index, channel_id in enumerate(channel_ids, start=1):
        candidate = candidates[channel_id]
        channel = channels[channel_id]
        classification = classify_channel(channel, titles.get(channel_id) or [])
        activity = compute_activity(uploads.get(channel_id) or [], video_details)
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
                "last_15_video_titles": " | ".join(titles.get(channel_id) or []),
            }
        )
        if index % 25 == 0 or index == len(channel_ids):
            print(f"classifications: {index}/{len(channel_ids)}")

    rows.sort(
        key=lambda row: (
            {"yes": 0, "partial": 1, "no": 2}.get(str(row["in_niche"]), 3),
            -int(row["subscriber_count"] or 0),
            str(row["title"]).lower(),
        )
    )
    write_csv(rows, output_path)
    print_summary(rows, quota, output_path)
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
    args = parser.parse_args()
    try:
        run(args.output.resolve())
    except (ResearchError, requests.RequestException) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
