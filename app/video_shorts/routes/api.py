import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

from flask import current_app, flash, g, jsonify, redirect, request, url_for

from app.video_shorts import video_shorts_bp
from app.video_shorts.config import CAPTION_API_TOKEN, OPENAI_MODEL, _openai_client
from app.video_shorts.services.auth_protection import RateLimitRule, check_rate_limits
from app.video_shorts.services.brands import current_brand_id, ensure_brand_schema
from app.video_shorts.services.db import (
    _ensure_transcript_schema,
    _ensure_video_crop_schema,
    ensure_channel_owner_schema,
    ensure_postgres_youtube_transcripts_id_default,
    ensure_youtube_video_local_bucket_schema,
    get_db,
    get_db_readonly,
    table_columns,
)
from app.video_shorts.services.error_capture import CLIENT_ERROR_MAX_BODY_BYTES, capture_client_error, current_event_user_id
from app.video_shorts.services.email_verification import send_autopilot_upgrade_request_email
from app.video_shorts.services.render_jobs import JOB_TYPE_ENRICH_DISCOVERY_EMAILS, enqueue_preview_frame_job, enqueue_worker_job, get_job
from app.video_shorts.services.transcript_service import _normalize_segments_for_use
from app.video_shorts.services.user_events import prepare_transcript_completed_transition, track_event
from app.video_shorts.services.usage_metering import add_transcription_minutes, get_usage_snapshot
from app.video_shorts.services.autopilot_leads import (
    AutopilotLeadSchemaUnavailable,
    create_autopilot_lead_from_video,
)
from app.video_shorts.services.apify_enrich import apify_trakk_enrich
from app.video_shorts.youtube_api import (
    YoutubeApiError,
    _parse_duration_iso8601,
    _youtube_get_json,
    extract_channel_id,
    extract_video_id,
    fetch_channel_subscriber_counts,
    fetch_playlist_items_batch,
    fetch_video_metadata,
    get_channel_metadata,
)
from app.video_shorts.routes.videos import _get_or_create_real_youtube_channel, _load_admin_global_outreach_match


CLIENT_ERROR_RATE_LIMITS = [
    RateLimitRule(limit=10, window_seconds=60),
    RateLimitRule(limit=30, window_seconds=3600),
]
LONGFORM_WINDOW_DAYS = 60
SHORTS_WINDOW_DAYS = 15
UPLOAD_SAMPLE_SIZE = 50
EMAIL_RE = re.compile(r"\b[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}\b", re.IGNORECASE)
CONTACT_LINE_RE = re.compile(r"(iletisim|iletişim|contact|business)", re.IGNORECASE)
LEAD_DISCOVERY_DEFAULT_MIN_SUBSCRIBERS = 1_000
LEAD_DISCOVERY_DEFAULT_MAX_SUBSCRIBERS = 2_000_000
LEAD_DISCOVERY_DEFAULT_MIN_LONGFORM_60D = 2
LEAD_DISCOVERY_DEFAULT_MAX_SHORTS_15D = 3
LEAD_DISCOVERY_DEFAULT_MIN_LONGFORM_SECONDS = 300
LEAD_DISCOVERY_DEFAULT_SHORT_MAX_SECONDS = 180
LEAD_DISCOVERY_MAX_KEYWORDS = 10
LEAD_DISCOVERY_MAX_RESULTS_PER_KEYWORD = 40
LEAD_DISCOVERY_MAX_CHANNELS_ENRICHED = 150
LEAD_DISCOVERY_EMAIL_VIDEO_DESCRIPTION_LIMIT = 5
LEAD_DISCOVERY_SEED_CHANNEL_LIMIT = 50
LEAD_DISCOVERY_SEED_RECENT_TITLES = 5
LEAD_DISCOVERY_EMAIL_ENRICH_LIMIT = 100
LEAD_DISCOVERY_QUEUE_TAKE_DEFAULT = 10
LEAD_DISCOVERY_QUEUE_TAKE_MAX = 20
SYNTHETIC_SEED_PREFIX = "[Synthetic discovery seed - no transcript]"
DISCOVERY_PROMOTION_OWNER_USER_ID = "f97df4cb-93de-4761-9c39-62d303261b0a"
DISCOVERY_PROMOTION_BRAND_ID = "63f772f8-2d31-4416-9239-c546949bfa98"


def _duration_minutes(duration_seconds) -> float:
    try:
        seconds = float(duration_seconds or 0)
    except Exception:
        seconds = 0.0
    if seconds <= 0:
        return 0.0
    return round(seconds / 60.0, 2)


def _check_caption_token(req):
    token = req.headers.get("X-Api-Token")
    return bool(token and token == CAPTION_API_TOKEN)


def _parse_yt_timestamp(value):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _resolve_channel_context(raw_url: str):
    candidate = str(raw_url or "").strip()
    if not candidate:
        return None, None, ("missing_url", "Missing url.")

    video_id = extract_video_id(candidate)
    if video_id:
        meta = fetch_video_metadata(video_id)
        channel_id = str(meta.get("channel_id") or "").strip()
        if not channel_id:
            return None, None, ("channel_not_found", "Channel could not be resolved from video metadata.")
        return channel_id, meta, None

    channel_id = extract_channel_id(candidate)
    if not channel_id:
        return None, None, ("invalid_url", "Unsupported or invalid YouTube URL.")
    return str(channel_id).strip(), None, None


def _collect_recent_uploads(
    uploads_playlist_id: str,
    *,
    limit: int = UPLOAD_SAMPLE_SIZE,
    quota_counter: Optional[Dict[str, int]] = None,
):
    videos = []
    page_token = None
    while len(videos) < limit:
        if quota_counter is not None:
            quota_counter["enrichment_read_calls"] = int(quota_counter.get("enrichment_read_calls") or 0) + 1
        batch = fetch_playlist_items_batch(
            playlist_id=uploads_playlist_id,
            page_token=page_token,
            max_results=min(50, limit),
        )
        batch_videos = batch.get("videos") or []
        if not batch_videos:
            break
        videos.extend(batch_videos)
        page_token = batch.get("next_page_token")
        if not page_token:
            break
    return videos[:limit]


def _subscriber_gate(subscriber_count):
    try:
        count = int(subscriber_count)
    except (TypeError, ValueError):
        return "hedef_disi"
    if 5_000 <= count <= 100_000:
        return "uygun"
    if count < 5_000:
        return "hedef_disi"
    if count <= 300_000:
        return "uygun"
    return "hedef_disi"


def _youtube_env_error_message(exc: Exception) -> str | None:
    message = str(exc or "").strip()
    lowered = message.lower()
    if "youtub" in lowered and ("api key" in lowered or "oauth" in lowered):
        return "YouTube API credentials are not configured for this environment."
    return None


def _resolve_brand_local_uploads_channel(conn, brand_id: str):
    row = conn.execute(
        """
        SELECT b.owner_user_id, c.channel_id, c.owner_user_id, c.brand_id
        FROM shorts_brands b
        LEFT JOIN youtube_channels c
          ON c.brand_id = b.id
         AND c.owner_user_id = b.owner_user_id
         AND lower(COALESCE(c.channel_url, '')) = 'local://uploads'
         AND COALESCE(c.is_active, true) = true
        WHERE b.id = ?
        LIMIT 1
        """,
        [brand_id],
    ).fetchone()
    if not row:
        return None
    owner_user_id = str(row[0] or "").strip() or None
    local_channel_id = row[1]
    local_owner_user_id = str(row[2] or "").strip() or None
    local_brand_id = str(row[3] or "").strip() or None
    if not owner_user_id or local_channel_id is None:
        return None
    if local_owner_user_id != owner_user_id or local_brand_id != brand_id:
        return None
    return {
        "owner_user_id": owner_user_id,
        "channel_id": local_channel_id,
        "brand_id": brand_id,
    }


def _video_already_in_bucket(conn, video_id: str, local_bucket_channel_id) -> bool:
    if not video_id or local_bucket_channel_id is None:
        return False
    row = conn.execute(
        """
        SELECT id
        FROM youtube_videos
        WHERE video_id = ?
          AND (channel_id = ? OR local_bucket_channel_id = ?)
        LIMIT 1
        """,
        [video_id, local_bucket_channel_id, local_bucket_channel_id],
    ).fetchone()
    return bool(row)


def _count_videos_for_creator_channel(conn, channel_id: str) -> int:
    normalized_channel_id = str(channel_id or "").strip()
    if not normalized_channel_id:
        return 0
    row = conn.execute(
        """
        SELECT COUNT(*)
        FROM youtube_videos v
        JOIN youtube_channels c
          ON c.channel_id = v.channel_id
        WHERE COALESCE(c.youtube_channel_id, '') = ?
        """,
        [normalized_channel_id],
    ).fetchone()
    try:
        return int((row or [0])[0] or 0)
    except Exception:
        return 0


def _extract_creator_email(description: str | None) -> str | None:
    match = EMAIL_RE.search(str(description or ""))
    if not match:
        return None
    return match.group(0).strip() or None


def _looks_like_urlish_name(value: str) -> bool:
    candidate = str(value or "").strip().lower()
    if not candidate:
        return False
    blocked_fragments = ("http", "www.", ".com", ".net", ".org", "/", "@")
    if any(fragment in candidate for fragment in blocked_fragments):
        return True
    return "." in candidate and "/" in candidate


def _extract_creator_name(description: str | None, channel_title: str | None) -> str | None:
    for raw_line in str(description or "").splitlines():
        line = raw_line.strip()
        if not line or not CONTACT_LINE_RE.search(line):
            continue
        if ":" not in line:
            continue
        candidate = line.split(":", 1)[1].strip()
        candidate = EMAIL_RE.sub("", candidate).strip(" -|,;/")
        if _looks_like_urlish_name(candidate):
            continue
        parts = [part for part in re.split(r"\s+", candidate) if part]
        if 1 <= len(parts) <= 4 and all(any(ch.isalpha() for ch in part) for part in parts):
            return " ".join(parts)[:255]
    fallback = str(channel_title or "").strip()
    return fallback[:255] if fallback else None


def _resolve_auto_creator_email(channel_description: str | None, video_description: str | None) -> str | None:
    return _extract_creator_email(channel_description) or _extract_creator_email(video_description)


def _resolve_creator_email_with_source(
    channel_description: str | None,
    video_descriptions: List[str] | None = None,
    fallback_video_description: str | None = None,
) -> tuple[str | None, str | None]:
    channel_email = _extract_creator_email(channel_description)
    if channel_email:
        return channel_email, "channel_desc"
    for description in video_descriptions or []:
        video_email = _extract_creator_email(description)
        if video_email:
            return video_email, "video_desc"
    fallback_email = _extract_creator_email(fallback_video_description)
    if fallback_email:
        return fallback_email, "video_desc"
    return None, None


def _resolve_auto_creator_name(
    channel_description: str | None,
    video_description: str | None,
    channel_title: str | None,
) -> str | None:
    from_channel = _extract_creator_name(channel_description, None)
    if from_channel:
        return from_channel
    from_video = _extract_creator_name(video_description, None)
    if from_video:
        return from_video
    fallback = str(channel_title or "").strip()
    return fallback[:255] if fallback else None


_CREATOR_FIELD_UNSET = object()


def _normalize_manual_creator_name(value, channel_title: str | None) -> str | None:
    candidate = str(value or "").strip()
    if not candidate:
        return None
    if _looks_like_urlish_name(candidate):
        fallback = str(channel_title or "").strip()
        return fallback[:255] if fallback else None
    return candidate[:255]


def _normalize_manual_creator_email(value) -> str | None:
    candidate = str(value or "").strip()
    if not candidate:
        return None
    if "@" not in candidate or "." not in candidate:
        return None
    return candidate[:255]


def _update_existing_creator_fields(conn, row_id, *, creator_name=_CREATOR_FIELD_UNSET, creator_email=_CREATOR_FIELD_UNSET) -> bool:
    assignments = []
    params = []
    if creator_name is not _CREATOR_FIELD_UNSET:
        assignments.append("creator_name = ?")
        params.append(creator_name)
    if creator_email is not _CREATOR_FIELD_UNSET:
        assignments.append("creator_email = ?")
        params.append(creator_email)
    if not assignments:
        return False
    conn.execute(
        f"""
        UPDATE youtube_videos
        SET {", ".join(assignments)}
        WHERE id = ?
        """,
        params + [row_id],
    )
    return True


def _json_error(error: str, message: str, status: int):
    return jsonify({"error": error, "message": message}), status


def _coerce_video_pk(value):
    try:
        return int(value)
    except Exception:
        return value


def _increment_enrichment_counter(quota_counter: Optional[Dict[str, int]], amount: int = 1) -> None:
    if quota_counter is not None:
        quota_counter["enrichment_read_calls"] = int(quota_counter.get("enrichment_read_calls") or 0) + max(0, int(amount or 0))


def _fetch_video_details(video_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    clean_ids = [str(video_id or "").strip() for video_id in video_ids if str(video_id or "").strip()]
    if not clean_ids:
        return {}
    details: Dict[str, Dict[str, Any]] = {}
    for i in range(0, len(clean_ids), 50):
        chunk = clean_ids[i : i + 50]
        payload = _youtube_get_json(
            "videos",
            {
                "part": "snippet,contentDetails,statistics",
                "id": ",".join(chunk),
            },
            timeout=10,
            require_auth=False,
        )
        for item in payload.get("items") or []:
            video_id = str(item.get("id") or "").strip()
            if not video_id:
                continue
            snippet = item.get("snippet") or {}
            content_details = item.get("contentDetails") or {}
            statistics = item.get("statistics") or {}
            try:
                duration_seconds = _parse_duration_iso8601(content_details.get("duration") or "")
            except Exception:
                duration_seconds = 0

            def _to_int(value):
                try:
                    return int(value)
                except Exception:
                    return None

            details[video_id] = {
                "duration_seconds": duration_seconds or None,
                "description": snippet.get("description") or "",
                "title": snippet.get("title") or "",
                "view_count": _to_int(statistics.get("viewCount")),
                "like_count": _to_int(statistics.get("likeCount")),
                "comment_count": _to_int(statistics.get("commentCount")),
            }
    return details


def diagnose_channel(
    channel_id: str,
    *,
    video_meta: Optional[Dict[str, Any]] = None,
    quota_counter: Optional[Dict[str, int]] = None,
    min_longform_seconds: int = LEAD_DISCOVERY_DEFAULT_MIN_LONGFORM_SECONDS,
    short_max_seconds: int = LEAD_DISCOVERY_DEFAULT_SHORT_MAX_SECONDS,
    email_video_description_limit: int = LEAD_DISCOVERY_EMAIL_VIDEO_DESCRIPTION_LIMIT,
    count_unknown_as_short: bool = False,
) -> Dict[str, Any]:
    resolved_channel_id = str(channel_id or "").strip()
    if not resolved_channel_id:
        raise YoutubeApiError("Channel not found on YouTube")

    channel_lookup_url = f"https://www.youtube.com/channel/{resolved_channel_id}"
    _increment_enrichment_counter(quota_counter)
    channel_meta = get_channel_metadata(channel_lookup_url)
    _increment_enrichment_counter(quota_counter)
    subscriber_map = fetch_channel_subscriber_counts([resolved_channel_id])
    subscriber_info = subscriber_map.get(resolved_channel_id) or {}
    recent_uploads = _collect_recent_uploads(
        channel_meta["uploads_playlist_id"],
        limit=UPLOAD_SAMPLE_SIZE,
        quota_counter=quota_counter,
    )
    video_ids = [item.get("video_id") for item in recent_uploads if item.get("video_id")]
    if video_ids:
        _increment_enrichment_counter(quota_counter, (len(video_ids) + 49) // 50)
    stats_map = _fetch_video_details(video_ids)
    sweetspot = _select_sweetspot_from_uploads(recent_uploads, stats_map)

    now_utc = datetime.now(timezone.utc)
    longform_last_60d = 0
    shorts_last_15d = 0
    latest_short_dt = None
    min_longform_seconds = max(0, int(min_longform_seconds or 0))
    short_max_seconds = max(0, int(short_max_seconds or 0))
    for item in recent_uploads:
        video_id = str(item.get("video_id") or "").strip()
        published_at = _parse_yt_timestamp(item.get("published_at"))
        duration_seconds = (stats_map.get(video_id) or {}).get("duration_seconds")
        try:
            duration_value = int(duration_seconds or 0)
        except (TypeError, ValueError):
            duration_value = 0
        if duration_value <= short_max_seconds and (duration_value > 0 or count_unknown_as_short):
            if published_at and (latest_short_dt is None or published_at > latest_short_dt):
                latest_short_dt = published_at
            if published_at and published_at >= now_utc - timedelta(days=SHORTS_WINDOW_DAYS):
                shorts_last_15d += 1
            continue
        if duration_value and duration_value > min_longform_seconds and published_at and published_at >= now_utc - timedelta(days=LONGFORM_WINDOW_DAYS):
            longform_last_60d += 1

    subscriber_count = subscriber_info.get("subscriber_count")
    gate_subscriber = _subscriber_gate(subscriber_count)
    gate_cadence = "aktif" if longform_last_60d >= LEAD_DISCOVERY_DEFAULT_MIN_LONGFORM_60D else "aktif_degil"
    channel_title = (
        subscriber_info.get("channel_title")
        or (video_meta or {}).get("channel_title")
        or None
    )
    channel_description = subscriber_info.get("channel_description")
    creator_name = _resolve_auto_creator_name(
        channel_description,
        (video_meta or {}).get("description"),
        channel_title,
    )
    recent_video_descriptions = [
        str((stats_map.get(str(item.get("video_id") or "").strip()) or {}).get("description") or "")
        for item in recent_uploads[: max(0, int(email_video_description_limit or 0))]
        if str(item.get("video_id") or "").strip()
    ]
    creator_email, email_source = _resolve_creator_email_with_source(
        channel_description,
        recent_video_descriptions,
        (video_meta or {}).get("description"),
    )
    return {
        "channel_id": resolved_channel_id,
        "channel_title": channel_title,
        "channel_description": channel_description,
        "subscriber_count": subscriber_count,
        "creator_name": creator_name,
        "creator_email": creator_email,
        "email_source": email_source,
        "eligible": gate_subscriber != "hedef_disi" and gate_cadence == "aktif",
        "gate_subscriber": gate_subscriber,
        "gate_cadence": gate_cadence,
        "longform_last_60d": longform_last_60d,
        "shorts_last_15d": shorts_last_15d,
        "shorts_color": "yellow" if shorts_last_15d >= 7 else "green",
        "shorts_window": "last_15_days",
        "latest_short_date": latest_short_dt.isoformat().replace("+00:00", "Z") if latest_short_dt else None,
        "sweetspot_score": sweetspot.get("score") if sweetspot else None,
        "best_source_video_id": sweetspot.get("video_id") if sweetspot else None,
        "best_source_video_minutes": sweetspot.get("minutes") if sweetspot else None,
    }


def _coerce_int_param(value: Any, default: int, *, minimum: int = 0, maximum: Optional[int] = None) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    parsed = max(minimum, parsed)
    if maximum is not None:
        parsed = min(maximum, parsed)
    return parsed


def _normalize_discovery_keywords(raw_keywords: Any) -> List[str]:
    if isinstance(raw_keywords, list):
        values = raw_keywords
    else:
        values = re.split(r"[\n,;]+", str(raw_keywords or ""))
    keywords: List[str] = []
    seen = set()
    for value in values:
        keyword = " ".join(str(value or "").strip().split())
        if not keyword:
            continue
        key = keyword.lower()
        if key in seen:
            continue
        seen.add(key)
        keywords.append(keyword[:120])
    return keywords[:LEAD_DISCOVERY_MAX_KEYWORDS]


def _generate_lead_discovery_keywords(niche: str, max_keywords: int, lang: str) -> List[str]:
    if not _openai_client:
        raise RuntimeError("OPENAI_API_KEY missing")
    prompt = (
        "Generate YouTube search keywords for finding creator channels in this niche.\n"
        "Return JSON only: {\"keywords\":[...]}.\n"
        f"Language code: {lang or 'en'}\n"
        f"Maximum keywords: {max_keywords}\n"
        f"Niche: {niche}"
    )
    response = _openai_client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": "You create concise YouTube discovery search keywords."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.35,
    )
    content = (response.choices[0].message.content or "").strip()
    try:
        payload = json.loads(content)
        return _normalize_discovery_keywords(payload.get("keywords"))[:max_keywords]
    except Exception:
        return _normalize_discovery_keywords(content)[:max_keywords]


def _search_youtube_channels_for_keyword(keyword: str, *, max_results: int, lang: str, region: str) -> tuple[List[Dict[str, str]], int]:
    params: Dict[str, Any] = {
        "part": "snippet,id",
        "q": keyword,
        "type": "video",
        "maxResults": max_results,
        "order": "relevance",
    }
    if lang:
        params["relevanceLanguage"] = lang
    if region:
        params["regionCode"] = region
    payload = _youtube_get_json("search", params, timeout=10)
    candidates: List[Dict[str, str]] = []
    items = payload.get("items") or []
    for item in items:
        snippet = item.get("snippet") or {}
        channel_id = str(snippet.get("channelId") or "").strip()
        if not channel_id:
            continue
        candidates.append(
            {
                "channel_id": channel_id,
                "channel_title": str(snippet.get("channelTitle") or "").strip(),
                "matched_keyword": keyword,
            }
        )
    return candidates, len(items)


def _sweetspot_score_for_minutes(minutes: float) -> Optional[int]:
    if minutes < 5:
        return None
    if minutes < 10:
        return 60
    if minutes < 15:
        return 100
    if minutes < 20:
        return 85
    if minutes < 25:
        return 80
    if minutes < 30:
        return 55
    if minutes < 35:
        return 30
    return 5


def _select_sweetspot_from_uploads(recent_uploads: List[Dict[str, Any]], stats_map: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    best: Optional[Dict[str, Any]] = None
    for index, item in enumerate((recent_uploads or [])[:5]):
        video_id = str(item.get("video_id") or "").strip()
        if not video_id:
            continue
        details = stats_map.get(video_id) or {}
        try:
            duration_seconds = float(details.get("duration_seconds") or 0)
        except (TypeError, ValueError):
            duration_seconds = 0
        minutes = duration_seconds / 60.0 if duration_seconds > 0 else 0
        score = _sweetspot_score_for_minutes(minutes)
        if score is None:
            continue
        published_at = _parse_yt_timestamp(item.get("published_at"))
        candidate = {
            "video_id": video_id,
            "canonical_url": f"https://www.youtube.com/watch?v={video_id}",
            "minutes": round(minutes, 2),
            "score": int(score),
            "published_at": published_at,
            "recent_index": index,
        }
        if best is None:
            best = candidate
            continue
        best_dt = best.get("published_at")
        candidate_dt = candidate.get("published_at")
        if score > int(best.get("score") or 0):
            best = candidate
        elif score == int(best.get("score") or 0):
            if candidate_dt and best_dt and candidate_dt > best_dt:
                best = candidate
            elif candidate_dt and not best_dt:
                best = candidate
            elif not candidate_dt and not best_dt and index < int(best.get("recent_index") or 999):
                best = candidate
    if not best:
        return None
    return {
        "video_id": best["video_id"],
        "canonical_url": best["canonical_url"],
        "minutes": best["minutes"],
        "score": best["score"],
    }


def select_source_video_for_channel(youtube_channel_id: str) -> Optional[Dict[str, Any]]:
    clean_channel_id = str(youtube_channel_id or "").strip()
    if not clean_channel_id:
        return None
    channel_meta = get_channel_metadata(f"https://www.youtube.com/channel/{clean_channel_id}")
    uploads_playlist_id = str(channel_meta.get("uploads_playlist_id") or "").strip()
    if not uploads_playlist_id:
        return None
    recent_uploads = _collect_recent_uploads(uploads_playlist_id, limit=5)
    video_ids = [str(item.get("video_id") or "").strip() for item in recent_uploads if str(item.get("video_id") or "").strip()]
    stats_map = _fetch_video_details(video_ids)
    return _select_sweetspot_from_uploads(recent_uploads, stats_map)


def _classify_lead_discovery_icp(niche: str, row: Dict[str, Any]) -> Dict[str, Any]:
    if not _openai_client:
        return {"icp_fit": None, "icp_reason": "OpenAI is not configured."}
    channel_title = str(row.get("channel_title") or "")
    channel_description = str(row.get("channel_description") or "")[:1400]
    sweetspot_score = row.get("sweetspot_score")
    sweetspot_minutes = row.get("best_source_video_minutes")
    source_signal = (
        f"Best recent source-video sweet-spot score: {sweetspot_score}; minutes: {sweetspot_minutes}. "
        "Score is based on recent video duration suitability for making demo Shorts. "
        "All recent videos under 5 minutes or over 35 minutes is a weak Minti fit signal, but not a hard exclusion."
    )
    prompt = (
        "Decide if this YouTube creator channel fits the target niche.\n"
        "Return JSON only: {\"icp_fit\":true|false,\"reason\":\"one short sentence\"}.\n\n"
        f"Niche: {niche}\n"
        f"Channel title: {channel_title}\n"
        f"Channel description: {channel_description}\n"
        f"{source_signal}"
    )
    response = _openai_client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": "You classify creator-channel ICP fit for B2B lead discovery."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.1,
    )
    content = (response.choices[0].message.content or "").strip()
    try:
        payload = json.loads(content)
    except Exception:
        return {"icp_fit": None, "icp_reason": content[:180] or "Could not parse ICP response."}
    return {
        "icp_fit": bool(payload.get("icp_fit")),
        "icp_reason": str(payload.get("reason") or "").strip()[:220],
    }


def _load_emailed_seed_channels(limit: int = LEAD_DISCOVERY_SEED_CHANNEL_LIMIT) -> List[Dict[str, Any]]:
    conn = get_db_readonly()
    try:
        has_scheduled = bool(table_columns(conn, "outreach_scheduled_emails"))
        sent_clause = "sl.emailed_at IS NOT NULL"
        join_scheduled = ""
        if has_scheduled:
            join_scheduled = "LEFT JOIN outreach_scheduled_emails ose ON ose.share_link_id = sl.id"
            sent_clause = "(sl.emailed_at IS NOT NULL OR ose.sent_at IS NOT NULL OR ose.status = 'sent')"
        rows = conn.execute(
            f"""
            SELECT
                l.youtube_channel_id,
                COALESCE(c.channel_name, l.creator_name, '') AS channel_name,
                COALESCE(v.title, '') AS first_video_title,
                MAX(COALESCE(sl.emailed_at, {'ose.sent_at' if has_scheduled else 'NULL'})) AS last_sent_at
            FROM autopilot_leads l
            JOIN short_share_links sl
              ON CAST(sl.autopilot_lead_id AS VARCHAR) = CAST(l.id AS VARCHAR)
            {join_scheduled}
            LEFT JOIN youtube_channels c
              ON c.channel_id = l.channel_id
            LEFT JOIN youtube_videos v
              ON v.id = l.first_video_id
            WHERE COALESCE(l.youtube_channel_id, '') <> ''
              AND COALESCE(sl.archived, false) = false
              AND {sent_clause}
            GROUP BY l.youtube_channel_id, COALESCE(c.channel_name, l.creator_name, ''), COALESCE(v.title, '')
            ORDER BY last_sent_at DESC NULLS LAST, l.youtube_channel_id
            LIMIT ?
            """,
            [max(1, min(int(limit or LEAD_DISCOVERY_SEED_CHANNEL_LIMIT), LEAD_DISCOVERY_SEED_CHANNEL_LIMIT))],
        ).fetchall()
    finally:
        conn.close()
    seeds: List[Dict[str, Any]] = []
    for row in rows:
        channel_id = str(row[0] or "").strip()
        if not channel_id:
            continue
        seeds.append(
            {
                "channel_id": channel_id,
                "channel_name": str(row[1] or "").strip(),
                "first_video_title": str(row[2] or "").strip(),
            }
        )
    return seeds


def _hydrate_seed_channel_context(seeds: List[Dict[str, Any]]) -> tuple[List[Dict[str, Any]], int]:
    if not seeds:
        return [], 0
    read_calls = 0
    seed_by_id = {seed["channel_id"]: dict(seed) for seed in seeds if seed.get("channel_id")}
    channel_ids = list(seed_by_id.keys())
    for i in range(0, len(channel_ids), 50):
        chunk = channel_ids[i : i + 50]
        payload = _youtube_get_json(
            "channels",
            {"part": "snippet,contentDetails", "id": ",".join(chunk)},
            timeout=10,
        )
        read_calls += 1
        for item in payload.get("items") or []:
            channel_id = str(item.get("id") or "").strip()
            if channel_id not in seed_by_id:
                continue
            snippet = item.get("snippet") or {}
            content_details = item.get("contentDetails") or {}
            related = content_details.get("relatedPlaylists") or {}
            seed_by_id[channel_id]["channel_name"] = seed_by_id[channel_id].get("channel_name") or str(snippet.get("title") or "").strip()
            seed_by_id[channel_id]["channel_description"] = str(snippet.get("description") or "").strip()
            seed_by_id[channel_id]["uploads_playlist_id"] = str(related.get("uploads") or "").strip()

    hydrated: List[Dict[str, Any]] = []
    for seed in seed_by_id.values():
        uploads_playlist_id = str(seed.get("uploads_playlist_id") or "").strip()
        titles: List[str] = []
        if seed.get("first_video_title"):
            titles.append(str(seed["first_video_title"]).strip())
        if uploads_playlist_id:
            read_calls += 1
            batch = fetch_playlist_items_batch(
                playlist_id=uploads_playlist_id,
                max_results=LEAD_DISCOVERY_SEED_RECENT_TITLES,
            )
            for video in batch.get("videos") or []:
                title = str(video.get("title") or "").strip()
                if title and title not in titles:
                    titles.append(title)
                if len(titles) >= LEAD_DISCOVERY_SEED_RECENT_TITLES:
                    break
        seed["recent_titles"] = titles[:LEAD_DISCOVERY_SEED_RECENT_TITLES]
        hydrated.append(seed)
    return hydrated, read_calls


def _generate_keywords_from_seed_context(seeds: List[Dict[str, Any]]) -> List[str]:
    if not _openai_client:
        raise RuntimeError("OPENAI_API_KEY missing")
    compact_items = []
    for seed in seeds:
        compact_items.append(
            {
                "name": str(seed.get("channel_name") or "")[:120],
                "description": str(seed.get("channel_description") or "")[:700],
                "recent_titles": [str(title)[:140] for title in (seed.get("recent_titles") or [])[:LEAD_DISCOVERY_SEED_RECENT_TITLES]],
            }
        )
    prompt = (
        "These are my ideal target YouTube channels. Produce 15 distinct YouTube search "
        "keywords likely to surface similar channels I have not contacted. Return JSON only: "
        "{\"keywords\":[...]}.\n\n"
        + json.dumps(compact_items, ensure_ascii=False)
    )
    response = _openai_client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": "You create concise YouTube lead-discovery search keywords from seed channels."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.35,
    )
    content = (response.choices[0].message.content or "").strip()
    try:
        payload = json.loads(content)
        return _normalize_discovery_keywords(payload.get("keywords"))[:15]
    except Exception:
        return _normalize_discovery_keywords(content)[:15]


def _admin_json_auth_error():
    current_user = getattr(g, "vs_current_user", None)
    if not current_user:
        return {"success": False, "errors": [{"error": "unauthorized", "message": "Admin session required."}]}, 401
    if (current_user.get("role") or "").strip().lower() != "admin":
        return {"success": False, "errors": [{"error": "forbidden", "message": "Admin access required."}]}, 403
    return None


def _lead_discovery_channel_url(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith("http://") or text.startswith("https://"):
        return text
    if text.startswith("UC") and "/" not in text:
        return f"https://www.youtube.com/channel/{text}"
    return text


def _json_sql_param(conn, placeholder: str = "?") -> str:
    if getattr(conn, "backend_name", "") == "postgres":
        return f"CAST({placeholder} AS JSONB)"
    return placeholder


def _lead_discovery_status(row: Dict[str, Any]) -> str:
    if row.get("icp_fit") is True:
        return "icp_qualified"
    if row.get("icp_fit") is False or row.get("eligible") is False:
        return "disqualified"
    return "discovered"


def _persist_discovery_lead(row: Dict[str, Any]) -> str:
    youtube_channel_id = str(row.get("channel_id") or "").strip()
    if not youtube_channel_id:
        return "skipped"
    conn = None
    try:
        conn = get_db()
        columns = table_columns(conn, "discovery_leads")
        if not columns:
            return "skipped"
        status = _lead_discovery_status(row)
        raw_json = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
        params = [
            youtube_channel_id,
            str(row.get("channel_url") or f"https://www.youtube.com/channel/{youtube_channel_id}"),
            row.get("channel_title"),
            row.get("channel_description"),
            row.get("subscriber_count"),
            row.get("longform_last_60d"),
            row.get("shorts_last_15d"),
            row.get("gate_subscriber"),
            row.get("gate_cadence"),
            row.get("eligible"),
            row.get("icp_fit"),
            row.get("icp_reason"),
            row.get("creator_name"),
            row.get("creator_email"),
            row.get("email_source"),
            row.get("email_confidence"),
            row.get("email_validation"),
            row.get("email_role"),
            row.get("is_generic_email"),
            row.get("website"),
            row.get("phone"),
            row.get("lead_tier"),
            row.get("email_source_url"),
            row.get("matched_keyword"),
            status,
            raw_json,
        ]
        insert_sql = f"""
            INSERT INTO discovery_leads (
                youtube_channel_id, channel_url, channel_title, channel_description,
                subscriber_count, longform_last_60d, shorts_last_15d,
                gate_subscriber, gate_cadence, eligible, icp_fit, icp_reason,
                creator_name, creator_email, email_source, email_confidence,
                email_validation, email_role, is_generic_email, website, phone,
                lead_tier, email_source_url, matched_keyword, status, raw_json,
                first_seen_at, last_seen_at, last_discovered_at
            )
            VALUES (
                ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?, {_json_sql_param(conn)},
                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            )
            ON CONFLICT (youtube_channel_id) DO UPDATE SET
                channel_url = EXCLUDED.channel_url,
                channel_title = COALESCE(EXCLUDED.channel_title, discovery_leads.channel_title),
                channel_description = COALESCE(EXCLUDED.channel_description, discovery_leads.channel_description),
                subscriber_count = EXCLUDED.subscriber_count,
                longform_last_60d = EXCLUDED.longform_last_60d,
                shorts_last_15d = EXCLUDED.shorts_last_15d,
                gate_subscriber = EXCLUDED.gate_subscriber,
                gate_cadence = EXCLUDED.gate_cadence,
                eligible = EXCLUDED.eligible,
                icp_fit = EXCLUDED.icp_fit,
                icp_reason = COALESCE(EXCLUDED.icp_reason, discovery_leads.icp_reason),
                creator_name = COALESCE(EXCLUDED.creator_name, discovery_leads.creator_name),
                creator_email = COALESCE(NULLIF(EXCLUDED.creator_email, ''), discovery_leads.creator_email),
                email_source = COALESCE(NULLIF(EXCLUDED.email_source, ''), discovery_leads.email_source),
                email_confidence = COALESCE(EXCLUDED.email_confidence, discovery_leads.email_confidence),
                email_validation = COALESCE(NULLIF(EXCLUDED.email_validation, ''), discovery_leads.email_validation),
                email_role = COALESCE(NULLIF(EXCLUDED.email_role, ''), discovery_leads.email_role),
                is_generic_email = COALESCE(EXCLUDED.is_generic_email, discovery_leads.is_generic_email),
                website = COALESCE(NULLIF(EXCLUDED.website, ''), discovery_leads.website),
                phone = COALESCE(NULLIF(EXCLUDED.phone, ''), discovery_leads.phone),
                lead_tier = COALESCE(NULLIF(EXCLUDED.lead_tier, ''), discovery_leads.lead_tier),
                email_source_url = COALESCE(NULLIF(EXCLUDED.email_source_url, ''), discovery_leads.email_source_url),
                matched_keyword = COALESCE(NULLIF(EXCLUDED.matched_keyword, ''), discovery_leads.matched_keyword),
                status = CASE
                    WHEN discovery_leads.status IN ('email_enriched', 'contacted', 'converted', 'archived') THEN discovery_leads.status
                    WHEN discovery_leads.status = 'icp_qualified' AND EXCLUDED.status = 'discovered' THEN discovery_leads.status
                    ELSE EXCLUDED.status
                END,
                raw_json = EXCLUDED.raw_json,
                last_seen_at = CURRENT_TIMESTAMP,
                last_discovered_at = CURRENT_TIMESTAMP
            RETURNING (xmax = 0) AS inserted
        """
        result = conn.execute(insert_sql, params)
        inserted_row = result.fetchone()
        optional_updates = []
        optional_params: List[Any] = []
        if "sweetspot_score" in columns:
            optional_updates.append("sweetspot_score = ?")
            optional_params.append(row.get("sweetspot_score"))
        if "best_source_video_id" in columns:
            optional_updates.append("best_source_video_id = ?")
            optional_params.append(row.get("best_source_video_id"))
        if "best_source_video_minutes" in columns:
            optional_updates.append("best_source_video_minutes = ?")
            optional_params.append(row.get("best_source_video_minutes"))
        if optional_updates:
            conn.execute(
                f"""
                UPDATE discovery_leads
                SET {", ".join(optional_updates)}
                WHERE youtube_channel_id = ?
                """,
                optional_params + [youtube_channel_id],
            )
        conn.commit()
        return "inserted" if inserted_row and bool(inserted_row[0]) else "updated"
    except Exception:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        current_app.logger.exception("Lead discovery persistence failed for channel=%s", youtube_channel_id)
        return "failed"
    finally:
        if conn:
            conn.close()


def _load_emailed_seed_sources(conn) -> List[Dict[str, Any]]:
    has_scheduled = bool(table_columns(conn, "outreach_scheduled_emails"))
    scheduled_union = ""
    if has_scheduled:
        scheduled_union = """
            UNION ALL
            SELECT DISTINCT
                COALESCE(NULLIF(l.youtube_channel_id, ''), c.youtube_channel_id) AS youtube_channel_id,
                c.channel_id AS local_channel_id,
                COALESCE(c.channel_name, l.creator_name, '') AS channel_name,
                COALESCE(c.channel_description, '') AS channel_description,
                v.video_id AS source_video_id,
                COALESCE(v.title, '') AS source_video_title,
                ose.sent_at AS sent_at
            FROM autopilot_leads l
            JOIN short_share_links sl
              ON CAST(sl.autopilot_lead_id AS VARCHAR) = CAST(l.id AS VARCHAR)
            JOIN outreach_scheduled_emails ose
              ON ose.share_link_id = sl.id
            LEFT JOIN youtube_channels c ON c.channel_id = l.channel_id
            LEFT JOIN youtube_videos v ON v.id = l.first_video_id
            WHERE (ose.sent_at IS NOT NULL OR ose.status = 'sent')
              AND COALESCE(sl.archived, false) = false
              AND COALESCE(COALESCE(NULLIF(l.youtube_channel_id, ''), c.youtube_channel_id), '') <> ''
        """
    rows = conn.execute(
        f"""
        WITH emailed AS (
            SELECT DISTINCT
                COALESCE(NULLIF(l.youtube_channel_id, ''), c.youtube_channel_id) AS youtube_channel_id,
                c.channel_id AS local_channel_id,
                COALESCE(c.channel_name, l.creator_name, '') AS channel_name,
                COALESCE(c.channel_description, '') AS channel_description,
                v.video_id AS source_video_id,
                COALESCE(v.title, '') AS source_video_title,
                sl.emailed_at AS sent_at
            FROM autopilot_leads l
            JOIN short_share_links sl
              ON CAST(sl.autopilot_lead_id AS VARCHAR) = CAST(l.id AS VARCHAR)
            LEFT JOIN youtube_channels c ON c.channel_id = l.channel_id
            LEFT JOIN youtube_videos v ON v.id = l.first_video_id
            WHERE sl.emailed_at IS NOT NULL
              AND COALESCE(sl.archived, false) = false
              AND COALESCE(COALESCE(NULLIF(l.youtube_channel_id, ''), c.youtube_channel_id), '') <> ''
            {scheduled_union}
        ), ranked AS (
            SELECT
                youtube_channel_id,
                local_channel_id,
                channel_name,
                channel_description,
                source_video_id,
                source_video_title,
                sent_at,
                ROW_NUMBER() OVER (
                    PARTITION BY youtube_channel_id
                    ORDER BY sent_at DESC NULLS LAST, source_video_id
                ) AS rn
            FROM emailed
        )
        SELECT
            r.youtube_channel_id,
            r.local_channel_id,
            r.channel_name,
            r.channel_description,
            r.source_video_id,
            r.source_video_title,
            COALESCE(t.full_text, '') AS transcript_text
        FROM ranked r
        LEFT JOIN youtube_transcripts t ON t.video_id = r.source_video_id
        WHERE r.rn = 1
        ORDER BY r.sent_at DESC NULLS LAST, r.youtube_channel_id
        """,
    ).fetchall()
    seeds: List[Dict[str, Any]] = []
    for row in rows:
        channel_id = str(row[0] or "").strip()
        if not channel_id:
            continue
        seeds.append(
            {
                "channel_id": channel_id,
                "local_channel_id": row[1],
                "channel_name": str(row[2] or "").strip(),
                "channel_description": str(row[3] or "").strip(),
                "source_video_id": str(row[4] or "").strip(),
                "source_video_title": str(row[5] or "").strip(),
                "transcript_text": str(row[6] or "").strip(),
            }
        )
    return seeds


def _summarize_seed_transcript(seed: Dict[str, Any]) -> str:
    if not _openai_client:
        raise RuntimeError("OPENAI_API_KEY missing")
    transcript = " ".join(str(seed.get("transcript_text") or "").split())
    if not transcript:
        return ""
    prompt = (
        "Summarize this YouTube seed channel transcript in 3-5 sentences for learning an ICP lookalike profile. "
        "Focus on the creator's persona, audience, teaching style, expertise, monetizable offer signals, and recurring topics. "
        "Return plain text only.\n\n"
        f"Channel: {seed.get('channel_name') or seed.get('channel_id')}\n"
        f"Channel description: {str(seed.get('channel_description') or '')[:1200]}\n\n"
        f"Transcript:\n{transcript[:18000]}"
    )
    response = _openai_client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": "You summarize creator transcripts for B2B lead-discovery ICP learning."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
    )
    return " ".join((response.choices[0].message.content or "").strip().split())[:3000]


def _generate_seed_icp_profile(profile_items: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not _openai_client:
        raise RuntimeError("OPENAI_API_KEY missing")
    compact_items = [
        {
            "channel_name": str(item.get("channel_name") or "")[:160],
            "channel_description": str(item.get("channel_description") or "")[:900],
            "transcript_summary": str(item.get("transcript_summary") or "")[:1600],
        }
        for item in profile_items
    ]
    prompt = (
        "These are channels we already emailed and consider strong lookalike seeds. "
        "Learn the shared ICP profile, then produce YouTube search keywords likely to find similar creators. "
        "Return STRICT JSON only: {\"profile_text\":\"concise profile\", \"keywords\":[\"keyword\", ...]}.\n\n"
        "The target is broad: solo educators, coaches, consultants, and expert creators who talk to camera, "
        "make long-form educational content, and likely have something to sell. Do not narrow to therapists only.\n\n"
        + json.dumps(compact_items, ensure_ascii=False)
    )
    response = _openai_client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": "You learn concise ICP profiles and YouTube discovery keywords from seed creator channels."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.25,
    )
    content = (response.choices[0].message.content or "").strip()
    try:
        payload = json.loads(content)
    except Exception:
        payload = {"profile_text": content[:2500], "keywords": _normalize_discovery_keywords(content)}
    return {
        "profile_text": str(payload.get("profile_text") or "").strip()[:5000],
        "keywords": _normalize_discovery_keywords(payload.get("keywords"))[:15],
    }


def _normalize_seed_search_keywords(raw_keywords: Any, *, limit: int = 20) -> List[str]:
    if isinstance(raw_keywords, list):
        values = raw_keywords
    else:
        values = re.split(r"[\n,;]+", str(raw_keywords or ""))
    keywords: List[str] = []
    seen = set()
    blocked = ("solo educator", "online coach", "consultant", "youtube channel", "expert creator")
    for value in values:
        keyword = " ".join(str(value or "").strip().strip('"').split())
        if not keyword:
            continue
        lowered = keyword.lower()
        if lowered in seen or any(term == lowered for term in blocked):
            continue
        seen.add(lowered)
        keywords.append(keyword[:120])
        if len(keywords) >= limit:
            break
    return keywords


def _generate_seed_search_keywords(profile_items: List[Dict[str, Any]]) -> List[str]:
    if not _openai_client:
        raise RuntimeError("OPENAI_API_KEY missing")
    compact_items = [
        {
            "channel_name": str(item.get("channel_name") or "")[:160],
            "source_video_title": str(item.get("source_video_title") or "")[:180],
            "transcript_summary": str(item.get("transcript_summary") or "")[:1200],
        }
        for item in profile_items
    ]
    prompt = (
        "These are real channels we target. Produce 20 YouTube search queries that would surface MORE channels "
        "making videos like these. Use concrete topic language a viewer types into YouTube, not category labels "
        "describing the creators.\n\n"
        "Good examples: chronic fatigue recovery, mindset shifts after 50, how to journal for personal growth, "
        "discipline neuroscience, frugal living tips.\n\n"
        "Avoid meta or industry labels like: solo educator, online coach, consultant, consultant YouTube channel, "
        "expert creator, personal brand, content creator.\n"
        "Each query must be 2-5 words, specific to a topic/audience/problem, not a job title. "
        "Return STRICT JSON only: {\"keywords\":[\"query\", ...]}.\n\n"
        "Seed channel signals:\n"
        + json.dumps(compact_items, ensure_ascii=False)
    )
    response = _openai_client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": "You create searchable YouTube topic queries from seed video titles and transcript summaries."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.35,
    )
    content = (response.choices[0].message.content or "").strip()
    try:
        payload = json.loads(content)
        return _normalize_seed_search_keywords(payload.get("keywords"), limit=20)
    except Exception:
        return _normalize_seed_search_keywords(content, limit=20)


def _fetch_recent_channel_titles(channel_id: str, *, limit: int = LEAD_DISCOVERY_SEED_RECENT_TITLES) -> List[str]:
    clean_channel_id = str(channel_id or "").strip()
    if not clean_channel_id:
        return []
    try:
        channel_meta = get_channel_metadata(f"https://www.youtube.com/channel/{clean_channel_id}")
        uploads_playlist_id = str(channel_meta.get("uploads_playlist_id") or "").strip()
        if not uploads_playlist_id:
            return []
        batch = fetch_playlist_items_batch(
            playlist_id=uploads_playlist_id,
            max_results=max(1, min(int(limit or LEAD_DISCOVERY_SEED_RECENT_TITLES), LEAD_DISCOVERY_SEED_RECENT_TITLES)),
        )
    except Exception:
        current_app.logger.exception("Could not fetch recent titles for promoted discovery seed channel=%s", clean_channel_id)
        return []
    titles: List[str] = []
    for video in batch.get("videos") or []:
        title = " ".join(str(video.get("title") or "").strip().split())
        if title and title not in titles:
            titles.append(title[:180])
    return titles[:LEAD_DISCOVERY_SEED_RECENT_TITLES]


def _summarize_promoted_discovery_seed(lead: Dict[str, Any], recent_titles: List[str]) -> str:
    if not _openai_client:
        raise RuntimeError("OPENAI_API_KEY missing")
    prompt = (
        "Create a 3-5 sentence synthetic seed summary for this promoted YouTube discovery lead. "
        "There is no transcript, so infer only from the channel title, channel description, and recent video titles. "
        "Focus on creator persona, audience, teaching style, expertise, offer signals, and recurring topics. "
        "Return plain text only.\n\n"
        f"Channel: {lead.get('channel_title') or lead.get('youtube_channel_id')}\n"
        f"Channel description: {str(lead.get('channel_description') or '')[:1600]}\n"
        f"Recent titles: {json.dumps(recent_titles[:LEAD_DISCOVERY_SEED_RECENT_TITLES], ensure_ascii=False)}"
    )
    response = _openai_client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": "You summarize promoted creator-channel leads for seed-based ICP keyword discovery."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
    )
    summary = " ".join((response.choices[0].message.content or "").strip().split())[:2800]
    return f"{SYNTHETIC_SEED_PREFIX} {summary}".strip()


def load_seed_pool(conn=None) -> List[Dict[str, Any]]:
    owns_conn = conn is None
    if owns_conn:
        conn = get_db_readonly()
    try:
        seed_rows = conn.execute(
            """
            SELECT
                scp.channel_id,
                scp.source_video_id,
                scp.transcript_summary,
                COALESCE(
                    dl.channel_title,
                    (
                        SELECT yc.channel_name
                        FROM youtube_channels yc
                        WHERE yc.youtube_channel_id = scp.channel_id
                          AND COALESCE(yc.channel_name, '') <> ''
                        ORDER BY yc.channel_id DESC
                        LIMIT 1
                    ),
                    scp.channel_id
                ) AS channel_name,
                COALESCE(
                    dl.channel_description,
                    (
                        SELECT yc.channel_description
                        FROM youtube_channels yc
                        WHERE yc.youtube_channel_id = scp.channel_id
                          AND COALESCE(yc.channel_description, '') <> ''
                        ORDER BY yc.channel_id DESC
                        LIMIT 1
                    ),
                    ''
                ) AS channel_description,
                COALESCE(yv.title, '') AS source_video_title,
                CASE WHEN scp.transcript_summary LIKE ? THEN 'synthetic' ELSE 'transcript' END AS seed_kind
            FROM seed_channel_profiles scp
            LEFT JOIN discovery_leads dl ON dl.youtube_channel_id = scp.channel_id
            LEFT JOIN youtube_videos yv ON yv.video_id = scp.source_video_id
            ORDER BY scp.created_at DESC NULLS LAST, scp.channel_id
            """,
            [f"{SYNTHETIC_SEED_PREFIX}%"],
        ).fetchall()
        pool: List[Dict[str, Any]] = []
        seen = set()
        for row in seed_rows:
            channel_id = str(row[0] or "").strip()
            summary = str(row[2] or "").strip()
            if not channel_id or not summary:
                continue
            seen.add(channel_id)
            pool.append(
                {
                    "channel_id": channel_id,
                    "source_video_id": str(row[1] or "").strip(),
                    "transcript_summary": summary,
                    "channel_name": str(row[3] or channel_id).strip(),
                    "channel_description": str(row[4] or "").strip(),
                    "source_video_title": str(row[5] or "").strip(),
                    "seed_kind": str(row[6] or "transcript"),
                }
            )

        if "is_seed" in table_columns(conn, "discovery_leads"):
            promoted_rows = conn.execute(
                """
                SELECT youtube_channel_id, channel_title, channel_description
                FROM discovery_leads
                WHERE COALESCE(is_seed, false) IS TRUE
                ORDER BY promoted_at DESC NULLS LAST, id DESC
                """
            ).fetchall()
            for row in promoted_rows:
                channel_id = str(row[0] or "").strip()
                if not channel_id or channel_id in seen:
                    continue
                pool.append(
                    {
                        "channel_id": channel_id,
                        "source_video_id": "",
                        "transcript_summary": f"{SYNTHETIC_SEED_PREFIX} {str(row[2] or '').strip()}"[:3000],
                        "channel_name": str(row[1] or channel_id).strip(),
                        "channel_description": str(row[2] or "").strip(),
                        "source_video_title": "",
                        "seed_kind": "synthetic",
                    }
                )
        return pool
    finally:
        if owns_conn and conn:
            conn.close()


def _insert_keywords_into_queue(conn, keywords: List[str], *, source: str = "seed") -> Dict[str, Any]:
    inserted = 0
    already_present = 0
    for keyword in keywords:
        clean_keyword = " ".join(str(keyword or "").strip().split())[:120]
        if not clean_keyword:
            continue
        existing = conn.execute("SELECT id FROM keyword_queue WHERE lower(keyword) = lower(?) LIMIT 1", [clean_keyword]).fetchone()
        if existing:
            already_present += 1
            continue
        conn.execute(
            """
            INSERT INTO keyword_queue (keyword, source, status, priority, created_at, updated_at)
            VALUES (?, ?, 'queued', 100, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT (keyword) DO NOTHING
            """,
            [clean_keyword, source],
        )
        inserted += 1
    return {"newly_enqueued": inserted, "already_present": already_present}


def _load_queue_keywords(conn, take_n: int) -> List[Dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, keyword
        FROM keyword_queue
        WHERE status = 'queued'
          AND (next_run_at IS NULL OR next_run_at <= CURRENT_TIMESTAMP)
        ORDER BY priority ASC, created_at ASC, id ASC
        LIMIT ?
        """,
        [take_n],
    ).fetchall()
    return [{"id": row[0], "keyword": str(row[1] or "").strip()} for row in rows if str(row[1] or "").strip()]


def _update_keyword_queue_after_run(conn, queue_keywords: List[Dict[str, Any]], results: List[Dict[str, Any]]) -> Dict[str, int]:
    qualified_by_keyword: Dict[str, int] = {}
    for row in results:
        keyword = str(row.get("matched_keyword") or "").strip()
        if not keyword:
            continue
        if _lead_discovery_status(row) == "icp_qualified":
            qualified_by_keyword[keyword.lower()] = qualified_by_keyword.get(keyword.lower(), 0) + 1
    for item in queue_keywords:
        keyword = str(item.get("keyword") or "").strip()
        found_count = int(qualified_by_keyword.get(keyword.lower(), 0))
        conn.execute(
            """
            UPDATE keyword_queue
            SET times_searched = times_searched + 1,
                last_searched_at = CURRENT_TIMESTAMP,
                found_count = found_count + ?,
                status = 'searched',
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            [found_count, item["id"]],
        )
    return {str(item["keyword"]): int(qualified_by_keyword.get(str(item["keyword"]).lower(), 0)) for item in queue_keywords}


@video_shorts_bp.route("/api/admin/youtube-channel-diagnose", methods=["POST"])
def admin_youtube_channel_diagnose():
    current_user = getattr(g, "vs_current_user", None)
    if not current_user:
        return jsonify({"error": "unauthorized", "message": "Admin session required."}), 401
    if (current_user.get("role") or "").strip().lower() != "admin":
        return jsonify({"error": "forbidden", "message": "Admin access required."}), 403

    payload = request.get_json(silent=True) or {}
    raw_url = str(payload.get("url") or "").strip()
    if not raw_url:
        return jsonify({"error": "bad_request", "message": "Request body must include a YouTube url."}), 400

    try:
        resolved_channel_id, video_meta, resolve_error = _resolve_channel_context(raw_url)
        if resolve_error:
            code, message = resolve_error
            status = 400 if code in {"missing_url", "invalid_url"} else 404
            return jsonify({"error": code, "message": message}), status

        diagnosis = diagnose_channel(
            resolved_channel_id,
            video_meta=video_meta,
            min_longform_seconds=60,
            short_max_seconds=60,
            email_video_description_limit=0,
            count_unknown_as_short=True,
        )
        outreach_detail = None
        already_added = False
        channel_video_count = 0
        try:
            conn = get_db_readonly()
        except RuntimeError as exc:
            message = str(exc or "").strip()
            return jsonify({"error": "server_config", "message": message or "Database is not configured."}), 500
        try:
            outreach_detail = _load_admin_global_outreach_match(conn, resolved_channel_id)
            channel_video_count = _count_videos_for_creator_channel(conn, resolved_channel_id)
            candidate_video_id = str((video_meta or {}).get("video_id") or "").strip()
            active_brand_id = str(current_brand_id() or "").strip() or None
            if candidate_video_id and active_brand_id:
                local_channel = _resolve_brand_local_uploads_channel(conn, active_brand_id)
                if local_channel:
                    already_added = _video_already_in_bucket(
                        conn,
                        candidate_video_id,
                        local_channel.get("channel_id"),
                    )
        finally:
            conn.close()
        return jsonify(
            {
                "channel_id": resolved_channel_id,
                "channel_title": diagnosis.get("channel_title"),
                "subscriber_count": diagnosis.get("subscriber_count"),
                "creator_name": diagnosis.get("creator_name"),
                "creator_email": diagnosis.get("creator_email"),
                "email_source": diagnosis.get("email_source"),
                "eligible": diagnosis.get("eligible"),
                "gate_subscriber": diagnosis.get("gate_subscriber"),
                "gate_cadence": diagnosis.get("gate_cadence"),
                "longform_last_60d": diagnosis.get("longform_last_60d"),
                "shorts_last_15d": diagnosis.get("shorts_last_15d"),
                "shorts_color": diagnosis.get("shorts_color"),
                "shorts_window": diagnosis.get("shorts_window"),
                "latest_short_date": diagnosis.get("latest_short_date"),
                "already_added": already_added,
                "already_reached": bool(outreach_detail),
                "channel_already_in_minti": channel_video_count > 0,
                "channel_video_count": channel_video_count,
                "outreach_detail": outreach_detail,
            }
        )
    except YoutubeApiError as exc:
        env_message = _youtube_env_error_message(exc)
        if env_message:
            return jsonify({"error": "server_config", "message": env_message}), 500
        message = str(exc or "").strip() or "YouTube API request failed."
        status = 404 if "not found" in message.lower() else 502
        return jsonify({"error": "youtube_api_error", "message": message}), status
    except RuntimeError as exc:
        message = str(exc or "").strip()
        if "database" in message.lower():
            return jsonify({"error": "server_config", "message": message}), 500
        current_app.logger.exception("Unexpected runtime error in admin YouTube diagnose endpoint")
        return jsonify({"error": "server_error", "message": message or "Unexpected server error."}), 500
    except Exception:
        current_app.logger.exception("Unexpected error in admin YouTube diagnose endpoint")
        return jsonify({"error": "server_error", "message": "Unexpected server error."}), 500


def run_lead_discovery_payload(payload: Dict[str, Any]) -> tuple[Dict[str, Any], int]:
    niche = " ".join(str(payload.get("niche") or "").strip().split())
    supplied_keywords = _normalize_discovery_keywords(payload.get("keywords"))
    use_queue = bool(payload.get("use_queue"))
    take_n = _coerce_int_param(
        payload.get("take_n"),
        LEAD_DISCOVERY_QUEUE_TAKE_DEFAULT,
        minimum=1,
        maximum=LEAD_DISCOVERY_QUEUE_TAKE_MAX,
    )
    queue_keyword_items: List[Dict[str, Any]] = []
    max_keywords = _coerce_int_param(
        payload.get("max_keywords"),
        5,
        minimum=1,
        maximum=LEAD_DISCOVERY_MAX_KEYWORDS,
    )
    max_results_per_keyword = _coerce_int_param(
        payload.get("max_results_per_keyword"),
        20,
        minimum=1,
        maximum=LEAD_DISCOVERY_MAX_RESULTS_PER_KEYWORD,
    )
    max_channels_enriched = _coerce_int_param(
        payload.get("max_channels_enriched"),
        50,
        minimum=1,
        maximum=LEAD_DISCOVERY_MAX_CHANNELS_ENRICHED,
    )
    min_longform_seconds = _coerce_int_param(
        payload.get("min_longform_seconds"),
        LEAD_DISCOVERY_DEFAULT_MIN_LONGFORM_SECONDS,
        minimum=0,
        maximum=3600,
    )
    short_max_seconds = _coerce_int_param(
        payload.get("short_max_seconds"),
        LEAD_DISCOVERY_DEFAULT_SHORT_MAX_SECONDS,
        minimum=0,
        maximum=600,
    )
    lang = re.sub(r"[^A-Za-z-]", "", str(payload.get("lang") or "en").strip())[:12] or "en"
    region = re.sub(r"[^A-Za-z]", "", str(payload.get("region") or "US").strip()).upper()[:2] or "US"
    ai_icp = bool(payload.get("ai_icp"))
    errors: List[Dict[str, Any]] = []

    if use_queue:
        conn = None
        try:
            conn = get_db()
            if not table_columns(conn, "keyword_queue"):
                return {
                    "success": False,
                    "keywords": [],
                    "queue_mode": True,
                    "queue_taken": 0,
                    "keyword_found_counts": {},
                    "search_calls": 0,
                    "enrichment_read_calls": 0,
                    "results": [],
                    "errors": [{"error": "keyword_queue_missing", "message": "keyword_queue table is not available."}],
                }, 500
            queue_keyword_items = _load_queue_keywords(conn, take_n)
        finally:
            if conn:
                conn.close()
        supplied_keywords = [item["keyword"] for item in queue_keyword_items]
        max_keywords = max(max_keywords, len(supplied_keywords))

    if not use_queue and not niche and not supplied_keywords:
        return {
            "success": False,
            "keywords": [],
            "search_calls": 0,
            "enrichment_read_calls": 0,
            "results": [],
            "errors": [{"error": "bad_request", "message": "Provide a niche or keywords."}],
        }, 400

    try:
        keywords = supplied_keywords[:max_keywords] if use_queue else (supplied_keywords[:max_keywords] or _generate_lead_discovery_keywords(niche, max_keywords, lang))
    except Exception as exc:
        current_app.logger.exception("Lead discovery keyword generation failed")
        return {
            "success": False,
            "keywords": [],
            "search_calls": 0,
            "enrichment_read_calls": 0,
            "results": [],
            "errors": [{"error": "keyword_generation_failed", "message": str(exc) or "Keyword generation failed."}],
        }, 500
    keywords = keywords[:max_keywords]
    if not keywords:
        return {
            "success": False,
            "keywords": [],
            "queue_mode": use_queue,
            "queue_taken": 0,
            "keyword_found_counts": {},
            "search_calls": 0,
            "enrichment_read_calls": 0,
            "results": [],
            "errors": [{"error": "no_keywords", "message": "No queued keywords are available." if use_queue else "No usable keywords were generated."}],
        }, 400

    search_calls = 0
    raw_search_items = 0
    candidates_by_channel: Dict[str, Dict[str, str]] = {}
    for keyword in keywords:
        try:
            search_calls += 1
            candidates, raw_count = _search_youtube_channels_for_keyword(
                keyword,
                max_results=max_results_per_keyword,
                lang=lang,
                region=region,
            )
            raw_search_items += raw_count
            for candidate in candidates:
                channel_id = candidate["channel_id"]
                if channel_id not in candidates_by_channel:
                    candidates_by_channel[channel_id] = candidate
        except YoutubeApiError as exc:
            errors.append({"error": "youtube_search_error", "keyword": keyword, "message": str(exc)})
        except Exception as exc:
            current_app.logger.exception("Lead discovery search failed for keyword=%s", keyword)
            errors.append({"error": "search_failed", "keyword": keyword, "message": str(exc) or "Search failed."})

    quota_counter = {"enrichment_read_calls": 0}
    results: List[Dict[str, Any]] = []
    persistence_counts = {"inserted": 0, "updated": 0, "skipped": 0, "failed": 0}
    enrichment_candidates = list(candidates_by_channel.values())[:max_channels_enriched]
    channels_enriched = 0
    for candidate in enrichment_candidates:
        channel_id = candidate["channel_id"]
        try:
            channels_enriched += 1
            row = diagnose_channel(
                channel_id,
                quota_counter=quota_counter,
                min_longform_seconds=min_longform_seconds,
                short_max_seconds=short_max_seconds,
            )
            row["matched_keyword"] = candidate.get("matched_keyword")
            row["channel_url"] = f"https://www.youtube.com/channel/{channel_id}"
            if ai_icp:
                row.update(_classify_lead_discovery_icp(niche or candidate.get("matched_keyword") or "", row))
            else:
                row["icp_fit"] = None
                row["icp_reason"] = ""
            persistence_result = _persist_discovery_lead(row)
            if persistence_result in persistence_counts:
                persistence_counts[persistence_result] += 1
            results.append(row)
        except YoutubeApiError as exc:
            errors.append({"error": "youtube_enrichment_error", "channel_id": channel_id, "message": str(exc)})
        except Exception as exc:
            current_app.logger.exception("Lead discovery enrichment failed for channel=%s", channel_id)
            errors.append({"error": "enrichment_failed", "channel_id": channel_id, "message": str(exc) or "Enrichment failed."})

    def _passes_default_thresholds(row: Dict[str, Any]) -> bool:
        try:
            subscribers = int(row.get("subscriber_count") or -1)
        except Exception:
            subscribers = -1
        try:
            longform_count = int(row.get("longform_last_60d") or 0)
        except Exception:
            longform_count = 0
        try:
            shorts_count = int(row.get("shorts_last_15d") or 0)
        except Exception:
            shorts_count = 0
        return (
            LEAD_DISCOVERY_DEFAULT_MIN_SUBSCRIBERS <= subscribers <= LEAD_DISCOVERY_DEFAULT_MAX_SUBSCRIBERS
            and longform_count >= LEAD_DISCOVERY_DEFAULT_MIN_LONGFORM_60D
            and shorts_count <= LEAD_DISCOVERY_DEFAULT_MAX_SHORTS_15D
        )

    keyword_found_counts: Dict[str, int] = {}
    if use_queue and queue_keyword_items:
        conn = None
        try:
            conn = get_db()
            keyword_found_counts = _update_keyword_queue_after_run(conn, queue_keyword_items, results)
            conn.commit()
        except Exception:
            if conn:
                try:
                    conn.rollback()
                except Exception:
                    pass
            current_app.logger.exception("Keyword queue update failed after discovery run")
            errors.append({"error": "keyword_queue_update_failed", "message": "Discovery completed, but keyword queue metrics could not be updated."})
        finally:
            if conn:
                conn.close()

    return {
        "success": not errors or bool(results),
        "keywords": keywords,
        "search_calls": search_calls,
        "enrichment_read_calls": int(quota_counter.get("enrichment_read_calls") or 0),
        "raw_search_items": raw_search_items,
        "unique_channel_ids": len(candidates_by_channel),
        "channels_enriched": channels_enriched,
        "rows_returned": len(results),
        "rows_passing_default_thresholds": sum(1 for row in results if _passes_default_thresholds(row)),
        "persistence": persistence_counts,
        "results": results,
        "errors": errors,
        "queue_mode": use_queue,
        "queue_taken": len(queue_keyword_items),
        "keyword_found_counts": keyword_found_counts,
    }, 200


@video_shorts_bp.route("/api/admin/lead-discovery-test", methods=["POST"])
def admin_lead_discovery_test():
    current_user = getattr(g, "vs_current_user", None)
    if not current_user:
        return jsonify({"success": False, "errors": [{"error": "unauthorized", "message": "Admin session required."}]}), 401
    if (current_user.get("role") or "").strip().lower() != "admin":
        return jsonify({"success": False, "errors": [{"error": "forbidden", "message": "Admin access required."}]}), 403

    response_payload, status = run_lead_discovery_payload(request.get_json(silent=True) or {})
    return jsonify(response_payload), status


@video_shorts_bp.route("/api/admin/discovery-promote-seed", methods=["POST"])
def admin_discovery_promote_seed():
    auth_error = _admin_json_auth_error()
    if auth_error:
        payload, status = auth_error
        payload.update({"promoted": 0, "skipped": [], "summaries_created": 0})
        return jsonify(payload), status

    payload = request.get_json(silent=True) or {}
    raw_ids = payload.get("lead_ids") or payload.get("leadIds") or []
    if isinstance(raw_ids, (str, int)):
        raw_ids = [raw_ids]
    lead_ids: List[int] = []
    seen_ids = set()
    for raw_id in raw_ids if isinstance(raw_ids, list) else []:
        try:
            lead_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if lead_id > 0 and lead_id not in seen_ids:
            seen_ids.add(lead_id)
            lead_ids.append(lead_id)
    if not lead_ids:
        return jsonify(
            {
                "success": False,
                "promoted": 0,
                "skipped": [{"id": None, "reason": "Provide at least one lead id."}],
                "summaries_created": 0,
                "errors": [{"error": "bad_request", "message": "Provide lead_ids."}],
            }
        ), 400

    promoted = 0
    summaries_created = 0
    skipped: List[Dict[str, Any]] = []
    conn = None
    try:
        conn = get_db()
        columns = table_columns(conn, "discovery_leads")
        missing_columns = [column for column in ("is_seed", "promoted_at", "seed_notes") if column not in columns]
        if missing_columns:
            return jsonify(
                {
                    "success": False,
                    "promoted": 0,
                    "skipped": [],
                    "summaries_created": 0,
                    "errors": [{"error": "schema_missing", "message": f"Missing discovery_leads columns: {', '.join(missing_columns)}"}],
                }
            ), 500
        for lead_id in lead_ids:
            row = conn.execute(
                """
                SELECT id, youtube_channel_id, channel_title, channel_description,
                       creator_email, status, is_seed
                FROM discovery_leads
                WHERE id = ?
                LIMIT 1
                """,
                [lead_id],
            ).fetchone()
            if not row:
                skipped.append({"id": lead_id, "reason": "not_found"})
                continue
            lead = {
                "id": row[0],
                "youtube_channel_id": str(row[1] or "").strip(),
                "channel_title": str(row[2] or "").strip(),
                "channel_description": str(row[3] or "").strip(),
                "creator_email": str(row[4] or "").strip(),
                "status": str(row[5] or "").strip(),
                "is_seed": bool(row[6]),
            }
            if not lead["creator_email"]:
                skipped.append({"id": lead_id, "reason": "missing_email"})
                continue
            if lead["status"] != "email_enriched":
                skipped.append({"id": lead_id, "reason": "status_not_email_enriched"})
                continue
            if not lead["youtube_channel_id"]:
                skipped.append({"id": lead_id, "reason": "missing_channel_id"})
                continue

            existing_seed = conn.execute(
                """
                SELECT source_video_id, transcript_summary
                FROM seed_channel_profiles
                WHERE channel_id = ?
                LIMIT 1
                """,
                [lead["youtube_channel_id"]],
            ).fetchone()
            existing_summary = str((existing_seed or [None, ""])[1] or "").strip()
            should_create_summary = not existing_summary or existing_summary.startswith(SYNTHETIC_SEED_PREFIX)
            if should_create_summary:
                recent_titles = _fetch_recent_channel_titles(lead["youtube_channel_id"])
                summary = _summarize_promoted_discovery_seed(lead, recent_titles)
                summaries_created += 1
                if existing_seed:
                    conn.execute(
                        """
                        UPDATE seed_channel_profiles
                        SET source_video_id = NULL,
                            transcript_summary = ?
                        WHERE channel_id = ?
                        """,
                        [summary, lead["youtube_channel_id"]],
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO seed_channel_profiles (
                            channel_id, source_video_id, transcript_summary, created_at
                        )
                        VALUES (?, NULL, ?, CURRENT_TIMESTAMP)
                        """,
                        [lead["youtube_channel_id"], summary],
                    )
            conn.execute(
                """
                UPDATE discovery_leads
                SET is_seed = true,
                    promoted_at = COALESCE(promoted_at, CURRENT_TIMESTAMP),
                    seed_notes = COALESCE(seed_notes, ?)
                WHERE id = ?
                """,
                ["manual seed promotion", lead_id],
            )
            promoted += 1
        conn.commit()
        return jsonify({"success": True, "promoted": promoted, "skipped": skipped, "summaries_created": summaries_created, "errors": []})
    except Exception as exc:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        current_app.logger.exception("Discovery seed promotion failed")
        return jsonify(
            {
                "success": False,
                "promoted": promoted,
                "skipped": skipped,
                "summaries_created": summaries_created,
                "errors": [{"error": "seed_promotion_failed", "message": str(exc) or "Seed promotion failed."}],
            }
        ), 500
    finally:
        if conn:
            conn.close()


def _short_promotion_error(exc: Exception) -> str:
    return " ".join(str(exc or "Promotion failed.").strip().split())[:500] or "Promotion failed."


@video_shorts_bp.route("/api/admin/discovery-promote-to-lead", methods=["POST"])
def admin_discovery_promote_to_lead():
    auth_error = _admin_json_auth_error()
    if auth_error:
        payload, status = auth_error
        payload.update({"promoted": [], "linked": [], "skipped": []})
        return jsonify(payload), status

    payload = request.get_json(silent=True) or {}
    raw_ids = payload.get("lead_ids") or payload.get("leadIds") or payload.get("ids") or []
    if isinstance(raw_ids, (str, int)):
        raw_ids = [raw_ids]
    lead_ids: List[int] = []
    seen_ids = set()
    for raw_id in raw_ids if isinstance(raw_ids, list) else []:
        try:
            lead_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if lead_id > 0 and lead_id not in seen_ids:
            seen_ids.add(lead_id)
            lead_ids.append(lead_id)
    if not lead_ids:
        return jsonify(
            {
                "success": False,
                "promoted": [],
                "linked": [],
                "skipped": [{"id": None, "reason": "Provide at least one discovery lead id."}],
                "errors": [{"error": "bad_request", "message": "Provide lead_ids."}],
            }
        ), 400

    promoted: List[Dict[str, Any]] = []
    linked: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    required_columns = {
        "autopilot_lead_id",
        "promoted_to_autopilot_at",
        "promoted_source_video_id",
        "promotion_error",
        "sweetspot_score",
        "best_source_video_id",
        "best_source_video_minutes",
    }
    conn = get_db()
    try:
        discovery_columns = table_columns(conn, "discovery_leads")
        missing_columns = sorted(required_columns - set(discovery_columns))
        if missing_columns:
            return jsonify(
                {
                    "success": False,
                    "promoted": [],
                    "linked": [],
                    "skipped": [],
                    "errors": [{"error": "schema_missing", "message": f"Missing discovery_leads columns: {', '.join(missing_columns)}"}],
                }
            ), 500

        for lead_id in lead_ids:
            try:
                row = conn.execute(
                    """
                    SELECT id, youtube_channel_id, channel_title, channel_description,
                           subscriber_count, creator_name, creator_email, icp_fit,
                           autopilot_lead_id
                    FROM discovery_leads
                    WHERE id = ?
                    LIMIT 1
                    """,
                    [lead_id],
                ).fetchone()
                if not row:
                    skipped.append({"id": lead_id, "reason": "not_found"})
                    continue
                lead = {
                    "id": int(row[0]),
                    "youtube_channel_id": str(row[1] or "").strip(),
                    "channel_title": str(row[2] or "").strip(),
                    "channel_description": str(row[3] or "").strip(),
                    "subscriber_count": row[4],
                    "creator_name": str(row[5] or "").strip(),
                    "creator_email": str(row[6] or "").strip(),
                    "icp_fit": bool(row[7]) if row[7] is not None else None,
                    "autopilot_lead_id": str(row[8] or "").strip(),
                }
                if not lead["creator_email"]:
                    skipped.append({"id": lead_id, "reason": "missing_email"})
                    continue
                if lead["icp_fit"] is not True:
                    skipped.append({"id": lead_id, "reason": "icp_not_fit"})
                    continue
                if not lead["youtube_channel_id"]:
                    skipped.append({"id": lead_id, "reason": "missing_channel_id"})
                    continue

                existing = conn.execute(
                    """
                    SELECT CAST(id AS VARCHAR), first_video_id
                    FROM autopilot_leads
                    WHERE youtube_channel_id = ?
                    LIMIT 1
                    """,
                    [lead["youtube_channel_id"]],
                ).fetchone()
                if existing:
                    conn.execute(
                        """
                        UPDATE discovery_leads
                        SET autopilot_lead_id = ?,
                            promoted_to_autopilot_at = COALESCE(promoted_to_autopilot_at, CURRENT_TIMESTAMP),
                            promoted_source_video_id = COALESCE(promoted_source_video_id, ?),
                            promotion_error = NULL
                        WHERE id = ?
                        """,
                        [str(existing[0]), existing[1], lead_id],
                    )
                    conn.commit()
                    linked.append({"id": lead_id, "autopilot_lead_id": str(existing[0]), "reason": "already_a_lead_linked"})
                    continue

                source = select_source_video_for_channel(lead["youtube_channel_id"])
                if not source:
                    conn.execute(
                        "UPDATE discovery_leads SET promotion_error = ? WHERE id = ?",
                        ["no_suitable_source_video", lead_id],
                    )
                    conn.commit()
                    skipped.append({"id": lead_id, "reason": "no_suitable_source_video"})
                    continue

                meta = fetch_video_metadata(str(source["video_id"]))
                if lead.get("channel_description"):
                    meta["channel_description"] = lead["channel_description"]
                autopilot = create_autopilot_lead_from_video(
                    conn,
                    meta=meta,
                    video_id=str(source["video_id"]),
                    canonical_url=str(source["canonical_url"]),
                    creator_name=lead["creator_name"] or lead["channel_title"],
                    creator_email=lead["creator_email"],
                    subscriber_count=lead["subscriber_count"],
                    discovery_owner_user_id=DISCOVERY_PROMOTION_OWNER_USER_ID,
                    discovery_brand_id=DISCOVERY_PROMOTION_BRAND_ID,
                )
                conn.execute(
                    """
                    UPDATE discovery_leads
                    SET autopilot_lead_id = ?,
                        promoted_to_autopilot_at = CURRENT_TIMESTAMP,
                        promoted_source_video_id = ?,
                        promotion_error = NULL,
                        sweetspot_score = ?,
                        best_source_video_id = ?,
                        best_source_video_minutes = ?
                    WHERE id = ?
                    """,
                    [
                        autopilot["lead_id"],
                        autopilot["video_pk"],
                        source.get("score"),
                        source.get("video_id"),
                        source.get("minutes"),
                        lead_id,
                    ],
                )
                conn.commit()
                promoted.append(
                    {
                        "id": lead_id,
                        "autopilot_lead_id": autopilot["lead_id"],
                        "source_video_id": source.get("video_id"),
                        "source_video_pk": autopilot["video_pk"],
                        "source_video_minutes": source.get("minutes"),
                        "sweetspot_score": source.get("score"),
                    }
                )
            except Exception as exc:
                try:
                    conn.rollback()
                except Exception:
                    pass
                message = _short_promotion_error(exc)
                try:
                    conn.execute("UPDATE discovery_leads SET promotion_error = ? WHERE id = ?", [message, lead_id])
                    conn.commit()
                except Exception:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                current_app.logger.exception("Discovery lead promotion failed id=%s", lead_id)
                skipped.append({"id": lead_id, "reason": "promotion_failed"})
                errors.append({"id": lead_id, "error": "promotion_failed", "message": message})
    finally:
        conn.close()

    return jsonify(
        {
            "success": bool(promoted or linked or skipped) and not errors,
            "promoted": promoted,
            "linked": linked,
            "skipped": skipped,
            "errors": errors,
        }
    )


@video_shorts_bp.route("/api/admin/seed-generate-keywords", methods=["POST"])
def admin_seed_generate_keywords():
    auth_error = _admin_json_auth_error()
    if auth_error:
        payload, status = auth_error
        payload.update({"generated": 0, "newly_enqueued": 0, "already_present": 0, "keywords": []})
        return jsonify(payload), status

    conn = None
    try:
        conn = get_db()
        pool = load_seed_pool(conn)
        if not pool:
            return jsonify(
                {
                    "success": False,
                    "generated": 0,
                    "newly_enqueued": 0,
                    "already_present": 0,
                    "keywords": [],
                    "errors": [{"error": "no_seed_pool", "message": "No seed profiles are available."}],
                }
            ), 404
        keywords = _generate_seed_search_keywords(pool)
        queue_counts = _insert_keywords_into_queue(conn, keywords, source="seed")
        existing_profile = conn.execute("SELECT profile_text FROM icp_profile WHERE id = 1").fetchone()
        profile_text = str((existing_profile or [None])[0] or "").strip()
        if existing_profile:
            conn.execute(
                """
                UPDATE icp_profile
                SET keywords_json = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = 1
                """,
                [json.dumps(keywords, ensure_ascii=False)],
            )
        else:
            conn.execute(
                """
                INSERT INTO icp_profile (id, profile_text, keywords_json, updated_at)
                VALUES (1, ?, ?, CURRENT_TIMESTAMP)
                """,
                [profile_text, json.dumps(keywords, ensure_ascii=False)],
            )
        conn.commit()
        return jsonify(
            {
                "success": True,
                "generated": len(keywords),
                "newly_enqueued": int(queue_counts["newly_enqueued"]),
                "already_present": int(queue_counts["already_present"]),
                "keywords": keywords,
                "seed_pool_count": len(pool),
                "errors": [],
            }
        )
    except Exception as exc:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        current_app.logger.exception("Seed keyword queue generation failed")
        return jsonify(
            {
                "success": False,
                "generated": 0,
                "newly_enqueued": 0,
                "already_present": 0,
                "keywords": [],
                "errors": [{"error": "seed_keyword_queue_failed", "message": str(exc) or "Seed keyword generation failed."}],
            }
        ), 500
    finally:
        if conn:
            conn.close()


@video_shorts_bp.route("/api/admin/lead-discovery-enrich-emails", methods=["POST"])
def admin_lead_discovery_enrich_emails():
    auth_error = _admin_json_auth_error()
    if auth_error:
        payload, status = auth_error
        payload.update({"results": [], "enriched_count": 0, "channels_sent": 0, "cost_note": ""})
        return jsonify(payload), status

    payload = request.get_json(silent=True) or {}
    supplied = (
        payload.get("channels")
        or payload.get("channel_urls")
        or payload.get("channelUrls")
        or payload.get("channel_ids")
        or []
    )
    if isinstance(supplied, (str, dict)):
        supplied = [supplied]

    channel_urls: List[str] = []
    seen = set()
    for item in (supplied if isinstance(supplied, list) else []):
        if isinstance(item, dict):
            raw_value = (
                item.get("channel_url")
                or item.get("channelUrl")
                or item.get("url")
                or item.get("channel_id")
                or item.get("channelId")
            )
        else:
            raw_value = item
        channel_url = _lead_discovery_channel_url(raw_value)
        key = channel_url.rstrip("/")
        if not channel_url or key in seen:
            continue
        seen.add(key)
        channel_urls.append(channel_url)
        if len(channel_urls) >= LEAD_DISCOVERY_EMAIL_ENRICH_LIMIT:
            break

    if not channel_urls:
        return jsonify(
            {
                "success": False,
                "results": [],
                "enriched_count": 0,
                "channels_sent": 0,
                "cost_note": "",
                "errors": [{"error": "bad_request", "message": "Provide channel URLs or channel IDs."}],
            }
        ), 400

    try:
        enrichment = apify_trakk_enrich(channel_urls)
    except Exception as exc:
        current_app.logger.exception("Lead discovery Apify email enrichment failed")
        return jsonify(
            {
                "success": False,
                "results": [],
                "enriched_count": 0,
                "channels_sent": len(channel_urls),
                "cost_note": f"Estimated Apify actor cost: {len(channel_urls)} x $0.005 = ${len(channel_urls) * 0.005:.3f}",
                "errors": [{"error": "apify_enrichment_failed", "message": str(exc) or "Email enrichment failed."}],
            }
        ), 502

    errors = enrichment.get("errors") or []
    return jsonify(
        {
            "success": not errors or bool(enrichment.get("results")),
            "results": enrichment.get("results") or [],
            "enriched_count": int(enrichment.get("enriched_count") or 0),
            "channels_sent": len(channel_urls),
            "cost_note": f"Estimated Apify actor cost: {len(channel_urls)} x $0.005 = ${len(channel_urls) * 0.005:.3f}",
            "errors": errors,
        }
    )


@video_shorts_bp.route("/api/admin/discovery-enrich-emails", methods=["POST"])
def admin_discovery_enrich_emails():
    auth_error = _admin_json_auth_error()
    if auth_error:
        payload, status = auth_error
        payload.update({"enqueued": False, "job_id": None, "batch_size": 0, "pending_count": 0})
        return jsonify(payload), status

    current_user = getattr(g, "vs_current_user", None) or {}
    payload = request.get_json(silent=True) or {}
    batch_size = _coerce_int_param(payload.get("batch_size"), 5, minimum=1, maximum=20)

    conn = None
    try:
        conn = get_db()
        columns = table_columns(conn, "discovery_leads")
        required_columns = {
            "enrichment_attempted_at",
            "email_enriched_at",
            "email_enrichment_error",
            "email_validation_scope",
            "has_hidden_email",
            "protected_email_status",
        }
        missing = sorted(required_columns - set(columns))
        if missing:
            return jsonify(
                {
                    "success": False,
                    "enqueued": False,
                    "job_id": None,
                    "batch_size": batch_size,
                    "pending_count": 0,
                    "errors": [{"error": "schema_missing", "message": f"Missing discovery_leads columns: {', '.join(missing)}"}],
                }
            ), 500
        pending_row = conn.execute(
            """
            SELECT COUNT(*)
            FROM discovery_leads
            WHERE status IN ('icp_qualified', 'email_failed')
              AND COALESCE(creator_email, '') = ''
            """
        ).fetchone()
        pending_count = int((pending_row[0] if pending_row else 0) or 0)
    finally:
        if conn:
            conn.close()

    enqueue_result = enqueue_worker_job(
        user_id=str(current_user.get("id") or "admin"),
        job_type=JOB_TYPE_ENRICH_DISCOVERY_EMAILS,
        payload={"batch_size": batch_size},
        input_hash=f"discovery-email-enrich:{uuid4()}",
        max_attempts=1,
        priority=20,
    )
    job = enqueue_result.get("job") or {}
    return jsonify(
        {
            "success": True,
            "enqueued": True,
            "job_id": job.get("id"),
            "batch_size": batch_size,
            "pending_count": pending_count,
            "cost_note": f"Batch of {batch_size}; estimated Apify actor cost: {batch_size} x $0.005 = ${batch_size * 0.005:.3f}",
            "errors": [],
        }
    )


@video_shorts_bp.route("/api/admin/discovery-automation/toggle", methods=["POST"])
def admin_discovery_automation_toggle():
    auth_error = _admin_json_auth_error()
    if auth_error:
        payload, status = auth_error
        payload.update({"control": {}})
        return jsonify(payload), status

    payload = request.get_json(silent=True) or {}
    try:
        from app.video_shorts.services.discovery_automation import update_discovery_automation_control

        control = update_discovery_automation_control(
            {
                "enabled": bool(payload.get("enabled")),
                "paused_reason": payload.get("paused_reason") or "",
            }
        )
        return jsonify({"success": True, "control": control, "errors": []})
    except Exception as exc:
        current_app.logger.exception("Discovery automation toggle failed")
        return jsonify({"success": False, "control": {}, "errors": [{"error": "automation_toggle_failed", "message": str(exc)}]}), 500


@video_shorts_bp.route("/api/admin/discovery-automation/caps", methods=["POST"])
def admin_discovery_automation_caps():
    auth_error = _admin_json_auth_error()
    if auth_error:
        payload, status = auth_error
        payload.update({"control": {}})
        return jsonify(payload), status

    payload = request.get_json(silent=True) or {}
    try:
        from app.video_shorts.services.discovery_automation import update_discovery_automation_control

        control = update_discovery_automation_control(
            {
                "runs_per_day": payload.get("runs_per_day"),
                "offpeak_hours_pt": payload.get("offpeak_hours_pt"),
                "max_keywords_per_cycle": payload.get("max_keywords_per_cycle"),
                "max_results_per_keyword": payload.get("max_results_per_keyword"),
                "max_channels_enriched_per_cycle": payload.get("max_channels_enriched_per_cycle"),
                "max_trakk_per_cycle": payload.get("max_trakk_per_cycle"),
            }
        )
        return jsonify({"success": True, "control": control, "errors": []})
    except Exception as exc:
        current_app.logger.exception("Discovery automation caps update failed")
        return jsonify({"success": False, "control": {}, "errors": [{"error": "automation_caps_failed", "message": str(exc)}]}), 500


@video_shorts_bp.route("/api/admin/discovery-automation/run-now", methods=["POST"])
def admin_discovery_automation_run_now():
    auth_error = _admin_json_auth_error()
    if auth_error:
        payload, status = auth_error
        payload.update({"run_id": None, "result": {}})
        return jsonify(payload), status

    try:
        from app.video_shorts.services.discovery_automation import run_discovery_automation_cycle

        result = run_discovery_automation_cycle(manual=True, require_enabled=False)
        return jsonify({"success": bool(result.get("success")), **result, "errors": [] if result.get("success") else [{"error": "automation_run_failed", "message": result.get("error") or result.get("reason") or "Run did not complete."}]})
    except Exception as exc:
        current_app.logger.exception("Discovery automation run-now failed")
        return jsonify({"success": False, "run_id": None, "result": {}, "errors": [{"error": "automation_run_failed", "message": str(exc)}]}), 500


@video_shorts_bp.route("/api/admin/lead-discovery-seed-keywords", methods=["POST"])
def admin_lead_discovery_seed_keywords():
    current_user = getattr(g, "vs_current_user", None)
    if not current_user:
        return jsonify(
            {
                "success": False,
                "seed_channels_used": 0,
                "seed_read_calls": 0,
                "keywords": [],
                "errors": [{"error": "unauthorized", "message": "Admin session required."}],
            }
        ), 401
    if (current_user.get("role") or "").strip().lower() != "admin":
        return jsonify(
            {
                "success": False,
                "seed_channels_used": 0,
                "seed_read_calls": 0,
                "keywords": [],
                "errors": [{"error": "forbidden", "message": "Admin access required."}],
            }
        ), 403

    try:
        seeds = _load_emailed_seed_channels(LEAD_DISCOVERY_SEED_CHANNEL_LIMIT)
        hydrated, seed_read_calls = _hydrate_seed_channel_context(seeds)
        if not hydrated:
            return jsonify(
                {
                    "success": False,
                    "seed_channels_used": 0,
                    "seed_read_calls": seed_read_calls,
                    "keywords": [],
                    "errors": [{"error": "no_seed_channels", "message": "No emailed seed channels were found."}],
                }
            ), 404
        keywords = _generate_keywords_from_seed_context(hydrated)
        return jsonify(
            {
                "success": True,
                "seed_channels_used": len(hydrated),
                "seed_read_calls": seed_read_calls,
                "keywords": keywords,
                "errors": [],
            }
        )
    except YoutubeApiError as exc:
        env_message = _youtube_env_error_message(exc)
        message = env_message or str(exc) or "YouTube API request failed."
        return jsonify(
            {
                "success": False,
                "seed_channels_used": 0,
                "seed_read_calls": 0,
                "keywords": [],
                "errors": [{"error": "youtube_api_error", "message": message}],
            }
        ), 500 if env_message else 502
    except Exception as exc:
        current_app.logger.exception("Lead discovery seed keyword generation failed")
        return jsonify(
            {
                "success": False,
                "seed_channels_used": 0,
                "seed_read_calls": 0,
                "keywords": [],
                "errors": [{"error": "seed_keyword_generation_failed", "message": str(exc) or "Seed keyword generation failed."}],
            }
        ), 500


@video_shorts_bp.route("/api/admin/seed-backfill-descriptions", methods=["POST"])
def admin_seed_backfill_descriptions():
    auth_error = _admin_json_auth_error()
    if auth_error:
        payload, status = auth_error
        payload.update({"updated_count": 0, "read_calls": 0})
        return jsonify(payload), status

    conn = None
    try:
        conn = get_db()
        seeds = _load_emailed_seed_sources(conn)
        channel_ids = sorted({seed["channel_id"] for seed in seeds if seed.get("channel_id")})
        if not channel_ids:
            return jsonify({"success": True, "updated_count": 0, "read_calls": 0, "errors": []})

        descriptions: Dict[str, str] = {}
        read_calls = 0
        for i in range(0, len(channel_ids), 50):
            chunk = channel_ids[i : i + 50]
            payload = _youtube_get_json(
                "channels",
                {"part": "snippet", "id": ",".join(chunk)},
                timeout=10,
            )
            read_calls += 1
            for item in payload.get("items") or []:
                channel_id = str(item.get("id") or "").strip()
                snippet = item.get("snippet") or {}
                description = str(snippet.get("description") or "").strip()
                if channel_id and description:
                    descriptions[channel_id] = description

        updated_count = 0
        for channel_id, description in descriptions.items():
            result = conn.execute(
                """
                UPDATE youtube_channels
                SET channel_description = ?
                WHERE youtube_channel_id = ?
                  AND COALESCE(channel_description, '') <> ?
                """,
                [description, channel_id, description],
            )
            try:
                updated_count += max(0, int(result.rowcount or 0))
            except Exception:
                updated_count += 1
        conn.commit()
        return jsonify(
            {
                "success": True,
                "updated_count": updated_count,
                "read_calls": read_calls,
                "seed_channels": len(channel_ids),
                "errors": [],
            }
        )
    except Exception as exc:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        current_app.logger.exception("Seed channel description backfill failed")
        return jsonify(
            {
                "success": False,
                "updated_count": 0,
                "read_calls": 0,
                "errors": [{"error": "seed_backfill_failed", "message": str(exc) or "Backfill failed."}],
            }
        ), 500
    finally:
        if conn:
            conn.close()


@video_shorts_bp.route("/api/admin/seed-build-profile", methods=["POST"])
def admin_seed_build_profile():
    auth_error = _admin_json_auth_error()
    if auth_error:
        payload, status = auth_error
        payload.update({"seed_used": 0, "summaries_created": 0, "keywords": [], "profile_text": "", "seed_source_video_titles": []})
        return jsonify(payload), status

    conn = None
    try:
        conn = get_db()
        seeds = [seed for seed in _load_emailed_seed_sources(conn) if seed.get("source_video_id") and seed.get("transcript_text")]
        if not seeds:
            return jsonify(
                {
                    "success": False,
                    "seed_used": 0,
                    "summaries_created": 0,
                    "keywords": [],
                    "profile_text": "",
                    "seed_source_video_titles": [],
                    "errors": [{"error": "no_seed_transcripts", "message": "No emailed seed transcripts were found."}],
                }
            ), 404

        summaries_created = 0
        profile_items: List[Dict[str, Any]] = []
        for seed in seeds:
            existing = conn.execute(
                """
                SELECT transcript_summary
                FROM seed_channel_profiles
                WHERE channel_id = ?
                LIMIT 1
                """,
                [seed["channel_id"]],
            ).fetchone()
            summary = str((existing or [None])[0] or "").strip()
            if not summary:
                summary = _summarize_seed_transcript(seed)
                summaries_created += 1
                if existing:
                    conn.execute(
                        """
                        UPDATE seed_channel_profiles
                        SET source_video_id = ?, transcript_summary = ?
                        WHERE channel_id = ?
                        """,
                        [seed["source_video_id"], summary, seed["channel_id"]],
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO seed_channel_profiles (
                            channel_id, source_video_id, transcript_summary, created_at
                        )
                        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                        """,
                        [seed["channel_id"], seed["source_video_id"], summary],
                    )
            profile_items.append(
                {
                    "channel_id": seed["channel_id"],
                    "channel_name": seed.get("channel_name") or "",
                    "channel_description": seed.get("channel_description") or "",
                    "source_video_title": seed.get("source_video_title") or "",
                    "transcript_summary": summary,
                }
            )

        source_video_titles = [
            str(item.get("source_video_title") or "").strip()
            for item in profile_items
            if str(item.get("source_video_title") or "").strip()
        ]
        keywords = _generate_seed_search_keywords(profile_items)
        existing_profile = conn.execute("SELECT profile_text FROM icp_profile WHERE id = 1").fetchone()
        profile_text = str((existing_profile or [None])[0] or "").strip()
        if not profile_text:
            profile = _generate_seed_icp_profile(profile_items)
            profile_text = profile["profile_text"]
        if existing_profile:
            conn.execute(
                """
                UPDATE icp_profile
                SET profile_text = ?, keywords_json = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = 1
                """,
                [profile_text, json.dumps(keywords, ensure_ascii=False)],
            )
        else:
            conn.execute(
                """
                INSERT INTO icp_profile (id, profile_text, keywords_json, updated_at)
                VALUES (1, ?, ?, CURRENT_TIMESTAMP)
                """,
                [profile_text, json.dumps(keywords, ensure_ascii=False)],
            )
        conn.commit()
        return jsonify(
            {
                "success": True,
                "seed_used": len(profile_items),
                "summaries_created": summaries_created,
                "keywords": keywords,
                "profile_text": profile_text,
                "seed_source_video_titles": source_video_titles,
                "errors": [],
            }
        )
    except Exception as exc:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        current_app.logger.exception("Seed ICP profile build failed")
        return jsonify(
            {
                "success": False,
                "seed_used": 0,
                "summaries_created": 0,
                "keywords": [],
                "profile_text": "",
                "seed_source_video_titles": [],
                "errors": [{"error": "seed_profile_failed", "message": str(exc) or "Seed profile build failed."}],
            }
        ), 500
    finally:
        if conn:
            conn.close()


@video_shorts_bp.route("/api/admin/seed-icp-profile", methods=["GET", "POST"])
def admin_seed_icp_profile():
    auth_error = _admin_json_auth_error()
    if auth_error:
        payload, status = auth_error
        payload.update({"keywords": [], "profile_text": ""})
        return jsonify(payload), status
    conn = None
    try:
        conn = get_db_readonly()
        row = conn.execute(
            """
            SELECT profile_text, keywords_json, updated_at
            FROM icp_profile
            WHERE id = 1
            LIMIT 1
            """
        ).fetchone()
        if not row:
            return jsonify(
                {
                    "success": False,
                    "keywords": [],
                    "profile_text": "",
                    "errors": [{"error": "profile_missing", "message": "ICP profile has not been built yet."}],
                }
            ), 404
        try:
            keywords = _normalize_discovery_keywords(json.loads(row[1] or "[]"))[:15]
        except Exception:
            keywords = _normalize_discovery_keywords(row[1])[:15]
        updated_at = row[2]
        return jsonify(
            {
                "success": True,
                "keywords": keywords,
                "profile_text": str(row[0] or ""),
                "updated_at": updated_at.isoformat() if hasattr(updated_at, "isoformat") else str(updated_at or ""),
                "errors": [],
            }
        )
    except Exception as exc:
        current_app.logger.exception("Seed ICP profile load failed")
        return jsonify(
            {
                "success": False,
                "keywords": [],
                "profile_text": "",
                "errors": [{"error": "profile_load_failed", "message": str(exc) or "Profile load failed."}],
            }
        ), 500
    finally:
        if conn:
            conn.close()


@video_shorts_bp.route("/api/admin/add-youtube-video", methods=["POST"])
def admin_add_youtube_video():
    current_user = getattr(g, "vs_current_user", None)
    if not current_user:
        return _json_error("unauthorized", "Admin session required.", 401)
    if (current_user.get("role") or "").strip().lower() != "admin":
        return _json_error("forbidden", "Admin access required.", 403)

    is_browser_form = not request.is_json
    payload = request.get_json(silent=True) if request.is_json else request.form
    payload = payload or {}
    raw_url = str(payload.get("url") or "").strip()
    if not raw_url:
        return _json_error("invalid_url", "Request body must include a YouTube video url.", 400)

    brand_id = str(current_brand_id() or "").strip() or None
    if not brand_id:
        return _json_error("no_active_brand", "No active brand is selected for this session.", 400)

    video_id = extract_video_id(raw_url)
    if not video_id or len(video_id) != 11:
        return _json_error("invalid_url", "Unsupported or invalid YouTube video URL.", 400)

    canonical_url = f"https://www.youtube.com/watch?v={video_id}"

    try:
        meta = fetch_video_metadata(video_id)
    except YoutubeApiError as exc:
        env_message = _youtube_env_error_message(exc)
        if env_message:
            return _json_error("server_config", env_message, 500)
        message = str(exc or "").strip() or "YouTube API request failed."
        return _json_error("youtube_api_error", message, 502)

    resolved_channel_key = str(meta.get("channel_id") or "").strip()
    if not resolved_channel_key:
        return _json_error("channel_not_found", "Channel could not be resolved from video metadata.", 404)

    manual_creator_name_present = bool(str(payload.get("creator_name") or "").strip())
    manual_creator_email_present = bool(str(payload.get("creator_email") or "").strip())
    try:
        subscriber_map = fetch_channel_subscriber_counts([resolved_channel_key])
    except YoutubeApiError as exc:
        env_message = _youtube_env_error_message(exc)
        if env_message:
            return _json_error("server_config", env_message, 500)
        message = str(exc or "").strip() or "YouTube API request failed."
        return _json_error("youtube_api_error", message, 502)
    subscriber_info = subscriber_map.get(resolved_channel_key) or {}
    try:
        subscriber_count = int(subscriber_info.get("subscriber_count"))
    except (TypeError, ValueError):
        subscriber_count = None
    channel_title = (
        subscriber_info.get("channel_title")
        or meta.get("channel_title")
        or None
    )
    channel_description = subscriber_info.get("channel_description")
    if channel_description:
        meta["channel_description"] = channel_description
    if channel_title and not meta.get("channel_title"):
        meta["channel_title"] = channel_title
    fallback_creator_name = _resolve_auto_creator_name(
        channel_description,
        meta.get("description"),
        channel_title,
    )
    fallback_creator_email = _resolve_auto_creator_email(
        channel_description,
        meta.get("description"),
    )
    creator_name = (
        _normalize_manual_creator_name(payload.get("creator_name"), channel_title)
        if manual_creator_name_present
        else fallback_creator_name
    )
    creator_email = (
        _normalize_manual_creator_email(payload.get("creator_email"))
        if manual_creator_email_present
        else fallback_creator_email
    )

    conn = None
    try:
        conn = get_db()
        current_brand = _resolve_brand_local_uploads_channel(conn, brand_id)
        if not current_brand:
            return _json_error("no_local_uploads_channel", "No active Local uploads channel exists for the active brand.", 400)

        lead = create_autopilot_lead_from_video(
            conn,
            meta=meta,
            video_id=video_id,
            canonical_url=canonical_url,
            creator_name=creator_name,
            creator_email=creator_email,
            subscriber_count=subscriber_count,
            discovery_owner_user_id=current_brand["owner_user_id"],
            discovery_brand_id=brand_id,
        )
        conn.commit()
        response = {
            "ok": True,
            "lead_id": lead["lead_id"],
            "video_id": lead["video_pk"],
            "youtube_video_id": lead["video_id"],
            "channel_id": lead["channel_id"],
            "brand_id": lead["brand_id"],
            "owner_user_id": lead["owner_user_id"],
            "creator_name": lead["creator_name"],
            "creator_email": lead["creator_email"],
            "subscriber_count": lead["subscriber_count"],
            "discovery_only": lead["discovery_only"],
            "title": meta.get("title") or canonical_url,
            "already_exists": lead["already_exists"],
            "updated": False,
        }
        if is_browser_form:
            if lead["discovery_only"]:
                flash("Discovery lead added. Add an email before provisioning its customer account.", "info")
            else:
                flash("Autopilot lead provisioned and its source video was added to that lead's brand.", "success")
            return redirect(url_for("video_shorts_bp.admin_leads"))
        return jsonify(response)

        # The legacy current-brand insertion path remains below temporarily for
        # reference while this endpoint is migrated; it is unreachable.
        local_bucket_channel_id = current_brand["channel_id"]
        owner_user_id = current_brand["owner_user_id"]

        if _video_already_in_bucket(conn, video_id, local_bucket_channel_id):
            existing_local = conn.execute(
                """
                SELECT id
                FROM youtube_videos
                WHERE video_id = ?
                  AND (channel_id = ? OR local_bucket_channel_id = ?)
                LIMIT 1
                """,
                [video_id, local_bucket_channel_id, local_bucket_channel_id],
            ).fetchone()
            row = conn.execute(
                """
                SELECT id, channel_id, brand_id, title, creator_name, creator_email
                FROM youtube_videos
                WHERE id = ?
                LIMIT 1
                """,
                [existing_local[0]],
            ).fetchone()
            updated = False
            resolved_creator_name = row[4] if row else None
            resolved_creator_email = row[5] if row else None
            if row:
                updated = _update_existing_creator_fields(
                    conn,
                    row[0],
                    creator_name=creator_name if manual_creator_name_present else _CREATOR_FIELD_UNSET,
                    creator_email=creator_email if manual_creator_email_present else _CREATOR_FIELD_UNSET,
                )
                if updated:
                    conn.commit()
                    resolved_creator_name = creator_name if manual_creator_name_present else resolved_creator_name
                    resolved_creator_email = creator_email if manual_creator_email_present else resolved_creator_email
            return jsonify(
                {
                    "ok": True,
                    "video_id": _coerce_video_pk(row[0] if row else existing_local[0]),
                    "youtube_video_id": video_id,
                    "channel_id": str((row[1] if row else meta.get("channel_id")) or "").strip() or None,
                    "brand_id": str((row[2] if row else brand_id) or "").strip() or brand_id,
                    "creator_name": resolved_creator_name,
                    "creator_email": resolved_creator_email,
                    "title": (row[3] if row else meta.get("title")),
                    "already_exists": True,
                    "updated": updated,
                }
            )

        try:
            resolved_channel_id = _get_or_create_real_youtube_channel(
                conn,
                meta,
                owner_user_id,
                brand_id,
            )
        except Exception:
            current_app.logger.exception(
                "Failed to resolve or create real YouTube channel for admin add endpoint video_id=%s",
                video_id,
            )
            return _json_error("server_error", "Failed to resolve the creator channel.", 500)
        if resolved_channel_id is None:
            return _json_error("channel_not_found", "Creator channel could not be resolved.", 404)

        existing = conn.execute(
            """
            SELECT id, owner_user_id, brand_id, local_bucket_channel_id, channel_id, title, creator_name, creator_email
            FROM youtube_videos
            WHERE video_id = ?
            LIMIT 1
            """,
            [video_id],
        ).fetchone()
        if existing:
            existing_owner_user_id = str(existing[1] or "").strip() or None
            existing_brand_id = str(existing[2] or "").strip() or None
            existing_local_bucket_channel_id = existing[3]
            same_scope = existing_owner_user_id == owner_user_id and existing_brand_id == brand_id
            if same_scope and existing_local_bucket_channel_id != local_bucket_channel_id:
                conn.execute(
                    """
                    UPDATE youtube_videos
                    SET local_bucket_channel_id = ?,
                        creator_name = COALESCE(creator_name, ?),
                        creator_email = COALESCE(creator_email, ?)
                    WHERE id = ?
                    """,
                    [local_bucket_channel_id, creator_name, creator_email, existing[0]],
                )
                updated = _update_existing_creator_fields(
                    conn,
                    existing[0],
                    creator_name=creator_name if manual_creator_name_present else _CREATOR_FIELD_UNSET,
                    creator_email=creator_email if manual_creator_email_present else _CREATOR_FIELD_UNSET,
                )
                conn.commit()
                return jsonify(
                    {
                        "ok": True,
                        "video_id": _coerce_video_pk(existing[0]),
                        "youtube_video_id": video_id,
                        "channel_id": str(existing[4] or "").strip() or resolved_channel_id,
                        "brand_id": brand_id,
                        "creator_name": creator_name if manual_creator_name_present else (existing[6] or creator_name),
                        "creator_email": creator_email if manual_creator_email_present else (existing[7] or creator_email),
                        "title": existing[5] or meta.get("title"),
                        "already_exists": True,
                        "updated": updated,
                    }
                )
            updated = _update_existing_creator_fields(
                conn,
                existing[0],
                creator_name=creator_name if manual_creator_name_present else _CREATOR_FIELD_UNSET,
                creator_email=creator_email if manual_creator_email_present else _CREATOR_FIELD_UNSET,
            )
            if updated:
                conn.commit()
            return jsonify(
                {
                    "ok": True,
                    "video_id": _coerce_video_pk(existing[0]),
                    "youtube_video_id": video_id,
                    "channel_id": str(existing[4] or "").strip() or resolved_channel_id,
                    "brand_id": existing_brand_id or brand_id,
                    "creator_name": creator_name if manual_creator_name_present else (existing[6] or creator_name),
                    "creator_email": creator_email if manual_creator_email_present else (existing[7] or creator_email),
                    "title": existing[5] or meta.get("title"),
                    "already_exists": True,
                    "updated": updated,
                }
            )

        conn.execute(
            """
            INSERT INTO youtube_videos
                (channel_id, video_id, title, published_at, thumbnail_url, fetch_transcript,
                 duration_seconds, view_count, like_count, comment_count, video_url, local_bucket_channel_id,
                 owner_user_id, brand_id, download_status, subtitle_style, creator_name, creator_email)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                resolved_channel_id,
                video_id,
                meta.get("title") or canonical_url,
                meta.get("published_at"),
                meta.get("thumbnail_url"),
                False,
                meta.get("duration_seconds"),
                meta.get("view_count"),
                meta.get("like_count"),
                meta.get("comment_count"),
                canonical_url,
                local_bucket_channel_id,
                owner_user_id,
                brand_id,
                "pending",
                "karaoke",
                creator_name,
                creator_email,
            ],
        )
        inserted = conn.execute(
            """
            SELECT id
            FROM youtube_videos
            WHERE video_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            [video_id],
        ).fetchone()
        conn.commit()
        return jsonify(
            {
                "ok": True,
                "video_id": _coerce_video_pk(inserted[0] if inserted else None),
                "youtube_video_id": video_id,
                "channel_id": resolved_channel_id,
                "brand_id": brand_id,
                "creator_name": creator_name,
                "creator_email": creator_email,
                "title": meta.get("title") or canonical_url,
                "already_exists": False,
                "updated": False,
            }
        )
    except AutopilotLeadSchemaUnavailable as exc:
        if conn is not None:
            conn.rollback()
        if is_browser_form:
            flash(str(exc), "warning")
            return redirect(url_for("video_shorts_bp.admin_leads"))
        return _json_error("autopilot_leads_unavailable", str(exc), 503)
    except ValueError as exc:
        if conn is not None:
            conn.rollback()
        if is_browser_form:
            flash(str(exc), "warning")
            return redirect(url_for("video_shorts_bp.admin_leads"))
        return _json_error("lead_not_created", str(exc), 400)
    except RuntimeError as exc:
        message = str(exc or "").strip()
        return _json_error("server_config", message or "Database is not configured.", 500)
    except Exception:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        current_app.logger.exception("Unexpected error in admin add YouTube video endpoint")
        return _json_error("server_error", "Unexpected server error.", 500)
    finally:
        if conn is not None:
            conn.close()


@video_shorts_bp.route("/api/caption-tasks", methods=["GET"])
def caption_tasks():
    if not _check_caption_token(request):
        return jsonify({"error": "forbidden"}), 403

    try:
        limit = int(request.args.get("limit", "20"))
    except Exception:
        limit = 20

    conn = get_db_readonly()
    rows = conn.execute(
        """
        SELECT id, video_id, title AS video_title, video_url
        FROM youtube_videos
        WHERE fetch_transcript = TRUE
          AND lower(transcript_status) = 'pending'
        ORDER BY published_at DESC
        LIMIT ?
        """,
        [limit],
    ).fetchall()
    cols = [d[0] for d in conn.description]
    tasks = [dict(zip(cols, r)) for r in rows]
    conn.close()
    return jsonify({"tasks": tasks})


@video_shorts_bp.route("/api/usage", methods=["GET"])
def usage_api():
    current_user = getattr(g, "vs_current_user", None)
    if not current_user:
        return jsonify({"error": "unauthorized"}), 401
    try:
        payload = get_usage_snapshot(current_user["id"])
        if str(current_user.get("service_mode") or "").strip().lower() == "autopilot":
            payload["autopilot"] = _autopilot_usage_summary(current_user)
        return jsonify(payload)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


def _autopilot_usage_summary(current_user: dict) -> dict:
    """Return customer-facing short progress without exposing self-serve metering."""
    try:
        tier = int(current_user.get("service_tier") or 15)
    except (TypeError, ValueError):
        tier = 15
    period_start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    generated_count = 0
    published_count = 0
    conn = get_db_readonly()
    try:
        row = conn.execute(
            """
            SELECT
                COUNT(*),
                COALESCE(SUM(CASE WHEN lower(coalesce(publish_status, '')) = 'published' THEN 1 ELSE 0 END), 0)
            FROM shorts_generated_videos
            WHERE CAST(user_id AS VARCHAR) = ?
              AND created_at >= ?
            """,
            [str(current_user["id"]), period_start],
        ).fetchone()
        generated_count = int((row or [0, 0])[0] or 0)
        published_count = int((row or [0, 0])[1] or 0)
    finally:
        conn.close()
    return {
        "tier": tier,
        "prepared_count": generated_count,
        "published_count": published_count,
        "display_count": published_count if published_count else generated_count,
        "display_kind": "published" if published_count else "prepared",
    }


@video_shorts_bp.route("/api/autopilot/upgrade-request", methods=["POST"])
def autopilot_upgrade_request_api():
    current_user = getattr(g, "vs_current_user", None)
    if not current_user:
        return jsonify({"error": "unauthorized"}), 401
    if str(current_user.get("service_mode") or "").strip().lower() != "autopilot":
        return jsonify({"error": "not_found"}), 404

    payload = request.get_json(silent=True) or {}
    try:
        requested_tier = int(payload.get("tier"))
    except (TypeError, ValueError):
        return jsonify({"error": "Choose a valid autopilot tier."}), 400
    if requested_tier not in {15, 30, 45, 60}:
        return jsonify({"error": "Choose a valid autopilot tier."}), 400

    try:
        current_tier = int(current_user.get("service_tier") or 15)
    except (TypeError, ValueError):
        current_tier = 15
    user_email = str(current_user.get("email") or current_user.get("username") or "").strip()
    track_event(
        current_user["id"],
        "autopilot_tier_upgrade_requested",
        metadata={
            "current_tier": current_tier,
            "requested_tier": requested_tier,
            "email": user_email,
        },
    )
    try:
        send_autopilot_upgrade_request_email(
            user_email=user_email or "(missing)",
            current_tier=current_tier,
            requested_tier=requested_tier,
        )
    except Exception:
        current_app.logger.exception(
            "Could not send autopilot upgrade request notification user_id=%s", current_user["id"]
        )
    return jsonify({"ok": True, "message": "Your upgrade request has been sent to Minti."})


@video_shorts_bp.route("/api/jobs/<job_id>", methods=["GET"])
def render_job_status_api(job_id: str):
    current_user = getattr(g, "vs_current_user", None)
    if not current_user:
        return jsonify({"error": "unauthorized"}), 401
    try:
        job = get_job(job_id, user_id=str(current_user["id"]))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    if not job:
        return jsonify({"error": "not_found"}), 404
    error_text = str(job.get("error") or "").strip()
    error_code = None
    lowered_error = error_text.lower()
    if "export limit reached" in lowered_error or "monthly export limit reached" in lowered_error:
        error_code = "export_limit_reached"
    return jsonify(
        {
            "id": job["id"],
            "status": job["status"],
            "priority": job["priority"],
            "created_at": job["created_at"],
            "started_at": job["started_at"],
            "finished_at": job["finished_at"],
            "result": job.get("result"),
            "error": job.get("error"),
            "error_code": error_code,
            "queue_position": job.get("queue_position"),
        }
    )


@video_shorts_bp.route("/api/client-error", methods=["POST"])
def client_error_api():
    content_length = int(request.content_length or 0)
    if content_length > CLIENT_ERROR_MAX_BODY_BYTES:
        return ("", 204)

    key_parts = [
        current_event_user_id(),
        request.headers.get("X-Forwarded-For", ""),
        request.remote_addr or "",
    ]
    allowed, _retry_after = check_rate_limits("client-error", key_parts, CLIENT_ERROR_RATE_LIMITS)
    if not allowed:
        return ("", 204)

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return ("", 204)

    error_type = str(payload.get("error_type") or "").strip().lower()
    if not error_type:
        return ("", 204)

    capture_client_error(
        error_type=error_type,
        message=payload.get("message"),
        source=payload.get("source") or payload.get("page"),
        user_agent=request.headers.get("User-Agent"),
    )
    return ("", 204)


@video_shorts_bp.route("/api/caption-result", methods=["POST"])
def caption_result():
    if not _check_caption_token(request):
        return jsonify({"error": "forbidden"}), 403

    data = request.get_json(silent=True) or {}
    video_db_id = data.get("video_db_id")
    caption_text = (data.get("caption_text") or "").strip()
    lang = (data.get("lang") or "en").strip()
    segments = data.get("segments")

    if not video_db_id or not caption_text:
        return jsonify({"error": "missing fields"}), 400

    segments_json = None
    whisper_segments_json = None
    if isinstance(segments, list):
        try:
            normalized = _normalize_segments_for_use(segments)
            whisper_segments_json = json.dumps(normalized, ensure_ascii=False)
            segments_json = whisper_segments_json
        except Exception:
            segments_json = None
            whisper_segments_json = None

    conn = get_db()
    _ensure_video_crop_schema(conn)
    _ensure_transcript_schema(conn)
    ensure_postgres_youtube_transcripts_id_default(conn)
    try:
        row = conn.execute(
            "SELECT video_id, owner_user_id, duration_seconds, title FROM youtube_videos WHERE id = ?",
            [video_db_id],
        ).fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "video not found"}), 404
        video_id = row[0]
        owner_user_id = row[1]
        duration_seconds = row[2]
        video_title = row[3]
        event_video_id, should_emit_transcript_completed = prepare_transcript_completed_transition(
            conn,
            video_pk=video_db_id,
        )

        conn.execute(
            """
            INSERT INTO youtube_transcripts (video_id, full_text, segments_json, whisper_segments_json)
            VALUES (?, ?, ?, ?)
            """,
            [video_id, caption_text, segments_json, whisper_segments_json],
        )

        conn.execute(
            """
            UPDATE youtube_videos
            SET transcript_status = 'done',
                fetch_transcript = FALSE,
                last_checked_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            [video_db_id],
        )
        conn.commit()
        conn.close()
        if should_emit_transcript_completed and owner_user_id:
            track_event(
                str(owner_user_id),
                "transcript_completed",
                video_id=event_video_id or video_id,
                status="completed",
            )
        if owner_user_id:
            minutes = _duration_minutes(duration_seconds)
            if minutes > 0:
                try:
                    add_transcription_minutes(
                        str(owner_user_id),
                        minutes,
                        video_id=video_id,
                        video_title=video_title,
                    )
                except Exception:
                    current_app.logger.exception(
                        "Failed to meter caption worker transcription usage for video_db_id=%s",
                        video_db_id,
                    )
        return jsonify({"ok": True}), 200
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()
        return jsonify({"error": str(e)}), 500


@video_shorts_bp.route("/api/caption-status", methods=["POST"])
def caption_status():
    """
    Worker can report non-success states (e.g., no transcript available or an error)
    so the same video does not keep re-appearing in the queue.
    """
    if not _check_caption_token(request):
        return jsonify({"error": "forbidden"}), 403

    data = request.get_json(silent=True) or {}
    video_db_id = data.get("video_db_id")
    status = (data.get("status") or "").strip().lower()
    if not video_db_id or not status:
        return jsonify({"error": "missing fields"}), 400

    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id FROM youtube_videos WHERE id = ?",
            [video_db_id],
        ).fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "video not found"}), 404

        conn.execute(
            """
            UPDATE youtube_videos
            SET transcript_status = ?,
                fetch_transcript = FALSE,
                last_checked_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            [status, video_db_id],
        )
        conn.commit()
        conn.close()
        return jsonify({"ok": True}), 200
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()
        return jsonify({"error": str(e)}), 500


@video_shorts_bp.route("/api/download-status", methods=["POST"])
def download_status():
    """
    Allow a local downloader to mark the video as downloaded (or failed) on the central DB.
    This mirrors how transcript workers report status so local/remote stay in sync.
    """
    if not _check_caption_token(request):
        return jsonify({"error": "forbidden"}), 403

    data = request.get_json(silent=True) or {}
    video_db_id = data.get("video_db_id")
    status = (data.get("status") or "").strip().lower()
    source_key = str(data.get("source_key") or "").strip().lstrip("/")
    if not video_db_id or not status:
        return jsonify({"error": "missing fields"}), 400
    if status == "downloaded" and not source_key:
        return jsonify({"error": "missing source_key"}), 400

    conn = get_db()
    try:
        row = conn.execute(
            """
            SELECT id, video_id, owner_user_id, brand_id, duration_seconds
            FROM youtube_videos
            WHERE id = ?
            """,
            [video_db_id],
        ).fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "video not found"}), 404
        owner_user_id = str(row[2] or "").strip()
        brand_id = str(row[3] or "").strip()
        if status == "downloaded" and (not owner_user_id or not brand_id):
            conn.close()
            return jsonify({"error": "video scope missing"}), 409
        if status == "downloaded":
            video_id = str(row[1] or "").strip()
            expected_prefix = f"videos/{video_id}."
            if not video_id or not source_key.startswith(expected_prefix) or "/" in source_key[len("videos/") :]:
                conn.close()
                return jsonify({"error": "source_key mismatch"}), 400

        conn.execute(
            """
            UPDATE youtube_videos
            SET download_status = ?,
                video_url = CASE WHEN ? = 'downloaded' THEN ? ELSE video_url END,
                downloaded_at = CASE WHEN ? = 'downloaded' THEN CURRENT_TIMESTAMP ELSE NULL END,
                last_checked_at = CURRENT_TIMESTAMP
            WHERE id = ? AND owner_user_id = ? AND brand_id = ?
            """,
            [
                status,
                status,
                f"s3://{source_key}",
                status,
                video_db_id,
                owner_user_id,
                brand_id,
            ],
        )
        conn.commit()
        conn.close()
        preview_result = None
        if status == "downloaded":
            try:
                preview_result = enqueue_preview_frame_job(
                    owner_user_id=owner_user_id,
                    brand_id=brand_id,
                    video_pk=int(row[0]),
                    source_key=source_key,
                    duration_seconds=row[4],
                )
            except Exception as preview_exc:
                current_app.logger.warning(
                    "preview enqueue skipped after download callback video_db_id=%s key=%s error=%s",
                    video_db_id,
                    source_key,
                    preview_exc,
                )
                preview_result = {"kind": "error", "error": str(preview_exc)}
        return jsonify({"ok": True, "preview": preview_result}), 200
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()
        return jsonify({"error": str(e)}), 500


@video_shorts_bp.route("/api/download-tasks", methods=["GET"])
def download_tasks():
    """
    Provide a queue for downloader workers based on download_status='pending'.
    """
    if not _check_caption_token(request):
        return jsonify({"error": "forbidden"}), 403

    try:
        limit = int(request.args.get("limit", "20"))
    except Exception:
        limit = 20

    conn = get_db_readonly()
    rows = conn.execute(
        """
        SELECT
          yv.id,
          yv.channel_id,
          ch.channel_name,
          yv.video_id,
          yv.title AS video_title,
          yv.video_url,
          yv.owner_user_id,
          yv.brand_id,
          yv.duration_seconds,
          yv.download_status
        FROM youtube_videos yv
        LEFT JOIN youtube_channels ch ON ch.channel_id = yv.channel_id
        WHERE lower(coalesce(yv.download_status,'')) = 'pending'
        ORDER BY yv.published_at DESC
        LIMIT ?
        """,
        [limit],
    ).fetchall()
    cols = [d[0] for d in conn.description]
    tasks = [dict(zip(cols, r)) for r in rows]
    conn.close()
    return jsonify({"tasks": tasks})
