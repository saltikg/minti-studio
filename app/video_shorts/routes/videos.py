from datetime import datetime, timedelta, timezone, time as dt_time
from collections import deque
import os
import re
import unicodedata
from zoneinfo import ZoneInfo
from typing import Any, Dict, List, Optional, Tuple
from pathlib import Path

from flask import flash, redirect, render_template, request, url_for, jsonify, current_app, g, abort, has_request_context
import json
import requests

from app.video_shorts import video_shorts_bp
from app.video_shorts.services.brands import current_brand_id, ensure_brand_schema
from app.video_shorts.config import (
    SHORTS_DIR,
    VIDEOS_DIR,
    SHORTS_OVERVIEW_STATS_TTL_MINUTES,
    SHORTS_OVERVIEW_STATS_MAX_VIDEOS,
    SHORTS_OVERVIEW_QUOTA_COOLDOWN_HOURS,
    SHORTS_OVERVIEW_FIRST_FILL_MAX_VIDEOS,
    FB_API_BASE,
)
from app.video_shorts.services.db import (
    get_db,
    get_db_readonly,
    _ensure_video_crop_schema,
    ensure_channel_owner_schema,
    table_columns,
)
from app.video_shorts.services.comment_moderation import moderate_text_entries
from app.video_shorts.services.comment_store import (
    upsert_comment_records,
    fetch_comment_records,
    fetch_comment_records_for_video_ids,
    fetch_latest_comment_timestamps,
    fetch_comments_missing_moderation,
    fetch_comment_records_for_video,
    fetch_comment_owner,
    fetch_instagram_comments_for_queue,
    fetch_instagram_comments_with_statuses,
    update_comment_moderation,
    update_comment_status,
    delete_comment_record,
    owner_user_id_matches,
)
from app.video_shorts.services.instagram_queue import (
    load_instagram_queue_map,
    get_instagram_queue_entry,
    update_instagram_last_seen_comment_count,
    get_instagram_comment_cache,
    load_instagram_comment_cache_map,
)
from app.video_shorts.services.tiktok_queue import load_tiktok_queue_map
from app.video_shorts.services.facebook_queue import (
    load_facebook_queue_map,
    get_facebook_queue_entry,
    update_facebook_last_seen_comment_count,
    update_facebook_queue_metrics,
)
from app.video_shorts.services.instagram_api import (
    InstagramActionError,
    refresh_instagram_media,
    delete_instagram_comment,
    hide_instagram_comment,
    reply_instagram_comment,
    set_instagram_comment_like,
    unhide_instagram_comment,
)
from src.trends.facebook_page_tokens import get_facebook_page_data
from app.video_shorts.youtube_api import (
    YoutubeApiError,
    fetch_playlist_items_batch,
    fetch_video_stats,
    get_channel_metadata,
    fetch_video_comments,
)
from app.video_shorts.services.youtube_oauth import build_authenticated_youtube
from app.video_shorts.services.storage import get_media_storage
from app.video_shorts.services.ai_video_workspace import list_ai_broadcast_entries


def _pretty_duration(seconds):
    try:
        secs = int(seconds)
    except (TypeError, ValueError):
        return None
    mins, secs = divmod(secs, 60)
    hours, mins = divmod(mins, 60)
    if hours:
        return f"{hours}:{mins:02d}:{secs:02d}"
    return f"{mins}:{secs:02d}"


def _short_storage_key(filename: str) -> str:
    safe_name = Path(filename or "").name
    return f"shorts/{safe_name}" if safe_name else ""


def _short_s3_index() -> Optional[set[str]]:
    cached = getattr(g, "_vs_short_s3_index", None)
    if cached is not None:
        return cached
    storage = get_media_storage()
    if getattr(storage, "backend_name", "local") != "s3":
        g._vs_short_s3_index = None
        return None
    try:
        keys = {
            Path(entry.key).name
            for entry in storage.list_prefix("shorts/")
            if getattr(entry, "exists", False)
        }
    except Exception:
        current_app.logger.exception("Failed to build shorts S3 index for overview")
        keys = set()
    g._vs_short_s3_index = keys
    return keys


def _short_public_url(filename: str) -> Optional[str]:
    safe_name = Path(filename or "").name
    if not safe_name:
        return None
    key = _short_storage_key(safe_name)
    storage = get_media_storage()
    if getattr(storage, "backend_name", "local") == "s3":
        try:
            short_keys = _short_s3_index()
            if short_keys is not None and safe_name in short_keys:
                return storage.public_url(key)
        except Exception:
            current_app.logger.exception("Failed to resolve overview short url filename=%s key=%s", safe_name, key)
    clip_path = SHORTS_DIR / safe_name
    if clip_path.exists() and clip_path.is_file():
        return get_media_storage("local").public_url(key)
    return None


SHORT_WINDOW_SECONDS = timedelta(minutes=5).total_seconds()
PENDING_STATUSES = {"heldForReview", "likelySpam"}
COMMENT_COUNT_REFRESH_INTERVAL = timedelta(minutes=20)
COMMENT_COUNT_REFRESH_MAX_VIDEOS = 30
DEFAULT_TIME_ZONE = "America/Los_Angeles"
INSTAGRAM_STATUS_META = {
    "pending": ("Queued", "bg-warning text-dark"),
    "retry": ("Retry", "bg-warning text-dark"),
    "uploading": ("Uploading", "bg-info text-dark"),
    "published": ("Published", "bg-success"),
    "failed": ("Failed", "bg-danger"),
}
TIKTOK_STATUS_META = {
    "pending": ("Queued", "bg-warning text-dark"),
    "retry": ("Retry", "bg-warning text-dark"),
    "uploading": ("Uploading", "bg-info text-dark"),
    "published": ("Published", "bg-success"),
    "failed": ("Failed", "bg-danger"),
}
FACEBOOK_STATUS_META = {
    "pending": ("Queued", "bg-warning text-dark"),
    "retry": ("Retry", "bg-warning text-dark"),
    "uploading": ("Uploading", "bg-info text-dark"),
    "published": ("Published", "bg-success"),
    "failed": ("Failed", "bg-danger"),
}


def _ensure_short_comment_cache_table(conn):
    if getattr(conn, "backend_name", "") == "postgres":
        return
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS short_comment_cache (
                short_video_id VARCHAR PRIMARY KEY,
                pending_comment_count INTEGER DEFAULT 0,
                published_comment_count INTEGER DEFAULT 0,
                rejected_comment_count INTEGER DEFAULT 0,
                last_seen_comment_count INTEGER DEFAULT 0,
                comments_last_synced_at TIMESTAMP
            )
            """
        )
    except Exception as exc:
        if "read-only" in str(exc).lower():
            return
        raise
    try:
        cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info('short_comment_cache')").fetchall()
        }
    except Exception:
        return
    if "last_seen_comment_count" not in cols:
        try:
            conn.execute(
                "ALTER TABLE short_comment_cache ADD COLUMN last_seen_comment_count INTEGER DEFAULT 0"
            )
        except Exception:
            pass


def _ensure_shorts_overview_stats_cache(conn) -> None:
    if getattr(conn, "backend_name", "") == "postgres":
        return
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS shorts_overview_stats_cache (
                video_id VARCHAR PRIMARY KEY,
                view_count BIGINT,
                like_count BIGINT,
                comment_count BIGINT,
                thumbnail_url VARCHAR,
                fetched_at TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS shorts_overview_quota_state (
                id INTEGER PRIMARY KEY,
                exhausted_until TIMESTAMP,
                reason VARCHAR,
                last_error_code INTEGER,
                last_error_reason VARCHAR,
                last_error_message VARCHAR,
                last_error_domain VARCHAR,
                last_error_at TIMESTAMP
            )
            """
        )
        try:
            cols = {
                row[1]
                for row in conn.execute(
                    "PRAGMA table_info('shorts_overview_quota_state')"
                ).fetchall()
            }
            if "reason" not in cols:
                conn.execute("ALTER TABLE shorts_overview_quota_state ADD COLUMN reason VARCHAR")
            if "last_error_code" not in cols:
                conn.execute("ALTER TABLE shorts_overview_quota_state ADD COLUMN last_error_code INTEGER")
            if "last_error_reason" not in cols:
                conn.execute("ALTER TABLE shorts_overview_quota_state ADD COLUMN last_error_reason VARCHAR")
            if "last_error_message" not in cols:
                conn.execute("ALTER TABLE shorts_overview_quota_state ADD COLUMN last_error_message VARCHAR")
            if "last_error_domain" not in cols:
                conn.execute("ALTER TABLE shorts_overview_quota_state ADD COLUMN last_error_domain VARCHAR")
            if "last_error_at" not in cols:
                conn.execute("ALTER TABLE shorts_overview_quota_state ADD COLUMN last_error_at TIMESTAMP")
        except Exception:
            pass
    except Exception as exc:
        if "read-only" in str(exc).lower():
            return
        raise


def _get_overview_quota_state(conn) -> Tuple[Optional[datetime], Optional[str]]:
    try:
        row = conn.execute(
            "SELECT exhausted_until, reason FROM shorts_overview_quota_state WHERE id = 1"
        ).fetchone()
    except Exception:
        return None, None
    if row and row[0]:
        return row[0], row[1]
    return None, None


def _set_overview_quota_exhausted_until(
    conn,
    until_dt: datetime,
    reason: Optional[str] = None,
    last_error_code: Optional[int] = None,
    last_error_reason: Optional[str] = None,
    last_error_message: Optional[str] = None,
    last_error_domain: Optional[str] = None,
    last_error_at: Optional[datetime] = None,
) -> None:
    try:
        conn.execute(
            """
            INSERT INTO shorts_overview_quota_state (
                id,
                exhausted_until,
                reason,
                last_error_code,
                last_error_reason,
                last_error_message,
                last_error_domain,
                last_error_at
            )
            VALUES (1, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (id) DO UPDATE SET
                exhausted_until = EXCLUDED.exhausted_until,
                reason = EXCLUDED.reason,
                last_error_code = EXCLUDED.last_error_code,
                last_error_reason = EXCLUDED.last_error_reason,
                last_error_message = EXCLUDED.last_error_message,
                last_error_domain = EXCLUDED.last_error_domain,
                last_error_at = EXCLUDED.last_error_at
            """,
            [
                until_dt,
                reason,
                last_error_code,
                last_error_reason,
                last_error_message,
                last_error_domain,
                last_error_at,
            ],
        )
        conn.commit()
    except Exception as exc:
        if "read-only" in str(exc).lower():
            return
        raise


def _load_overview_stats_cache(
    conn,
    video_ids: List[str],
    ttl_minutes: int,
) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
    if not video_ids:
        return {}, []
    _ensure_shorts_overview_stats_cache(conn)
    ttl_minutes = max(0, int(ttl_minutes or 0))
    if ttl_minutes == 0:
        return {}, list(video_ids)
    threshold = datetime.utcnow() - timedelta(minutes=ttl_minutes)
    placeholders = ", ".join("?" for _ in video_ids)
    params: List[Any] = list(video_ids)
    where_ttl = ""
    if threshold:
        where_ttl = " AND fetched_at >= ?"
        params.append(threshold)
    rows = conn.execute(
        f"""
        SELECT video_id, view_count, like_count, comment_count, thumbnail_url
        FROM shorts_overview_stats_cache
        WHERE video_id IN ({placeholders}){where_ttl}
        """,
        params,
    ).fetchall()
    cached = {
        row[0]: {
            "view_count": row[1],
            "like_count": row[2],
            "comment_count": row[3],
            "thumbnail_url": row[4],
        }
        for row in rows
    }
    missing = [vid for vid in video_ids if vid not in cached]
    return cached, missing


def _upsert_overview_stats_cache(conn, stats_map: Dict[str, Dict[str, Any]]) -> None:
    if not stats_map:
        return
    _ensure_shorts_overview_stats_cache(conn)
    now = datetime.utcnow()
    rows = []
    for video_id, stats in stats_map.items():
        rows.append(
            (
                video_id,
                stats.get("view_count"),
                stats.get("like_count"),
                stats.get("comment_count"),
                stats.get("thumbnail_url"),
                now,
            )
        )
    conn.executemany(
        """
        INSERT INTO shorts_overview_stats_cache (
            video_id, view_count, like_count, comment_count, thumbnail_url, fetched_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (video_id) DO UPDATE SET
            view_count = EXCLUDED.view_count,
            like_count = EXCLUDED.like_count,
            comment_count = EXCLUDED.comment_count,
            thumbnail_url = EXCLUDED.thumbnail_url,
            fetched_at = EXCLUDED.fetched_at
        """,
        rows,
    )
    conn.commit()


def _select_overview_stats_ids(entries: List[Dict[str, Any]], max_items: int) -> List[str]:
    if not entries or max_items <= 0:
        return []
    sorted_entries = sorted(
        entries,
        key=lambda item: item.get("publish_sort_key") or "",
        reverse=True,
    )
    seen = set()
    picked: List[str] = []
    for entry in sorted_entries:
        short_id = entry.get("short_video_id")
        if not short_id or short_id in seen:
            continue
        picked.append(short_id)
        seen.add(short_id)
        if len(picked) >= max_items:
            break
    return picked


def _select_recent_comment_entries(
    entries: List[Dict[str, Any]],
    max_items: int,
) -> List[Dict[str, Any]]:
    if not entries or max_items <= 0:
        return []
    sorted_entries = sorted(
        entries,
        key=lambda item: item.get("publish_sort_key") or "",
        reverse=True,
    )
    picked: List[Dict[str, Any]] = []
    seen: set = set()
    for entry in sorted_entries:
        short_id = entry.get("short_video_id")
        if not short_id or short_id in seen:
            continue
        picked.append(entry)
        seen.add(short_id)
        if len(picked) >= max_items:
            break
    return picked


def _is_quota_exhausted_error(exc: Exception) -> bool:
    message = str(exc).lower()
    status_code = getattr(exc, "status_code", None)
    return (
        "quota" in message
        or "403" in message
        or "forbidden" in message
        or status_code in {403, 429}
    )


def _quota_reason_from_error(exc: Exception) -> str:
    message = str(exc).lower()
    if "quota" in message or "quotaexceeded" in message:
        return "quotaExceeded"
    if "forbidden" in message or "403" in message:
        return "forbidden"
    return "unknown"


def _extract_youtube_error_details(
    exc: Exception,
) -> Tuple[Optional[int], Optional[str], Optional[str], Optional[str]]:
    payload = getattr(exc, "payload", None)
    if isinstance(payload, dict):
        error = payload.get("error") or {}
        code = error.get("code")
        message = error.get("message")
        errors = error.get("errors") or []
        first = errors[0] if errors else {}
        reason = first.get("reason")
        domain = first.get("domain")
        message = first.get("message") or message
        return code, reason, message, domain
    status_code = getattr(exc, "status_code", None)
    return status_code, None, None, None


def _get_overview_cache_last_fetched(conn) -> Optional[datetime]:
    try:
        row = conn.execute(
            "SELECT MAX(fetched_at) FROM shorts_overview_stats_cache"
        ).fetchone()
        if row and row[0]:
            return row[0]
    except Exception:
        return None
    return None


def _get_overview_cache_count(conn) -> int:
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM shorts_overview_stats_cache"
        ).fetchone()
        if row and row[0] is not None:
            return int(row[0])
    except Exception:
        return 0
    return 0


def _quota_cooldown_until(reason: Optional[str]) -> Optional[datetime]:
    if reason == "quotaExceeded":
        return _next_pt_midnight_plus()
    if SHORTS_OVERVIEW_QUOTA_COOLDOWN_HOURS <= 0:
        return None
    return datetime.utcnow() + timedelta(hours=SHORTS_OVERVIEW_QUOTA_COOLDOWN_HOURS)


def _next_pt_midnight_plus(minutes: int = 5) -> datetime:
    try:
        pt = ZoneInfo("America/Los_Angeles")
    except Exception:
        return datetime.utcnow() + timedelta(hours=SHORTS_OVERVIEW_QUOTA_COOLDOWN_HOURS)
    now_pt = datetime.now(pt)
    next_day = (now_pt + timedelta(days=1)).date()
    midnight_pt = datetime.combine(next_day, dt_time(0, 0), tzinfo=pt)
    target_pt = midnight_pt + timedelta(minutes=minutes)
    return target_pt.astimezone(timezone.utc).replace(tzinfo=None)


def _fetch_short_comment_counts(conn, short_video_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    if not short_video_ids:
        return {}
    _ensure_short_comment_cache_table(conn)
    placeholders = ", ".join("?" for _ in short_video_ids)
    cols = table_columns(conn, "short_comment_cache")
    last_seen_expr = "last_seen_comment_count" if "last_seen_comment_count" in cols else "0"
    try:
        rows = conn.execute(
            f"""
            SELECT
              short_video_id,
              COALESCE(pending_comment_count, 0) AS pending_comment_count,
              COALESCE(published_comment_count, 0) AS published_comment_count,
              COALESCE(rejected_comment_count, 0) AS rejected_comment_count,
              COALESCE({last_seen_expr}, 0) AS last_seen_comment_count,
              comments_last_synced_at
            FROM short_comment_cache
            WHERE short_video_id IN ({placeholders})
            """,
            short_video_ids,
        ).fetchall()
    except Exception as exc:
        if "short_comment_cache" in str(exc).lower():
            return {}
        raise
    result: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        result[row[0]] = {
            "pending_comment_count": row[1] or 0,
            "published_comment_count": row[2] or 0,
            "rejected_comment_count": row[3] or 0,
            "last_seen_comment_count": row[4] or 0,
            "comments_last_synced_at": row[5],
        }
    return result


def _fetch_and_cache_comment_counts(short_video_id: str) -> Dict[str, int]:
    aggregated: List[Dict[str, Any]] = []
    any_success = False
    try:
        pending_comments = fetch_video_comments(
            short_video_id,
            max_results=50,
            moderation_status="heldForReview",
        )
        aggregated.extend(pending_comments)
        any_success = True
    except YoutubeApiError:
        pass
    except Exception:
        current_app.logger.exception("Failed to fetch heldForReview comments for %s", short_video_id)
    try:
        published_comments = fetch_video_comments(
            short_video_id,
            max_results=50,
            moderation_status=None,
        )
        aggregated.extend(published_comments)
        any_success = True
    except YoutubeApiError:
        pass
    except Exception:
        current_app.logger.exception("Failed to fetch published comments for %s", short_video_id)
    if not any_success:
        return {}
    summary = _summarize_comment_counts_for_entries(aggregated)
    _upsert_short_comment_counts(short_video_id, summary)
    return summary


def _parse_comments_synced_at(value):
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    if not value:
        return None
    text_value = str(value).strip()
    if not text_value:
        return None
    text_value = text_value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text_value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _should_refresh_comment_counts(cache_entry: Dict[str, Any]) -> bool:
    if not cache_entry:
        return True
    last_synced_at = _parse_comments_synced_at(cache_entry.get("comments_last_synced_at"))
    if not last_synced_at:
        return True
    return datetime.now(timezone.utc) - last_synced_at >= COMMENT_COUNT_REFRESH_INTERVAL


def _hydrate_missing_short_comment_counts(
    entries: List[Dict[str, Any]],
    cache: Dict[str, Dict[str, Any]],
) -> int:
    processed: set = set()
    refreshed = 0
    for entry in entries:
        short_id = entry.get("short_video_id")
        if not short_id or short_id in processed:
            continue
        cache_entry = cache.get(short_id) or {}
        if cache_entry and not _should_refresh_comment_counts(cache_entry):
            continue
        processed.add(short_id)
        summary = _fetch_and_cache_comment_counts(short_id)
        if not summary:
            continue
        cache[short_id] = {
            "pending_comment_count": summary.get("pending", 0),
            "published_comment_count": summary.get("published", 0),
            "rejected_comment_count": summary.get("rejected", 0),
            "last_seen_comment_count": cache_entry.get("last_seen_comment_count", 0),
            "comments_last_synced_at": datetime.now(timezone.utc),
        }
        refreshed += 1
    return refreshed


def _load_short_comment_cache(short_video_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    if not short_video_ids:
        return {}
    conn_counts = get_db_readonly()
    try:
        cache = _fetch_short_comment_counts(conn_counts, short_video_ids)
    finally:
        conn_counts.close()
    return cache


def _normalize_status_for_bucket(status: str) -> str:
    if status in PENDING_STATUSES:
        return "pending"
    if status == "published":
        return "published"
    if status == "rejected":
        return "rejected"
    return ""


def _summarize_comment_counts_for_entries(comments: List[Dict[str, Any]]) -> Dict[str, int]:
    summary = {"pending": 0, "published": 0, "rejected": 0}
    for comment in comments:
        if comment.get("is_reply"):
            continue
        bucket = _normalize_status_for_bucket(comment.get("status") or "")
        if bucket:
            summary[bucket] += 1
    return summary


def _upsert_short_comment_counts(short_video_id: str, summary: Dict[str, int]):
    if not short_video_id:
        return
    conn = get_db()
    _ensure_video_crop_schema(conn)
    try:
        _ensure_short_comment_cache_table(conn)
        now = datetime.now(timezone.utc)
        conn.execute(
            """
            INSERT INTO short_comment_cache (
                short_video_id,
                pending_comment_count,
                published_comment_count,
                rejected_comment_count,
                comments_last_synced_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (short_video_id)
            DO UPDATE SET
                pending_comment_count = excluded.pending_comment_count,
                published_comment_count = excluded.published_comment_count,
                rejected_comment_count = excluded.rejected_comment_count,
                comments_last_synced_at = excluded.comments_last_synced_at
            """,
            [
                short_video_id,
                summary.get("pending", 0),
                summary.get("published", 0),
                summary.get("rejected", 0),
                now,
            ],
        )
        conn.commit()
    finally:
        conn.close()


def _update_short_comment_last_seen_count(short_video_id: str, last_seen_count: int) -> None:
    if not short_video_id:
        return
    try:
        last_seen_value = max(0, int(last_seen_count))
    except (TypeError, ValueError):
        return
    conn = get_db()
    try:
        _ensure_short_comment_cache_table(conn)
        conn.execute(
            """
            INSERT INTO short_comment_cache (
                short_video_id,
                pending_comment_count,
                published_comment_count,
                rejected_comment_count,
                last_seen_comment_count,
                comments_last_synced_at
            ) VALUES (?, 0, 0, 0, ?, NULL)
            ON CONFLICT (short_video_id)
            DO UPDATE SET
                last_seen_comment_count = excluded.last_seen_comment_count
            """,
            [short_video_id, last_seen_value],
        )
        conn.commit()
    finally:
        conn.close()


def _adjust_short_comment_counts(short_video_id: str, delta_summary: Dict[str, int]) -> Dict[str, int]:
    if not short_video_id:
        return {}
    conn = get_db()
    try:
        _ensure_short_comment_cache_table(conn)
        row = conn.execute(
            """
            SELECT
              COALESCE(pending_comment_count, 0),
              COALESCE(published_comment_count, 0),
              COALESCE(rejected_comment_count, 0)
            FROM short_comment_cache
            WHERE short_video_id = ?
            """,
            [short_video_id],
        ).fetchone()
        pending, published, rejected = row if row else (0, 0, 0)
        pending = max(0, pending + delta_summary.get("pending", 0))
        published = max(0, published + delta_summary.get("published", 0))
        rejected = max(0, rejected + delta_summary.get("rejected", 0))
        conn.execute(
            """
            INSERT INTO short_comment_cache (
                short_video_id,
                pending_comment_count,
                published_comment_count,
                rejected_comment_count,
                comments_last_synced_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (short_video_id)
            DO UPDATE SET
                pending_comment_count = excluded.pending_comment_count,
                published_comment_count = excluded.published_comment_count,
                rejected_comment_count = excluded.rejected_comment_count,
                comments_last_synced_at = excluded.comments_last_synced_at
            """,
            [short_video_id, pending, published, rejected, datetime.now(timezone.utc)],
        )
        conn.commit()
        return {"pending": pending, "published": published, "rejected": rejected}
    finally:
        conn.close()


def _status_transition_delta(previous: str, new_status: str) -> Dict[str, int]:
    delta = {"pending": 0, "published": 0, "rejected": 0}
    prev_bucket = _normalize_status_for_bucket(previous)
    new_bucket = _normalize_status_for_bucket(new_status)
    if prev_bucket and prev_bucket != new_bucket:
        delta[prev_bucket] -= 1
    if new_bucket and new_bucket != prev_bucket:
        delta[new_bucket] += 1
    return delta


def _removal_delta(previous: str) -> Dict[str, int]:
    bucket = _normalize_status_for_bucket(previous)
    delta = {"pending": 0, "published": 0, "rejected": 0}
    if bucket:
        delta[bucket] = -1
    return delta


def _require_youtube_client():
    current_user = getattr(g, "vs_current_user", None) or {}
    youtube = build_authenticated_youtube(user_id=current_user.get("id"))
    if not youtube:
        raise YoutubeApiError("YouTube OAuth bağlantısı bulunamadı.")
    return youtube


def _normalize_timestamp(value):
    if not value:
        return None
    dt = value
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            if isinstance(value, str) and value.endswith("Z"):
                value = value[:-1] + "+00:00"
            dt = datetime.fromisoformat(value)
        except (ValueError, TypeError):
            return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _instagram_status_meta(status: Optional[str]) -> Tuple[str, str]:
    key = (status or "pending").lower()
    label, badge = INSTAGRAM_STATUS_META.get(key, ("Queued", "bg-secondary"))
    return label, badge


def _tiktok_status_meta(status: Optional[str]) -> Tuple[str, str]:
    key = (status or "pending").lower()
    label, badge = TIKTOK_STATUS_META.get(key, ("Queued", "bg-secondary"))
    return label, badge


def _facebook_status_meta(status: Optional[str]) -> Tuple[str, str]:
    key = (status or "pending").lower()
    label, badge = FACEBOOK_STATUS_META.get(key, ("Queued", "bg-secondary"))
    return label, badge


def _format_display_timestamp(value: Optional[str], tz_name: str) -> Optional[str]:
    dt = _normalize_timestamp(value)
    if not dt:
        return None
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo(DEFAULT_TIME_ZONE)
    return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M %Z")


def _format_instagram_publish_display(value: Optional[str], tz_name: str) -> Optional[str]:
    return _format_display_timestamp(value, tz_name)


def _instagram_type_label(media_type: Optional[str]) -> str:
    mt = (media_type or "reel").lower()
    if mt == "feed":
        return "Feed"
    return "Reel"


def _has_flagged_instagram_comments(comments: List[Dict[str, Any]]) -> bool:
    for comment in comments or []:
        moderation = comment.get("moderation")
        if isinstance(moderation, dict) and moderation.get("flagged"):
            return True
        replies = comment.get("replies") or {}
        if isinstance(replies, dict):
            reply_items = replies.get("data") or []
        elif isinstance(replies, list):
            reply_items = replies
        else:
            reply_items = []
        for reply in reply_items:
            moderation = reply.get("moderation")
            if isinstance(moderation, dict) and moderation.get("flagged"):
                return True
    return False




def _build_instagram_media_payload(entry: Dict[str, Any]) -> Dict[str, Any]:
    cache = get_instagram_comment_cache(entry.get("instagram_media_id"))
    payload = {
        "queue_id": entry.get("id"),
        "instagram_media_id": entry.get("instagram_media_id"),
        "plan_title": entry.get("plan_title"),
        "media_type": entry.get("media_type"),
        "permalink": entry.get("permalink"),
        "like_count": entry.get("like_count"),
        "comment_count": entry.get("comment_count"),
        "last_seen_comment_count": entry.get("last_seen_comment_count"),
        "impressions": entry.get("impressions"),
        "reach": entry.get("reach"),
        "saved": entry.get("saved"),
        "shares": entry.get("shares"),
        "publish_at": entry.get("publish_at"),
        "published_at": entry.get("published_at"),
        "status": entry.get("status"),
        "status_detail": entry.get("status_detail"),
        "instagram_username": entry.get("instagram_username"),
    }
    comments = (cache.get("comments") or []) if cache else []
    authoritative_comments = fetch_instagram_comments_for_queue(str(entry.get("id") or ""), limit=250)

    def normalize_comment_authors(items: List[Dict[str, Any]]) -> None:
        for item in items:
            if not isinstance(item, dict):
                continue
            if not item.get("author") and item.get("username"):
                item["author"] = item.get("username")
            replies = (item.get("replies") or {}).get("data") if isinstance(item.get("replies"), dict) else []
            normalize_comment_authors(replies)

    normalize_comment_authors(comments)

    if authoritative_comments:
        authoritative_by_id = {
            str(item.get("id") or item.get("comment_id") or ""): item
            for item in authoritative_comments
            if str(item.get("id") or item.get("comment_id") or "")
        }
        existing_ids: set[str] = set()
        top_level_by_id: Dict[str, Dict[str, Any]] = {}

        def apply_authoritative(items: List[Dict[str, Any]]) -> None:
            for item in items:
                if not isinstance(item, dict):
                    continue
                comment_id = str(item.get("id") or item.get("comment_id") or "")
                if comment_id:
                    existing_ids.add(comment_id)
                    top_level_by_id.setdefault(comment_id, item)
                    override = authoritative_by_id.get(comment_id)
                    if override:
                        if override.get("author"):
                            item["author"] = override.get("author")
                            item["username"] = override.get("author")
                        if override.get("text") and not item.get("text"):
                            item["text"] = override.get("text")
                        if override.get("timestamp") and not item.get("timestamp"):
                            item["timestamp"] = override.get("timestamp")
                        if override.get("like_count") is not None:
                            item["like_count"] = override.get("like_count")
                        if override.get("status"):
                            item["status"] = override.get("status")
                        if override.get("moderation_flagged") is not None:
                            item["moderation_flagged"] = override.get("moderation_flagged")
                        if override.get("moderation_reason"):
                            item["moderation_reason"] = override.get("moderation_reason")
                        if override.get("moderation_checked_at"):
                            item["moderation_checked_at"] = override.get("moderation_checked_at")
                        if override.get("is_hidden"):
                            item["is_hidden"] = True
                        if override.get("is_deleted"):
                            item["is_deleted"] = True
                replies = item.get("replies")
                if not isinstance(replies, dict):
                    replies = {"data": []}
                    item["replies"] = replies
                reply_items = replies.get("data")
                if not isinstance(reply_items, list):
                    reply_items = []
                    replies["data"] = reply_items
                for reply in reply_items:
                    if isinstance(reply, dict):
                        reply_id = str(reply.get("id") or reply.get("comment_id") or "")
                        if reply_id:
                            existing_ids.add(reply_id)
                apply_authoritative(reply_items)

        apply_authoritative(comments)

        for authoritative in authoritative_comments:
            comment_id = str(authoritative.get("id") or authoritative.get("comment_id") or "")
            if not comment_id or comment_id in existing_ids:
                continue
            normalized = {
                "id": comment_id,
                "comment_id": comment_id,
                "author": authoritative.get("author") or authoritative.get("username"),
                "username": authoritative.get("author") or authoritative.get("username"),
                "text": authoritative.get("text"),
                "timestamp": authoritative.get("timestamp"),
                "like_count": authoritative.get("like_count"),
                "status": authoritative.get("status"),
                "moderation_flagged": authoritative.get("moderation_flagged"),
                "moderation_reason": authoritative.get("moderation_reason"),
                "moderation_checked_at": authoritative.get("moderation_checked_at"),
            }
            parent_id = str(authoritative.get("parent_id") or "")
            if parent_id:
                parent = top_level_by_id.get(parent_id)
                if parent is not None:
                    replies = parent.get("replies")
                    if not isinstance(replies, dict):
                        replies = {"data": []}
                        parent["replies"] = replies
                    reply_items = replies.get("data")
                    if not isinstance(reply_items, list):
                        reply_items = []
                        replies["data"] = reply_items
                    reply_items.append(normalized)
                    existing_ids.add(comment_id)
                continue
            normalized["replies"] = {"data": []}
            comments.append(normalized)
            top_level_by_id[comment_id] = normalized
            existing_ids.add(comment_id)

    def normalize_instagram_comment_tree(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        top_level_items: List[Dict[str, Any]] = []
        top_level_by_id: Dict[str, Dict[str, Any]] = {}
        pending_replies: Dict[str, List[Dict[str, Any]]] = {}

        def ensure_replies_bucket(item: Dict[str, Any]) -> List[Dict[str, Any]]:
            replies = item.get("replies")
            if not isinstance(replies, dict):
                replies = {"data": []}
                item["replies"] = replies
            reply_items = replies.get("data")
            if not isinstance(reply_items, list):
                reply_items = []
                replies["data"] = reply_items
            return reply_items

        for item in items:
            if not isinstance(item, dict):
                continue
            comment_id = str(item.get("id") or item.get("comment_id") or "")
            authoritative_parent_id = ""
            authoritative_item = authoritative_by_id.get(comment_id) if authoritative_comments else None
            if authoritative_item:
                authoritative_parent_id = str(authoritative_item.get("parent_id") or "")
            parent_id = authoritative_parent_id or str(item.get("parent_id") or "")
            if parent_id:
                item["parent_id"] = parent_id
                pending_replies.setdefault(parent_id, []).append(item)
                continue
            top_level_items.append(item)
            if comment_id:
                top_level_by_id[comment_id] = item

        for parent_id, replies in pending_replies.items():
            parent = top_level_by_id.get(parent_id)
            if parent is None:
                continue
            reply_bucket = ensure_replies_bucket(parent)
            existing_reply_ids = {
                str(reply.get("id") or reply.get("comment_id") or "")
                for reply in reply_bucket
                if isinstance(reply, dict)
            }
            for reply in replies:
                reply_id = str(reply.get("id") or reply.get("comment_id") or "")
                if reply_id and reply_id in existing_reply_ids:
                    continue
                reply_bucket.append(reply)
                if reply_id:
                    existing_reply_ids.add(reply_id)
        return top_level_items

    comments = normalize_instagram_comment_tree(comments)

    status_overrides = fetch_instagram_comments_with_statuses(
        str(entry.get("id") or ""),
        ["hidden", "deleted"],
    )
    if status_overrides:
        override_by_id = {
            str(item.get("id") or item.get("comment_id") or ""): item
            for item in status_overrides
            if str(item.get("id") or item.get("comment_id") or "")
        }

        def apply_overrides(items: List[Dict[str, Any]]) -> None:
            for item in items:
                if not isinstance(item, dict):
                    continue
                comment_id = str(item.get("id") or item.get("comment_id") or "")
                override = override_by_id.get(comment_id)
                if override:
                    if not override.get("author") and override.get("username"):
                        override["author"] = override.get("username")
                    item["status"] = override.get("status")
                    item["moderation_flagged"] = override.get("moderation_flagged")
                    item["moderation_reason"] = override.get("moderation_reason")
                    item["moderation_checked_at"] = override.get("moderation_checked_at")
                    if override.get("is_hidden"):
                        item["is_hidden"] = True
                    if override.get("is_deleted"):
                        item["is_deleted"] = True
                replies = (item.get("replies") or {}).get("data") if isinstance(item.get("replies"), dict) else []
                apply_overrides(replies)

        def collect_existing_ids(items: List[Dict[str, Any]], bucket: set[str]) -> None:
            for item in items:
                if not isinstance(item, dict):
                    continue
                comment_id = str(item.get("id") or item.get("comment_id") or "")
                if comment_id:
                    bucket.add(comment_id)
                replies = (item.get("replies") or {}).get("data") if isinstance(item.get("replies"), dict) else []
                collect_existing_ids(replies, bucket)

        apply_overrides(comments)

        existing_ids: set[str] = set()
        collect_existing_ids(comments, existing_ids)
        for override in status_overrides:
            if not override.get("author") and override.get("username"):
                override["author"] = override.get("username")
            override_id = str(override.get("id") or override.get("comment_id") or "")
            if override_id and override_id not in existing_ids:
                comments.append(override)
    payload["comments"] = comments
    payload["deleted_comment_count"] = sum(1 for item in status_overrides if item.get("status") == "deleted")

    if cache:
        payload["comments_last_synced"] = cache.get("last_synced")
        payload["cached_like_count"] = cache.get("like_count")
        payload["cached_comment_count"] = cache.get("comment_count")
    else:
        payload["comments"] = []
        payload["comments_last_synced"] = None
    return payload


def _effective_non_admin_owner_user_id(explicit_owner_user_id: Optional[str] = None) -> Optional[str]:
    if explicit_owner_user_id is not None:
        owner_text = str(explicit_owner_user_id).strip()
        return owner_text or None
    if not has_request_context():
        return None
    current_user = getattr(g, "vs_current_user", None) or {}
    if current_user.get("role") == "admin":
        return None
    owner_text = str(current_user.get("id") or "").strip()
    return owner_text or None


def _brand_allowed_source_video_ids(
    brand_id: Optional[str],
    *,
    owner_user_id: Optional[str] = None,
) -> Optional[set[str]]:
    effective_owner_user_id = _effective_non_admin_owner_user_id(owner_user_id)
    if not brand_id and not effective_owner_user_id:
        return None
    conn = get_db_readonly()
    try:
        where: List[str] = []
        params: List[Any] = []
        if brand_id:
            where.append("brand_id = ?")
            params.append(brand_id)
        if effective_owner_user_id:
            where.append("owner_user_id = ?")
            params.append(effective_owner_user_id)
        sql = f"SELECT video_id FROM youtube_videos WHERE {' AND '.join(where)}"
        rows = conn.execute(sql, params).fetchall()
        return {str(row[0]) for row in rows if row and row[0]}
    finally:
        conn.close()


def _normalize_scope_label(value: Optional[str]) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or "").strip().lower())
    ascii_only = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    return "".join(ch for ch in ascii_only if ch.isalnum())


def _load_brand_scope_context(brand_id: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    if not brand_id:
        return None, None
    conn = get_db_readonly()
    try:
        row = conn.execute(
            """
            SELECT owner_user_id, name
            FROM shorts_brands
            WHERE id = ?
            LIMIT 1
            """,
            [brand_id],
        ).fetchone()
        if not row:
            return None, None
        return (str(row[0]).strip() if row[0] else None), (str(row[1]).strip() if row[1] else None)
    finally:
        conn.close()


def _preferred_brand_channel_ids(owner_user_id: Optional[str], brand_id: Optional[str]) -> Optional[set[str]]:
    owner_text = str(owner_user_id or "").strip()
    if not owner_text or not brand_id:
        return None
    brand_owner_user_id, brand_name = _load_brand_scope_context(brand_id)
    if not brand_owner_user_id or brand_owner_user_id != owner_text:
        return None
    conn = get_db_readonly()
    try:
        rows = conn.execute(
            """
            SELECT channel_id, channel_name
            FROM youtube_channels
            WHERE owner_user_id = ?
              AND brand_id = ?
              AND COALESCE(is_active, true) = true
            ORDER BY channel_id
            """,
            [owner_text, brand_id],
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return set()
    if len(rows) == 1:
        return {str(rows[0][0])}
    brand_key = _normalize_scope_label(brand_name)
    if not brand_key:
        return {str(row[0]) for row in rows}
    scored: List[Tuple[int, str]] = []
    for channel_id, channel_name in rows:
        channel_key = _normalize_scope_label(channel_name)
        score = 0
        if channel_key == brand_key:
            score = 100
        elif brand_key and channel_key and (brand_key in channel_key or channel_key in brand_key):
            score = 80
        scored.append((score, str(channel_id)))
    best_score = max((score for score, _channel_id in scored), default=0)
    if best_score <= 0:
        return {channel_id for _score, channel_id in scored}
    return {channel_id for score, channel_id in scored if score == best_score}


def _filter_entries_to_channel_scope(
    entries: List[Dict[str, Any]],
    *,
    owner_user_id: Optional[str],
    brand_id: Optional[str],
    preferred_channel_ids: Optional[set[str]],
) -> List[Dict[str, Any]]:
    if not entries or not preferred_channel_ids:
        return entries
    source_video_ids = sorted({str(entry.get("video_id") or "").strip() for entry in entries if str(entry.get("video_id") or "").strip()})
    if not source_video_ids:
        return []
    conn = get_db_readonly()
    try:
        placeholders = ", ".join("?" for _ in source_video_ids)
        sql = f"SELECT video_id, channel_id FROM youtube_videos WHERE video_id IN ({placeholders})"
        params: List[Any] = list(source_video_ids)
        owner_text = str(owner_user_id or "").strip()
        if owner_text:
            sql += " AND owner_user_id = ?"
            params.append(owner_text)
        if brand_id:
            sql += " AND brand_id = ?"
            params.append(brand_id)
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    channel_by_video = {str(video_id): str(channel_id) for video_id, channel_id in rows if video_id is not None and channel_id is not None}
    return [
        entry
        for entry in entries
        if channel_by_video.get(str(entry.get("video_id") or "").strip()) in preferred_channel_ids
    ]


def _load_video_scope(video_id: str) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    if not video_id:
        return None, None, None, None
    conn = get_db_readonly()
    try:
        ensure_channel_owner_schema(conn)
        row = conn.execute(
            """
            SELECT v.title, v.owner_user_id, c.owner_user_id, v.brand_id, v.channel_id
            FROM youtube_videos v
            LEFT JOIN youtube_channels c ON c.channel_id = v.channel_id
            WHERE v.video_id = ?
            """,
            [video_id],
        ).fetchone()
        if not row:
            return None, None, None, None
        title, video_owner_id, channel_owner_id, brand_id, channel_id = row
        return title, video_owner_id or channel_owner_id, brand_id, (str(channel_id) if channel_id is not None else None)
    finally:
        conn.close()


def _resolve_owner_for_short_id(short_video_id: str) -> Optional[str]:
    if not short_video_id:
        return None
    entries = _collect_short_broadcast_entries(brand_id=current_brand_id())
    source_video_id = None
    for entry in entries:
        if entry.get("short_video_id") == short_video_id:
            source_video_id = entry.get("video_id")
            break
    if not source_video_id:
        return None
    conn = get_db_readonly()
    try:
        ensure_channel_owner_schema(conn)
        row = conn.execute(
            """
            SELECT v.owner_user_id, c.owner_user_id
            FROM youtube_videos v
            LEFT JOIN youtube_channels c ON c.channel_id = v.channel_id
            WHERE v.video_id = ?
            """,
            [source_video_id],
        ).fetchone()
        if not row:
            return None
        return row[0] or row[1]
    finally:
        conn.close()


def _comment_status_meta(status: Optional[str]) -> Tuple[str, str]:
    key = (status or "").lower()
    if key in {"heldforreview", "pending", "likelyspam"}:
        return "Pending", "bg-warning text-dark"
    if key == "published":
        return "Published", "bg-success"
    if key == "hidden":
        return "Hidden", "bg-warning text-dark"
    if key == "rejected":
        return "Rejected", "bg-danger"
    if key == "deleted":
        return "Deleted", "bg-secondary"
    return "Unknown", "bg-secondary"


def _parse_multi_filter_values(param_name: str) -> List[str]:
    values: List[str] = []
    for raw in request.args.getlist(param_name):
        for item in str(raw or "").split(","):
            normalized = item.strip().lower()
            if not normalized or normalized == "all" or normalized in values:
                continue
            values.append(normalized)
    return values


def _build_short_title_map() -> Dict[str, str]:
    entries = _collect_short_broadcast_entries(brand_id=current_brand_id())
    title_map: Dict[str, str] = {}
    for entry in entries:
        short_id = entry.get("short_video_id")
        if not short_id:
            continue
        title = (
            entry.get("plan_title")
            or entry.get("video_title")
            or entry.get("video_id")
            or short_id
        )
        title_map[short_id] = str(title)
    return title_map


def _build_allowed_comment_video_ids() -> set[str]:
    current_user = getattr(g, "vs_current_user", None) or {}
    brand_id = current_brand_id()
    owner_user_id = current_user.get("id")
    entries = _collect_short_broadcast_entries(
        brand_id=brand_id,
        owner_user_id=owner_user_id,
    )
    preferred_channel_ids = _preferred_brand_channel_ids(owner_user_id, brand_id)
    entries = _filter_entries_to_channel_scope(
        entries,
        owner_user_id=owner_user_id,
        brand_id=brand_id,
        preferred_channel_ids=preferred_channel_ids,
    )
    allowed: set[str] = set()
    for entry in entries:
        short_id = str(entry.get("short_video_id") or "").strip()
        source_video_id = str(entry.get("video_id") or "").strip()
        if short_id:
            allowed.add(short_id)
        if source_video_id:
            allowed.add(source_video_id)
    return allowed


def _apply_short_title_fallback(comments: List[Dict[str, Any]], title_map: Dict[str, str]) -> None:
    for comment in comments:
        video_id = comment.get("video_id")
        if not video_id:
            continue
        current_title = (comment.get("video_title") or "").strip()
        if not current_title or current_title == video_id:
            mapped = title_map.get(video_id)
            if mapped:
                comment["video_title"] = mapped


def _ensure_comment_owner_access(platform: str, comment_id: str):
    current_user = getattr(g, "vs_current_user", None)
    if not current_user:
        return None, (jsonify(success=False, message="Unauthorized"), 401)
    if current_user.get("role") == "admin":
        return current_user.get("id"), None
    owner_user_id = fetch_comment_owner(platform, comment_id)
    if not owner_user_id_matches(current_user.get("id"), owner_user_id):
        return None, (jsonify(success=False, message="Forbidden"), 403)
    return owner_user_id, None


def _tail_log_lines(path: Path, limit: int = 200) -> List[str]:
    if limit <= 0:
        return []
    if not path.exists():
        return []
    lines: deque[str] = deque(maxlen=limit)
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            lines.append(line.rstrip("\n"))
    return list(lines)


def _parse_social_error_lines(lines: List[str]) -> List[Dict[str, Any]]:
    pattern = re.compile(r"^\[(?P<ts>[^]]+)\]\s+(?P<step>.+?) failed:\s+(?P<err>.+)$")
    step_map = {
        "Instagram publish run": "instagram",
        "TikTok publish run": "tiktok",
        "Facebook publish run": "facebook",
        "YouTube comment sync": "youtube_comments",
        "Instagram metrics sync": "instagram_comments",
    }
    rows: List[Dict[str, Any]] = []
    for line in lines:
        match = pattern.match(line)
        if not match:
            continue
        ts = match.group("ts")
        step = match.group("step").strip()
        err = match.group("err").strip()
        row = {
            "timestamp": ts,
            "instagram": None,
            "tiktok": None,
            "facebook": None,
            "youtube_comments": None,
            "instagram_comments": None,
        }
        key = step_map.get(step)
        if key:
            row[key] = err
        rows.append(row)
    return rows


def _extract_last_social_run(log_path: Path) -> Dict[str, Optional[str]]:
    if not log_path.exists():
        return {"started_at": None, "finished_at": None}
    started_at = None
    finished_at = None
    with log_path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if "== Social jobs start ==" in line:
                started_at = line.split("]")[0].lstrip("[") if line.startswith("[") else line
            elif "== Social jobs done ==" in line:
                finished_at = line.split("]")[0].lstrip("[") if line.startswith("[") else line
    return {"started_at": started_at, "finished_at": finished_at}


def _merge_youtube_comments(all_comments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    priority = {"heldForReview": 0, "likelySpam": 1, "published": 2, "unknown": 3}
    merged: List[Dict[str, Any]] = []
    index_map: Dict[tuple, int] = {}
    for comment in all_comments:
        key = (
            comment.get("comment_id"),
            comment.get("parent_id"),
            comment.get("is_reply"),
        )
        if not key[0]:
            merged.append(comment)
            continue
        if key not in index_map:
            merged.append(comment)
            index_map[key] = len(merged) - 1
        else:
            idx = index_map[key]
            existing = merged[idx]
            existing_status = existing.get("status") or "unknown"
            new_status = comment.get("status") or "unknown"
            if priority.get(new_status, 99) < priority.get(existing_status, 99):
                merged[idx] = comment
    return merged


def _comment_sort_timestamp(comment: Dict[str, Any]) -> tuple[int, str]:
    published_at = str(comment.get("published_at") or "")
    updated_at = str(comment.get("updated_at") or "")
    return (
        0 if published_at else 1,
        published_at or updated_at,
    )


def _thread_youtube_comment_rows(comments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not comments:
        return []
    top_level_comments: List[Dict[str, Any]] = []
    replies_by_parent: Dict[str, List[Dict[str, Any]]] = {}
    orphan_replies: List[Dict[str, Any]] = []

    for comment in comments:
        parent_id = str(comment.get("parent_id") or "").strip()
        is_reply = bool(comment.get("is_reply")) or bool(parent_id)
        comment["is_reply"] = is_reply
        if is_reply and parent_id:
            replies_by_parent.setdefault(parent_id, []).append(comment)
        elif is_reply:
            orphan_replies.append(comment)
        else:
            top_level_comments.append(comment)

    for reply_list in replies_by_parent.values():
        reply_list.sort(key=_comment_sort_timestamp)

    threaded_comments: List[Dict[str, Any]] = []
    seen_parent_ids = set()
    for parent in top_level_comments:
        threaded_comments.append(parent)
        parent_id = str(parent.get("comment_id") or "").strip()
        if not parent_id:
            continue
        seen_parent_ids.add(parent_id)
        threaded_comments.extend(replies_by_parent.get(parent_id, []))

    remaining_replies = list(orphan_replies)
    for parent_id, reply_list in replies_by_parent.items():
        if parent_id in seen_parent_ids:
            continue
        remaining_replies.extend(reply_list)
    remaining_replies.sort(key=_comment_sort_timestamp)
    threaded_comments.extend(remaining_replies)
    return threaded_comments


def _sync_youtube_comments_for_user(
    current_user: Dict[str, Any],
    *,
    max_videos: int = 12,
    latest_by_video: Optional[Dict[str, Optional[str]]] = None,
) -> int:
    entries = _collect_short_broadcast_entries()
    if not entries:
        return 0
    is_admin = current_user.get("role") == "admin"
    owned_video_ids: Optional[set] = None
    if not is_admin:
        conn = get_db_readonly()
        try:
            ensure_channel_owner_schema(conn)
            rows = conn.execute(
                "SELECT video_id FROM youtube_videos WHERE owner_user_id = ?",
                [current_user["id"]],
            ).fetchall()
            owned_video_ids = {row[0] for row in rows}
        finally:
            conn.close()
    ranked: List[Tuple[Optional[datetime], Dict[str, Any]]] = []
    for entry in entries:
        short_id = entry.get("short_video_id")
        if not short_id:
            continue
        if owned_video_ids is not None and entry.get("video_id") not in owned_video_ids:
            continue
        publish_dt = _normalize_timestamp(entry.get("publish_sort_key"))
        ranked.append((publish_dt, entry))
    ranked.sort(
        key=lambda item: item[0] or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    selected: List[Dict[str, Any]] = []
    seen = set()
    for _, entry in ranked:
        short_id = entry.get("short_video_id")
        if not short_id or short_id in seen:
            continue
        seen.add(short_id)
        selected.append(entry)
        if len(selected) >= max_videos:
            break
    if not selected:
        return 0
    record_count = 0
    for entry in selected:
        short_id = entry.get("short_video_id")
        if not short_id:
            continue
        latest_published_at = None
        if latest_by_video:
            latest_published_at = latest_by_video.get(short_id)
        comments: List[Dict[str, Any]] = []
        try:
            comments.extend(
                fetch_video_comments(
                    short_id,
                    max_results=50,
                    moderation_status="heldForReview",
                    user_id=current_user.get("id"),
                )
            )
        except YoutubeApiError:
            pass
        try:
            comments.extend(
                fetch_video_comments(
                    short_id,
                    max_results=50,
                    moderation_status=None,
                    user_id=current_user.get("id"),
                )
            )
        except YoutubeApiError:
            continue
        if not comments:
            continue
        comments = _merge_youtube_comments(comments)
        if latest_published_at:
            comments = [
                comment
                for comment in comments
                if (comment.get("status") in PENDING_STATUSES)
                or not comment.get("published_at")
                or comment.get("published_at") > latest_published_at
            ]
        if not comments:
            continue
        moderation_entries = [
            {"id": str(comment.get("comment_id")), "text": comment.get("text") or ""}
            for comment in comments
            if comment.get("comment_id") and comment.get("text")
        ]
        moderation_map = (
            moderate_text_entries(moderation_entries, current_user.get("id"))
            if moderation_entries
            else {}
        )
        now = datetime.now(timezone.utc)
        records = []
        for comment in comments:
            comment_id = comment.get("comment_id")
            if not comment_id:
                continue
            moderation = moderation_map.get(str(comment_id)) or {}
            records.append(
                {
                    "platform": "youtube",
                    "comment_id": str(comment_id),
                    "parent_id": comment.get("parent_id"),
                    "thread_id": comment.get("thread_id"),
                    "video_id": short_id,
                    "instagram_media_id": None,
                    "queue_id": None,
                    "owner_user_id": current_user["id"],
                    "video_title": entry.get("plan_title")
                    or entry.get("video_id")
                    or short_id,
                    "author": comment.get("author"),
                    "text": comment.get("text"),
                    "status": comment.get("status"),
                    "comment_url": comment.get("comment_url"),
                    "published_at": comment.get("published_at"),
                    "like_count": comment.get("like_count"),
                    "moderation_flagged": moderation.get("flagged")
                    if moderation
                    else None,
                    "moderation_reason": moderation.get("reason")
                    if moderation
                    else None,
                    "moderation_checked_at": now if moderation else None,
                }
            )
        if records:
            upsert_comment_records(records)
            record_count += len(records)
    return record_count


def _sync_youtube_comments_for_video(
    owner_user_id: str,
    short_video_id: str,
    *,
    video_title: Optional[str] = None,
) -> int:
    if not owner_user_id or not short_video_id:
        return 0
    comments: List[Dict[str, Any]] = []
    any_success = False
    try:
        comments.extend(
            fetch_video_comments(
                short_video_id,
                max_results=50,
                moderation_status="heldForReview",
                user_id=owner_user_id,
            )
        )
        any_success = True
    except YoutubeApiError:
        pass
    try:
        comments.extend(
            fetch_video_comments(
                short_video_id,
                max_results=50,
                moderation_status=None,
                user_id=owner_user_id,
            )
        )
        any_success = True
    except YoutubeApiError:
        pass
    if not any_success or not comments:
        return 0
    comments = _merge_youtube_comments(comments)
    moderation_entries = [
        {"id": str(comment.get("comment_id")), "text": comment.get("text") or ""}
        for comment in comments
        if comment.get("comment_id") and comment.get("text")
    ]
    moderation_map = (
        moderate_text_entries(moderation_entries, owner_user_id) if moderation_entries else {}
    )
    now = datetime.now(timezone.utc)
    title_value = (video_title or "").strip() or short_video_id
    records = []
    for comment in comments:
        comment_id = comment.get("comment_id")
        if not comment_id:
            continue
        moderation = moderation_map.get(str(comment_id)) or {}
        records.append(
            {
                "platform": "youtube",
                "comment_id": str(comment_id),
                "parent_id": comment.get("parent_id"),
                "thread_id": comment.get("thread_id"),
                "video_id": short_video_id,
                "instagram_media_id": None,
                "queue_id": None,
                "owner_user_id": owner_user_id,
                "video_title": title_value,
                "author": comment.get("author"),
                "text": comment.get("text"),
                "status": comment.get("status"),
                "comment_url": comment.get("comment_url"),
                "published_at": comment.get("published_at"),
                "like_count": comment.get("like_count"),
                "moderation_flagged": moderation.get("flagged") if moderation else None,
                "moderation_reason": moderation.get("reason") if moderation else None,
                "moderation_checked_at": now if moderation else None,
            }
        )
    if records:
        upsert_comment_records(records)
    return len(records)


def _require_instagram_media_entry(queue_id: str):
    entry = get_instagram_queue_entry(queue_id)
    if not entry:
        return None, (jsonify(success=False, message="Instagram kuyruğu kaydı bulunamadı."), 404)
    if not entry.get("instagram_media_id"):
        return None, (
            jsonify(success=False, message="Instagram gönderisi henüz yayınlanmadı veya yayın kimliği kaydedilmedi."),
            400,
        )
    return entry, None


def _parse_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _normalize_instagram_limit(value, default=25):
    if value is None:
        return default
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return default
    if limit < 0:
        return 0
    return min(limit, 100)


def _facebook_api_request(
    method: str,
    path: str,
    token: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    base = (FB_API_BASE or "https://graph.facebook.com/v24.0").rstrip("/")
    url = f"{base}/{path.lstrip('/')}"
    request_params = dict(params or {})
    request_params["access_token"] = token
    try:
        if method.upper() == "GET":
            resp = requests.get(url, params=request_params, timeout=12)
        elif method.upper() == "DELETE":
            resp = requests.delete(url, params=request_params, timeout=12)
        else:
            resp = requests.post(url, params=request_params, json=payload or {}, timeout=12)
        resp.raise_for_status()
    except requests.RequestException as exc:
        response = getattr(exc, "response", None)
        if response is not None:
            safe_params = dict(request_params)
            if "access_token" in safe_params:
                safe_params["access_token"] = "***"
            body = response.text or ""
            if len(body) > 2000:
                body = f"{body[:2000]}...[truncated]"
            current_app.logger.warning(
                "Facebook API error method=%s url=%s status=%s params=%s body=%s",
                method,
                url,
                response.status_code,
                safe_params,
                body,
            )
        raise RuntimeError(f"Facebook API request failed: {exc}") from exc
    data = resp.json() if resp.content else {}
    if isinstance(data, dict) and data.get("error"):
        message = data["error"].get("message") if isinstance(data["error"], dict) else str(data["error"])
        raise RuntimeError(message or "Facebook API error")
    return data if isinstance(data, dict) else {}


def _require_facebook_media_entry(queue_id: str):
    entry = get_facebook_queue_entry(queue_id)
    if not entry:
        return None, (jsonify(success=False, message="Facebook kuyruğu kaydı bulunamadı."), 404)
    if not entry.get("facebook_video_id"):
        return None, (
            jsonify(success=False, message="Facebook videosu henüz yayınlanmadı veya video kimliği yok."),
            400,
        )
    return entry, None


def _normalize_facebook_limit(value, default=25):
    if value is None:
        return default
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return default
    if limit < 0:
        return 0
    return min(limit, 100)


def _normalize_facebook_comment(raw: Dict[str, Any], *, parent_id: Optional[str] = None) -> Dict[str, Any]:
    author = raw.get("from") if isinstance(raw.get("from"), dict) else {}
    return {
        "comment_id": raw.get("id"),
        "text": raw.get("message") or "",
        "author": author.get("name") if isinstance(author, dict) else None,
        "published_at": raw.get("created_time"),
        "like_count": raw.get("like_count"),
        "comment_url": raw.get("permalink_url"),
        "parent_id": parent_id,
        "is_reply": parent_id is not None,
    }


def _fetch_facebook_comments(
    entry: Dict[str, Any],
    *,
    limit: int = 25,
) -> List[Dict[str, Any]]:
    user_id = entry.get("user_id")
    if not user_id:
        return []
    token_info = get_facebook_page_data(user_id)
    if not token_info or not token_info.get("page_access_token"):
        raise RuntimeError("Facebook Page token missing.")
    video_id = entry.get("facebook_video_id")
    if not video_id:
        return []
    payload = _facebook_api_request(
        "GET",
        f"{video_id}/comments",
        token_info["page_access_token"],
        params={
            "fields": (
                "id,message,from,created_time,like_count,comment_count,permalink_url,"
                "comments.limit(5){id,message,from,created_time,like_count,permalink_url}"
            ),
            "order": "reverse_chronological",
            "limit": limit,
        },
    )
    comments = payload.get("data") or []
    normalized: List[Dict[str, Any]] = []
    for item in comments:
        if not isinstance(item, dict):
            continue
        normalized.append(_normalize_facebook_comment(item))
        replies = (item.get("comments") or {}).get("data") if isinstance(item.get("comments"), dict) else []
        for reply in replies or []:
            if isinstance(reply, dict):
                normalized.append(_normalize_facebook_comment(reply, parent_id=item.get("id")))
    return normalized


def _build_facebook_comment_records(
    entry: Dict[str, Any],
    comments: List[Dict[str, Any]],
    moderation_map: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    now = datetime.now(timezone.utc)
    for comment in comments:
        comment_id = comment.get("comment_id")
        if not comment_id:
            continue
        moderation = moderation_map.get(str(comment_id)) or {}
        records.append(
            {
                "platform": "facebook",
                "comment_id": str(comment_id),
                "parent_id": comment.get("parent_id"),
                "thread_id": comment.get("parent_id") or str(comment_id),
                "video_id": entry.get("video_id"),
                "instagram_media_id": None,
                "queue_id": entry.get("id"),
                "owner_user_id": entry.get("user_id"),
                "video_title": entry.get("plan_title") or entry.get("video_id"),
                "author": comment.get("author"),
                "text": comment.get("text"),
                "status": "published",
                "comment_url": comment.get("comment_url"),
                "published_at": comment.get("published_at"),
                "like_count": comment.get("like_count"),
                "moderation_flagged": moderation.get("flagged") if moderation else None,
                "moderation_reason": moderation.get("reason") if moderation else None,
                "moderation_checked_at": now if moderation else None,
            }
        )
    return records


@video_shorts_bp.route("/instagram/media/<queue_id>/comments", methods=["GET"])
def instagram_media_comments(queue_id):
    entry, error = _require_instagram_media_entry(queue_id)
    if error:
        return error
    refresh_flag = _parse_bool(request.args.get("refresh"), False)
    limit_value = _normalize_instagram_limit(request.args.get("limit"), 25)
    if refresh_flag:
        try:
            refresh_instagram_media(queue_id, comments_limit=limit_value)
            entry = get_instagram_queue_entry(queue_id) or entry
        except InstagramActionError as exc:
            return jsonify(success=False, message=str(exc)), 400
    payload = _build_instagram_media_payload(entry)
    current_count = payload.get("comment_count")
    if not isinstance(current_count, int):
        cached_count = payload.get("cached_comment_count")
        if isinstance(cached_count, int):
            current_count = cached_count
        else:
            current_count = len(payload.get("comments") or [])
    update_instagram_last_seen_comment_count(queue_id, current_count)
    payload["last_seen_comment_count"] = current_count
    return jsonify(success=True, **payload)


@video_shorts_bp.route("/instagram/media/<queue_id>/refresh", methods=["POST"])
def instagram_media_refresh_endpoint(queue_id):
    entry, error = _require_instagram_media_entry(queue_id)
    if error:
        return error
    data = request.get_json(silent=True) or {}
    limit_value = _normalize_instagram_limit(data.get("comments_limit"), 25)
    try:
        refresh_instagram_media(queue_id, comments_limit=limit_value)
        entry = get_instagram_queue_entry(queue_id) or entry
    except InstagramActionError as exc:
        return jsonify(success=False, message=str(exc)), 400
    payload = _build_instagram_media_payload(entry)
    return jsonify(success=True, **payload)


@video_shorts_bp.route("/instagram/media/<queue_id>/comments/<comment_id>", methods=["DELETE"])
def instagram_comment_delete(queue_id, comment_id):
    entry, error = _require_instagram_media_entry(queue_id)
    if error:
        return error
    try:
        delete_instagram_comment(queue_id, comment_id)
        update_comment_status("instagram", comment_id, "deleted")
        refresh_instagram_media(queue_id, comments_limit=25)
        entry = get_instagram_queue_entry(queue_id) or entry
    except InstagramActionError as exc:
        return jsonify(success=False, message=str(exc)), 400
    payload = _build_instagram_media_payload(entry)
    return jsonify(success=True, **payload)


@video_shorts_bp.route("/instagram/media/<queue_id>/comments/<comment_id>/hide", methods=["POST"])
def instagram_comment_hide(queue_id, comment_id):
    entry, error = _require_instagram_media_entry(queue_id)
    if error:
        return error
    try:
        hide_instagram_comment(queue_id, comment_id)
        update_comment_status("instagram", comment_id, "hidden")
    except InstagramActionError as exc:
        return jsonify(success=False, message=str(exc)), 400
    payload = _build_instagram_media_payload(entry)
    return jsonify(success=True, **payload)


@video_shorts_bp.route("/instagram/media/<queue_id>/comments/<comment_id>/unhide", methods=["POST"])
def instagram_comment_unhide(queue_id, comment_id):
    entry, error = _require_instagram_media_entry(queue_id)
    if error:
        return error
    try:
        unhide_instagram_comment(queue_id, comment_id)
        update_comment_status("instagram", comment_id, "published")
    except InstagramActionError as exc:
        return jsonify(success=False, message=str(exc)), 400
    payload = _build_instagram_media_payload(entry)
    return jsonify(success=True, **payload)


@video_shorts_bp.route("/instagram/media/<queue_id>/comments/<comment_id>/reply", methods=["POST"])
def instagram_comment_reply(queue_id, comment_id):
    entry, error = _require_instagram_media_entry(queue_id)
    if error:
        return error
    data = request.get_json(silent=True) or {}
    text = (data.get("message") or data.get("text") or "").strip()
    if not text:
        return jsonify(success=False, message="Yanıt metni boş olamaz."), 400
    try:
        reply_result = reply_instagram_comment(queue_id, comment_id, text)
        refresh_instagram_media(queue_id, comments_limit=25)
        entry = get_instagram_queue_entry(queue_id) or entry
    except InstagramActionError as exc:
        return jsonify(success=False, message=str(exc)), 400
    payload = _build_instagram_media_payload(entry)
    payload["reply"] = reply_result
    return jsonify(success=True, **payload)


@video_shorts_bp.route("/instagram/media/<queue_id>/comments/<comment_id>/like", methods=["POST"])
def instagram_comment_like(queue_id, comment_id):
    entry, error = _require_instagram_media_entry(queue_id)
    if error:
        return error
    data = request.get_json(silent=True) or {}
    like_flag = _parse_bool(data.get("like"), True)
    try:
        set_instagram_comment_like(queue_id, comment_id, like=like_flag)
        refresh_instagram_media(queue_id, comments_limit=25)
        entry = get_instagram_queue_entry(queue_id) or entry
    except InstagramActionError as exc:
        return jsonify(success=False, message=str(exc)), 400
    payload = _build_instagram_media_payload(entry)
    payload["liked"] = like_flag
    return jsonify(success=True, **payload)


@video_shorts_bp.route("/facebook/media/<queue_id>/comments", methods=["GET"])
def facebook_media_comments(queue_id):
    entry, error = _require_facebook_media_entry(queue_id)
    if error:
        return error
    limit_value = _normalize_facebook_limit(request.args.get("limit"), 25)
    try:
        comments = _fetch_facebook_comments(entry, limit=limit_value)
    except RuntimeError as exc:
        return jsonify(success=False, message=str(exc)), 400
    moderation_entries = [
        {"id": str(comment.get("comment_id")), "text": comment.get("text") or ""}
        for comment in comments
        if comment.get("comment_id") and comment.get("text")
    ]
    moderation_map = (
        moderate_text_entries(moderation_entries, entry.get("user_id"))
        if moderation_entries
        else {}
    )
    if moderation_map:
        for comment in comments:
            comment_id = comment.get("comment_id")
            if not comment_id:
                continue
            comment["moderation"] = moderation_map.get(str(comment_id))
    records = _build_facebook_comment_records(entry, comments, moderation_map)
    if records:
        upsert_comment_records(records)
    current_count = entry.get("comment_count")
    if not isinstance(current_count, int):
        current_count = len([c for c in comments if not c.get("is_reply")])
    update_facebook_queue_metrics(queue_id, comment_count=current_count)
    update_facebook_last_seen_comment_count(queue_id, current_count)
    return jsonify(success=True, comments=comments, comment_count=current_count)


@video_shorts_bp.route("/facebook/media/<queue_id>/comments/<comment_id>/reply", methods=["POST"])
def facebook_comment_reply(queue_id, comment_id):
    entry, error = _require_facebook_media_entry(queue_id)
    if error:
        return error
    data = request.get_json(silent=True) or {}
    text = (data.get("message") or data.get("text") or "").strip()
    if not text:
        return jsonify(success=False, message="Yanıt metni boş olamaz."), 400
    token_info = get_facebook_page_data(entry.get("user_id"))
    if not token_info or not token_info.get("page_access_token"):
        return jsonify(success=False, message="Facebook Page token missing."), 400
    try:
        _facebook_api_request(
            "POST",
            f"{comment_id}/comments",
            token_info["page_access_token"],
            payload={"message": text},
        )
        comments = _fetch_facebook_comments(entry, limit=25)
    except RuntimeError as exc:
        return jsonify(success=False, message=str(exc)), 400
    moderation_entries = [
        {"id": str(comment.get("comment_id")), "text": comment.get("text") or ""}
        for comment in comments
        if comment.get("comment_id") and comment.get("text")
    ]
    moderation_map = (
        moderate_text_entries(moderation_entries, entry.get("user_id"))
        if moderation_entries
        else {}
    )
    if moderation_map:
        for comment in comments:
            comment_id_value = comment.get("comment_id")
            if not comment_id_value:
                continue
            comment["moderation"] = moderation_map.get(str(comment_id_value))
    records = _build_facebook_comment_records(entry, comments, moderation_map)
    if records:
        upsert_comment_records(records)
    return jsonify(success=True, comments=comments)


@video_shorts_bp.route("/facebook/media/<queue_id>/comments/<comment_id>", methods=["DELETE"])
def facebook_comment_delete(queue_id, comment_id):
    entry, error = _require_facebook_media_entry(queue_id)
    if error:
        return error
    token_info = get_facebook_page_data(entry.get("user_id"))
    if not token_info or not token_info.get("page_access_token"):
        return jsonify(success=False, message="Facebook Page token missing."), 400
    try:
        _facebook_api_request(
            "DELETE",
            f"{comment_id}",
            token_info["page_access_token"],
        )
        comments = _fetch_facebook_comments(entry, limit=25)
    except RuntimeError as exc:
        return jsonify(success=False, message=str(exc)), 400
    moderation_entries = [
        {"id": str(comment.get("comment_id")), "text": comment.get("text") or ""}
        for comment in comments
        if comment.get("comment_id") and comment.get("text")
    ]
    moderation_map = (
        moderate_text_entries(moderation_entries, entry.get("user_id"))
        if moderation_entries
        else {}
    )
    if moderation_map:
        for comment in comments:
            comment_id_value = comment.get("comment_id")
            if not comment_id_value:
                continue
            comment["moderation"] = moderation_map.get(str(comment_id_value))
    records = _build_facebook_comment_records(entry, comments, moderation_map)
    if records:
        upsert_comment_records(records)
    return jsonify(success=True, comments=comments)


def _collect_short_candidates(publish_map):
    entries = sorted(
        ((ts, vid) for vid, ts in publish_map.items() if ts),
        key=lambda pair: pair[0],
    )
    short_ids = set()
    for idx, (base_ts, base_vid) in enumerate(entries):
        j = idx + 1
        while j < len(entries):
            next_ts, next_vid = entries[j]
            if (next_ts - base_ts).total_seconds() >= SHORT_WINDOW_SECONDS:
                break
            short_ids.add(base_vid)
            short_ids.add(next_vid)
            j += 1
    return short_ids


def _bulk_mark_short(conn, video_ids):
    if not video_ids:
        return
    placeholders = ", ".join("?" for _ in video_ids)
    conn.execute(
        f"""
        UPDATE youtube_videos
        SET download_status = 'short'
        WHERE video_id IN ({placeholders})
          AND lower(coalesce(download_status,'')) != 'downloaded'
        """,
        list(video_ids),
    )


def _collect_short_broadcast_entries(
    brand_id: Optional[str] = None,
    *,
    owner_user_id: Optional[str] = None,
):
    entries: List[Dict[str, Any]] = []
    plan_suffix = "_plan.json"
    effective_owner_user_id = _effective_non_admin_owner_user_id(owner_user_id)
    ai_items = list_ai_broadcast_entries(
        brand_id=brand_id,
        user_id=effective_owner_user_id,
    )
    ai_video_ids = [item.get("video_id") for item in ai_items if item.get("video_id")]
    ai_instagram_queue_map = load_instagram_queue_map(ai_video_ids)
    ai_tiktok_queue_map = load_tiktok_queue_map(ai_video_ids)
    ai_facebook_queue_map = load_facebook_queue_map(ai_video_ids)
    if not SHORTS_DIR.exists():
        for item in ai_items:
            queue_key = (item.get("video_id") or "", "1")
            ig_records = ai_instagram_queue_map.get(queue_key) or []
            tt_records = ai_tiktok_queue_map.get(queue_key) or []
            fb_records = ai_facebook_queue_map.get(queue_key) or []
            has_queue = bool(ig_records or tt_records or fb_records)
            youtube_status = str(item.get("youtube_status") or "").strip().lower()
            has_youtube = youtube_status in {"scheduled", "published", "uploaded"}
            if not has_youtube and not has_queue:
                continue
            entries.append(
                {
                    "video_id": item.get("video_id"),
                    "plan_index": 1,
                    "plan_index_display": "1",
                    "plan_title": item.get("title") or "",
                    "publish_status": youtube_status or ("not_planned" if has_queue else ""),
                    "publish_status_label": (
                        "Published" if youtube_status == "published"
                        else ("Scheduled" if youtube_status in {"scheduled", "uploaded"} else "Not planned")
                    ),
                    "publish_at_iso": item.get("youtube_publish_at") or item.get("youtube_published_at"),
                    "publish_label": _format_display_timestamp(
                        item.get("youtube_publish_at") or item.get("youtube_published_at"),
                        DEFAULT_TIME_ZONE,
                    ) if (item.get("youtube_publish_at") or item.get("youtube_published_at")) else None,
                    "publish_sort_key": item.get("youtube_publish_at") or item.get("youtube_published_at") or "",
                    "clip_filename": item.get("clip_filename"),
                    "clip_ready": bool(_short_public_url(item.get("clip_filename") or "")),
                    "status_value": "created",
                    "status_label": "Created",
                    "short_video_id": item.get("youtube_video_id") or None,
                    "excerpt": None,
                    "description": item.get("description"),
                }
            )
        return entries
    plan_paths = [path for path in SHORTS_DIR.glob(f"*{plan_suffix}") if path.is_file()]
    allowed_source_ids = _brand_allowed_source_video_ids(
        brand_id,
        owner_user_id=effective_owner_user_id,
    )
    if allowed_source_ids is not None:
        plan_paths = [
            path for path in plan_paths
            if path.name.endswith(plan_suffix) and path.name[: -len(plan_suffix)] in allowed_source_ids
        ]
    video_ids = [path.name[: -len(plan_suffix)] for path in plan_paths if path.name.endswith(plan_suffix)]
    instagram_queue_map = load_instagram_queue_map(video_ids)
    tiktok_queue_map = load_tiktok_queue_map(video_ids)
    facebook_queue_map = load_facebook_queue_map(video_ids)
    for plan_path in plan_paths:
        video_id = plan_path.name[: -len(plan_suffix)]
        if not video_id:
            continue
        try:
            plan_data = json.loads(plan_path.read_text())
        except Exception:
            continue
        plan_entries = plan_data.get("plan") or plan_data.get("clips") or []
        seen_plan_keys = set()
        for plan_entry in plan_entries:
            publish_status = (plan_entry.get("publish_status") or "").lower()
            plan_index_raw = plan_entry.get("plan_index")
            plan_index_key = str(plan_index_raw or "").strip()
            plan_index_value = plan_entry.get("plan_index")
            try:
                plan_index_value = int(plan_index_value)
            except Exception:
                plan_index_value = None
            if not plan_index_key and plan_index_value is not None:
                plan_index_key = str(plan_index_value)
            queue_key = (video_id, plan_index_key)
            seen_plan_keys.add(queue_key)
            ig_records = instagram_queue_map.get(queue_key) or []
            tt_records = tiktok_queue_map.get(queue_key) or []
            fb_records = facebook_queue_map.get(queue_key) or []
            has_queue = bool(ig_records or tt_records or fb_records)
            has_youtube = publish_status in ("scheduled", "published")
            if not has_youtube and not has_queue:
                continue
            status_value = (plan_entry.get("status") or "").lower()
            clip_filename = plan_entry.get("clip_filename") or plan_entry.get("output_filename")
            clip_ready = status_value == "created" or bool(_short_public_url(clip_filename))
            publish_iso = plan_entry.get("publish_at_iso") or plan_entry.get("publish_at")
            publish_dt = _normalize_timestamp(publish_iso)
            publish_label = publish_dt.strftime("%Y-%m-%d %H:%M UTC") if publish_dt else None
            queue_publish_iso = None
            if not has_youtube and has_queue:
                queue_times = []
                for record in ig_records + tt_records + fb_records:
                    candidate = record.get("published_at") or record.get("publish_at")
                    dt_val = _normalize_timestamp(candidate)
                    if dt_val:
                        queue_times.append((dt_val, candidate))
                if queue_times:
                    queue_times.sort(key=lambda item: item[0])
                    queue_publish_iso = queue_times[0][1]
            status_label = plan_entry.get("status") or ""
            if not has_youtube and has_queue:
                publish_status = "not_planned"
                publish_label = None
            entries.append(
                {
                    "video_id": video_id,
                    "plan_index": plan_index_value if plan_index_value is not None else (plan_index_key or None),
                    "plan_index_display": str(plan_index_value) if plan_index_value is not None else plan_index_key,
                    "plan_title": plan_entry.get("title") or plan_entry.get("yt_title") or "",
                    "publish_status": publish_status,
                    "publish_status_label": "Not planned" if publish_status == "not_planned" else publish_status.capitalize(),
                    "publish_at_iso": publish_iso if has_youtube else None,
                    "publish_label": publish_label,
                    "publish_sort_key": publish_iso or queue_publish_iso or "",
                    "clip_filename": clip_filename,
                    "clip_ready": clip_ready,
                    "status_value": status_value,
                    "status_label": status_label.capitalize() if status_label else "Pending",
                    "short_video_id": plan_entry.get("yt_video_id") or plan_entry.get("short_video_id"),
                    "excerpt": plan_entry.get("excerpt"),
                    "description": plan_entry.get("yt_description") or plan_entry.get("description"),
                }
            )

        # Add queue-only entries (e.g. longcomp) that do not exist in *_plan.json
        queue_only_keys = set()
        queue_only_keys.update(k for k in instagram_queue_map.keys() if k[0] == video_id)
        queue_only_keys.update(k for k in tiktok_queue_map.keys() if k[0] == video_id)
        queue_only_keys.update(k for k in facebook_queue_map.keys() if k[0] == video_id)
        for queue_key in sorted(queue_only_keys):
            if queue_key in seen_plan_keys:
                continue
            plan_index_key = str(queue_key[1] or "").strip()
            if not plan_index_key:
                continue
            ig_records = instagram_queue_map.get(queue_key) or []
            tt_records = tiktok_queue_map.get(queue_key) or []
            fb_records = facebook_queue_map.get(queue_key) or []
            all_queue_records = ig_records + tt_records + fb_records
            if not all_queue_records:
                continue
            queue_times = []
            for record in all_queue_records:
                candidate = record.get("published_at") or record.get("publish_at")
                dt_val = _normalize_timestamp(candidate)
                if dt_val:
                    queue_times.append((dt_val, candidate))
            queue_publish_iso = None
            publish_label = None
            if queue_times:
                queue_times.sort(key=lambda item: item[0])
                queue_publish_iso = queue_times[0][1]
                publish_label = queue_times[0][0].strftime("%Y-%m-%d %H:%M UTC")
            latest_record = all_queue_records[-1]
            clip_filename = ""
            for record in reversed(all_queue_records):
                clip_candidate = str(record.get("clip_filename") or "").strip()
                if clip_candidate:
                    clip_filename = clip_candidate
                    break
            clip_ready = bool(_short_public_url(clip_filename))
            status_value = str(latest_record.get("status") or "").strip().lower()
            youtube_publish_status = "not_planned"
            youtube_publish_iso = None
            youtube_video_id = None
            if clip_filename:
                clip_path = SHORTS_DIR / clip_filename
                meta_path = clip_path.with_suffix(".long.meta.json")
                if meta_path.exists():
                    try:
                        meta_payload = json.loads(meta_path.read_text())
                    except Exception:
                        meta_payload = {}
                    publish_state = meta_payload.get("publish_state")
                    yt_state = publish_state.get("youtube") if isinstance(publish_state, dict) else {}
                    if isinstance(yt_state, dict):
                        yt_status = str(yt_state.get("status") or "").strip().lower()
                        yt_iso = yt_state.get("published_at") or yt_state.get("publish_at")
                        yt_video_id = str(yt_state.get("yt_video_id") or "").strip() or None
                        if yt_status == "published":
                            youtube_publish_status = "published"
                            youtube_publish_iso = yt_iso
                            youtube_video_id = yt_video_id
                        elif yt_status in {"scheduled", "pending", "retry", "uploading"}:
                            youtube_publish_status = "scheduled"
                            youtube_publish_iso = yt_iso
                            youtube_video_id = yt_video_id
                        else:
                            youtube_video_id = yt_video_id
            plan_title = ""
            for record in reversed(all_queue_records):
                title_candidate = str(record.get("plan_title") or "").strip()
                if title_candidate:
                    plan_title = title_candidate
                    break
            publish_status_label = (
                "Published"
                if youtube_publish_status == "published"
                else ("Scheduled" if youtube_publish_status == "scheduled" else "Not planned")
            )
            entries.append(
                {
                    "video_id": video_id,
                    "plan_index": plan_index_key,
                    "plan_index_display": plan_index_key,
                    "plan_title": plan_title,
                    "publish_status": youtube_publish_status,
                    "publish_status_label": publish_status_label,
                    "publish_at_iso": youtube_publish_iso,
                    "publish_label": (
                        _format_display_timestamp(youtube_publish_iso, DEFAULT_TIME_ZONE)
                        if youtube_publish_iso
                        else publish_label
                    ),
                    "publish_sort_key": youtube_publish_iso or queue_publish_iso or "",
                    "clip_filename": clip_filename or None,
                    "clip_ready": clip_ready,
                    "status_value": status_value,
                    "status_label": status_value.capitalize() if status_value else "Pending",
                    "short_video_id": youtube_video_id,
                    "excerpt": None,
                    "description": None,
                }
            )
    for item in ai_items:
        queue_key = (item.get("video_id") or "", "1")
        ig_records = ai_instagram_queue_map.get(queue_key) or []
        tt_records = ai_tiktok_queue_map.get(queue_key) or []
        fb_records = ai_facebook_queue_map.get(queue_key) or []
        has_queue = bool(ig_records or tt_records or fb_records)
        youtube_status = str(item.get("youtube_status") or "").strip().lower()
        has_youtube = youtube_status in {"scheduled", "published", "uploaded"}
        if not has_youtube and not has_queue:
            continue
        publish_iso = item.get("youtube_publish_at") or item.get("youtube_published_at")
        entries.append(
            {
                "video_id": item.get("video_id"),
                "plan_index": 1,
                "plan_index_display": "1",
                "plan_title": item.get("title") or "",
                "publish_status": youtube_status or ("not_planned" if has_queue else ""),
                "publish_status_label": (
                    "Published" if youtube_status == "published"
                    else ("Scheduled" if youtube_status in {"scheduled", "uploaded"} else "Not planned")
                ),
                "publish_at_iso": publish_iso,
                "publish_label": _format_display_timestamp(publish_iso, DEFAULT_TIME_ZONE) if publish_iso else None,
                "publish_sort_key": publish_iso or "",
                "clip_filename": item.get("clip_filename"),
                "clip_ready": bool(_short_public_url(item.get("clip_filename") or "")),
                "status_value": "created",
                "status_label": "Created",
                "short_video_id": item.get("youtube_video_id") or None,
                "excerpt": None,
                "description": item.get("description"),
            }
        )
    return entries


def _guess_video_download_timestamp(video: Dict[str, Any]):
    """
    Try to infer download timestamp from files under VIDEOS_DIR when DB field is empty.
    """
    candidates = []
    video_id = video.get("video_id")
    video_pk = video.get("id")
    potential_names = []
    if video_id:
        potential_names.extend(
            [
                video_id,
                f"{video_id}.mp4",
                f"{video_id}.mov",
                f"{video_id}.mkv",
                f"{video_id}.mp3",
                f"{video_id}.wav",
                f"{video_id}.m4a",
                f"{video_id}.aac",
                f"{video_id}.ogg",
                f"{video_id}.flac",
                f"{video_id}.mp4M",
            ]
        )
    if video_pk:
        pk_str = str(video_pk)
        potential_names.extend(
            [
                pk_str,
                f"{pk_str}.mp4",
                f"{pk_str}.mov",
                f"{pk_str}.mkv",
                f"{pk_str}.mp3",
                f"{pk_str}.wav",
                f"{pk_str}.m4a",
                f"{pk_str}.aac",
                f"{pk_str}.ogg",
                f"{pk_str}.flac",
            ]
        )
    seen = set()
    for name in potential_names:
        path = VIDEOS_DIR / name
        if path in seen:
            continue
        seen.add(path)
        try:
            if path.exists():
                stats = path.stat()
                return datetime.fromtimestamp(stats.st_mtime)
        except Exception:
            continue
    return None


@video_shorts_bp.route("/videos/<int:channel_id>")
def videos_page(channel_id):

    current_user = getattr(g, "vs_current_user", None)
    brand_id = current_brand_id()
    if not current_user:
        return redirect(url_for("video_shorts_bp.login", next=request.url))
    is_admin = current_user.get("role") == "admin"
    conn = get_db()
    ensure_brand_schema(conn)
    ensure_channel_owner_schema(conn)

    # Pagination & sorting params
    try:
        page = max(int(request.args.get("page", 1)), 1)
    except Exception:
        page = 1
    page_size = 50
    sort_key = request.args.get("sort", "published")
    sort_dir = request.args.get("dir", "desc")

    sort_map = {
        "title": "title",
        "published": "published_at",
        "downloaded": "downloaded_at",
    }
    sort_col = sort_map.get(sort_key, "published_at")
    sort_dir_clean = "ASC" if str(sort_dir).lower() == "asc" else "DESC"
    search_q = (request.args.get("q") or "").strip()
    video_id_q = (request.args.get("vid") or "").strip()
    try:
        duration_min = int(request.args.get("dmin", "").strip())
        if duration_min < 0:
            duration_min = 0
    except (ValueError, TypeError):
        duration_min = None
    try:
        duration_max_raw = request.args.get("dmax")
        duration_max = int(duration_max_raw.strip()) if duration_max_raw else None
        if duration_max is not None and duration_max < 0:
            duration_max = None
    except (ValueError, TypeError):
        duration_max = None
    caption_filters = [
        c.strip().lower()
        for c in request.args.getlist("caption")
        if c and c.strip().lower() in ("ready", "paused")
    ]
    short_filter = (request.args.get("short_filter") or "").strip()
    local_filters = [
        l.strip().lower()
        for l in request.args.getlist("lstatus")
        if l and l.strip().lower() in ("downloaded", "not_downloaded")
    ]

    # Kanal bilgisi
    row = conn.execute(
        """
        SELECT
          channel_id,
          channel_name,
          channel_url,
          notes,
          added_at,
          youtube_channel_id,
          uploads_playlist_id,
          total_videos,
          is_active,
          next_page_token,
          owner_user_id,
          brand_id
        FROM youtube_channels
        WHERE channel_id = ?
        """,
        [channel_id],
    ).fetchone()

    if not row:
        conn.close()
        flash("Channel not found", "danger")
        return redirect(url_for("video_shorts_bp.channels_page"))

    channel = {
        "channel_id": row[0],
        "channel_name": row[1],
        "channel_url": row[2],
        "notes": row[3],
        "added_at": row[4],
        "youtube_channel_id": row[5],
        "uploads_playlist_id": row[6],
        "total_videos": row[7],
        "is_active": row[8],
        "next_page_token": row[9],
        "owner_user_id": row[10],
        "brand_id": row[11],
    }
    preferred_channel_ids = _preferred_brand_channel_ids(
        channel.get("owner_user_id"),
        brand_id,
    )
    if not is_admin and channel["owner_user_id"] != current_user["id"]:
        conn.close()
        flash("You do not have permission to view this channel.", "danger")
        return redirect(url_for("video_shorts_bp.channels_page"))
    if not is_admin and brand_id and channel.get("brand_id") != brand_id:
        conn.close()
        flash("Bu channel aktif brand'e ait değil.", "warning")
        return redirect(url_for("video_shorts_bp.channels_page"))
    if preferred_channel_ids and str(channel.get("channel_id")) not in preferred_channel_ids:
        conn.close()
        flash("Bu channel aktif brand scope'una ait değil.", "warning")
        return redirect(url_for("video_shorts_bp.channels_page"))

    # Fetch parametresi geldiyse bir batch çek
    if request.args.get("fetch") == "1":
        try:
            # Metadata + toplam video sayısını her seferinde tazele
            meta = get_channel_metadata(channel["channel_url"])
            channel["youtube_channel_id"] = meta["youtube_channel_id"]
            channel["uploads_playlist_id"] = meta["uploads_playlist_id"]
            channel["total_videos"] = meta["total_videos"]

            conn.execute(
                """
                UPDATE youtube_channels
                SET youtube_channel_id = ?,
                    uploads_playlist_id = ?,
                    total_videos = ?
                WHERE channel_id = ?
                """,
                [
                    channel["youtube_channel_id"],
                    channel["uploads_playlist_id"],
                    channel["total_videos"],
                    channel_id,
                ],
            )

            existing_publish_map = {
                row[0]: _normalize_timestamp(row[1])
                for row in conn.execute(
                    "SELECT video_id, published_at FROM youtube_videos WHERE channel_id = ?",
                    [channel_id],
                ).fetchall()
            }
            existing_video_ids = set(existing_publish_map.keys())

            video_to_fetch = (request.args.get("video_to_fetch") or "").strip()
            to_process: List[Dict[str, Any]] = []
            batch = None
            stats_map: Dict[str, Any] = {}
            if video_to_fetch:
                target_meta = None
                page_token = None
                while True:
                    batch = fetch_playlist_items_batch(
                        playlist_id=channel["uploads_playlist_id"],
                        page_token=page_token,
                        max_results=50,
                    )
                    for v in batch["videos"]:
                        if v["video_id"] == video_to_fetch:
                            target_meta = v
                            break
                    if target_meta or not batch["next_page_token"]:
                        break
                    page_token = batch["next_page_token"]
                if target_meta:
                    to_process = [target_meta]
                    stats_map = fetch_video_stats([video_to_fetch])
                else:
                    flash("Specified video not found in uploads playlist.", "warning")
            else:
                def _find_next_batch_with_missing_videos(start_token: Optional[str]) -> Optional[Dict[str, Any]]:
                    page_token = start_token
                    seen_tokens = set()
                    while page_token not in seen_tokens:
                        seen_tokens.add(page_token)
                        candidate_batch = fetch_playlist_items_batch(
                            playlist_id=channel["uploads_playlist_id"],
                            page_token=page_token,
                            max_results=50,
                        )
                        missing_videos = [
                            v for v in candidate_batch["videos"]
                            if v["video_id"] not in existing_video_ids
                        ]
                        if missing_videos:
                            candidate_batch["videos"] = missing_videos
                            return candidate_batch
                        next_token = candidate_batch["next_page_token"]
                        if not next_token:
                            break
                        page_token = next_token
                    return None

                batch = _find_next_batch_with_missing_videos(channel["next_page_token"])
                if not batch and channel["next_page_token"]:
                    batch = _find_next_batch_with_missing_videos(None)
                if batch:
                    to_process = batch["videos"]
                    stats_map = fetch_video_stats([v["video_id"] for v in to_process])

            imported = 0
            already_in_channel = 0
            imported_lengths = []

            for v in to_process:
                video_id = v["video_id"]
                published_dt = _normalize_timestamp(v.get("published_at"))

                exists = conn.execute(
                    "SELECT 1 FROM youtube_videos WHERE channel_id = ? AND video_id = ?",
                    [channel_id, video_id],
                ).fetchone()

                stats = stats_map.get(video_id, {})

                if not exists:
                    conn.execute(
                        """
                        INSERT INTO youtube_videos
                            (channel_id, video_id, title, published_at, thumbnail_url, fetch_transcript,
                             duration_seconds, view_count, like_count, comment_count, video_url, owner_user_id, brand_id, download_status)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            channel_id,
                            video_id,
                            v["title"],
                            v["published_at"],
                            v["thumbnail_url"],
                            False,  # default: captions off
                            stats.get("duration_seconds"),
                            stats.get("view_count"),
                            stats.get("like_count"),
                            stats.get("comment_count"),
                            f"https://www.youtube.com/watch?v={video_id}",
                            channel.get("owner_user_id"),
                            channel.get("brand_id"),
                            "pending",
                        ],
                    )
                    imported += 1
                    duration_readable = _pretty_duration(stats.get("duration_seconds"))
                    if duration_readable:
                        imported_lengths.append(duration_readable)
                    if published_dt:
                        existing_publish_map[video_id] = published_dt
                else:
                    already_in_channel += 1
                    # Varsa, eksik istatistikleri güncelle
                    if any(stats.get(k) is not None for k in ["duration_seconds", "view_count", "like_count", "comment_count"]):
                        conn.execute(
                            """
                            UPDATE youtube_videos
                            SET duration_seconds = COALESCE(duration_seconds, ?),
                                view_count = COALESCE(view_count, ?),
                                like_count = COALESCE(like_count, ?),
                                comment_count = COALESCE(comment_count, ?),
                                video_url = COALESCE(video_url, ?),
                                download_status = CASE
                                    WHEN lower(coalesce(download_status, '')) IN ('downloaded', 'downloaded_deleted', 'short', 'irrelevant')
                                        THEN download_status
                                    ELSE 'pending'
                                END
                            WHERE video_id = ?
                            """,
                            [
                                stats.get("duration_seconds"),
                                stats.get("view_count"),
                                stats.get("like_count"),
                                stats.get("comment_count"),
                                f"https://www.youtube.com/watch?v={video_id}",
                                video_id,
                            ],
                        )
                    if published_dt:
                        existing_publish_map[video_id] = published_dt

            # next_page_token güncelle (sadece playlist batch)
            if batch and not video_to_fetch:
                next_token = batch["next_page_token"]
                channel["next_page_token"] = next_token

                conn.execute(
                    "UPDATE youtube_channels SET next_page_token = ? WHERE channel_id = ?",
                    [next_token, channel_id],
                )

            # Playlist bitti ise is_active alanını pasif yapabiliriz
            duration_note = ""
            if imported_lengths:
                duration_note = " (durations: " + ", ".join(imported_lengths) + ")"

            # Update durations for every video we already have for this channel
            existing_ids = [
                row[0]
                for row in conn.execute(
                    "SELECT video_id FROM youtube_videos WHERE channel_id = ?",
                    [channel_id],
                ).fetchall()
            ]
            extra_stats = fetch_video_stats(existing_ids)
            for vid, extra in extra_stats.items():
                if extra.get("duration_seconds") is not None:
                    conn.execute(
                        """
                        UPDATE youtube_videos
                        SET duration_seconds = ?
                        WHERE video_id = ?
                        """,
                        [extra["duration_seconds"], vid],
                    )

            _bulk_mark_short(conn, _collect_short_candidates(existing_publish_map))

            if not video_to_fetch and not channel["next_page_token"] and channel["total_videos"] is not None:
                conn.execute(
                    "UPDATE youtube_channels SET is_active = 0 WHERE channel_id = ?",
                    [channel_id],
                )
                channel["is_active"] = 0
                flash(
                    f"Imported {imported} videos{duration_note}. Channel seems fully synced, set to inactive.",
                    "success",
                )
            else:
                if video_to_fetch:
                    if imported > 0:
                        flash(f"Imported 1 video: {video_to_fetch}{duration_note}.", "success")
                    elif already_in_channel > 0:
                        flash("This video is already imported for this channel.", "info")
                    else:
                        flash("No video was imported for this request.", "warning")
                elif imported == 0:
                    flash("No missing videos found in uploads playlist.", "info")
                else:
                    flash(f"Imported {imported} videos from playlist batch{duration_note}.", "success")

            conn.commit()

        except YoutubeApiError as e:
            flash(f"YouTube API error: {e}", "danger")
        except Exception as e:
            flash(f"Unexpected error while fetching videos: {e}", "danger")

        conn.close()
        return redirect(url_for("video_shorts_bp.videos_page", channel_id=channel_id))

    # Normal sayfa: filtre + sayfalama
    duration_min_seconds = duration_min * 60 if duration_min is not None else None
    duration_max_seconds = duration_max * 60 if duration_max is not None else None

    where_clauses = ["channel_id = ?", "title ILIKE ?"]
    where_params = [channel_id, f"%{search_q}%" if search_q else "%"]
    if video_id_q:
        where_clauses.append("video_id = ?")
        where_params.append(video_id_q)

    # Download status filter (single-select)
    allowed_dstatus = {"not_needed", "pending", "downloaded", "short", "irrelevant", "downloaded_deleted"}
    download_filter = request.args.get("dstatus", "").strip().lower()
    if download_filter in allowed_dstatus:
        where_clauses.append("lower(coalesce(download_status,'not_needed')) = ?")
        where_params.append(download_filter)
    else:
        download_filter = ""

    if duration_min_seconds is not None:
        where_clauses.append("duration_seconds >= ?")
        where_params.append(duration_min_seconds)
    if duration_max_seconds is not None:
        where_clauses.append("duration_seconds <= ?")
        where_params.append(duration_max_seconds)

    where_sql = " AND ".join(where_clauses)

    total_count = conn.execute(
        f"SELECT COUNT(*) FROM youtube_videos WHERE {where_sql}",
        where_params,
    ).fetchone()[0]

    caption_pending = conn.execute(
        """
        SELECT COUNT(*) FROM youtube_videos
        WHERE channel_id = ? AND fetch_transcript = TRUE
              AND lower(transcript_status) = 'pending'
        """,
        [channel_id],
    ).fetchone()[0]

    caption_ready = conn.execute(
        """
        SELECT COUNT(*) FROM youtube_videos
        WHERE channel_id = ? AND lower(transcript_status) IN ('done','ready','completed','ok')
        """,
        [channel_id],
    ).fetchone()[0]

    download_ready = conn.execute(
        """
        SELECT COUNT(*) FROM youtube_videos
        WHERE channel_id = ? AND lower(coalesce(download_status,'')) = 'downloaded'
        """,
        [channel_id],
    ).fetchone()[0]
    download_ready_deleted = conn.execute(
        """
        SELECT COUNT(*) FROM youtube_videos
        WHERE channel_id = ? AND lower(coalesce(download_status,'')) = 'downloaded_deleted'
        """,
        [channel_id],
    ).fetchone()[0]
    download_short = conn.execute(
        """
        SELECT COUNT(*) FROM youtube_videos
        WHERE channel_id = ? AND lower(coalesce(download_status,'')) = 'short'
        """,
        [channel_id],
    ).fetchone()[0]
    download_irrelevant = conn.execute(
        """
        SELECT COUNT(*) FROM youtube_videos
        WHERE channel_id = ? AND lower(coalesce(download_status,'')) = 'irrelevant'
        """,
        [channel_id],
    ).fetchone()[0]
    download_pending = conn.execute(
        """
        SELECT COUNT(*) FROM youtube_videos
        WHERE channel_id = ? AND lower(coalesce(download_status,'')) = 'pending'
        """,
        [channel_id],
    ).fetchone()[0]

    # aggregate short plan stats per video
    short_plan_stats = {}
    short_dir = SHORTS_DIR
    for row in conn.execute(
        """
        SELECT id, video_id, download_status FROM youtube_videos WHERE channel_id = ?
        """,
        [channel_id],
    ).fetchall():
        vid_id = row[1]
        row_download_status = (row[2] or "").lower()
        plan_path = short_dir / f"{vid_id}_plan.json"
        plan_count = 0
        if plan_path.exists():
            try:
                data = json.loads(plan_path.read_text())
                plan_count = len(data.get("plan") or data.get("clips") or [])
            except Exception:
                plan_count = 0
        created = 0
        desc_ready = 0
        plan_entries = []
        if plan_path.exists():
            try:
                plan_data = json.loads(plan_path.read_text())
                plan_entries = plan_data.get("plan") or plan_data.get("clips") or []
            except Exception:
                plan_entries = []
        video_status = None
        if row_download_status in {"downloaded", "downloaded_deleted"}:
            for entry in plan_entries:
                status = (entry.get("status") or "").lower()
                filename = entry.get("clip_filename") or entry.get("output_filename")
                output_path = (short_dir / filename) if filename else None
                clip_created = status == "created" or (output_path and output_path.exists())
                if clip_created:
                    created += 1
                if (entry.get("yt_status") or "").lower() == "ready":
                    desc_ready += 1
            if plan_count:
                video_status = "completed" if created >= plan_count else "processing"
        else:
            plan_count = 0
        short_plan_stats[row[0]] = {
            "plan_count": plan_count,
            "created_count": created,
            "desc_ready": desc_ready,
            "short_plan_status": video_status,
            "download_status": row_download_status,
        }

    download_not_needed = conn.execute(
        """
        SELECT COUNT(*) FROM youtube_videos
        WHERE channel_id = ? AND (download_status IS NULL OR lower(download_status) = 'not_needed')
        """,
        [channel_id],
    ).fetchone()[0]

    offset = (page - 1) * page_size
    manual_download_sort = sort_key == "downloaded"
    base_query = f"""
        SELECT
          id,
          video_id,
          title,
          published_at,
          downloaded_at,
          thumbnail_url,
          download_status,
          transcript_status,
          fetch_transcript,
          view_count,
          like_count,
          comment_count,
          video_url,
          duration_seconds
        FROM youtube_videos
        WHERE {where_sql}
    """
    if short_filter:
        query = base_query + f" ORDER BY {sort_col} {sort_dir_clean}"
        rows = conn.execute(query, where_params).fetchall()
    elif manual_download_sort:
        rows = conn.execute(base_query, where_params).fetchall()
    else:
        query = base_query + f" ORDER BY {sort_col} {sort_dir_clean} LIMIT ? OFFSET ?"
        rows = conn.execute(query, where_params + [page_size, offset]).fetchall()

    cols = [d[0] for d in conn.description]
    videos = [dict(zip(cols, r)) for r in rows]

    # Normalize published_at for display (date only)
    for v in videos:
        pa = v.get("published_at")
        try:
            if hasattr(pa, "strftime"):
                v["published_at_str"] = pa.strftime("%Y-%m-%d")
            elif isinstance(pa, str):
                v["published_at_str"] = pa[:10]
            else:
                v["published_at_str"] = ""
        except Exception:
            v["published_at_str"] = ""
        downloaded_label = ""
        downloaded_dt_value = None
        downloaded_at = v.get("downloaded_at")
        try:
            if hasattr(downloaded_at, "strftime"):
                downloaded_dt_value = downloaded_at if downloaded_at.tzinfo else downloaded_at.replace(tzinfo=timezone.utc)
            elif isinstance(downloaded_at, str) and downloaded_at:
                normalized = _normalize_timestamp(downloaded_at)
                if normalized:
                    downloaded_dt_value = normalized
                    downloaded_label = normalized.strftime("%Y-%m-%d %H:%M")
                else:
                    downloaded_label = downloaded_at[:16]
        except Exception:
            downloaded_label = ""
        if downloaded_dt_value:
            downloaded_label = downloaded_dt_value.strftime("%Y-%m-%d %H:%M")
        if not downloaded_dt_value:
            fallback_dt = _guess_video_download_timestamp(v)
            if fallback_dt:
                if fallback_dt.tzinfo is None:
                    fallback_dt = fallback_dt.replace(tzinfo=timezone.utc)
                downloaded_dt_value = fallback_dt
                downloaded_label = fallback_dt.strftime("%Y-%m-%d %H:%M")
        if not downloaded_label:
            downloaded_label = ""
        v["downloaded_at_dt"] = downloaded_dt_value
        v["downloaded_at_epoch"] = downloaded_dt_value.timestamp() if downloaded_dt_value else None
        v["downloaded_at_str"] = downloaded_label

    conn.close()

    if manual_download_sort:
        reverse_download = sort_dir_clean == "DESC"

        def _download_sort_value(entry):
            epoch = entry.get("downloaded_at_epoch")
            if epoch is None:
                return float("-inf") if reverse_download else float("inf")
            return epoch

        videos.sort(key=_download_sort_value, reverse=reverse_download)
        if not short_filter:
            total_count = len(videos)
            start_idx = (page - 1) * page_size
            end_idx = start_idx + page_size
            videos = videos[start_idx:end_idx]

    # attach short stats per video
    for v in videos:
        stats = short_plan_stats.get(
            v["id"], {"plan_count": 0, "created_count": 0, "desc_ready": 0}
        )
        v["plan_count"] = stats["plan_count"]
        v["created_count"] = stats["created_count"]
        v["desc_ready"] = stats["desc_ready"]
        v["short_plan_status"] = stats.get("short_plan_status")
        plan_total = v["plan_count"] or 0
        created_total = v["created_count"] or 0
        if plan_total:
            v["short_plan_status"] = (
                "completed" if created_total >= plan_total else "processing"
            )
        else:
            v["short_plan_status"] = None
    total_plans = sum(stat["plan_count"] for stat in short_plan_stats.values())
    total_created = sum(stat["created_count"] for stat in short_plan_stats.values())
    total_desc_ready = sum(stat["desc_ready"] for stat in short_plan_stats.values())
    total_processing = sum(1 for stat in short_plan_stats.values() if stat.get("short_plan_status") == "processing")
    total_completed = sum(1 for stat in short_plan_stats.values() if stat.get("short_plan_status") == "completed")
    total_not_started = sum(
        1
        for stat in short_plan_stats.values()
        if stat.get("plan_count", 0) == 0
        and stat.get("created_count", 0) == 0
        and stat.get("desc_ready", 0) == 0
        and stat.get("download_status") == "downloaded"
    )

    def _matches_short_filter(video):
        if short_filter == "plans":
            return video.get("plan_count", 0) > 0
        if short_filter == "created":
            return video.get("created_count", 0) > 0
        if short_filter == "descriptions":
            return video.get("desc_ready", 0) > 0
        if short_filter == "not_started":
            return (
                video.get("plan_count", 0) == 0
                and video.get("created_count", 0) == 0
                and video.get("desc_ready", 0) == 0
                and (video.get("download_status") or "").lower() == "downloaded"
            )
        if short_filter == "processing":
            return video.get("short_plan_status") == "processing"
        if short_filter == "completed":
            return video.get("short_plan_status") == "completed"
        return True

    if short_filter:
        videos = [v for v in videos if _matches_short_filter(v)]
        total_count = len(videos)
    # pagination info
    if short_filter:
        total_pages = 1
        page = 1
    else:
        total_pages = (total_count + page_size - 1) // page_size if total_count else 1

    return render_template(
        "videos.html",
        channel=channel,
        videos=videos,
        page=page,
        total_pages=total_pages,
        sort=sort_key,
        sort_dir=sort_dir_clean.lower(),
        search_q=search_q,
        video_id_q=video_id_q,
        total_count=total_count,
        caption_pending=caption_pending,
        caption_ready=caption_ready,
        download_ready=download_ready,
        download_ready_deleted=download_ready_deleted,
        download_pending=download_pending,
        download_not_needed=download_not_needed,
        download_short=download_short,
        download_irrelevant=download_irrelevant,
        shorts_total_plans=total_plans,
        shorts_total_created=total_created,
        shorts_total_desc_ready=total_desc_ready,
        short_filter=short_filter,
        caption_filters=caption_filters,
        local_filters=local_filters,
        download_filter=download_filter,
        transcript_filters=[],
        duration_min=duration_min if duration_min is not None else "",
        duration_max=duration_max if duration_max is not None else "",
        shorts_processing_count=total_processing,
        shorts_completed_count=total_completed,
        shorts_not_started_count=total_not_started,
    )


@video_shorts_bp.route("/shorts/overview")
def shorts_overview():
    current_user = getattr(g, "vs_current_user", None)
    brand_id = current_brand_id()
    is_admin = current_user and current_user.get("role") == "admin"
    brand_owner_user_id, _brand_name = _load_brand_scope_context(brand_id)
    owner_scope_user_id = brand_owner_user_id or (str((current_user or {}).get("id") or "").strip() or None)
    user_tz = (current_user or {}).get("time_zone") or DEFAULT_TIME_ZONE
    status_filter = (request.args.get("status") or "").strip().lower()
    comments_filter = (request.args.get("comments") or "").strip().lower()
    if comments_filter not in {"new"}:
        comments_filter = ""
    search_q = (request.args.get("q") or "").strip()
    channel_filter = (request.args.get("channel") or "").strip()
    day_filter = (request.args.get("day") or "").strip()
    sort_key = (request.args.get("sort") or "publish").strip().lower()
    sort_dir = (request.args.get("dir") or "desc").strip().lower()
    sort_dir = "asc" if sort_dir == "asc" else "desc"
    pending_only_raw = (request.args.get("pending_only") or "").strip().lower()
    pending_only = pending_only_raw in {"1", "true", "yes", "pending"}
    refresh_stats = (request.args.get("refresh_stats") or "").strip().lower() in {"1", "true", "yes"}

    all_entries = _collect_short_broadcast_entries(
        brand_id=brand_id,
        owner_user_id=owner_scope_user_id,
    )
    preferred_channel_ids = _preferred_brand_channel_ids(owner_scope_user_id, brand_id)
    all_entries = _filter_entries_to_channel_scope(
        all_entries,
        owner_user_id=owner_scope_user_id,
        brand_id=brand_id,
        preferred_channel_ids=preferred_channel_ids,
    )
    total_scheduled = sum(1 for entry in all_entries if entry["publish_status"] == "scheduled")
    total_published = sum(1 for entry in all_entries if entry["publish_status"] == "published")

    video_ids = sorted({entry["video_id"] for entry in all_entries if entry.get("video_id")})

    channel_rows: List[Any] = []
    channel_map: Dict[Any, str] = {}
    video_map: Dict[str, Dict[str, Any]] = {}
    schema_conn = get_db()
    ensure_brand_schema(schema_conn)
    ensure_channel_owner_schema(schema_conn)
    schema_conn.close()
    conn = get_db_readonly()
    try:
        channel_sql = """
            SELECT channel_id, channel_name, owner_user_id, brand_id
            FROM youtube_channels
            WHERE owner_user_id = ?
        """
        channel_params: List[Any] = [owner_scope_user_id]
        if brand_id:
            channel_sql += " AND brand_id = ?"
            channel_params.append(brand_id)
        channel_sql += " ORDER BY channel_name"
        channel_rows = conn.execute(channel_sql, channel_params).fetchall()
        if preferred_channel_ids:
            channel_rows = [row for row in channel_rows if str(row[0]) in preferred_channel_ids]
        channel_map = {
            str(row[0]): row[1] or f"Channel {row[0]}"
            for row in channel_rows
        }
        if video_ids:
            placeholders = ", ".join("?" for _ in video_ids)
            sql = f"""
                SELECT
                  v.video_id,
                  v.id AS video_pk,
                  v.title,
                  v.thumbnail_url,
                  v.view_count,
                  v.like_count,
                  v.comment_count,
                  v.video_url,
                  v.channel_id,
                  c.channel_name,
                  c.channel_url,
                  c.owner_user_id,
                  v.brand_id
                FROM youtube_videos v
                LEFT JOIN youtube_channels c ON c.channel_id = v.channel_id
                WHERE v.video_id IN ({placeholders})
            """
            params = list(video_ids)
            sql += " AND v.owner_user_id = ?"
            params.append(owner_scope_user_id)
            if brand_id:
                sql += " AND v.brand_id = ?"
                params.append(brand_id)
            if preferred_channel_ids:
                channel_placeholders = ", ".join("?" for _ in preferred_channel_ids)
                sql += f" AND CAST(v.channel_id AS VARCHAR) IN ({channel_placeholders})"
                params.extend(sorted(preferred_channel_ids))
            video_rows = conn.execute(sql, params).fetchall()
            for row in video_rows:
                channel_id_val = row[8]
                channel_id_str = str(channel_id_val) if channel_id_val is not None else None
                video_map[row[0]] = {
                    "video_pk": row[1],
                    "title": row[2],
                    "thumbnail_url": row[3],
                    "view_count": row[4],
                    "like_count": row[5],
                    "comment_count": row[6],
                    "video_url": row[7],
                    "channel_id": channel_id_str,
                    "channel_name": row[9],
                    "channel_url": row[10],
                    "owner_user_id": row[11],
                    "brand_id": row[12],
                }
    finally:
        conn.close()

    allowed_channel_ids = set()
    for row in channel_rows:
        owner_id = row[2]
        if owner_scope_user_id and owner_id == owner_scope_user_id:
            allowed_channel_ids.add(str(row[0]))
    processed_entries: List[Dict[str, Any]] = []
    for entry in all_entries:
        video_meta = video_map.get(entry["video_id"], {})
        channel_id = video_meta.get("channel_id")
        short_video_id = entry.get("short_video_id")
        clip_preview_url = None
        clip_filename = entry.get("clip_filename")
        if clip_filename:
            clip_preview_url = _short_public_url(clip_filename)
        thumbnail_url = video_meta.get("thumbnail_url")
        if not thumbnail_url and entry.get("video_id"):
            thumbnail_url = f"https://i.ytimg.com/vi/{entry['video_id']}/hqdefault.jpg"
        processed_entries.append(
            {
                **entry,
                "video_pk": video_meta.get("video_pk"),
                "video_title": video_meta.get("title") or entry.get("plan_title") or short_video_id or entry["video_id"],
                "video_url": video_meta.get("video_url") or f"https://www.youtube.com/watch?v={entry['video_id']}",
                "thumbnail_url": thumbnail_url,
                "view_count": video_meta.get("view_count"),
                "like_count": video_meta.get("like_count"),
                "comment_count": video_meta.get("comment_count"),
                "channel_id": channel_id,
                "channel_name": video_meta.get("channel_name") or channel_map.get(channel_id or "") or "Unknown channel",
                "channel_url": video_meta.get("channel_url"),
                "channel_link": channel_id and url_for("video_shorts_bp.videos_page", channel_id=channel_id),
                "generate_short_link": video_meta.get("video_pk")
                and url_for("video_shorts_bp.generate_short", video_pk=video_meta.get("video_pk")),
                "short_video_id": short_video_id,
                "short_video_url": short_video_id
                and f"https://www.youtube.com/watch?v={short_video_id}",
                "comments_url": short_video_id
                and url_for("video_shorts_bp.shorts_comments", video_id=short_video_id),
                "clip_preview_url": clip_preview_url,
                "short_view_count": None,
                "short_like_count": None,
                "short_comment_count": None,
                "short_thumbnail_url": None,
            }
        )

    video_ids_for_queue = sorted({entry.get("video_id") for entry in processed_entries if entry.get("video_id")})
    instagram_queue_map = load_instagram_queue_map(video_ids_for_queue)
    tiktok_queue_map = load_tiktok_queue_map(video_ids_for_queue)
    facebook_queue_map = load_facebook_queue_map(video_ids_for_queue)
    instagram_media_ids = sorted(
        {
            str(record.get("instagram_media_id")).strip()
            for queued in instagram_queue_map.values()
            for record in queued
            if str(record.get("instagram_media_id") or "").strip()
        }
    )
    instagram_comment_cache: Dict[str, Optional[Dict[str, object]]] = {
        media_id: cache
        for media_id, cache in load_instagram_comment_cache_map(instagram_media_ids).items()
    }
    for entry in processed_entries:
        entry["publish_label"] = (
            _format_display_timestamp(entry.get("publish_at_iso"), user_tz)
            or entry.get("publish_label")
        )
        plan_index_key = str(entry.get("plan_index") or "")
        queued = instagram_queue_map.get((entry.get("video_id"), plan_index_key)) or []
        ig_entries = []
        for record in queued:
            status_label, badge_class = _instagram_status_meta(record.get("status"))
            publish_iso_value = record.get("published_at") or record.get("publish_at")
            media_id = record.get("instagram_media_id")
            if media_id:
                cache = instagram_comment_cache.get(media_id)
            else:
                cache = None
            has_flagged = _has_flagged_instagram_comments(cache.get("comments") if cache else [])
            current_count = record.get("comment_count")
            if not isinstance(current_count, int):
                cached_count = cache.get("comment_count") if cache else None
                if isinstance(cached_count, int):
                    current_count = cached_count
                else:
                    current_count = len(cache.get("comments") or []) if cache else 0
            last_seen = record.get("last_seen_comment_count") or 0
            has_unread = current_count > last_seen
            ig_entries.append(
                {
                    "queue_id": record.get("id"),
                    "status": record.get("status"),
                    "status_label": status_label,
                    "badge_class": badge_class,
                    "publish_display": _format_instagram_publish_display(
                        record.get("published_at") or record.get("publish_at"),
                        user_tz,
                    ),
                    "permalink": record.get("permalink"),
                    "like_count": record.get("like_count"),
                    "comment_count": record.get("comment_count"),
                    "impressions": record.get("impressions"),
                    "reach": record.get("reach"),
                    "saved": record.get("saved"),
                    "shares": record.get("shares"),
                    "media_type_label": _instagram_type_label(record.get("media_type")),
                    "publish_iso": publish_iso_value,
                    "has_flagged_comments": has_flagged,
                    "has_unread_comments": has_unread,
                }
            )
        entry["instagram_entries"] = ig_entries
        tt_queued = tiktok_queue_map.get((entry.get("video_id"), plan_index_key)) or []
        tt_entries = []
        for record in tt_queued:
            status_label, badge_class = _tiktok_status_meta(record.get("status"))
            publish_iso_value = record.get("published_at") or record.get("publish_at")
            raw_error_message = record.get("last_error_message")
            display_error_message = raw_error_message
            if raw_error_message:
                if "unaudited_client_can_only_post_to_private_accounts" in raw_error_message:
                    display_error_message = "Hesap private olmali + SELF_ONLY gerekli."
                elif "SELF_ONLY not available for unaudited client" in raw_error_message:
                    display_error_message = "Hesap private olmali + SELF_ONLY gerekli."
            tt_entries.append(
                {
                    "queue_id": record.get("id"),
                    "status": record.get("status"),
                    "status_label": status_label,
                    "badge_class": badge_class,
                    "publish_display": _format_display_timestamp(
                        record.get("published_at") or record.get("publish_at"),
                        user_tz,
                    ),
                    "tiktok_video_id": record.get("tiktok_video_id"),
                    "publish_iso": publish_iso_value,
                    "last_error_code": record.get("last_error_code"),
                    "last_error_message": display_error_message,
                    "last_error_logid": record.get("last_error_logid"),
                    "last_error_payload": record.get("last_error_payload"),
                    "last_http_status": record.get("last_http_status"),
                    "last_step": record.get("last_step"),
                }
            )
        entry["tiktok_entries"] = tt_entries
        fb_queued = facebook_queue_map.get((entry.get("video_id"), plan_index_key)) or []
        fb_entries = []
        for record in fb_queued:
            status_label, badge_class = _facebook_status_meta(record.get("status"))
            publish_iso_value = record.get("published_at") or record.get("publish_at")
            current_count = record.get("comment_count")
            if not isinstance(current_count, int):
                current_count = 0
            last_seen = record.get("last_seen_comment_count") or 0
            fb_entries.append(
                {
                    "queue_id": record.get("id"),
                    "user_id": record.get("user_id"),
                    "status": record.get("status"),
                    "status_label": status_label,
                    "badge_class": badge_class,
                    "publish_display": _format_display_timestamp(
                        record.get("published_at") or record.get("publish_at"),
                        user_tz,
                    ),
                    "publish_iso": publish_iso_value,
                    "facebook_video_id": record.get("facebook_video_id"),
                    "page_id": record.get("page_id"),
                    "page_name": record.get("page_name"),
                    "media_type": record.get("media_type"),
                    "view_count": record.get("view_count"),
                    "reach": record.get("reach"),
                    "impressions": record.get("impressions"),
                    "reactions": record.get("reactions"),
                    "comment_count": record.get("comment_count"),
                    "last_seen_comment_count": record.get("last_seen_comment_count"),
                    "has_unread_comments": current_count > last_seen,
                }
            )
        entry["facebook_entries"] = fb_entries

    short_video_ids = [
        entry["short_video_id"]
        for entry in processed_entries
        if entry.get("short_video_id")
    ]
    refresh_entries = _select_recent_comment_entries(
        processed_entries,
        COMMENT_COUNT_REFRESH_MAX_VIDEOS,
    )
    short_comment_cache: Dict[str, Dict[str, Any]] = _load_short_comment_cache(short_video_ids)
    for entry in processed_entries:
        counts = short_comment_cache.get(entry.get("short_video_id") or "", {}) if entry.get("short_video_id") else {}
        entry["pending_comment_count"] = counts.get("pending_comment_count", 0)
        entry["published_comment_count"] = counts.get("published_comment_count", 0)
        entry["rejected_comment_count"] = counts.get("rejected_comment_count", 0)
        entry["last_seen_comment_count"] = counts.get("last_seen_comment_count", 0)
        entry["comments_last_synced_at"] = counts.get("comments_last_synced_at")
    last_comment_check = None
    for entry in refresh_entries:
        short_id = entry.get("short_video_id")
        if not short_id:
            continue
        cache_entry = short_comment_cache.get(short_id) or {}
        synced_at = _parse_comments_synced_at(cache_entry.get("comments_last_synced_at"))
        if not synced_at:
            continue
        if not last_comment_check or synced_at > last_comment_check:
            last_comment_check = synced_at
    last_comment_check_display = _format_display_timestamp(last_comment_check, user_tz)
    pending_videos_total = sum(1 for entry in processed_entries if (entry.get("pending_comment_count") or 0) > 0)
    total_pending_comments = sum(entry.get("pending_comment_count") or 0 for entry in processed_entries)

    stats_map: Dict[str, Dict[str, Any]] = {}
    stats_ids = _select_overview_stats_ids(
        processed_entries,
        SHORTS_OVERVIEW_STATS_MAX_VIDEOS,
    )
    if stats_ids:
        stats_conn = get_db()
        try:
            cached_stats, missing_ids = _load_overview_stats_cache(
                stats_conn,
                stats_ids,
                SHORTS_OVERVIEW_STATS_TTL_MINUTES,
            )
            stats_map.update(cached_stats)
            quota_until, _quota_reason = _get_overview_quota_state(stats_conn)
            quota_active = quota_until and quota_until > datetime.utcnow()
            if missing_ids and not quota_active:
                try:
                    cache_count = _get_overview_cache_count(stats_conn)
                    if cache_count <= 0:
                        fetch_max = max(1, SHORTS_OVERVIEW_FIRST_FILL_MAX_VIDEOS)
                    else:
                        fetch_max = SHORTS_OVERVIEW_STATS_MAX_VIDEOS
                    missing_ids = missing_ids[:fetch_max]
                    fetched_stats = fetch_video_stats(missing_ids)
                    stats_map.update(fetched_stats)
                    _upsert_overview_stats_cache(stats_conn, fetched_stats)
                except YoutubeApiError as exc:
                    if _is_quota_exhausted_error(exc):
                        err_code, err_reason, err_message, err_domain = _extract_youtube_error_details(
                            exc
                        )
                        cooldown_until = _quota_cooldown_until(err_reason or _quota_reason_from_error(exc))
                        if cooldown_until:
                            _set_overview_quota_exhausted_until(
                                stats_conn,
                                cooldown_until,
                                _quota_reason_from_error(exc),
                                last_error_code=err_code,
                                last_error_reason=err_reason,
                                last_error_message=err_message,
                                last_error_domain=err_domain,
                                last_error_at=datetime.utcnow(),
                            )
                    current_app.logger.warning(
                        "YouTube stats fetch failed in shorts overview: %s",
                        exc,
                    )
        finally:
            stats_conn.close()
    for entry in processed_entries:
        short_id = entry.get("short_video_id")
        stats = stats_map.get(short_id) if short_id else None
        if short_id and stats:
            entry["short_view_count"] = stats.get("view_count")
            entry["short_like_count"] = stats.get("like_count")
            entry["short_comment_count"] = stats.get("comment_count")
            entry["short_thumbnail_url"] = stats.get("thumbnail_url")
        if short_id and not entry["short_thumbnail_url"]:
            entry["short_thumbnail_url"] = f"https://i.ytimg.com/vi/{short_id}/hqdefault.jpg"
        if entry["publish_status"] == "scheduled" and entry.get("short_video_id"):
            entry["short_thumbnail_url"] = entry["short_thumbnail_url"] or f"https://i.ytimg.com/vi/{entry['short_video_id']}/hqdefault.jpg"
        elif entry["publish_status"] == "scheduled" and entry.get("video_id"):
            entry["short_thumbnail_url"] = entry["short_thumbnail_url"] or f"https://i.ytimg.com/vi/{entry['video_id']}/hqdefault.jpg"
        if short_id and entry.get("publish_status") != "published":
            comment_activity = (
                (entry.get("pending_comment_count") or 0)
                + (entry.get("published_comment_count") or 0)
                + (entry.get("rejected_comment_count") or 0)
            )
            if stats or comment_activity > 0:
                entry["publish_status"] = "published"
                entry["publish_status_label"] = "Published"
        total_comments = (entry.get("pending_comment_count") or 0) + (
            entry.get("published_comment_count") or 0
        )
        if total_comments == 0 and entry.get("short_comment_count") is not None:
            total_comments = entry.get("short_comment_count") or 0
        last_seen = entry.get("last_seen_comment_count") or 0
        entry["has_unread_comments"] = total_comments > last_seen
        entry["has_any_unread_comments"] = bool(entry.get("has_unread_comments")) or any(
            ig_entry.get("has_unread_comments") for ig_entry in (entry.get("instagram_entries") or [])
        ) or any(
            fb_entry.get("has_unread_comments") for fb_entry in (entry.get("facebook_entries") or [])
        )

    filtered_entries = processed_entries
    filtered_entries = [
        entry
        for entry in filtered_entries
        if entry.get("channel_id") and str(entry.get("channel_id")) in allowed_channel_ids
    ]
    total_scheduled = sum(1 for entry in filtered_entries if entry["publish_status"] == "scheduled")
    total_published = sum(1 for entry in filtered_entries if entry["publish_status"] == "published")
    pending_videos_total = sum(1 for entry in filtered_entries if (entry.get("pending_comment_count") or 0) > 0)
    total_pending_comments = sum(entry.get("pending_comment_count") or 0 for entry in filtered_entries)
    unread_videos_total = sum(
        1 for entry in filtered_entries if entry.get("has_any_unread_comments")
    )
    if status_filter in {"scheduled", "published"}:
        filtered_entries = [
            entry for entry in filtered_entries if entry["publish_status"] == status_filter
        ]
    if channel_filter:
        filtered_entries = [
            entry
            for entry in filtered_entries
            if entry.get("channel_id") is not None and str(entry["channel_id"]) == channel_filter
        ]
    if search_q:
        needle = search_q.lower()
        filtered_entries = [
            entry
            for entry in filtered_entries
            if needle in (entry.get("video_title") or "").lower()
            or needle in (entry.get("plan_title") or "").lower()
        ]
    if pending_only:
        filtered_entries = [
            entry
            for entry in filtered_entries
            if (entry.get("pending_comment_count") or 0) > 0
        ]
    if comments_filter == "new":
        filtered_entries = [
            entry
            for entry in filtered_entries
            if entry.get("has_any_unread_comments")
        ]
    def _entry_sort_key(entry: Dict[str, Any]) -> Any:
        if sort_key == "title":
            return (entry.get("video_title") or "").lower()
        if sort_key == "channel":
            return (entry.get("channel_name") or "").lower()
        if sort_key == "plan":
            return (entry.get("plan_title") or "").lower()
        if sort_key == "status":
            return (entry.get("publish_status") or "").lower()
        return entry.get("publish_sort_key") or ""

    filtered_entries.sort(key=_entry_sort_key, reverse=sort_dir == "desc")

    calendar_summary: Dict[str, Dict[str, Dict[str, int]]] = {}

    def _platform_bucket(day_key: str, platform: str) -> Dict[str, int]:
        day_summary = calendar_summary.setdefault(day_key, {})
        return day_summary.setdefault(platform, {"scheduled": 0, "published": 0})

    for entry in filtered_entries:
        publish_iso = entry.get("publish_at_iso")
        publish_dt = _normalize_timestamp(publish_iso)
        if publish_dt:
            date_key = publish_dt.date().isoformat()
            status_key = (entry.get("publish_status") or "").lower()
            if status_key in {"scheduled", "published"}:
                bucket = _platform_bucket(date_key, "youtube")
                bucket[status_key] += 1

        for ig_entry in entry.get("instagram_entries") or []:
            ig_publish_iso = ig_entry.get("publish_iso")
            ig_dt = _normalize_timestamp(ig_publish_iso)
            if not ig_dt:
                continue
            date_key = ig_dt.date().isoformat()
            status_value = (ig_entry.get("status") or "").lower()
            bucket = _platform_bucket(date_key, "instagram")
            if status_value == "published":
                bucket["published"] += 1
            elif status_value in {"pending", "retry", "uploading"}:
                bucket["scheduled"] += 1
        for tt_entry in entry.get("tiktok_entries") or []:
            tt_publish_iso = tt_entry.get("publish_iso")
            tt_dt = _normalize_timestamp(tt_publish_iso)
            if not tt_dt:
                continue
            date_key = tt_dt.date().isoformat()
            status_value = (tt_entry.get("status") or "").lower()
            bucket = _platform_bucket(date_key, "tiktok")
            if status_value == "published":
                bucket["published"] += 1
            elif status_value in {"pending", "retry", "uploading"}:
                bucket["scheduled"] += 1
        for fb_entry in entry.get("facebook_entries") or []:
            fb_publish_iso = fb_entry.get("publish_iso")
            fb_dt = _normalize_timestamp(fb_publish_iso)
            if not fb_dt:
                continue
            date_key = fb_dt.date().isoformat()
            status_value = (fb_entry.get("status") or "").lower()
            bucket = _platform_bucket(date_key, "facebook")
            if status_value == "published":
                bucket["published"] += 1
            elif status_value in {"pending", "retry", "uploading"}:
                bucket["scheduled"] += 1

    if day_filter:
        filtered_entries = [
            entry
            for entry in filtered_entries
            if _normalize_timestamp(entry.get("publish_at_iso"))
            and _normalize_timestamp(entry.get("publish_at_iso")).date().isoformat() == day_filter
        ]

    try:
        page = max(1, int(request.args.get("page", "1")))
    except ValueError:
        page = 1
    PAGE_SIZE = 50
    total_count = len(filtered_entries)
    total_pages = max(1, (total_count + PAGE_SIZE - 1) // PAGE_SIZE)
    start_idx = (page - 1) * PAGE_SIZE
    end_idx = start_idx + PAGE_SIZE
    paged_entries = filtered_entries[start_idx:end_idx]
    showing_from = start_idx + 1 if total_count else 0
    showing_to = min(start_idx + len(paged_entries), total_count)

    channel_options = [
        {"id": row[0], "name": row[1] or f"Channel {row[0]}"}
        for row in channel_rows
        if str(row[0]) in allowed_channel_ids
    ]

    return render_template(
        "shorts_overview.html",
        entries=paged_entries,
        total_scheduled=total_scheduled,
        total_published=total_published,
        entries_count=total_count,
        status_filter=status_filter,
        comments_filter=comments_filter,
        search_q=search_q,
        channel_filter=channel_filter,
        sort_key=sort_key,
        sort_dir=sort_dir,
        channel_options=channel_options,
        page=page,
        total_pages=total_pages,
        showing_from=showing_from,
        showing_to=showing_to,
        page_size=PAGE_SIZE,
        pending_only=pending_only,
        pending_only_query="1" if pending_only else None,
        refresh_stats=refresh_stats,
        pending_videos_total=pending_videos_total,
        calendar_summary=calendar_summary,
        day_filter=day_filter,
        total_pending_comments=total_pending_comments,
        last_comment_check_display=last_comment_check_display,
        unread_videos_total=unread_videos_total,
    )


@video_shorts_bp.route("/shorts/comments")
def shorts_comments_page():
    current_user = getattr(g, "vs_current_user", None)
    if not current_user:
        return redirect(url_for("video_shorts_bp.login", next=request.url))
    brand_id = current_brand_id()
    brand_owner_user_id, _brand_name = _load_brand_scope_context(brand_id)
    owner_scope_user_id = brand_owner_user_id or current_user["id"]
    user_tz = (current_user or {}).get("time_zone") or DEFAULT_TIME_ZONE
    status_filters = _parse_multi_filter_values("status")
    platform_filters = _parse_multi_filter_values("platform")
    status_filter = ",".join(status_filters) if status_filters else "all"
    platform_filter = ",".join(platform_filters) if platform_filters else "all"
    sort_key = (request.args.get("sort") or "date").strip().lower()
    sort_dir = (request.args.get("dir") or "desc").strip().lower()
    sort_dir = "asc" if sort_dir == "asc" else "desc"
    should_sync = (request.args.get("sync") or "").strip().lower() in {"1", "true", "yes"}
    allowed_video_ids = _build_allowed_comment_video_ids()
    missing = fetch_comments_missing_moderation(owner_scope_user_id, limit=200)
    if missing:
        moderation_entries = [
            {
                "id": f"{row['platform']}:{row['comment_id']}",
                "text": row.get("text") or "",
            }
            for row in missing
            if row.get("comment_id") and row.get("text")
        ]
        moderation_map = moderate_text_entries(moderation_entries, owner_scope_user_id)
        updates = []
        for row in missing:
            key = f"{row['platform']}:{row['comment_id']}"
            moderation = moderation_map.get(key)
            if not moderation:
                continue
            updates.append(
                {
                    "platform": row["platform"],
                    "comment_id": row["comment_id"],
                    "moderation_flagged": moderation.get("flagged"),
                    "moderation_reason": moderation.get("reason"),
                }
            )
        update_comment_moderation(updates)
    selected_platforms = set(platform_filters)
    include_all_platforms = not selected_platforms
    include_youtube = include_all_platforms or "youtube" in selected_platforms
    if include_youtube and should_sync:
        latest_by_video = fetch_latest_comment_timestamps(
            owner_scope_user_id,
            platform="youtube",
        )
        _sync_youtube_comments_for_user(
            {**current_user, "id": owner_scope_user_id},
            max_videos=12,
            latest_by_video=latest_by_video,
        )
    comments = fetch_comment_records_for_video_ids(
        sorted(allowed_video_ids),
        limit=2000,
        status=status_filters,
        platform=platform_filters,
        sort_key=sort_key,
        sort_dir=sort_dir,
        owner_user_id=owner_scope_user_id,
    )
    brand_title_map = _build_short_title_map()
    comments = [comment for comment in comments if str(comment.get("video_id") or "") in allowed_video_ids]
    if include_youtube:
        _apply_short_title_fallback(comments, brand_title_map)
    for comment in comments:
        status_label, status_badge = _comment_status_meta(comment.get("status"))
        comment["status_label"] = status_label
        comment["status_badge"] = status_badge
    return render_template(
        "shorts_comments.html",
        comments=comments,
        status_filter=status_filter,
        platform_filter=platform_filter,
        selected_status_filters=status_filters,
        selected_platform_filters=platform_filters,
        sort_key=sort_key,
        sort_dir=sort_dir,
    )


@video_shorts_bp.route("/shorts/social-logs")
def shorts_social_logs():
    current_user = getattr(g, "vs_current_user", None)
    if not current_user:
        return redirect(url_for("video_shorts_bp.login", next=request.url))
    if current_user.get("role") != "admin":
        abort(403)
    try:
        limit = int(request.args.get("limit", "200"))
    except ValueError:
        limit = 200
    limit = max(1, min(limit, 1000))
    log_path = Path(__file__).resolve().parents[3] / "logs" / "social_all_errors.log"
    lines = _tail_log_lines(log_path, limit=limit)
    rows = _parse_social_error_lines(lines)
    run_log_path = Path(__file__).resolve().parents[3] / "logs" / "social_all.log"
    last_run = _extract_last_social_run(run_log_path)
    return render_template(
        "social_logs.html",
        log_path=str(log_path),
        lines=lines,
        rows=rows,
        last_run=last_run,
        limit=limit,
    )


@video_shorts_bp.route("/shorts/comments/<video_id>")
def shorts_comments(video_id):
    if not video_id:
        return jsonify(success=False, message="Video ID is required"), 400
    current_user = getattr(g, "vs_current_user", None)
    if not current_user:
        return jsonify(success=False, message="Unauthorized"), 401
    is_admin = current_user.get("role") == "admin"
    active_brand_id = current_brand_id()
    brand_owner_user_id, _brand_name = _load_brand_scope_context(active_brand_id)
    video_title, owner_user_id, video_brand_id, video_channel_id = _load_video_scope(video_id)
    if not owner_user_id:
        owner_user_id = _resolve_owner_for_short_id(video_id)
    if is_admin and not owner_user_id:
        owner_user_id = brand_owner_user_id or current_user.get("id")
        if not video_title:
            video_title = video_id
    if active_brand_id and video_brand_id and str(video_brand_id) != str(active_brand_id):
        return jsonify(success=False, message="Forbidden"), 403
    preferred_channel_ids = _preferred_brand_channel_ids(brand_owner_user_id or owner_user_id, active_brand_id)
    if preferred_channel_ids and video_channel_id and video_channel_id not in preferred_channel_ids:
        return jsonify(success=False, message="Forbidden"), 403
    if not is_admin and owner_user_id and owner_user_id != current_user.get("id"):
        return jsonify(success=False, message="Forbidden"), 403
    if not is_admin and not owner_user_id:
        return jsonify(success=False, message="Forbidden"), 403
    requested_status = (request.args.get("status") or "all").strip() or "all"
    refresh = _parse_bool(request.args.get("refresh"), default=False)
    if refresh and owner_user_id:
        try:
            _sync_youtube_comments_for_video(
                owner_user_id,
                video_id,
                video_title=video_title,
            )
        except Exception:
            current_app.logger.exception("Failed to refresh comments for %s", video_id)
    try:
        comments = fetch_comment_records_for_video(
            owner_user_id,
            video_id=video_id,
            limit=250,
            status=requested_status,
            platform="youtube",
            sort_key="date",
            sort_dir="desc",
        )
        title_map = _build_short_title_map()
        _apply_short_title_fallback(comments, title_map)
        comments = _thread_youtube_comment_rows(comments)
        summary_counts = None
        if requested_status == "all" and comments:
            summary_counts = _summarize_comment_counts_for_entries(comments)
            _upsert_short_comment_counts(video_id, summary_counts)
        return jsonify(
            success=True,
            comments=comments,
            status=requested_status,
            counts=summary_counts,
        )
    except Exception:
        current_app.logger.exception("Failed to load comments for %s", video_id)
        return jsonify(success=False, message="Failed to load comments"), 500


@video_shorts_bp.route("/shorts/comments/<video_id>/cache_counts", methods=["POST"])
def shorts_comment_cache_counts(video_id):
    if not video_id:
        return jsonify(success=False, message="Video ID is required"), 400
    data = request.get_json(silent=True) or {}

    def _safe_count(key):
        val = data.get(key, 0)
        try:
            return max(0, int(val))
        except (TypeError, ValueError):
            return 0

    summary = {
        "pending": _safe_count("pending"),
        "published": _safe_count("published"),
        "rejected": _safe_count("rejected"),
    }
    _upsert_short_comment_counts(video_id, summary)
    mark_seen = _parse_bool(data.get("mark_seen"), default=False)
    last_seen_count = None
    if mark_seen:
        last_seen_count = summary.get("pending", 0) + summary.get("published", 0)
        _update_short_comment_last_seen_count(video_id, last_seen_count)
    payload = {"success": True, "counts": summary}
    if last_seen_count is not None:
        payload["last_seen_comment_count"] = last_seen_count
    return jsonify(payload)


@video_shorts_bp.route("/shorts/comments/<comment_id>/approve", methods=["POST"])
def shorts_comment_approve(comment_id):
    _, error = _ensure_comment_owner_access("youtube", comment_id)
    if error:
        return error
    data = request.get_json(silent=True) or {}
    previous_status = (data.get("previous_status") or "").strip()
    video_id = (data.get("video_id") or "").strip()
    try:
        youtube = _require_youtube_client()
        youtube.comments().setModerationStatus(
            id=comment_id,
            moderationStatus="published",
        ).execute()
        update_comment_status("youtube", comment_id, "published")
        counts = None
        if video_id:
            delta = _status_transition_delta(previous_status, "published")
            counts = _adjust_short_comment_counts(video_id, delta)
        return jsonify(success=True, counts=counts)
    except YoutubeApiError as exc:
        return jsonify(success=False, message=str(exc)), 400
    except Exception as exc:
        current_app.logger.exception("Failed to approve comment %s", comment_id)
        return jsonify(success=False, message=str(exc)), 500


@video_shorts_bp.route("/shorts/comments/<comment_id>/reject", methods=["POST"])
def shorts_comment_reject(comment_id):
    _, error = _ensure_comment_owner_access("youtube", comment_id)
    if error:
        return error
    data = request.get_json(silent=True) or {}
    previous_status = (data.get("previous_status") or "").strip()
    video_id = (data.get("video_id") or "").strip()
    try:
        youtube = _require_youtube_client()
        youtube.comments().setModerationStatus(
            id=comment_id,
            moderationStatus="rejected",
        ).execute()
        update_comment_status("youtube", comment_id, "rejected")
        counts = None
        if video_id:
            delta = _status_transition_delta(previous_status, "rejected")
            counts = _adjust_short_comment_counts(video_id, delta)
        return jsonify(success=True, counts=counts)
    except YoutubeApiError as exc:
        return jsonify(success=False, message=str(exc)), 400
    except Exception as exc:
        current_app.logger.exception("Failed to reject comment %s", comment_id)
        return jsonify(success=False, message=str(exc)), 500


@video_shorts_bp.route("/shorts/comments/<comment_id>/hide", methods=["POST"])
def shorts_comment_hide(comment_id):
    _, error = _ensure_comment_owner_access("youtube", comment_id)
    if error:
        return error
    data = request.get_json(silent=True) or {}
    previous_status = (data.get("previous_status") or "").strip()
    video_id = (data.get("video_id") or "").strip()
    try:
        youtube = _require_youtube_client()
        youtube.comments().setModerationStatus(
            id=comment_id,
            moderationStatus="rejected",
            banAuthor=True,
        ).execute()
        update_comment_status("youtube", comment_id, "rejected")
        counts = None
        if video_id:
            delta = _status_transition_delta(previous_status, "rejected")
            counts = _adjust_short_comment_counts(video_id, delta)
        return jsonify(success=True, counts=counts)
    except YoutubeApiError as exc:
        return jsonify(success=False, message=str(exc)), 400
    except Exception as exc:
        current_app.logger.exception("Failed to hide comment author %s", comment_id)
        return jsonify(success=False, message=str(exc)), 500


@video_shorts_bp.route("/shorts/comments/<comment_id>", methods=["DELETE"])
def shorts_comment_delete(comment_id):
    _, error = _ensure_comment_owner_access("youtube", comment_id)
    if error:
        return error
    data = request.get_json(silent=True) or {}
    previous_status = (data.get("previous_status") or "").strip()
    video_id = (data.get("video_id") or "").strip()
    thread_id = (data.get("thread_id") or "").strip()
    is_reply = bool(data.get("is_reply"))

    def _is_processing_failure(exc: Exception) -> bool:
        return "processingFailure" in str(exc)

    try:
        youtube = _require_youtube_client()
        try:
            youtube.comments().delete(id=comment_id).execute()
        except Exception as exc:
            if thread_id and not is_reply and _is_processing_failure(exc):
                youtube.commentThreads().delete(id=thread_id).execute()
            else:
                raise
        delete_comment_record("youtube", comment_id)
        counts = None
        if video_id:
            delta = _removal_delta(previous_status)
            counts = _adjust_short_comment_counts(video_id, delta)
        return jsonify(success=True, counts=counts)
    except YoutubeApiError as exc:
        return jsonify(success=False, message=str(exc)), 400
    except Exception as exc:
        current_app.logger.exception("Failed to delete comment %s", comment_id)
        return jsonify(success=False, message=str(exc)), 500


@video_shorts_bp.route("/shorts/comments/<comment_id>/reply", methods=["POST"])
def shorts_comment_reply(comment_id):
    _, error = _ensure_comment_owner_access("youtube", comment_id)
    if error:
        return error
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify(success=False, message="Yanıt metni boş olamaz"), 400
    try:
        youtube = _require_youtube_client()
        response = youtube.comments().insert(
            part="snippet",
            body={
                "snippet": {
                    "parentId": comment_id,
                    "textOriginal": text,
                }
            },
        ).execute()
        snippet = response.get("snippet") or {}
        new_comment = {
            "author": snippet.get("authorDisplayName") or "Siz",
            "text": snippet.get("textDisplay") or snippet.get("textOriginal") or text,
            "published_at": snippet.get("publishedAt") or snippet.get("updatedAt"),
            "like_count": snippet.get("likeCount") or 0,
            "comment_id": response.get("id"),
            "comment_url": (
                f"https://www.youtube.com/watch?v={snippet.get('videoId')}&lc={response.get('id')}"
                if snippet.get("videoId") and response.get("id")
                else None
            ),
            "status": snippet.get("moderationStatus") or "published",
            "parent_id": snippet.get("parentId") or comment_id,
            "is_reply": True,
        }
        bucket = _normalize_status_for_bucket(new_comment.get("status") or "published")
        counts = None
        if bucket and snippet.get("videoId"):
            delta = {"pending": 0, "published": 0, "rejected": 0}
            delta[bucket] = 1
            counts = _adjust_short_comment_counts(snippet.get("videoId"), delta)
        return jsonify(success=True, comment=new_comment, counts=counts)
    except YoutubeApiError as exc:
        return jsonify(success=False, message=str(exc)), 400
    except Exception as exc:
        current_app.logger.exception("Failed to reply to comment %s", comment_id)
        return jsonify(success=False, message=str(exc)), 500


@video_shorts_bp.route("/videos/<int:video_pk>/toggle_transcript", methods=["POST"])
def toggle_video_transcript(video_pk):
    conn = get_db()
    row = conn.execute(
        "SELECT channel_id, fetch_transcript FROM youtube_videos WHERE id = ?",
        [video_pk],
    ).fetchone()
    if not row:
        conn.close()
        flash("Video not found", "danger")
        return redirect(url_for("video_shorts_bp.channels_page"))

    channel_id, current_flag = row
    new_flag = 0 if current_flag else 1

    conn.execute(
        "UPDATE youtube_videos SET fetch_transcript = ? WHERE id = ?",
        [new_flag, video_pk],
    )
    conn.close()

    msg = "Transcript fetch disabled for video." if new_flag == 0 else "Transcript fetch enabled for video."
    flash(msg, "success")
    return redirect(url_for("video_shorts_bp.videos_page", channel_id=channel_id))


@video_shorts_bp.route("/videos/<int:channel_id>/transcripts", methods=["POST"])
def bulk_update_transcripts(channel_id):
    # which checkboxes were selected
    raw_ids = request.form.getlist("fetch_transcript_ids")
    selected_ids = []
    for rid in raw_ids:
        try:
            selected_ids.append(int(rid))
        except Exception:
            continue

    conn = get_db()
    # reset all to false for this channel
    conn.execute(
        "UPDATE youtube_videos SET fetch_transcript = FALSE WHERE channel_id = ?",
        [channel_id],
    )

    updated = 0
    if selected_ids:
        placeholders = ",".join("?" * len(selected_ids))
        params = [channel_id] + selected_ids
        conn.execute(
            f"UPDATE youtube_videos SET fetch_transcript = TRUE WHERE channel_id = ? AND id IN ({placeholders})",
            params,
        )
        updated = len(selected_ids)

    conn.commit()
    conn.close()
    flash(f"Caption readiness updated for {updated} video(s).", "success")
    return redirect(url_for("video_shorts_bp.videos_page", channel_id=channel_id))


@video_shorts_bp.route("/videos/<int:video_pk>/download_status", methods=["POST"])
def update_download_status(video_pk):
    new_status = (request.form.get("download_status") or "").strip().lower()
    allowed = {"not_needed", "pending", "downloaded", "short", "irrelevant"}
    redirect_params = {
        "sort": request.form.get("sort", "published"),
        "dir": request.form.get("dir", "desc"),
        "page": request.form.get("page", "1"),
        "q": request.form.get("q", ""),
        "dstatus": request.form.get("dstatus", ""),
        "dmin": request.form.get("dmin", ""),
        "dmax": request.form.get("dmax", ""),
    }
    if new_status not in allowed:
        flash("Invalid download status.", "danger")
        conn = get_db_readonly()
        row = conn.execute("SELECT channel_id FROM youtube_videos WHERE id = ?", [video_pk]).fetchone()
        conn.close()
        chan = row[0] if row else None
        if chan:
            return redirect(url_for("video_shorts_bp.videos_page", channel_id=chan, **redirect_params))
        return redirect(url_for("video_shorts_bp.channels_page"))

    success_message = "Kaydedildi"
    conn = get_db()
    _ensure_video_crop_schema(conn)
    row = conn.execute("SELECT channel_id FROM youtube_videos WHERE id = ?", [video_pk]).fetchone()
    if not row:
        conn.close()
        flash("Video not found.", "danger")
        return redirect(url_for("video_shorts_bp.channels_page"))
    channel_id = row[0]
    try:
        conn.execute(
            """
            UPDATE youtube_videos
            SET download_status = ?,
                downloaded_at = CASE WHEN ? = 'downloaded' THEN CURRENT_TIMESTAMP ELSE NULL END
            WHERE id = ?
            """,
            [new_status, new_status, video_pk],
        )
        conn.commit()
        flash(success_message, "success")
    except Exception as e:
        flash(f"Update failed: {e}", "danger")
    finally:
        conn.close()

    redirect_params["dstatus"] = request.form.get("dstatus", "")
    return redirect(url_for("video_shorts_bp.videos_page", channel_id=channel_id, **redirect_params))
