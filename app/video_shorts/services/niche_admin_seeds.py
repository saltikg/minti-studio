from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from app.video_shorts import youtube_api
from app.video_shorts.config import _openai_client
from app.video_shorts.services.db import get_db, get_db_readonly
from app.video_shorts.services.niche_definitions import load_niche_definition
from app.video_shorts.services.niches import is_final_niche_pool_row, load_niche_snapshot
from app.video_shorts.services.youtube_quota import (
    YoutubeQuotaCapExceeded,
    record_youtube_data_api_call,
    research_youtube_api_key,
    require_youtube_quota_available,
    unit_cost,
)


FEATURE = "niche_admin_seeds_research_project"
MODEL = "gpt-4.1-mini"
MAX_NAME_SEARCHES = 5
CACHE_ROOT = Path(__file__).resolve().parents[3] / "data" / "research" / "cache"
_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)


class SeedError(RuntimeError):
    pass


def _clean_name(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").casefold())
    return "".join(character for character in text if character.isalnum())


def _cache_read(path: Path, params: dict[str, Any]) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if value.get("params") != params or not isinstance(value.get("payload"), dict):
        return None
    return value["payload"]


def _cache_write(path: Path, params: dict[str, Any], payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps({"params": params, "payload": payload}, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(path)


class SeedRunner:
    def __init__(self, niche_slug: str):
        self.niche_slug = niche_slug
        self.session = requests.Session()
        self.calls: Counter[str] = Counter()
        self.units_used = 0
        self.searches = 0
        self.llm_input_tokens = 0
        self.llm_output_tokens = 0
        conn = get_db_readonly()
        try:
            self.definition = load_niche_definition(conn, slug=niche_slug)
            self.selected, self.existing_rows = load_niche_snapshot(conn, niche_slug)
        finally:
            conn.close()
        if not self.definition or not self.selected:
            raise SeedError("Unknown or unconfigured niche.")
        if not self.selected.get("snapshot_date"):
            raise SeedError("The selected niche does not have a snapshot yet.")
        self.existing_by_id = {
            str(row.get("youtube_channel_id") or ""): row for row in self.existing_rows
        }
        self.admin_decisions = [
            {
                "channel": row.get("title"),
                "decision": "exclude" if row.get("admin_exclude") else "include",
                "reason": row.get("exclude_reason") or "",
            }
            for row in sorted(
                (
                    row for row in self.existing_rows
                    if row.get("admin_exclude") or row.get("admin_include")
                ),
                key=lambda row: str(row.get("updated_at") or ""),
                reverse=True,
            )[:20]
        ]

    def close(self) -> None:
        self.session.close()

    def youtube_get(
        self, endpoint: str, method: str, params: dict[str, Any], cache_path: Path
    ) -> dict[str, Any]:
        cached = _cache_read(cache_path, params)
        if cached is not None:
            return cached
        if method == "search.list" and self.searches >= MAX_NAME_SEARCHES:
            raise SeedError("Submit limit reached: at most 5 plain-name searches are allowed.")
        require_youtube_quota_available(feature=FEATURE, method=method)
        try:
            api_key = research_youtube_api_key()
        except RuntimeError as exc:
            raise SeedError(str(exc)) from exc
        response = None
        try:
            response = self.session.get(
                f"{youtube_api.YOUTUBE_API_BASE}/{endpoint}",
                params={**params, "key": api_key},
                timeout=30,
            )
            record_youtube_data_api_call(feature=FEATURE, method=method)
            self.calls[method] += 1
            self.units_used += unit_cost(method)
            if method == "search.list":
                self.searches += 1
            response.raise_for_status()
            payload = response.json() or {}
        except (requests.RequestException, ValueError):
            reason = ""
            try:
                error = ((response.json() if response is not None else {}) or {}).get("error") or {}
                errors = error.get("errors") or []
                reason = str((errors[0] if errors else {}).get("reason") or "")
            except (TypeError, ValueError):
                reason = ""
            status = getattr(response, "status_code", None)
            detail = f"HTTP {status}" if status is not None else "request error"
            if reason:
                detail += f" ({reason})"
            raise SeedError(f"{method} failed: {detail}") from None
        if not isinstance(payload, dict):
            payload = {}
        _cache_write(cache_path, params, payload)
        return payload

    def _channels_lookup(self, *, channel_id: str = "", handle: str = "") -> dict[str, Any] | None:
        if channel_id:
            cached_path = CACHE_ROOT / "channels" / f"{channel_id}.json"
            try:
                cached_wrapper = json.loads(cached_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                cached_wrapper = {}
            cached_payload = cached_wrapper.get("payload") or {}
            cached_item = cached_payload.get("item")
            if isinstance(cached_item, dict):
                return cached_item
        params = {"part": "snippet,statistics,contentDetails", "maxResults": 1}
        if channel_id:
            params["id"] = channel_id
            cache_key = channel_id
        else:
            params["forHandle"] = handle.lstrip("@")
            cache_key = "handle-" + hashlib.sha256(handle.casefold().encode()).hexdigest()[:16]
        payload = self.youtube_get(
            "channels", "channels.list", params,
            CACHE_ROOT / self.niche_slug / "admin_seeds" / "resolve" / f"{cache_key}.json",
        )
        items = payload.get("items") or []
        return items[0] if items else None

    def resolve(self, value: str) -> tuple[str, dict[str, Any] | None]:
        raw = " ".join(str(value or "").split())
        if not raw:
            return "", None
        channel_id = raw if raw.startswith("UC") and "/" not in raw else ""
        handle = ""
        if raw.startswith("@"):
            handle = raw
        elif "://" in raw or raw.startswith("youtube.com/") or raw.startswith("www.youtube.com/"):
            url = raw if "://" in raw else "https://" + raw
            parts = [part for part in urlparse(url).path.split("/") if part]
            if len(parts) >= 2 and parts[0] == "channel":
                channel_id = parts[1]
            elif parts:
                handle = parts[-1]
        if channel_id or handle:
            item = self._channels_lookup(channel_id=channel_id, handle=handle)
            return (str((item or {}).get("id") or ""), item)

        params = {"part": "snippet", "q": raw, "type": "channel", "maxResults": 1}
        key = hashlib.sha256(raw.casefold().encode()).hexdigest()[:16]
        payload = self.youtube_get(
            "search", "search.list", params,
            CACHE_ROOT / self.niche_slug / "admin_seeds" / "name_search" / f"{key}.json",
        )
        item = (payload.get("items") or [None])[0]
        snippet = (item or {}).get("snippet") or {}
        if not item or _clean_name(snippet.get("title")) != _clean_name(raw):
            return "", None
        found_id = str(((item.get("id") or {}).get("channelId")) or snippet.get("channelId") or "")
        channel = self._channels_lookup(channel_id=found_id) if found_id else None
        return found_id, channel

    def name_candidates(self, value: str) -> list[dict[str, Any]]:
        name = str(value or "").strip().lstrip("@")
        params = {"part": "snippet", "q": name, "type": "channel", "maxResults": 3}
        key = hashlib.sha256(("fallback:" + name.casefold()).encode()).hexdigest()[:16]
        payload = self.youtube_get(
            "search", "search.list", params,
            CACHE_ROOT / self.niche_slug / "admin_seeds" / "name_search" / f"{key}.json",
        )
        output = []
        for item in (payload.get("items") or [])[:3]:
            snippet = item.get("snippet") or {}
            channel_id = str((item.get("id") or {}).get("channelId") or snippet.get("channelId") or "")
            channel_item = self._channels_lookup(channel_id=channel_id) if channel_id else None
            metadata = self._channel_metadata(channel_item or {})
            output.append({
                "title": metadata["title"] or str(snippet.get("title") or ""),
                "handle": str(((channel_item or {}).get("snippet") or {}).get("customUrl") or ""),
                "subscribers": metadata["subscriber_count"],
                "channel_id": channel_id,
            })
        return output

    @staticmethod
    def _channel_metadata(item: dict[str, Any]) -> dict[str, Any]:
        snippet = item.get("snippet") or {}
        stats = item.get("statistics") or {}
        playlists = ((item.get("contentDetails") or {}).get("relatedPlaylists") or {})
        def number(value: Any) -> int | None:
            try:
                return int(value)
            except (TypeError, ValueError):
                return None
        return {
            "title": str(snippet.get("title") or ""),
            "description": str(snippet.get("description") or ""),
            "country": str(snippet.get("country") or ""),
            "subscriber_count": number(stats.get("subscriberCount")),
            "uploads_playlist_id": str(playlists.get("uploads") or ""),
        }

    @staticmethod
    def _duration(value: Any) -> int | None:
        match = _DURATION_RE.fullmatch(str(value or ""))
        if not match:
            return None
        parts = {key: int(number or 0) for key, number in match.groupdict().items()}
        return parts["days"] * 86400 + parts["hours"] * 3600 + parts["minutes"] * 60 + parts["seconds"]

    def enrich(self, channel_id: str, channel_item: dict[str, Any] | None = None) -> dict[str, Any]:
        item = channel_item or self._channels_lookup(channel_id=channel_id)
        if not item:
            raise SeedError(f"Channel not found: {channel_id}")
        metadata = self._channel_metadata(item)
        playlist_id = metadata["uploads_playlist_id"]
        uploads: list[dict[str, Any]] = []
        cutoff = datetime.now(timezone.utc) - timedelta(days=90)
        token = ""
        page = 0
        while playlist_id:
            params: dict[str, Any] = {
                "part": "snippet,contentDetails", "playlistId": playlist_id, "maxResults": 50,
            }
            if token:
                params["pageToken"] = token
            payload = self.youtube_get(
                "playlistItems", "playlistItems.list", params,
                CACHE_ROOT / "uploads" / channel_id / f"page_{page:04d}.json",
            )
            crossed = False
            for upload in payload.get("items") or []:
                snippet = upload.get("snippet") or {}
                details = upload.get("contentDetails") or {}
                published_text = str(details.get("videoPublishedAt") or snippet.get("publishedAt") or "")
                try:
                    published = datetime.fromisoformat(published_text.replace("Z", "+00:00"))
                except ValueError:
                    published = None
                if published and published < cutoff:
                    crossed = True
                video_id = str(details.get("videoId") or (snippet.get("resourceId") or {}).get("videoId") or "")
                if video_id:
                    uploads.append({
                        "video_id": video_id, "title": " ".join(str(snippet.get("title") or "").split()),
                        "published": published_text, "within_90d": bool(published and published >= cutoff),
                    })
            token = str(payload.get("nextPageToken") or "")
            page += 1
            if not token or crossed or len(uploads) >= 100:
                break
        details_by_id: dict[str, dict[str, Any]] = {}
        for offset in range(0, len(uploads), 50):
            chunk = uploads[offset:offset + 50]
            joined = ",".join(item["video_id"] for item in chunk)
            params = {"part": "snippet,contentDetails,statistics", "id": joined, "maxResults": 50}
            key = hashlib.sha256(joined.encode()).hexdigest()[:16]
            payload = self.youtube_get(
                "videos", "videos.list", params, CACHE_ROOT / "video_batches" / f"{key}.json",
            )
            for video in payload.get("items") or []:
                stats = video.get("statistics") or {}
                details_by_id[str(video.get("id") or "")] = {
                    "duration": self._duration((video.get("contentDetails") or {}).get("duration")),
                    "views": int(stats.get("viewCount") or 0),
                }
        uploads.sort(key=lambda row: row["published"], reverse=True)
        titles = []
        for format_name, predicate in (("long", lambda d: d is not None and d > 180), ("short", lambda d: d is not None and d <= 180)):
            for upload in uploads:
                duration = (details_by_id.get(upload["video_id"]) or {}).get("duration")
                if predicate(duration) and upload["title"] and len(titles) < 15:
                    titles.append({"title": upload["title"], "format": format_name})
            if len(titles) >= 15:
                break
        recent = [row for row in uploads if row["within_90d"]]
        long_views, short_views = [], []
        for upload in recent:
            detail = details_by_id.get(upload["video_id"]) or {}
            if detail.get("duration") is None:
                continue
            (short_views if detail["duration"] <= 180 else long_views).append(int(detail.get("views") or 0))
        return {
            "channel": metadata, "titles": titles,
            "longform_90d": len(long_views), "shorts_90d": len(short_views),
            "avg_views_long_90d": round(sum(long_views) / len(long_views)) if long_views else None,
            "avg_views_short_90d": round(sum(short_views) / len(short_views)) if short_views else None,
            "views_90d": sum(long_views) + sum(short_views),
        }

    def classify(self, enriched: dict[str, Any]) -> dict[str, Any]:
        definition = self.definition
        focus_keys = [item["key"] for item in definition.get("focus_options") or []]
        type_options = definition.get("channel_type_options")
        type_keys = [item["key"] for item in type_options or []]
        source = {
            "niche": definition,
            "channel_title": enriched["channel"]["title"],
            "channel_description": enriched["channel"]["description"][:5000],
            "recent_titles": enriched["titles"],
            "admin_decisions": self.admin_decisions,
        }
        response = _openai_client.chat.completions.create(
            model=MODEL, temperature=0, max_tokens=900,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": (
                    "Classify a YouTube channel using the supplied niche configuration. Return JSON only with "
                    "primary_focus, language, entity, presenter, channel_type, title_results, reason. "
                    f"primary_focus is one of {focus_keys}. language is a short ISO key, mixed, or other. "
                    "entity is individual or company_clinic; presenter is real_person, org, or faceless. "
                    f"channel_type is one of {type_keys} when options exist, otherwise null. title_results must "
                    "have one core_topic boolean per supplied title. Judge MAIN channel focus, description first. "
                    "A target channel must TEACH or EXPLAIN its niche. Channels mainly offering workouts, yoga, "
                    "exercise classes, recipes, meal plans, or general lifestyle/mindset use the appropriate "
                    "neighbour focus even if titles mention the niche. Follow the configured exclusions and "
                    "channel-type definitions. Treat recent admin decisions as precedent for similar channels."
                )},
                {"role": "user", "content": json.dumps(source, ensure_ascii=False)},
            ],
        )
        raw = json.loads(response.choices[0].message.content or "{}")
        usage = response.usage
        self.llm_input_tokens += int(getattr(usage, "prompt_tokens", 0) or 0)
        self.llm_output_tokens += int(getattr(usage, "completion_tokens", 0) or 0)
        focus = str(raw.get("primary_focus") or "")
        if focus not in focus_keys:
            focus = "other" if "other" in focus_keys else (focus_keys[-1] if focus_keys else "")
        language = str(raw.get("language") or "other").casefold()
        entity = "individual" if str(raw.get("entity") or "").casefold() == "individual" else "company_clinic"
        presenter = str(raw.get("presenter") or "").casefold()
        if presenter not in {"real_person", "org", "faceless"}:
            presenter = "real_person" if entity == "individual" else "org"
        channel_type = str(raw.get("channel_type") or "").casefold() or None
        if type_keys and channel_type not in type_keys:
            channel_type = "other" if "other" in type_keys else type_keys[-1]
        if not type_keys:
            channel_type = None
        results = raw.get("title_results") if isinstance(raw.get("title_results"), list) else []
        flags = [(bool(item.get("core_topic")) if isinstance(item, dict) else bool(item)) for item in results]
        flags = (flags + [False] * len(enriched["titles"]))[:len(enriched["titles"])]
        return {
            "primary_focus": focus, "language": language, "entity": entity,
            "presenter": presenter, "channel_type": channel_type,
            "core_count": sum(flags), "title_count": len(enriched["titles"]),
            "core_share": sum(flags) / len(flags) if flags else 0.0,
            "reason": " ".join(str(raw.get("reason") or "").split())[:500],
            "classification_status": "classified" if enriched["titles"] else "no_titles",
        }

    def featured_ids(self, channel_id: str) -> list[str]:
        params = {"part": "snippet,contentDetails", "channelId": channel_id}
        payload = self.youtube_get(
            "channelSections", "channelSections.list", params,
            CACHE_ROOT / self.niche_slug / "featured" / f"{channel_id}.json",
        )
        output = []
        for item in payload.get("items") or []:
            if str((item.get("snippet") or {}).get("type") or "") == "multipleChannels":
                output.extend((item.get("contentDetails") or {}).get("channels") or [])
        return list(dict.fromkeys(str(value) for value in output if str(value).startswith("UC")))

    def persist_channel(
        self, channel_id: str, enriched: dict[str, Any], result: dict[str, Any],
        *, source: str, admin_include: bool, admin_reason: str = "added by admin",
    ) -> None:
        channel = enriched["channel"]
        in_niche = result["classification_status"] == "classified"
        conn = get_db()
        try:
            conn.execute(
                """
                INSERT INTO niche_channels (
                    niche_id,youtube_channel_id,snapshot_date,title,channel_url,credential,in_niche,
                    presenter,language,subscriber_count,longform_90d,shorts_90d,avg_views_long_90d,
                    avg_views_short_90d,source,replied,admin_note,updated_at,primary_niche,
                    on_topic_share,on_topic_count,entity,reclass_reason,views_90d,admin_exclude,
                    admin_include,exclude_reason,country,audience_language,primary_focus,
                    classification_status,classification_title_count,channel_type
                ) VALUES (?,?,?,?,?,'none',?,?,?,?,?,?,?,?,?,FALSE,'',CURRENT_TIMESTAMP,?,?,?,?,?,?,FALSE,?,?,?, ?,?,?,?,?)
                ON CONFLICT (niche_id,youtube_channel_id,snapshot_date) DO UPDATE SET
                    title=EXCLUDED.title,channel_url=EXCLUDED.channel_url,presenter=EXCLUDED.presenter,
                    language=EXCLUDED.language,subscriber_count=EXCLUDED.subscriber_count,
                    longform_90d=EXCLUDED.longform_90d,shorts_90d=EXCLUDED.shorts_90d,
                    avg_views_long_90d=EXCLUDED.avg_views_long_90d,
                    avg_views_short_90d=EXCLUDED.avg_views_short_90d,source=EXCLUDED.source,
                    primary_niche=EXCLUDED.primary_niche,on_topic_share=EXCLUDED.on_topic_share,
                    on_topic_count=EXCLUDED.on_topic_count,entity=EXCLUDED.entity,
                    reclass_reason=EXCLUDED.reclass_reason,views_90d=EXCLUDED.views_90d,
                    country=EXCLUDED.country,audience_language=EXCLUDED.audience_language,
                    primary_focus=EXCLUDED.primary_focus,classification_status=EXCLUDED.classification_status,
                    classification_title_count=EXCLUDED.classification_title_count,
                    channel_type=EXCLUDED.channel_type,
                    admin_exclude=CASE WHEN EXCLUDED.admin_include THEN FALSE ELSE niche_channels.admin_exclude END,
                    admin_include=CASE WHEN EXCLUDED.admin_include THEN TRUE ELSE niche_channels.admin_include END,
                    exclude_reason=CASE WHEN EXCLUDED.admin_include THEN EXCLUDED.exclude_reason ELSE niche_channels.exclude_reason END,
                    updated_at=CURRENT_TIMESTAMP
                """,
                [
                    self.definition["id"], channel_id, self.selected["snapshot_date"],
                    channel["title"] or channel_id, f"https://www.youtube.com/channel/{channel_id}",
                    "yes" if in_niche else "no", result["presenter"],
                    "Turkish" if result["language"] == "tr" else result["language"],
                    channel["subscriber_count"], enriched["longform_90d"], enriched["shorts_90d"],
                    enriched["avg_views_long_90d"], enriched["avg_views_short_90d"], source,
                    self.niche_slug, result["core_share"], result["core_count"], result["entity"],
                    result["reason"], enriched["views_90d"], admin_include,
                    admin_reason if admin_include else None, channel["country"].upper(),
                    result["language"], result["primary_focus"], result["classification_status"],
                    result["title_count"], result["channel_type"],
                ],
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        self.existing_by_id[channel_id] = {"youtube_channel_id": channel_id}

    def write_run(self, notes: str) -> int:
        conn = get_db()
        try:
            selected, rows = load_niche_snapshot(conn, self.niche_slug, self.selected["snapshot_date"])
            run_id = conn.execute(
                "INSERT INTO niche_classification_runs (niche_id,run_at,rule_version,notes) "
                "VALUES (?,CURRENT_TIMESTAMP,?,?) RETURNING run_id",
                [self.definition["id"], "generic-v3.1-admin-seeds", notes],
            ).fetchone()[0]
            for row in rows:
                conn.execute(
                    "INSERT INTO niche_channel_run_results "
                    "(run_id,youtube_channel_id,in_pool,primary_niche,on_topic_share,reason) "
                    "VALUES (?,?,?,?,?,?)",
                    [run_id, row["youtube_channel_id"], is_final_niche_pool_row(row, selected),
                     row.get("primary_focus"), float(row.get("on_topic_share") or 0), row.get("reclass_reason")],
                )
            conn.commit()
            return int(run_id)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def run(
        self,
        lines: list[str],
        *,
        featured_seed_ids: list[str] | None = None,
        admin_reason: str = "added by admin",
    ) -> dict[str, Any]:
        report: dict[str, Any] = {
            "resolved": [], "unresolved": [], "snowball": [], "remaining": [],
            "unresolved_candidates": {}, "stopped_reason": "",
        }
        explicit_ids: list[str] = []
        try:
            for index, line in enumerate(lines):
                channel_id, item = self.resolve(line)
                if not channel_id or not item:
                    report["unresolved"].append(line)
                    if line.startswith("@"):
                        report["unresolved_candidates"][line] = self.name_candidates(line)
                    continue
                enriched = self.enrich(channel_id, item)
                result = self.classify(enriched)
                self.persist_channel(
                    channel_id, enriched, result, source="admin_seed",
                    admin_include=True, admin_reason=admin_reason,
                )
                explicit_ids.append(channel_id)
                report["resolved"].append({
                    "input": line, "channel_id": channel_id, "title": enriched["channel"]["title"],
                    "channel_type": result["channel_type"], "primary_focus": result["primary_focus"],
                })
            featured_sources = list(dict.fromkeys(explicit_ids + (featured_seed_ids or [])))
            for source_id in featured_sources:
                for channel_id in self.featured_ids(source_id):
                    if channel_id in self.existing_by_id:
                        continue
                    enriched = self.enrich(channel_id)
                    result = self.classify(enriched)
                    self.persist_channel(
                        channel_id, enriched, result, source="snowball_featured", admin_include=False
                    )
                    row = {
                        "source_channel_id": source_id, "channel_id": channel_id,
                        "title": enriched["channel"]["title"], "channel_type": result["channel_type"],
                        "primary_focus": result["primary_focus"],
                    }
                    row["joined_pool"] = is_final_niche_pool_row({
                        "admin_include": False, "admin_exclude": False, "in_niche": "yes",
                        "primary_focus": result["primary_focus"], "audience_language": result["language"],
                        "language": result["language"], "entity": result["entity"],
                        "presenter": result["presenter"], "classification_status": result["classification_status"],
                        "channel_type": result["channel_type"],
                    }, self.definition)
                    report["snowball"].append(row)
        except (YoutubeQuotaCapExceeded, requests.RequestException, SeedError) as exc:
            report["stopped_reason"] = str(exc)
            completed = {row["input"] for row in report["resolved"]} | set(report["unresolved"])
            report["remaining"] = [line for line in lines if line not in completed]
        report["run_id"] = self.write_run("admin seeds + featured snowball")
        conn = get_db_readonly()
        try:
            selected, rows = load_niche_snapshot(conn, self.niche_slug, self.selected["snapshot_date"])
        finally:
            conn.close()
        pool = sorted(
            (row for row in rows if is_final_niche_pool_row(row, selected)),
            key=lambda row: (-int(row.get("subscriber_count") or 0), str(row.get("title") or "").casefold()),
        )
        rank_by_id = {
            str(row.get("youtube_channel_id") or ""): rank for rank, row in enumerate(pool, 1)
        }
        for row in report["resolved"]:
            row["rank"] = rank_by_id.get(row["channel_id"])
        for row in report["snowball"]:
            row["rank"] = rank_by_id.get(row["channel_id"])
        report["pool_size"] = len(pool)
        report["youtube"] = {
            "calls": dict(self.calls), "units_used": self.units_used, "name_searches": self.searches,
        }
        report["llm"] = {
            "input_tokens": self.llm_input_tokens, "output_tokens": self.llm_output_tokens,
            "cost_usd": (self.llm_input_tokens * 0.4 + self.llm_output_tokens * 1.6) / 1_000_000,
        }
        return report

    def fill_missing_data(self) -> dict[str, Any]:
        conn = get_db_readonly()
        try:
            selected, rows = load_niche_snapshot(
                conn, self.niche_slug, self.selected["snapshot_date"]
            )
        finally:
            conn.close()
        targets = [
            row for row in rows
            if int(row.get("views_90d") or 0) == 0
            or not any((CACHE_ROOT / "uploads" / str(row.get("youtube_channel_id") or "")).glob("page_*.json"))
        ]
        filled = []
        still_zero = []
        stopped_reason = ""
        for index, row in enumerate(targets):
            channel_id = str(row.get("youtube_channel_id") or "")
            try:
                enriched = self.enrich(channel_id)
                conn = get_db()
                try:
                    conn.execute(
                        """
                        UPDATE niche_channels
                        SET views_90d=?, longform_90d=?, shorts_90d=?,
                            avg_views_long_90d=?, avg_views_short_90d=?,
                            updated_at=CURRENT_TIMESTAMP
                        WHERE niche_id=? AND snapshot_date=? AND youtube_channel_id=?
                        """,
                        [
                            enriched["views_90d"], enriched["longform_90d"],
                            enriched["shorts_90d"], enriched["avg_views_long_90d"],
                            enriched["avg_views_short_90d"], self.definition["id"],
                            self.selected["snapshot_date"], channel_id,
                        ],
                    )
                    conn.commit()
                finally:
                    conn.close()
                if int(enriched["views_90d"] or 0) > 0:
                    filled.append({
                        "channel_id": channel_id, "title": enriched["channel"]["title"],
                        "views_90d": int(enriched["views_90d"]),
                    })
                else:
                    reason = (
                        "no public uploads playlist"
                        if not enriched["channel"].get("uploads_playlist_id")
                        else "no views on videos posted in the last 90 days"
                    )
                    still_zero.append({
                        "channel_id": channel_id,
                        "title": enriched["channel"]["title"] or row.get("title"),
                        "reason": reason,
                    })
            except YoutubeQuotaCapExceeded as exc:
                stopped_reason = str(exc)
                for remaining in targets[index:]:
                    still_zero.append({
                        "channel_id": remaining.get("youtube_channel_id"),
                        "title": remaining.get("title"), "reason": "quota cap reached",
                    })
                break
            except Exception as exc:
                still_zero.append({
                    "channel_id": channel_id, "title": row.get("title"),
                    "reason": str(exc)[:300],
                })
        return {
            "targets": len(targets), "filled": filled, "still_zero": still_zero,
            "stopped_reason": stopped_reason,
        }


def add_niche_channels(
    niche_slug: str,
    lines: list[str],
    *,
    featured_seed_ids: list[str] | None = None,
    admin_reason: str = "added by admin",
    fill_missing: bool = False,
) -> dict[str, Any]:
    runner = SeedRunner(niche_slug)
    try:
        report = runner.run(
            lines,
            featured_seed_ids=featured_seed_ids,
            admin_reason=admin_reason,
        )
        if fill_missing:
            report["missing_data"] = runner.fill_missing_data()
            report["youtube"] = {
                "calls": dict(runner.calls), "units_used": runner.units_used,
                "name_searches": runner.searches,
            }
        return report
    finally:
        runner.close()
