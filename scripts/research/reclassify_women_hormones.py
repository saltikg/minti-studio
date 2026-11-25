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
from app.video_shorts.services.niches import (
    NICHE_AUDIENCE_COUNTRIES,
    NICHE_ON_TOPIC_MIN,
    clinician_matches,
    is_final_niche_pool_row,
)
from app.video_shorts.services.youtube_quota import (
    YoutubeQuotaCapExceeded,
    record_youtube_data_api_call,
    require_youtube_quota_available,
)


CLASSIFIER_VERSION = "strict-menopause-longform-v3"
AUDIENCE_CLASSIFIER_VERSION = "niche-audience-language-v1"
RULE_VERSION = "strict-menopause-longform-v3-audience-v1"
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


_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)


def _duration_seconds(value: Any) -> int | None:
    match = _DURATION_RE.fullmatch(str(value or "").strip())
    if not match:
        return None
    parts = {name: int(number or 0) for name, number in match.groupdict().items()}
    return parts["days"] * 86_400 + parts["hours"] * 3_600 + parts["minutes"] * 60 + parts["seconds"]


def load_video_details(cache_root: Path) -> dict[str, dict[str, int | None]]:
    details: dict[str, dict[str, int | None]] = {}
    for path in sorted((cache_root / "video_batches").glob("*.json")):
        for item in _cache_payload(path).get("items") or []:
            video_id = str(item.get("id") or "").strip()
            raw_views = ((item.get("statistics") or {}).get("viewCount"))
            duration = _duration_seconds((item.get("contentDetails") or {}).get("duration"))
            try:
                views = int(raw_views) if raw_views is not None else None
            except (TypeError, ValueError):
                views = None
            if video_id:
                details[video_id] = {"views": views, "duration_seconds": duration}
    return details


def load_video_views(cache_root: Path) -> dict[str, int]:
    return {
        video_id: int(item["views"])
        for video_id, item in load_video_details(cache_root).items()
        if item.get("views") is not None
    }


def load_cached_country(cache_root: Path, channel_id: str) -> str:
    item = _cache_payload(cache_root / "channels" / f"{channel_id}.json").get("item") or {}
    return str((item.get("snippet") or {}).get("country") or "").strip().upper()


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


def load_cached_longform_titles(
    cache_root: Path,
    channel_id: str,
    video_details: dict[str, dict[str, int | None]],
) -> list[str]:
    titles: list[str] = []
    upload_dir = cache_root / "uploads" / channel_id
    for path in sorted(upload_dir.glob("page_*.json")):
        for item in _cache_payload(path).get("items") or []:
            snippet = item.get("snippet") or {}
            content = item.get("contentDetails") or {}
            video_id = str(
                content.get("videoId") or (snippet.get("resourceId") or {}).get("videoId") or ""
            ).strip()
            duration = (video_details.get(video_id) or {}).get("duration_seconds")
            title = " ".join(str(snippet.get("title") or "").split())
            if title and duration is not None and duration > 180:
                titles.append(title)
                if len(titles) == 15:
                    return titles
    return titles


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
        try:
            require_youtube_quota_available(feature="women_hormones_research", method="videos.list")
        except YoutubeQuotaCapExceeded as exc:
            print(f"WARNING: research request skipped: {exc}", file=sys.stderr)
            raise ReclassifyError(str(exc)) from exc
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
        "Classify a YouTube channel using only the channel description and the ordered list of its latest long-form video titles. "
        "Judge what the channel is MAINLY about, using the description first. Mentioning menopause in some titles is not enough.\n\n"
        "Return JSON only with exactly these fields:\n"
        '{"primary_niche":"menopause_hormones|fertility_obgyn|womens_fitness|womens_nutrition_fasting|weight_glp1|general_gyn|sexual_health|other",'
        '"titles":[{"title":"exact supplied title","on_topic":true}],'
        '"entity":"individual|company_clinic","reason":"one line"}\n\n'
        "The titles array must contain exactly one object for each supplied title, in the same order, and copy each title exactly. "
        "A title is on_topic true when the video is mainly about perimenopause, menopause, HRT, female sex hormones "
        "(estrogen, progesterone, or testosterone in women), OR midlife women's health that menopause/hormone clinicians "
        "commonly cover: bone density, heart health, sleep, brain or mood, skin, sexual health, or weight/metabolism in women "
        "40+. Pregnancy, fertility, general fitness, diet or fasting programs, non-women-specific health, and unrelated "
        "specialties are false. Telehealth brands, clinics, hospitals, and product companies are company_clinic.\n\n"
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


def _validate_audience_language(payload: dict[str, Any]) -> str:
    language = str(payload.get("language") or "").strip().lower()
    if language not in {"english", "mixed", "other"}:
        raise ReclassifyError(f"invalid audience language: {language!r}")
    return language


def classify_audience_language(
    *,
    channel_id: str,
    titles: list[str],
    cache_dir: Path,
    usage: Usage,
) -> tuple[str, bool]:
    source = {"titles": titles[:15]}
    input_hash = hashlib.sha256(
        json.dumps(source, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    cache_path = cache_dir / f"{channel_id}.json"
    if cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if (
                cached.get("version") == AUDIENCE_CLASSIFIER_VERSION
                and cached.get("model") == OPENAI_MODEL
                and cached.get("input_hash") == input_hash
            ):
                return _validate_audience_language(cached.get("result") or {}), True
        except (OSError, ValueError, json.JSONDecodeError, ReclassifyError):
            pass
    if not _openai_client:
        raise ReclassifyError("OPENAI_API_KEY is required")
    prompt = (
        "Decide the language of this JSON list of YouTube video titles. Romanized Hindi "
        "(Hinglish) is not English. Return JSON only as "
        '{"language":"english|mixed|other"}. Use english only when every supplied title '
        "is English. Use mixed when English and Hinglish or another language are mixed, "
        "and other when the titles are primarily non-English.\n\n"
        f"Titles:\n{json.dumps(titles[:15], ensure_ascii=False)}"
    )
    last_error: Exception | None = None
    for _attempt in range(3):
        try:
            response = _openai_client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": "You are a strict language classifier. Return valid JSON only."},
                    {"role": "user", "content": prompt},
                ],
                response_format={"type": "json_object"},
                temperature=0,
            )
            usage.add(response)
            language = _validate_audience_language(
                _extract_json(response.choices[0].message.content or "")
            )
            cache_dir.mkdir(parents=True, exist_ok=True)
            temporary = cache_path.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(
                    {
                        "version": AUDIENCE_CLASSIFIER_VERSION,
                        "model": OPENAI_MODEL,
                        "input_hash": input_hash,
                        "result": {"language": language},
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            temporary.replace(cache_path)
            return language, False
        except Exception as exc:
            last_error = exc
    raise ReclassifyError(f"audience language classification failed for {channel_id}: {last_error}")


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


def _pre_audience_core(row: dict[str, Any]) -> bool:
    try:
        share = float(row.get("on_topic_share") or 0)
    except (TypeError, ValueError):
        share = 0.0
    original_language = str(row.get("language") or "").strip().lower().replace("_", "-")
    return (
        str(row.get("primary_niche") or "").strip().lower() == "menopause_hormones"
        and share >= NICHE_ON_TOPIC_MIN
        and str(row.get("entity") or "").strip().lower() == "individual"
        and str(row.get("presenter") or "").strip().lower() == "real_person"
        and (original_language == "english" or original_language == "en" or original_language.startswith("en-"))
    )


def _result_dicts(result: Any) -> list[dict[str, Any]]:
    columns = [str(item[0]) for item in (result.description or [])]
    return [dict(zip(columns, row)) for row in result.fetchall()]


def apply_audience_filter_and_record_run(
    *,
    source_rows: list[dict[str, Any]],
    niche_slug: str,
    snapshot_date: str,
    cache_root: Path,
    video_details: dict[str, dict[str, int | None]],
    usage: Usage,
    workers: int,
) -> dict[str, Any]:
    conn = get_db()
    try:
        required_channel_columns = {"country", "audience_language"}
        missing = required_channel_columns - table_columns(conn, "niche_channels")
        if missing:
            raise ReclassifyError(f"niche_channels schema is missing: {', '.join(sorted(missing))}")
        if not table_columns(conn, "niche_classification_runs") or not table_columns(
            conn, "niche_channel_run_results"
        ):
            raise ReclassifyError("niche classification history tables are required")
        niche = conn.execute("SELECT id FROM niches WHERE slug=? LIMIT 1", [niche_slug]).fetchone()
        if not niche:
            raise ReclassifyError(f"unknown niche: {niche_slug}")
        rows = _result_dicts(
            conn.execute(
                """
                SELECT youtube_channel_id, title, credential, primary_niche,
                       on_topic_share, entity, reclass_reason, presenter, language,
                       subscriber_count, views_90d, admin_exclude, admin_include,
                       country, audience_language
                FROM niche_channels
                WHERE niche_id=? AND snapshot_date=?
                ORDER BY youtube_channel_id
                """,
                [niche[0], snapshot_date],
            )
        )
        # Do not hold an open database transaction while the network-bound LLM
        # classifications run; production enforces an idle-in-transaction timeout.
        conn.commit()
        candidates = [
            row for row in rows if _pre_audience_core(row) or bool(row.get("admin_include"))
        ]
        cache_hits = 0

        def classify_candidate(row: dict[str, Any]) -> tuple[str, str, bool]:
            channel_id = str(row.get("youtube_channel_id") or "").strip()
            titles = load_cached_longform_titles(cache_root, channel_id, video_details)
            language, cached = classify_audience_language(
                channel_id=channel_id,
                titles=titles,
                cache_dir=cache_root / "audience_languages",
                usage=usage,
            )
            return channel_id, language, cached

        language_by_id: dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=max(1, min(workers, 12))) as executor:
            for channel_id, language, cached in executor.map(classify_candidate, candidates):
                language_by_id[channel_id] = language
                cache_hits += int(cached)

        for row in rows:
            channel_id = str(row.get("youtube_channel_id") or "").strip()
            row["country"] = load_cached_country(cache_root, channel_id)
            if channel_id in language_by_id:
                row["audience_language"] = language_by_id[channel_id]
            conn.execute(
                """
                UPDATE niche_channels
                SET country=?, audience_language=?, updated_at=now()
                WHERE niche_id=? AND youtube_channel_id=? AND snapshot_date=?
                """,
                [
                    row["country"],
                    str(row.get("audience_language") or "").strip().lower() or None,
                    niche[0],
                    channel_id,
                    snapshot_date,
                ],
            )

        notes = (
            "Allowed country: US, CA, GB, AU, NZ, IE, or empty; "
            "latest long-form title language must be English. Admin overrides applied."
        )
        run = conn.execute(
            """
            INSERT INTO niche_classification_runs (niche_id, run_at, rule_version, notes)
            VALUES (?, now(), ?, ?)
            RETURNING run_id
            """,
            [niche[0], RULE_VERSION, notes],
        ).fetchone()
        run_id = run[0]
        for row in rows:
            conn.execute(
                """
                INSERT INTO niche_channel_run_results (
                    run_id, youtube_channel_id, in_pool, primary_niche,
                    on_topic_share, reason
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    run_id,
                    row["youtube_channel_id"],
                    is_final_niche_pool_row(row),
                    row.get("primary_niche"),
                    float(row.get("on_topic_share") or 0),
                    row.get("reclass_reason"),
                ],
            )
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()

    final_all = sorted(
        [row for row in rows if is_final_niche_pool_row(row)],
        key=lambda row: (-int(row.get("subscriber_count") or 0), str(row.get("title") or "").lower()),
    )
    final_clinicians = [row for row in final_all if clinician_matches(row)]
    removed = [
        row
        for row in rows
        if _pre_audience_core(row)
        and (
            str(row.get("country") or "").upper() not in NICHE_AUDIENCE_COUNTRIES
            or str(row.get("audience_language") or "").lower() != "english"
        )
        and not bool(row.get("admin_include"))
    ]
    shelley_index = next(
        (index for index, row in enumerate(final_clinicians) if row["youtube_channel_id"] == "UCx-TX9EAE0NOL9hhG1wnvwg"),
        None,
    )
    neighbors = (
        final_clinicians[max(0, shelley_index - 2) : shelley_index + 3]
        if shelley_index is not None
        else []
    )
    return {
        "run_id": run_id,
        "candidate_count": len(candidates),
        "cache_hits": cache_hits,
        "pool_all": len(final_all),
        "pool_clinicians": len(final_clinicians),
        "removed": sorted(removed, key=lambda row: str(row.get("title") or "").lower()),
        "shelley_rank": shelley_index + 1 if shelley_index is not None else None,
        "shelley_neighbors": neighbors,
    }


def print_audience_summary(result: dict[str, Any], usage: Usage) -> None:
    print(
        f"audience_candidates={result['candidate_count']} cache_hits={result['cache_hits']} "
        f"new_llm_calls={result['candidate_count'] - result['cache_hits']}"
    )
    print(f"pool_clinicians={result['pool_clinicians']} pool_all={result['pool_all']}")
    print("audience_removed=")
    for row in result["removed"]:
        print(
            "  "
            + " | ".join(
                [
                    str(row.get("title") or ""),
                    str(row.get("country") or "empty"),
                    str(row.get("audience_language") or "unknown"),
                    str(int(row.get("subscriber_count") or 0)),
                ]
            )
        )
    print(f"shelley_rank={result['shelley_rank']}")
    for row in result["shelley_neighbors"]:
        print(
            f"  neighbor={row.get('title')} subscribers={int(row.get('subscriber_count') or 0)} "
            f"views_90d={int(row.get('views_90d') or 0)}"
        )
    cost = usage.estimated_cost()
    print(f"audience_llm_model={OPENAI_MODEL} input_tokens={usage.input_tokens} output_tokens={usage.output_tokens}")
    print(f"audience_llm_cost_usd={cost:.6f}" if cost is not None else "audience_llm_cost_usd=unknown")
    print(f"classification_run_id={result['run_id']}")


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
    parser.add_argument(
        "--audience-only",
        action="store_true",
        help="Use cached channel/video data and classify language only for the current pool",
    )
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

    video_details = load_video_details(args.cache_root)
    if args.audience_only:
        usage = Usage()
        result = apply_audience_filter_and_record_run(
            source_rows=source_rows,
            niche_slug=args.niche_slug,
            snapshot_date=args.snapshot_date,
            cache_root=args.cache_root,
            video_details=video_details,
            usage=usage,
            workers=args.workers,
        )
        print("AUDIENCE_FILTER_COMPLETE")
        print_audience_summary(result, usage)
        print("youtube_units_used=0")
        return 0
    video_views = {
        video_id: int(item["views"])
        for video_id, item in video_details.items()
        if item.get("views") is not None
    }
    usage = Usage()
    output_rows: list[dict[str, Any]] = []
    cache_hits = 0
    missing_video_stats = 0
    channels_with_pipe_titles = 0

    def process_row(row: dict[str, Any]) -> tuple[dict[str, Any], bool, int, bool]:
        channel_id = str(row.get("youtube_channel_id") or "").strip()
        _recent_titles, views_90d, missing = load_cached_channel_activity(
            args.cache_root, channel_id, cutoff, video_views
        )
        titles = load_cached_longform_titles(args.cache_root, channel_id, video_details)
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
                "last_15_video_titles": json.dumps(titles, ensure_ascii=False),
            },
            cached,
            missing,
            any(" | " in title for title in titles),
        )

    worker_count = max(1, min(args.workers, 12))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        classified_rows = executor.map(process_row, source_rows)
        for index, (output_row, cached, missing, has_pipe_title) in enumerate(classified_rows, start=1):
            output_rows.append(output_row)
            cache_hits += int(cached)
            missing_video_stats += missing
            channels_with_pipe_titles += int(has_pipe_title)
            if index % 10 == 0 or index == len(source_rows):
                write_checkpoint(output_path, source_fields + OUTPUT_FIELDS, output_rows)
                print(f"classified {index}/{len(source_rows)} cache_hits={cache_hits}", flush=True)

    apply_to_database(output_rows, args.niche_slug, args.snapshot_date)
    audience_usage = Usage()
    audience_result = apply_audience_filter_and_record_run(
        source_rows=source_rows,
        niche_slug=args.niche_slug,
        snapshot_date=args.snapshot_date,
        cache_root=args.cache_root,
        video_details=video_details,
        usage=audience_usage,
        workers=args.workers,
    )
    niche_counts = Counter(row["primary_niche"] for row in output_rows)
    entity_counts = Counter(row["entity"] for row in output_rows)
    cost = usage.estimated_cost()
    print("RECLASSIFICATION_COMPLETE")
    print(f"rows={len(output_rows)} cache_hits={cache_hits} new_llm_calls={len(output_rows)-cache_hits}")
    print("primary_niche=" + ",".join(f"{key}:{niche_counts[key]}" for key in sorted(niche_counts)))
    print("entity=" + ",".join(f"{key}:{entity_counts[key]}" for key in sorted(entity_counts)))
    print(f"missing_cached_video_stats={missing_video_stats}")
    print(f"channels_with_pipe_titles={channels_with_pipe_titles}")
    print(f"youtube_units_used={backfill_result['units_used']}")
    print(f"llm_model={OPENAI_MODEL} input_tokens={usage.input_tokens} output_tokens={usage.output_tokens}")
    print(f"llm_cost_usd={cost:.6f}" if cost is not None else "llm_cost_usd=unknown")
    print_audience_summary(audience_result, audience_usage)
    print(f"output={output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
