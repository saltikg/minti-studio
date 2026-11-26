#!/usr/bin/env python3
"""Grow the menopause-hormones niche with cached, resumable YouTube research."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv


REPO_ROOT = Path(__file__).resolve().parents[2]
RESEARCH_DIR = Path(__file__).resolve().parent
for path in (REPO_ROOT, RESEARCH_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
load_dotenv(REPO_ROOT / ".env")

import niche_women_hormones as base
import reclassify_women_hormones as strict
from app.video_shorts.config import OPENAI_MODEL, _openai_client
from app.video_shorts.services.db import get_db, get_db_readonly
from app.video_shorts.services.niches import (
    NICHE_CLINICIAN_CREDENTIALS,
    final_niche_pool,
    is_final_niche_pool_row,
    load_niche_snapshot,
)


PACIFIC = ZoneInfo("America/Los_Angeles")
FEATURE = "women_hormones_growth_v2"
CHECKPOINT_VERSION = "women-hormones-growth-v2"
RULE_VERSION = "strict-menopause-longform-v3-audience-v1"
SEARCH_CALL_LIMIT = 60
OTHER_UNIT_LIMIT = 3_000
MAX_GUEST_SEARCHES = 30
ALLOWED_COUNTRIES = {"", "US", "CA", "GB", "AU", "NZ", "IE"}
SEARCH_V2_QUERIES = [
    "perimenopause doctor",
    "menopause hormone therapy explained",
    "HRT risks benefits",
    "menopause weight gain doctor",
    "perimenopause symptoms doctor explains",
    "menopause joint pain",
    "menopause sleep insomnia doctor",
    "vaginal estrogen",
    "testosterone therapy women",
    "progesterone perimenopause",
    "menopause bone loss",
    "menopause heart health women",
    "surgical menopause",
    "premature ovarian insufficiency",
    "menopause specialist MSCP",
]
SOURCE_ORDER = ("snowball_featured", "snowball_guest", "search_v2")

# The normal tracker already treats unknown methods as one unit. The research
# guard needs the explicit cost as well.
base.YOUTUBE_QUOTA_COSTS["channelSections.list"] = 1


class GrowthError(RuntimeError):
    pass


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def load_checkpoint(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "version": CHECKPOINT_VERSION,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "candidate_sources": {},
            "rows": {},
            "quota": {"search_calls": 0, "other_units": 0, "calls": {}},
        }
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("version") != CHECKPOINT_VERSION:
        raise GrowthError(f"incompatible checkpoint: {path}")
    return payload


def restore_quota(checkpoint: dict[str, Any]) -> base.QuotaBudget:
    saved = checkpoint.get("quota") or {}
    quota = base.QuotaBudget(search_limit=SEARCH_CALL_LIMIT, other_limit=OTHER_UNIT_LIMIT)
    quota.search_calls = int(saved.get("search_calls") or 0)
    quota.other_units = int(saved.get("other_units") or 0)
    quota.calls.update(saved.get("calls") or {})
    return quota


def save_checkpoint(
    path: Path,
    checkpoint: dict[str, Any],
    quota: base.QuotaBudget,
    usage: strict.Usage,
) -> None:
    checkpoint["quota"] = {
        "search_calls": quota.search_calls,
        "other_units": quota.other_units,
        "calls": dict(quota.calls),
    }
    checkpoint["llm_usage"] = {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
    }
    atomic_json(path, checkpoint)


def current_context() -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], set[str]]:
    conn = get_db_readonly()
    try:
        niche, rows = load_niche_snapshot(conn, "menopause_hormones")
        if not niche or not rows:
            raise GrowthError("menopause_hormones snapshot is missing")
        pool = final_niche_pool(rows, clinicians_only=False)
        existing = {
            str(row[0])
            for row in conn.execute(
                "SELECT youtube_channel_id FROM niche_channels WHERE niche_id=?",
                [niche["id"]],
            ).fetchall()
        }
        return niche, rows, pool, existing
    finally:
        conn.close()


def cached_channel(cache_root: Path, channel_id: str) -> dict[str, Any]:
    params = {"part": "snippet,statistics,contentDetails"}
    payload = base._load_cached_payload(cache_root / "channels" / f"{channel_id}.json", params)
    item = (payload or {}).get("item") or {}
    return base._channel_metadata_from_item(item) if item else {}


def guest_cache_key(channel_id: str, description: str, titles: list[str]) -> str:
    source = {"channel_id": channel_id, "description": description[:5000], "titles": titles[:15]}
    return hashlib.sha256(
        json.dumps(source, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def extract_guest_names(
    channel_id: str,
    description: str,
    titles: list[str],
    cache_root: Path,
    usage: strict.Usage,
) -> list[str]:
    cache_path = cache_root / "growth_v2" / "guest_names" / f"{channel_id}.json"
    input_hash = guest_cache_key(channel_id, description, titles)
    if cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if cached.get("version") == "guest-health-professionals-v1" and cached.get("input_hash") == input_hash:
                return [str(name).strip() for name in cached.get("names") or [] if str(name).strip()]
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    if not _openai_client:
        raise GrowthError("OPENAI_API_KEY is required")
    prompt = (
        "Extract guest names that look like health professionals from this YouTube channel description "
        "and ordered JSON list of long-form video titles. Include names in patterns such as 'with Dr. Doug "
        "Lucas' or 'Dr. Fran Haydanek'. Do not include the channel host unless a title explicitly presents "
        "them as a guest. Return JSON only as {\"names\":[\"Dr. Full Name\"]}. Return an empty list when unsure.\n\n"
        f"Description:\n{description[:5000]}\n\nTitles:\n{json.dumps(titles[:15], ensure_ascii=False)}"
    )
    last_error: Exception | None = None
    raw_names: list[Any] | None = None
    for _attempt in range(3):
        try:
            response = _openai_client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": "Extract named health-professional guests. Return valid JSON only."},
                    {"role": "user", "content": prompt},
                ],
                response_format={"type": "json_object"},
                temperature=0,
            )
            usage.add(response)
            payload = strict._extract_json(response.choices[0].message.content or "")
            values = payload.get("names") or []
            if not isinstance(values, list):
                raise GrowthError(f"guest extractor returned non-list for {channel_id}")
            raw_names = values
            break
        except Exception as exc:
            last_error = exc
    if raw_names is None:
        raise GrowthError(f"guest extraction failed for {channel_id}: {last_error}")
    names: list[str] = []
    for value in raw_names:
        name = " ".join(str(value or "").split())[:160]
        if name and name.casefold() not in {item.casefold() for item in names}:
            names.append(name)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(
        cache_path,
        {
            "version": "guest-health-professionals-v1",
            "model": OPENAI_MODEL,
            "input_hash": input_hash,
            "names": names,
        },
    )
    return names


def normalized_person_name(value: str) -> str:
    words = re.findall(r"[a-z0-9]+", value.lower())
    ignored = {"dr", "doctor", "md", "do", "np", "pa", "rd", "nd", "phd", "prof", "professor"}
    return " ".join(word for word in words if word not in ignored)


def title_contains_name(title: str, name: str) -> bool:
    clean_name = normalized_person_name(name)
    clean_title = normalized_person_name(title)
    return bool(clean_name and clean_name in clean_title)


def add_candidate(
    candidates: dict[str, dict[str, Any]],
    channel_id: str,
    source: str,
    detail: str,
    existing: set[str],
) -> None:
    channel_id = str(channel_id or "").strip()
    if not channel_id or channel_id in existing:
        return
    candidate = candidates.setdefault(channel_id, {"sources": [], "details": []})
    if source not in candidate["sources"]:
        candidate["sources"].append(source)
    if detail and detail not in candidate["details"]:
        candidate["details"].append(detail)


def discover_candidates(
    *,
    pool: list[dict[str, Any]],
    existing: set[str],
    session: requests.Session,
    quota: base.QuotaBudget,
    cache_root: Path,
    video_details: dict[str, dict[str, int | None]],
    usage: strict.Usage,
) -> dict[str, dict[str, Any]]:
    candidates: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(pool, start=1):
        channel_id = str(row["youtube_channel_id"])
        params = {"part": "snippet,contentDetails", "channelId": channel_id}
        try:
            payload = base.cached_youtube_get(
                session,
                quota,
                "channelSections",
                "channelSections.list",
                params,
                cache_root / "growth_v2" / "channel_sections" / f"{channel_id}.json",
            )
        except base.YouTubeRequestError as exc:
            if exc.global_failure:
                raise
            print(f"featured sections skipped {channel_id}: {exc}", flush=True)
            continue
        for item in payload.get("items") or []:
            if str((item.get("snippet") or {}).get("type") or "") != "multipleChannels":
                continue
            details = item.get("contentDetails") or {}
            for featured_id in details.get("channels") or []:
                add_candidate(candidates, featured_id, "snowball_featured", channel_id, existing)
        if index % 10 == 0 or index == len(pool):
            print(f"featured discovery {index}/{len(pool)}", flush=True)

    guest_names: list[str] = []
    seen_names: set[str] = set()

    def extract(row: dict[str, Any]) -> list[str]:
        channel_id = str(row["youtube_channel_id"])
        metadata = cached_channel(cache_root, channel_id)
        titles = strict.load_cached_longform_titles(cache_root, channel_id, video_details)
        return extract_guest_names(
            channel_id,
            str(metadata.get("description") or ""),
            titles,
            cache_root,
            usage,
        )

    with ThreadPoolExecutor(max_workers=6) as executor:
        for names in executor.map(extract, pool):
            for name in names:
                key = normalized_person_name(name)
                if key and key not in seen_names:
                    seen_names.add(key)
                    guest_names.append(name)
    searched_names = guest_names[:MAX_GUEST_SEARCHES]
    print(f"guest names extracted={len(guest_names)} searched={len(searched_names)}", flush=True)
    for name in searched_names:
        params = {
            "part": "snippet",
            "q": name,
            "type": "channel",
            "maxResults": 5,
            "order": "relevance",
            "regionCode": "US",
            "relevanceLanguage": "en",
        }
        payload = base.cached_youtube_get(
            session,
            quota,
            "search",
            "search.list",
            params,
            cache_root / "growth_v2" / "guest_search" / f"{base._cache_slug(name)}.json",
        )
        items = payload.get("items") or []
        if not items:
            continue
        snippet = items[0].get("snippet") or {}
        channel_id = str((items[0].get("id") or {}).get("channelId") or snippet.get("channelId") or "")
        title = str(snippet.get("title") or "")
        if title_contains_name(title, name):
            add_candidate(candidates, channel_id, "snowball_guest", name, existing)

    for index, query in enumerate(SEARCH_V2_QUERIES, start=1):
        params = {
            "part": "snippet",
            "q": query,
            "type": "video",
            "maxResults": 50,
            "order": "relevance",
            "regionCode": "US",
            "relevanceLanguage": "en",
        }
        payload = base.cached_youtube_get(
            session,
            quota,
            "search",
            "search.list",
            params,
            cache_root / "growth_v2" / "search_v2" / f"{base._cache_slug(query)}.json",
        )
        for item in payload.get("items") or []:
            snippet = item.get("snippet") or {}
            add_candidate(
                candidates,
                str(snippet.get("channelId") or ""),
                "search_v2",
                query,
                existing,
            )
        print(f"search_v2 {index}/{len(SEARCH_V2_QUERIES)} unique={len(candidates)}", flush=True)
    return candidates


def enrich_channel(
    *,
    channel_id: str,
    channel: dict[str, Any],
    session: requests.Session,
    quota: base.QuotaBudget,
    cache_root: Path,
    cutoff: datetime,
    scan_past_cutoff_for_long_titles: bool = True,
) -> dict[str, Any]:
    playlist_id = str(channel.get("uploads_playlist_id") or "").strip()
    if not playlist_id:
        return {"enrich_status": "no_public_uploads", "long_titles": [], "activity": {}, "views_90d": 0}
    page_token = ""
    page_index = 0
    crossed_cutoff = False
    long_titles: list[str] = []
    recent_uploads: list[dict[str, Any]] = []
    details_by_id: dict[str, dict[str, Any]] = {}
    while True:
        params: dict[str, Any] = {
            "part": "snippet,contentDetails",
            "playlistId": playlist_id,
            "maxResults": 50,
        }
        if page_token:
            params["pageToken"] = page_token
        payload = base.cached_youtube_get(
            session,
            quota,
            "playlistItems",
            "playlistItems.list",
            params,
            cache_root / "uploads" / channel_id / f"page_{page_index:04d}.json",
        )
        page_uploads: list[dict[str, Any]] = []
        for item in payload.get("items") or []:
            snippet = item.get("snippet") or {}
            content = item.get("contentDetails") or {}
            video_id = str(content.get("videoId") or (snippet.get("resourceId") or {}).get("videoId") or "").strip()
            published_at = base._parse_timestamp(content.get("videoPublishedAt") or snippet.get("publishedAt"))
            title = " ".join(str(snippet.get("title") or "").split())
            if video_id:
                page_uploads.append({"video_id": video_id, "title": title, "published_at": published_at})
            if published_at and published_at < cutoff:
                crossed_cutoff = True
        details_by_id.update(
            base.fetch_video_details(session, quota, {channel_id: page_uploads}, cache_root)
        )
        for upload in page_uploads:
            details = details_by_id.get(upload["video_id"]) or {}
            if details.get("duration_seconds") is not None and details["duration_seconds"] > 180:
                if upload["title"] and len(long_titles) < 15:
                    long_titles.append(upload["title"])
            published_at = upload.get("published_at")
            if isinstance(published_at, datetime) and published_at >= cutoff:
                recent_uploads.append(upload)
        page_token = str(payload.get("nextPageToken") or "")
        page_index += 1
        if not page_token or (
            crossed_cutoff
            and (len(long_titles) >= 15 or not scan_past_cutoff_for_long_titles)
        ):
            break
    activity = base.compute_activity(recent_uploads, details_by_id)
    views_90d = sum(
        int((details_by_id.get(upload["video_id"]) or {}).get("views") or 0)
        for upload in recent_uploads
    )
    return {
        "enrich_status": "ok",
        "long_titles": long_titles,
        "activity": activity,
        "views_90d": views_90d,
    }


def validate_identity(payload: dict[str, Any]) -> dict[str, str]:
    presenter = str(payload.get("presenter") or "").strip().lower()
    credential = str(payload.get("credential") or "").strip()
    aliases = {"md": "MD_DO", "do": "MD_DO", "md/do": "MD_DO", "md_do": "MD_DO"}
    credential = aliases.get(credential.lower(), credential)
    if presenter not in {"real_person", "org", "faceless"}:
        raise GrowthError(f"invalid presenter: {presenter!r}")
    if credential not in {"MD_DO", "NP", "PA", "ND", "RD", "other", "none"}:
        # The niche schema deliberately groups credentials outside its clinician
        # enum (for example PhD, RN, DPT, DC) under "other".
        credential = "other"
    return {"presenter": presenter, "credential": credential}


def classify_identity(
    *,
    channel_id: str,
    channel: dict[str, Any],
    titles: list[str],
    cache_root: Path,
    usage: strict.Usage,
) -> dict[str, str]:
    source = {
        "title": str(channel.get("title") or "")[:300],
        "description": str(channel.get("description") or "")[:5000],
        "titles": titles[:15],
    }
    input_hash = hashlib.sha256(
        json.dumps(source, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    cache_path = cache_root / "growth_v2" / "identity" / f"{channel_id}.json"
    if cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if cached.get("version") == "niche-identity-v1" and cached.get("input_hash") == input_hash:
                return validate_identity(cached.get("result") or {})
        except (OSError, ValueError, json.JSONDecodeError, GrowthError):
            pass
    prompt = (
        "Classify who presents this health YouTube channel and their professional credential. Return JSON only: "
        '{"presenter":"real_person|org|faceless","credential":"MD_DO|NP|PA|ND|RD|other|none"}. '
        "Use real_person only for an identifiable primary on-camera teacher. Clinics, hospitals, companies, media "
        "brands, and multi-presenter organizations are org. Infer credentials conservatively.\n\n"
        f"Channel title: {source['title']}\nDescription: {source['description']}\n"
        f"Long-form titles: {json.dumps(titles[:15], ensure_ascii=False)}"
    )
    last_error: Exception | None = None
    result: dict[str, str] | None = None
    for _attempt in range(3):
        try:
            response = _openai_client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": "Classify channel identity. Return valid JSON only."},
                    {"role": "user", "content": prompt},
                ],
                response_format={"type": "json_object"},
                temperature=0,
            )
            usage.add(response)
            result = validate_identity(strict._extract_json(response.choices[0].message.content or ""))
            break
        except Exception as exc:
            last_error = exc
    if result is None:
        raise GrowthError(f"identity classification failed for {channel_id}: {last_error}")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(
        cache_path,
        {"version": "niche-identity-v1", "model": OPENAI_MODEL, "input_hash": input_hash, "result": result},
    )
    return result


def classify_row(
    channel_id: str,
    row: dict[str, Any],
    cache_root: Path,
    usage: strict.Usage,
) -> dict[str, Any]:
    channel = row["channel"]
    titles = list(row.get("long_titles") or [])[:15]
    strict_result, _ = strict.classify(
        channel_id=channel_id,
        description=str(channel.get("description") or ""),
        titles=titles,
        cache_dir=cache_root / "strict_reclassifications",
        usage=usage,
    )
    identity = classify_identity(
        channel_id=channel_id,
        channel=channel,
        titles=titles,
        cache_root=cache_root,
        usage=usage,
    )
    audience_language, _ = strict.classify_audience_language(
        channel_id=channel_id,
        titles=titles,
        cache_dir=cache_root / "audience_languages",
        usage=usage,
    )
    return {
        **row,
        "primary_niche": strict_result["primary_niche"],
        "on_topic_share": strict_result["on_topic_share"],
        "on_topic_count": strict_result["on_topic_count"],
        "entity": strict_result["entity"],
        "reclass_reason": strict_result["reclass_reason"],
        "presenter": identity["presenter"],
        "credential": identity["credential"],
        "audience_language": audience_language,
        "classification_complete": True,
    }


def optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def database_row(channel_id: str, row: dict[str, Any], snapshot_date: str) -> dict[str, Any]:
    channel = row["channel"]
    activity = row.get("activity") or {}
    share = float(row.get("on_topic_share") or 0)
    in_niche = "yes" if row.get("primary_niche") == "menopause_hormones" and share >= strict.NICHE_ON_TOPIC_MIN else "partial" if row.get("primary_niche") == "menopause_hormones" else "no"
    return {
        "youtube_channel_id": channel_id,
        "snapshot_date": snapshot_date,
        "title": channel.get("title") or channel_id,
        "channel_url": f"https://www.youtube.com/channel/{channel_id}",
        "credential": row.get("credential") or "none",
        "in_niche": in_niche,
        "presenter": row.get("presenter") or "faceless",
        "language": "English" if row.get("audience_language") == "english" else str(row.get("audience_language") or ""),
        "subscriber_count": optional_int(channel.get("subscriber_count")),
        "longform_90d": int(activity.get("longform_90d") or 0),
        "shorts_90d": int(activity.get("shorts_90d") or 0),
        "avg_views_long_90d": optional_int(activity.get("avg_views_long_90d")),
        "avg_views_short_90d": optional_int(activity.get("avg_views_short_90d")),
        "source": row["source"],
        "primary_niche": row.get("primary_niche"),
        "on_topic_share": share,
        "on_topic_count": int(row.get("on_topic_count") or 0),
        "entity": row.get("entity"),
        "reclass_reason": row.get("reclass_reason"),
        "views_90d": int(row.get("views_90d") or 0),
        "country": str(channel.get("country") or "").upper(),
        "audience_language": row.get("audience_language"),
    }


def insert_rows_and_history(
    niche_id: Any,
    snapshot_date: str,
    rows: dict[str, dict[str, Any]],
) -> int:
    conn = get_db()
    try:
        for channel_id, source_row in rows.items():
            row = database_row(channel_id, source_row, snapshot_date)
            conn.execute(
                """
                INSERT INTO niche_channels (
                    niche_id, youtube_channel_id, snapshot_date, title, channel_url,
                    credential, in_niche, presenter, language, subscriber_count,
                    longform_90d, shorts_90d, avg_views_long_90d, avg_views_short_90d,
                    discovery_lead_id, source, replied, admin_note, updated_at,
                    primary_niche, on_topic_share, on_topic_count, entity, reclass_reason,
                    views_90d, admin_exclude, admin_include, exclude_reason, country,
                    audience_language
                ) VALUES (
                    ?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?,FALSE,'',now(),
                    ?,?,?,?,?,?,FALSE,FALSE,NULL,?,?
                )
                ON CONFLICT (niche_id, youtube_channel_id, snapshot_date) DO UPDATE SET
                    title=EXCLUDED.title, channel_url=EXCLUDED.channel_url,
                    credential=EXCLUDED.credential, in_niche=EXCLUDED.in_niche,
                    presenter=EXCLUDED.presenter, language=EXCLUDED.language,
                    subscriber_count=EXCLUDED.subscriber_count,
                    longform_90d=EXCLUDED.longform_90d, shorts_90d=EXCLUDED.shorts_90d,
                    avg_views_long_90d=EXCLUDED.avg_views_long_90d,
                    avg_views_short_90d=EXCLUDED.avg_views_short_90d,
                    source=EXCLUDED.source, primary_niche=EXCLUDED.primary_niche,
                    on_topic_share=EXCLUDED.on_topic_share,
                    on_topic_count=EXCLUDED.on_topic_count, entity=EXCLUDED.entity,
                    reclass_reason=EXCLUDED.reclass_reason, views_90d=EXCLUDED.views_90d,
                    country=EXCLUDED.country, audience_language=EXCLUDED.audience_language,
                    updated_at=now()
                """,
                [
                    niche_id, channel_id, snapshot_date, row["title"], row["channel_url"],
                    row["credential"], row["in_niche"], row["presenter"], row["language"],
                    row["subscriber_count"], row["longform_90d"], row["shorts_90d"],
                    row["avg_views_long_90d"], row["avg_views_short_90d"], row["source"],
                    row["primary_niche"], row["on_topic_share"], row["on_topic_count"],
                    row["entity"], row["reclass_reason"], row["views_90d"], row["country"],
                    row["audience_language"],
                ],
            )
        result = conn.execute(
            "SELECT * FROM niche_channels WHERE niche_id=? AND snapshot_date=?",
            [niche_id, snapshot_date],
        )
        names = [str(item[0]) for item in (result.description or [])]
        all_rows = [dict(zip(names, item)) for item in result.fetchall()]
        run_id = conn.execute(
            """
            INSERT INTO niche_classification_runs (niche_id, run_at, rule_version, notes)
            VALUES (?, now(), ?, ?) RETURNING run_id
            """,
            [niche_id, RULE_VERSION, "snowball + search_v2"],
        ).fetchone()[0]
        for row in all_rows:
            conn.execute(
                """
                INSERT INTO niche_channel_run_results
                    (run_id,youtube_channel_id,in_pool,primary_niche,on_topic_share,reason)
                VALUES (?,?,?,?,?,?)
                """,
                [
                    run_id, row["youtube_channel_id"], is_final_niche_pool_row(row),
                    row.get("primary_niche"), float(row.get("on_topic_share") or 0),
                    row.get("reclass_reason"),
                ],
            )
        conn.commit()
        return int(run_id)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def report(
    *,
    niche_id: Any,
    snapshot_date: str,
    new_ids: set[str],
    candidates: dict[str, dict[str, Any]],
    quota: base.QuotaBudget,
    usage: strict.Usage,
    run_id: int,
) -> None:
    conn = get_db_readonly()
    try:
        niche, rows = load_niche_snapshot(conn, "menopause_hormones", snapshot_date)
    finally:
        conn.close()
    pool_all = final_niche_pool(rows, clinicians_only=False)
    pool_clinicians = final_niche_pool(rows, clinicians_only=True)
    joined = [row for row in pool_all if str(row["youtube_channel_id"]) in new_ids]
    new_rows = [row for row in rows if str(row["youtube_channel_id"]) in new_ids]
    near_misses = [
        row for row in new_rows
        if str(row.get("primary_niche") or "") == "menopause_hormones"
        and float(row.get("on_topic_share") or 0) < strict.NICHE_ON_TOPIC_MIN
    ]
    source_counts = Counter(candidates[channel_id]["source"] for channel_id in candidates)
    print("GROWTH_COMPLETE")
    print("candidates_by_source=" + ",".join(f"{source}:{source_counts[source]}" for source in SOURCE_ORDER))
    print(f"new_candidates={len(new_ids)} joined={len(joined)} new_near_misses={len(near_misses)}")
    print(f"pool_clinicians={len(pool_clinicians)} pool_all={len(pool_all)}")
    print("joined_channels=")
    for row in sorted(joined, key=lambda item: -int(item.get("subscriber_count") or 0)):
        denominator = len(candidates[str(row["youtube_channel_id"])].get("long_titles") or [])
        print(
            f"  {row['title']} | {row.get('credential') or 'none'} | "
            f"{int(row.get('subscriber_count') or 0)} | {row.get('source')} | "
            f"{int(row.get('on_topic_count') or 0)}/{denominator}"
        )
    shelley_index = next(
        index for index, row in enumerate(pool_clinicians)
        if str(row["youtube_channel_id"]) == base.SHELLEY_MEYER_CHANNEL_ID
    )
    print(f"shelley_rank={shelley_index + 1}")
    for rank, row in enumerate(
        pool_clinicians[max(0, shelley_index - 2):shelley_index + 3],
        start=max(0, shelley_index - 2) + 1,
    ):
        print(
            f"  rank={rank} title={row['title']} subscribers={int(row.get('subscriber_count') or 0)} "
            f"views_90d={int(row.get('views_90d') or 0)}"
        )
    print(f"search_calls={quota.search_calls}/{quota.search_limit}")
    print(f"other_units={quota.other_units}/{quota.other_limit}")
    print("youtube_calls=" + ",".join(f"{key}:{quota.calls[key]}" for key in sorted(quota.calls)))
    cost = usage.estimated_cost()
    print(f"llm_model={OPENAI_MODEL} input_tokens={usage.input_tokens} output_tokens={usage.output_tokens}")
    print(f"llm_cost_usd={cost:.6f}" if cost is not None else "llm_cost_usd=unknown")
    print(f"classification_run_id={run_id}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--checkpoint",
        type=Path,
        default=REPO_ROOT / "data" / "research" / f"women_hormones_growth_{datetime.now(PACIFIC).date().isoformat()}.json",
    )
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()
    try:
        base.research_youtube_api_key()
    except RuntimeError as exc:
        raise GrowthError(str(exc)) from exc
    if not _openai_client:
        raise GrowthError("OPENAI_API_KEY is required")

    checkpoint_path = args.checkpoint.expanduser().resolve()
    checkpoint = load_checkpoint(checkpoint_path)
    quota = restore_quota(checkpoint)
    saved_usage = checkpoint.get("llm_usage") or {}
    usage = strict.Usage(
        input_tokens=int(saved_usage.get("input_tokens") or 0),
        output_tokens=int(saved_usage.get("output_tokens") or 0),
    )
    cache_root = REPO_ROOT / "data" / "research" / "cache"
    session = requests.Session()
    session.headers.update({"User-Agent": "minti-studio-women-hormones-growth-v2/1.0"})
    niche, _snapshot_rows, pool, existing = current_context()
    snapshot_date = str(niche["snapshot_date"])
    if len(pool) != 56:
        print(f"warning: expected 56 current pool channels, found {len(pool)}", flush=True)
    print(f"current_pool={len(pool)} existing_niche_channels={len(existing)} snapshot={snapshot_date}")

    candidates = checkpoint.get("candidate_sources") or {}
    if not candidates:
        video_details = strict.load_video_details(cache_root)
        candidates = discover_candidates(
            pool=pool,
            existing=existing,
            session=session,
            quota=quota,
            cache_root=cache_root,
            video_details=video_details,
            usage=usage,
        )
        for candidate in candidates.values():
            candidate["source"] = next(source for source in SOURCE_ORDER if source in candidate["sources"])
        checkpoint["candidate_sources"] = candidates
        save_checkpoint(checkpoint_path, checkpoint, quota, usage)
    else:
        print(f"resuming candidate checkpoint: {len(candidates)}")

    candidate_ids = sorted(candidates)
    channels, statuses = base.fetch_channel_metadata(session, quota, candidate_ids, cache_root)
    rows: dict[str, dict[str, Any]] = checkpoint.setdefault("rows", {})
    cutoff = datetime.now(timezone.utc) - timedelta(days=90)
    for index, channel_id in enumerate(candidate_ids, start=1):
        if rows.get(channel_id, {}).get("enrichment_complete"):
            continue
        channel = channels.get(channel_id)
        if not channel:
            rows[channel_id] = {
                **candidates[channel_id],
                "channel": {"title": channel_id, "country": ""},
                "enrich_status": statuses.get(channel_id, "channel_not_found"),
                "long_titles": [],
                "activity": {},
                "views_90d": 0,
                "enrichment_complete": True,
            }
        else:
            try:
                enriched = enrich_channel(
                    channel_id=channel_id,
                    channel=channel,
                    session=session,
                    quota=quota,
                    cache_root=cache_root,
                    cutoff=cutoff,
                )
            except base.YouTubeRequestError as exc:
                if exc.global_failure:
                    raise
                enriched = {
                    "enrich_status": "no_public_uploads" if exc.status_code == 404 else "error",
                    "long_titles": [], "activity": {}, "views_90d": 0,
                }
                print(f"enrichment skipped {channel_id}: {exc}", flush=True)
            rows[channel_id] = {
                **candidates[channel_id],
                "channel": channel,
                **enriched,
                "enrichment_complete": True,
            }
        save_checkpoint(checkpoint_path, checkpoint, quota, usage)
        if index % 20 == 0 or index == len(candidate_ids):
            print(f"enriched {index}/{len(candidate_ids)} other_units={quota.other_units}", flush=True)

    pending = [channel_id for channel_id in candidate_ids if not rows[channel_id].get("classification_complete")]
    worker_count = max(1, min(int(args.workers), 10))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {
            channel_id: executor.submit(classify_row, channel_id, rows[channel_id], cache_root, usage)
            for channel_id in pending
        }
        for index, channel_id in enumerate(pending, start=1):
            rows[channel_id] = futures[channel_id].result()
            save_checkpoint(checkpoint_path, checkpoint, quota, usage)
            if index % 10 == 0 or index == len(pending):
                print(f"classified {index}/{len(pending)}", flush=True)

    if checkpoint.get("classification_run_id"):
        run_id = int(checkpoint["classification_run_id"])
    else:
        run_id = insert_rows_and_history(niche["id"], snapshot_date, rows)
        checkpoint["classification_run_id"] = run_id
        save_checkpoint(checkpoint_path, checkpoint, quota, usage)
    report(
        niche_id=niche["id"],
        snapshot_date=snapshot_date,
        new_ids=set(candidate_ids),
        candidates=rows,
        quota=quota,
        usage=usage,
        run_id=run_id,
    )
    print(f"checkpoint={checkpoint_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
