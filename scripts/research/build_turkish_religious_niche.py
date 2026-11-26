#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import re
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests


REPO_ROOT = Path(__file__).resolve().parents[2]
RESEARCH_DIR = Path(__file__).resolve().parent
for path in (REPO_ROOT, RESEARCH_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from dotenv import load_dotenv

load_dotenv(REPO_ROOT / ".env")

import grow_niche_women_hormones as grow
import niche_women_hormones as base
import reclassify_women_hormones as legacy
from app.video_shorts.config import _openai_client
from app.video_shorts.services.db import get_db, get_db_readonly, table_columns
from app.video_shorts.services.niche_definitions import (
    load_niche_definition,
    parse_title_core_flags,
)
from app.video_shorts.services.niches import final_niche_pool, is_final_niche_pool_row, load_niche_snapshot


SLUG = "turkish_religious_talks"
SEED_CHANNEL_ID = "UCpZTTbddOVYPvQj02Zu8x_A"
FEATURE = "turkish_religious_talks_research_project"
CHECKPOINT_VERSION = "turkish-religious-talks-v1"
CACHE_ROOT = REPO_ROOT / "data" / "research" / "cache"
CHECKPOINT_PATH = REPO_ROOT / "data" / "research" / "turkish_religious_talks_checkpoint.json"
PACIFIC = ZoneInfo("America/Los_Angeles")
SEARCH_LIMIT = 25
OTHER_LIMIT = 1500
MODEL = "gpt-4.1-mini"


base.QUOTA_FEATURE = FEATURE


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    temporary.replace(path)


def load_checkpoint() -> dict[str, Any]:
    if not CHECKPOINT_PATH.exists():
        return {
            "version": CHECKPOINT_VERSION,
            "candidates": {},
            "rows": {},
            "classifications": {},
            "quota": {"search_calls": 0, "other_units": 0, "calls": {}},
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }
    value = json.loads(CHECKPOINT_PATH.read_text(encoding="utf-8"))
    if value.get("version") != CHECKPOINT_VERSION:
        raise RuntimeError("incompatible Turkish niche checkpoint")
    return value


def quota_from_checkpoint(checkpoint: dict[str, Any]) -> base.QuotaBudget:
    saved = checkpoint.get("quota") or {}
    quota = base.QuotaBudget(search_limit=SEARCH_LIMIT, other_limit=OTHER_LIMIT)
    quota.search_calls = int(saved.get("search_calls") or 0)
    quota.other_units = int(saved.get("other_units") or 0)
    quota.calls.update(saved.get("calls") or {})
    return quota


def save_checkpoint(checkpoint: dict[str, Any], quota: base.QuotaBudget) -> None:
    checkpoint["quota"] = {
        "search_calls": quota.search_calls,
        "other_units": quota.other_units,
        "calls": dict(quota.calls),
    }
    atomic_json(CHECKPOINT_PATH, checkpoint)


def add_candidate(checkpoint: dict[str, Any], channel_id: str, source: str, detail: str = "") -> None:
    channel_id = str(channel_id or "").strip()
    if not channel_id.startswith("UC"):
        return
    item = checkpoint["candidates"].setdefault(channel_id, {"sources": [], "details": []})
    if source not in item["sources"]:
        item["sources"].append(source)
    if detail and detail not in item["details"]:
        item["details"].append(detail)


def turkish_text(value: Any) -> bool:
    text = str(value or "").casefold()
    tokens = re.findall(r"[a-zçğıöşüâîû]+", text)
    markers = {
        "ve", "bir", "bu", "için", "ile", "dini", "islami", "islam", "sohbet",
        "manevi", "iman", "allah", "peygamber", "kuran", "kur'an", "dua", "namaz",
    }
    score = sum(token in markers for token in tokens)
    return bool(re.search(r"[çğıöşü]", text)) or score >= 2


def add_discovery_leads(checkpoint: dict[str, Any]) -> int:
    conn = get_db_readonly()
    try:
        columns = table_columns(conn, "discovery_leads")
        if not {"youtube_channel_id", "channel_title"}.issubset(columns):
            return 0
        description_sql = "channel_description" if "channel_description" in columns else "''"
        id_sql = "CAST(id AS VARCHAR)" if "id" in columns else "''"
        rows = conn.execute(
            f"SELECT youtube_channel_id, channel_title, {description_sql}, {id_sql} FROM discovery_leads"
        ).fetchall()
    finally:
        conn.close()
    added = 0
    for channel_id, title, description, lead_id in rows:
        if turkish_text(f"{title or ''} {description or ''}"):
            before = len(checkpoint["candidates"])
            add_candidate(checkpoint, str(channel_id or ""), "discovery_leads", str(lead_id or ""))
            added += len(checkpoint["candidates"]) > before
    return added


def search_candidates(
    definition: dict[str, Any], checkpoint: dict[str, Any], session: requests.Session,
    quota: base.QuotaBudget,
) -> None:
    for index, query in enumerate(definition.get("search_queries") or [], start=1):
        params = {
            "part": "snippet", "q": query, "type": "video", "maxResults": 50,
            "order": "relevance", "relevanceLanguage": "tr",
        }
        payload = base.cached_youtube_get(
            session, quota, "search", "search.list", params,
            CACHE_ROOT / SLUG / "search" / f"{base._cache_slug(query)}.json",
        )
        for item in payload.get("items") or []:
            snippet = item.get("snippet") or {}
            add_candidate(checkpoint, snippet.get("channelId"), "search", query)
        save_checkpoint(checkpoint, quota)
        print(f"search {index}/{len(definition.get('search_queries') or [])} candidates={len(checkpoint['candidates'])}", file=sys.stderr)


def featured_candidates(
    channel_ids: set[str], checkpoint: dict[str, Any], session: requests.Session,
    quota: base.QuotaBudget,
) -> None:
    for index, channel_id in enumerate(sorted(channel_ids), start=1):
        params = {"part": "snippet,contentDetails", "channelId": channel_id}
        try:
            payload = base.cached_youtube_get(
                session, quota, "channelSections", "channelSections.list", params,
                CACHE_ROOT / SLUG / "featured" / f"{channel_id}.json",
            )
        except Exception as exc:
            print(f"featured skipped {channel_id}: {exc}", file=sys.stderr)
            continue
        for item in payload.get("items") or []:
            if str((item.get("snippet") or {}).get("type") or "") != "multipleChannels":
                continue
            for featured_id in ((item.get("contentDetails") or {}).get("channels") or []):
                add_candidate(checkpoint, featured_id, "snowball_featured", channel_id)
        save_checkpoint(checkpoint, quota)
        print(f"featured {index}/{len(channel_ids)} candidates={len(checkpoint['candidates'])}", file=sys.stderr)


def enrich_pending(
    checkpoint: dict[str, Any], session: requests.Session, quota: base.QuotaBudget,
) -> None:
    pending = [channel_id for channel_id in checkpoint["candidates"] if channel_id not in checkpoint["rows"]]
    if not pending:
        return
    metadata, statuses = base.fetch_channel_metadata(session, quota, pending, CACHE_ROOT)
    cutoff = datetime.now(timezone.utc) - timedelta(days=90)
    for index, channel_id in enumerate(pending, start=1):
        channel = metadata.get(channel_id)
        if not channel:
            checkpoint["rows"][channel_id] = {"enrich_status": statuses.get(channel_id, "missing")}
            continue
        try:
            enriched = grow.enrich_channel(
                channel_id=channel_id, channel=channel, session=session, quota=quota,
                cache_root=CACHE_ROOT, cutoff=cutoff,
                scan_past_cutoff_for_long_titles=False,
            )
        except Exception as exc:
            enriched = {"enrich_status": "error", "error": str(exc), "long_titles": [], "activity": {}, "views_90d": 0}
        checkpoint["rows"][channel_id] = {"channel": channel, **enriched}
        save_checkpoint(checkpoint, quota)
        print(f"enriched {index}/{len(pending)} {channel_id}", file=sys.stderr)


def classification_input(definition: dict[str, Any], channel_id: str, row: dict[str, Any], video_details) -> dict[str, Any]:
    titles = []
    uploads = []
    for path in sorted((CACHE_ROOT / "uploads" / channel_id).glob("page_*.json")):
        for item in legacy._cache_payload(path).get("items") or []:
            snippet = item.get("snippet") or {}
            content = item.get("contentDetails") or {}
            video_id = str(content.get("videoId") or (snippet.get("resourceId") or {}).get("videoId") or "")
            title = " ".join(str(snippet.get("title") or "").split())
            published = str(content.get("videoPublishedAt") or snippet.get("publishedAt") or "")
            duration = (video_details.get(video_id) or {}).get("duration_seconds")
            if video_id and title and duration is not None:
                uploads.append({"title": title, "format": "long" if int(duration) > 180 else "short", "published": published})
    uploads.sort(key=lambda item: item["published"], reverse=True)
    titles.extend([{"title": item["title"], "format": "long"} for item in uploads if item["format"] == "long"][:15])
    if len(titles) < 15:
        titles.extend([{"title": item["title"], "format": "short"} for item in uploads if item["format"] == "short"][:15-len(titles)])
    return {
        "channel_id": channel_id,
        "title": str((row.get("channel") or {}).get("title") or ""),
        "description": str((row.get("channel") or {}).get("description") or "")[:5000],
        "titles": titles,
        "target_focus": definition["target_focus"],
        "focus_options": definition["focus_options"],
        "core_topics": definition["core_topics"],
    }


def classify_one(definition: dict[str, Any], source: dict[str, Any]) -> tuple[dict[str, Any], dict[str, int]]:
    focus_keys = {str(item["key"]) for item in definition["focus_options"]}
    response = _openai_client.chat.completions.create(
        model=MODEL, temperature=0, max_tokens=1300, response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": (
                "Classify a Turkish YouTube channel by its MAIN focus. Return JSON only with: "
                "primary_focus, language, entity, presenter, title_results, reason. primary_focus must be one "
                f"of {', '.join(sorted(focus_keys))}. language is exactly tr, mixed, or other. entity is exactly "
                "individual or institution. presenter is real_person, org, or faceless. title_results has one "
                "entry per supplied title in order with core_topic true only when mainly about a stored core topic. "
                "Judge description first. The target channel must TEACH or EXPLAIN through Islamic sohbet, vaaz, "
                "ders, or spiritual talks. Channels mainly offering Qur'an recitation, religious news/politics, "
                "generic motivation, or institutional mosque broadcasts belong to their neighbour focus even when "
                "some titles resemble sohbet. If unsure, do not choose the target focus."
            )},
            {"role": "user", "content": json.dumps(source, ensure_ascii=False)},
        ],
    )
    raw = json.loads(response.choices[0].message.content or "{}")
    focus = str(raw.get("primary_focus") or "").strip()
    if focus not in focus_keys:
        focus = "other" if "other" in focus_keys else sorted(focus_keys)[-1]
    language = str(raw.get("language") or "other").strip().casefold()
    if language not in {"tr", "mixed", "other"}:
        language = "other"
    entity = "individual" if str(raw.get("entity") or "").casefold() == "individual" else "institution"
    presenter = str(raw.get("presenter") or "").casefold()
    if presenter not in {"real_person", "org", "faceless"}:
        presenter = "real_person" if entity == "individual" else "org"
    flags = parse_title_core_flags(raw.get("title_results"), source["titles"])
    usage = response.usage
    return {
        "primary_focus": focus, "language": language,
        "entity": "individual" if entity == "individual" else "company_clinic",
        "presenter": presenter, "core_count": sum(flags), "title_count": len(source["titles"]),
        "core_share": sum(flags) / len(flags) if flags else 0.0,
        "reason": " ".join(str(raw.get("reason") or "").split())[:500],
        "classification_status": "classified" if source["titles"] else "no_titles",
    }, {
        "input_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
        "output_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
    }


def classify_pending(definition: dict[str, Any], checkpoint: dict[str, Any]) -> None:
    video_details = legacy.load_video_details(CACHE_ROOT)
    pending = [
        channel_id for channel_id, row in checkpoint["rows"].items()
        if row.get("channel") and channel_id not in checkpoint["classifications"]
    ]
    sources = {channel_id: classification_input(definition, channel_id, checkpoint["rows"][channel_id], video_details) for channel_id in pending}

    def run(channel_id: str):
        source = sources[channel_id]
        if not source["titles"]:
            return channel_id, {
                "primary_focus": None, "language": "other", "entity": "company_clinic",
                "presenter": "faceless", "core_count": 0, "title_count": 0,
                "core_share": 0.0, "reason": "No cached titles.", "classification_status": "no_titles",
            }, {"input_tokens": 0, "output_tokens": 0}
        return channel_id, *classify_one(definition, source)

    with ThreadPoolExecutor(max_workers=8) as executor:
        for index, (channel_id, result, usage) in enumerate(executor.map(run, pending), start=1):
            checkpoint["classifications"][channel_id] = result
            checkpoint["usage"]["input_tokens"] += usage["input_tokens"]
            checkpoint["usage"]["output_tokens"] += usage["output_tokens"]
            atomic_json(CHECKPOINT_PATH, checkpoint)
            print(f"classified {index}/{len(pending)} {channel_id}", file=sys.stderr)


def memory_pool_ids(definition: dict[str, Any], checkpoint: dict[str, Any]) -> set[str]:
    output = set()
    for channel_id, result in checkpoint["classifications"].items():
        if (
            result.get("classification_status") == "classified"
            and result.get("primary_focus") == definition["target_focus"]
            and result.get("language") == definition["language"]
            and result.get("entity") == "individual"
            and result.get("presenter") == "real_person"
        ):
            output.add(channel_id)
    return output


def discovery_lead_ids(channel_ids: list[str]) -> dict[str, str]:
    if not channel_ids:
        return {}
    conn = get_db_readonly()
    try:
        if not {"id", "youtube_channel_id"}.issubset(table_columns(conn, "discovery_leads")):
            return {}
        placeholders = ",".join("?" for _ in channel_ids)
        rows = conn.execute(
            f"SELECT youtube_channel_id, CAST(id AS VARCHAR) FROM discovery_leads WHERE youtube_channel_id IN ({placeholders})",
            channel_ids,
        ).fetchall()
        return {str(row[0]): str(row[1]) for row in rows}
    finally:
        conn.close()


def optional_int(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def persist(definition: dict[str, Any], checkpoint: dict[str, Any]) -> int:
    snapshot_date = datetime.now(PACIFIC).date().isoformat()
    lead_ids = discovery_lead_ids(list(checkpoint["rows"]))
    conn = get_db()
    try:
        niche_id = definition["id"]
        for channel_id, row in checkpoint["rows"].items():
            channel = row.get("channel")
            result = checkpoint["classifications"].get(channel_id)
            if not channel or not result:
                continue
            activity = row.get("activity") or {}
            sources = checkpoint["candidates"].get(channel_id, {}).get("sources") or []
            conn.execute(
                """
                INSERT INTO niche_channels (
                    niche_id,youtube_channel_id,snapshot_date,title,channel_url,credential,in_niche,
                    presenter,language,subscriber_count,longform_90d,shorts_90d,avg_views_long_90d,
                    avg_views_short_90d,discovery_lead_id,source,replied,admin_note,updated_at,
                    primary_niche,on_topic_share,on_topic_count,entity,reclass_reason,views_90d,
                    admin_exclude,admin_include,exclude_reason,country,audience_language,primary_focus,
                    classification_status,classification_title_count
                ) VALUES (?,?,?,?,?,'none',?,?,?,?,?,?,?,?,?,?,FALSE,'',CURRENT_TIMESTAMP,?,?,?,?,?,?,FALSE,FALSE,NULL,?,?,?,?,?)
                ON CONFLICT (niche_id,youtube_channel_id,snapshot_date) DO UPDATE SET
                    title=EXCLUDED.title,channel_url=EXCLUDED.channel_url,presenter=EXCLUDED.presenter,
                    language=EXCLUDED.language,subscriber_count=EXCLUDED.subscriber_count,
                    longform_90d=EXCLUDED.longform_90d,shorts_90d=EXCLUDED.shorts_90d,
                    avg_views_long_90d=EXCLUDED.avg_views_long_90d,
                    avg_views_short_90d=EXCLUDED.avg_views_short_90d,
                    discovery_lead_id=COALESCE(EXCLUDED.discovery_lead_id,niche_channels.discovery_lead_id),
                    source=EXCLUDED.source,primary_niche=EXCLUDED.primary_niche,
                    on_topic_share=EXCLUDED.on_topic_share,on_topic_count=EXCLUDED.on_topic_count,
                    entity=EXCLUDED.entity,reclass_reason=EXCLUDED.reclass_reason,views_90d=EXCLUDED.views_90d,
                    country=EXCLUDED.country,audience_language=EXCLUDED.audience_language,
                    primary_focus=EXCLUDED.primary_focus,classification_status=EXCLUDED.classification_status,
                    classification_title_count=EXCLUDED.classification_title_count,updated_at=CURRENT_TIMESTAMP
                """,
                [
                    niche_id,channel_id,snapshot_date,channel.get("title") or channel_id,
                    f"https://www.youtube.com/channel/{channel_id}",
                    "yes" if result["classification_status"] == "classified" else "no",
                    result["presenter"],"Turkish" if result["language"] == "tr" else result["language"],
                    optional_int(channel.get("subscriber_count")),int(activity.get("longform_90d") or 0),
                    int(activity.get("shorts_90d") or 0),optional_int(activity.get("avg_views_long_90d")),
                    optional_int(activity.get("avg_views_short_90d")),lead_ids.get(channel_id),",".join(sources),
                    SLUG,result["core_share"],result["core_count"],result["entity"],result["reason"],
                    int(row.get("views_90d") or 0),str(channel.get("country") or "").upper(),
                    "turkish" if result["language"] == "tr" else result["language"],result["primary_focus"],
                    result["classification_status"],result["title_count"],
                ],
            )
        result_set = conn.execute("SELECT * FROM niche_channels WHERE niche_id=? AND snapshot_date=?",[definition["id"],snapshot_date])
        columns = [str(item[0]) for item in result_set.description or []]
        rows = [dict(zip(columns,item)) for item in result_set.fetchall()]
        run_id = conn.execute(
            "INSERT INTO niche_classification_runs (niche_id,run_at,rule_version,notes) VALUES (?,CURRENT_TIMESTAMP,?,?) RETURNING run_id",
            [definition["id"],"generic-v3.1-tr-v1","tr niche v1"],
        ).fetchone()[0]
        for row in rows:
            conn.execute(
                "INSERT INTO niche_channel_run_results (run_id,youtube_channel_id,in_pool,primary_niche,on_topic_share,reason) VALUES (?,?,?,?,?,?)",
                [run_id,row["youtube_channel_id"],is_final_niche_pool_row(row,definition),row.get("primary_focus"),float(row.get("on_topic_share") or 0),row.get("reclass_reason")],
            )
        conn.commit()
        return int(run_id)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main() -> int:
    checkpoint = load_checkpoint()
    quota = quota_from_checkpoint(checkpoint)
    conn = get_db_readonly()
    try:
        definition = load_niche_definition(conn, slug=SLUG)
    finally:
        conn.close()
    if not definition or definition.get("status") != "draft":
        raise RuntimeError("Turkish draft niche definition is missing")
    if len(definition.get("search_queries") or []) != 20:
        raise RuntimeError("Turkish niche must have exactly 20 search queries")
    add_candidate(checkpoint, SEED_CHANNEL_ID, "seed")
    session = requests.Session()
    if not checkpoint.get("search_complete"):
        search_candidates(definition, checkpoint, session, quota)
        add_discovery_leads(checkpoint)
        checkpoint["search_complete"] = True
        save_checkpoint(checkpoint, quota)
    enrich_pending(checkpoint, session, quota)
    classify_pending(definition, checkpoint)
    first_pool = memory_pool_ids(definition, checkpoint)
    if not checkpoint.get("featured_complete"):
        featured_candidates(first_pool | {SEED_CHANNEL_ID}, checkpoint, session, quota)
        checkpoint["featured_complete"] = True
        save_checkpoint(checkpoint, quota)
        enrich_pending(checkpoint, session, quota)
        classify_pending(definition, checkpoint)
    run_id = persist(definition, checkpoint)
    usage = checkpoint["usage"]
    cost = (int(usage["input_tokens"]) * 0.4 + int(usage["output_tokens"]) * 1.6) / 1_000_000
    source_counts = Counter()
    for item in checkpoint["candidates"].values():
        source_counts.update(item.get("sources") or [])
    print(json.dumps({
        "run_id":run_id,"candidates_by_source":dict(source_counts),"unique_candidates":len(checkpoint["candidates"]),
        "first_pass_pool":len(first_pool),"search_calls":quota.search_calls,"other_units":quota.other_units,
        "api_calls":dict(quota.calls),"llm_usage":{**usage,"cost_usd":cost},"checkpoint":str(CHECKPOINT_PATH),
    },ensure_ascii=False,indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
