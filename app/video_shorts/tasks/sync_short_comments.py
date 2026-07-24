#!/usr/bin/env python3
"""
Refreshes cached comment counts for all published/scheduled shorts.
Also captures YouTube/Instagram/Facebook/TikTok follower/subscriber snapshots
into shorts_channel_subscriber_daily.

Intended for cron usage, e.g.
*/5 * * * * /path/to/venv/bin/python /home/ubuntu/blog-factory/app/video_shorts/tasks/sync_short_comments.py
"""
from __future__ import annotations

import logging
import os
import pathlib
import sys
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set, Tuple

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app import create_app
from app.video_shorts.routes.videos import (
    _collect_short_broadcast_entries,
    _merge_youtube_comments,
    _upsert_short_comment_counts,
    _upsert_short_comment_platform_total,
)
from app.video_shorts.services.comment_store import (
    fetch_latest_comment_timestamps,
    fetch_top_level_comment_counts,
)
from app.video_shorts.services.comment_moderation import moderate_text_entries
from app.video_shorts.services.comment_store import upsert_comment_records
from app.video_shorts.services.db import (
    ensure_channel_owner_schema,
    get_db,
    get_db_readonly,
    table_columns,
)
from app.video_shorts.services.facebook_queue import update_facebook_queue_metrics, load_facebook_queue_map
from app.video_shorts.services.instagram_queue import load_instagram_queue_map
from app.video_shorts.services.instagram_api import refresh_instagram_media, InstagramActionError
from app.video_shorts.services.ai_video_workspace import list_ai_broadcast_entries
from app.video_shorts.services.brands import brand_scoped_user_id
from app.video_shorts.tasks.daily_video_metrics_snapshot import capture_daily_snapshot
from app.video_shorts.tasks.daily_subscriber_snapshot import capture_daily_subscriber_snapshot
from app.video_shorts.services.youtube_oauth import resolve_token_lookup_user_id
from app.video_shorts.youtube_api import YoutubeApiError, fetch_video_comments, fetch_video_stats
from app.video_shorts.tasks.sync_instagram_metrics import _fetch_facebook_metrics
from src.trends.facebook_page_tokens import get_facebook_page_data

logger = logging.getLogger(__name__)

COMMENT_COUNT_SYNC_RECENT_SIZE = int(os.getenv("SHORT_COMMENT_COUNT_SYNC_LIMIT", "100"))
YOUTUBE_COMMENT_FETCH_MAX_RESULTS = 50


def _unique_short_ids(entries: List[Dict[str, object]]) -> List[str]:
    seen = set()
    result: List[str] = []
    for entry in entries:
        short_id = entry.get("short_video_id")
        if not short_id or short_id in seen:
            continue
        seen.add(short_id)
        result.append(short_id)
    return result


def _select_recent_entries(
    entries: List[Dict[str, object]],
    max_items: int,
) -> List[Dict[str, object]]:
    if not entries or max_items <= 0:
        return []
    sorted_entries = sorted(
        entries,
        key=lambda item: item.get("publish_sort_key") or "",
        reverse=True,
    )
    picked: List[Dict[str, object]] = []
    seen: set = set()
    for entry in sorted_entries:
        short_id = entry.get("short_video_id")
        unique_key = short_id or f"{entry.get('video_id')}:{entry.get('plan_index')}"
        if not unique_key or unique_key in seen:
            continue
        picked.append(entry)
        seen.add(unique_key)
        if len(picked) >= max_items:
            break
    return picked


def _normalize_comment_count(value: object) -> Optional[int]:
    if value is None:
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def _should_fetch_youtube_comments(
    short_id: str,
    current_comment_count: Optional[int],
    sync_state_entry: Optional[Dict[str, object]],
) -> bool:
    # Future per-user quota budgeting/fairness should hook into this decision point.
    if current_comment_count is None:
        logger.info(
            "Fetching YouTube comments for %s because current comment count is unavailable.",
            short_id,
        )
        return True
    synced_count = None
    if sync_state_entry:
        synced_count = _normalize_comment_count(sync_state_entry.get("last_comment_count"))
    if synced_count is None:
        logger.info(
            "Fetching YouTube comments for %s because no body-synced comment count is stored (observed=%s).",
            short_id,
            current_comment_count,
        )
        return True
    if synced_count != current_comment_count:
        logger.info(
            "Fetching YouTube comments for %s because observed comment_count differs from body-synced count %s -> %s.",
            short_id,
            synced_count,
            current_comment_count,
        )
        return True
    logger.info(
        "Skipping YouTube comment fetch for %s because observed comment_count matches body-synced count at %s.",
        short_id,
        current_comment_count,
    )
    return False


def _youtube_comment_body_target(current_comment_count: Optional[int]) -> Optional[int]:
    normalized = _normalize_comment_count(current_comment_count)
    if normalized is None:
        return None
    return min(normalized, YOUTUBE_COMMENT_FETCH_MAX_RESULTS)


def _should_fetch_youtube_comment_bodies(
    short_id: str,
    current_comment_count: Optional[int],
    sync_state_entry: Optional[Dict[str, object]],
    cached_top_level_count: Optional[int],
) -> bool:
    if _should_fetch_youtube_comments(short_id, current_comment_count, sync_state_entry):
        return True
    target_count = _youtube_comment_body_target(current_comment_count)
    cached_count = _normalize_comment_count(cached_top_level_count) or 0
    if target_count is not None and cached_count < target_count:
        logger.info(
            "Fetching YouTube comments for %s because cached top-level bodies are incomplete (%s/%s target rows).",
            short_id,
            cached_count,
            target_count,
        )
        return True
    logger.info(
        "Skipping YouTube comment fetch for %s because cached top-level bodies are complete (%s/%s target rows).",
        short_id,
        cached_count,
        target_count if target_count is not None else "unknown",
    )
    return False


def _resolve_short_oauth_user_ids(
    short_owner_context: Dict[str, Dict[str, Optional[str]]],
) -> Dict[str, Optional[str]]:
    resolved: Dict[str, Optional[str]] = {}
    for short_id, context in short_owner_context.items():
        owner_user_id = str(context.get("owner_user_id") or "").strip()
        brand_id = str(context.get("brand_id") or "").strip() or None
        if not owner_user_id:
            resolved[short_id] = None
            continue
        if brand_id:
            resolved[short_id] = brand_scoped_user_id(owner_user_id, brand_id=brand_id)
            continue
        lookup_user_id, resolution = resolve_token_lookup_user_id(owner_user_id, brand_id=None)
        if not lookup_user_id and resolution == "ambiguous_scoped_tokens":
            logger.warning(
                "Skipping YouTube owner OAuth resolution for short_id=%s owner_user_id=%s because brand is unknown and multiple scoped token rows exist.",
                short_id,
                owner_user_id,
            )
        resolved[short_id] = lookup_user_id
    return resolved


def _sync_youtube_comment_totals(
    short_ids: List[str],
    comment_count_map: Dict[str, Optional[int]],
    sync_updates: Dict[str, Dict[str, object]],
) -> int:
    if not short_ids:
        return 0
    refreshed = 0
    observed_at = datetime.now(timezone.utc)
    for short_id in short_ids:
        total = _normalize_comment_count(comment_count_map.get(short_id))
        if total is None:
            continue
        _upsert_short_comment_platform_total(short_id, total)
        sync_updates.setdefault(short_id, {}).update(
            {
                "observed_comment_count": total,
                "observed_comment_count_at": observed_at,
            }
        )
        refreshed += 1
    return refreshed


def _sync_instagram_comment_counts(entries: List[Dict[str, object]]) -> int:
    video_ids = sorted({entry.get("video_id") for entry in entries if entry.get("video_id")})
    if not video_ids:
        return 0
    instagram_queue_map = load_instagram_queue_map(video_ids)
    refreshed = 0
    for entry in entries:
        video_id = entry.get("video_id")
        if not video_id:
            continue
        plan_index_key = str(entry.get("plan_index") or "")
        queued = instagram_queue_map.get((video_id, plan_index_key)) or []
        for record in queued:
            queue_id = record.get("id")
            if not queue_id or not record.get("instagram_media_id"):
                continue
            try:
                refresh_instagram_media(queue_id, comments_limit=0)
                refreshed += 1
            except InstagramActionError as exc:
                logger.warning(
                    "Instagram comment count sync failed queue_id=%s: %s",
                    queue_id,
                    exc,
                )
    return refreshed


def _sync_facebook_comment_counts(entries: List[Dict[str, object]]) -> int:
    video_ids = sorted({entry.get("video_id") for entry in entries if entry.get("video_id")})
    if not video_ids:
        return 0
    facebook_queue_map = load_facebook_queue_map(video_ids)
    refreshed = 0
    for entry in entries:
        video_id = entry.get("video_id")
        if not video_id:
            continue
        plan_index_key = str(entry.get("plan_index") or "")
        queued = facebook_queue_map.get((video_id, plan_index_key)) or []
        for record in queued:
            queue_id = record.get("id")
            fb_video_id = record.get("facebook_video_id")
            user_id = record.get("user_id")
            if not queue_id or not fb_video_id or not user_id:
                continue
            page_info = get_facebook_page_data(user_id)
            if not page_info or not page_info.get("page_access_token"):
                continue
            try:
                metrics = _fetch_facebook_metrics(
                    fb_video_id,
                    page_info.get("page_access_token"),
                )
            except Exception as exc:
                logger.warning(
                    "Facebook comment count sync failed video_id=%s: %s",
                    fb_video_id,
                    exc,
                )
                continue
            update_facebook_queue_metrics(
                queue_id,
                facebook_video_id=fb_video_id,
                permalink=metrics.get("permalink"),
                view_count=metrics.get("view_count"),
                reach=metrics.get("reach"),
                impressions=metrics.get("impressions"),
                reactions=metrics.get("reactions"),
                comment_count=metrics.get("comment_count"),
            )
            refreshed += 1
    return refreshed


def _load_short_owner_context(entries: List[Dict[str, object]]) -> Dict[str, Dict[str, Optional[str]]]:
    video_ids = sorted({entry.get("video_id") for entry in entries if entry.get("video_id")})
    if not video_ids:
        return {}
    conn = get_db_readonly()
    try:
        ensure_channel_owner_schema(conn)
        placeholders = ", ".join("?" for _ in video_ids)
        rows = conn.execute(
            f"""
            SELECT video_id, owner_user_id, brand_id
            FROM youtube_videos
            WHERE video_id IN ({placeholders})
            """,
            video_ids,
        ).fetchall()
    finally:
        conn.close()
    owner_by_video = {
        row[0]: {
            "owner_user_id": row[1],
            "brand_id": row[2],
        }
        for row in rows
        if row[0] and row[1]
    }
    for item in list_ai_broadcast_entries(brand_id=None):
        video_id = item.get("video_id")
        owner_id = item.get("user_id")
        if video_id and owner_id:
            owner_by_video.setdefault(
                video_id,
                {
                    "owner_user_id": owner_id,
                    "brand_id": item.get("brand_id"),
                },
            )
    result: Dict[str, Dict[str, Optional[str]]] = {}
    for entry in entries:
        short_id = entry.get("short_video_id")
        source_video_id = entry.get("video_id")
        owner_context = owner_by_video.get(source_video_id) or {}
        owner_user_id = owner_context.get("owner_user_id")
        if not short_id or not owner_user_id:
            continue
        result[str(short_id)] = {
            "owner_user_id": str(owner_user_id),
            "brand_id": str(owner_context.get("brand_id") or "").strip() or None,
        }
    return result


def _group_short_ids_by_owner(
    short_owner_context: Dict[str, Dict[str, Optional[str]]]
) -> Dict[str, List[str]]:
    grouped: Dict[str, List[str]] = {}
    for short_id, context in short_owner_context.items():
        owner_user_id = str(context.get("owner_user_id") or "").strip()
        if not owner_user_id:
            continue
        grouped.setdefault(owner_user_id, []).append(short_id)
    return grouped


def _parse_sync_ts(value: object) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _ensure_sync_state_table(conn) -> None:
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS short_comment_sync_state (
                short_video_id VARCHAR PRIMARY KEY,
                last_synced_at TIMESTAMP,
                last_comment_count INTEGER,
                observed_comment_count INTEGER,
                observed_comment_count_at TIMESTAMP
            )
            """
        )
        cols = table_columns(conn, "short_comment_sync_state")
        for column_name, column_sql in (
            ("last_comment_count", "INTEGER"),
            ("observed_comment_count", "INTEGER"),
            ("observed_comment_count_at", "TIMESTAMP"),
        ):
            if column_name in cols:
                continue
            try:
                conn.execute(
                    f"ALTER TABLE short_comment_sync_state ADD COLUMN {column_name} {column_sql}"
                )
                if hasattr(conn, "commit"):
                    conn.commit()
            except Exception as exc:
                try:
                    conn.rollback()
                except Exception:
                    pass
                message = str(exc).lower()
                if (
                    "duplicate column" not in message
                    and "already exists" not in message
                    and "read-only" not in message
                ):
                    raise
        cols = table_columns(conn, "short_comment_sync_state")
        if "observed_comment_count" in cols:
            try:
                conn.execute(
                    """
                    UPDATE short_comment_sync_state
                    SET observed_comment_count = last_comment_count
                    WHERE observed_comment_count IS NULL
                      AND last_comment_count IS NOT NULL
                    """
                )
            except Exception as exc:
                try:
                    conn.rollback()
                except Exception:
                    pass
                if "read-only" not in str(exc).lower():
                    raise
        if "observed_comment_count_at" in cols:
            try:
                conn.execute(
                    """
                    UPDATE short_comment_sync_state
                    SET observed_comment_count_at = last_synced_at
                    WHERE observed_comment_count_at IS NULL
                      AND last_synced_at IS NOT NULL
                    """
                )
            except Exception as exc:
                try:
                    conn.rollback()
                except Exception:
                    pass
                if "read-only" not in str(exc).lower():
                    raise
        if hasattr(conn, "commit"):
            try:
                conn.commit()
            except Exception as exc:
                try:
                    conn.rollback()
                except Exception:
                    pass
                if "read-only" not in str(exc).lower():
                    raise
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        if "read-only" in str(exc).lower():
            return
        raise


def _load_sync_state(short_video_ids: List[str]) -> Dict[str, Dict[str, object]]:
    if not short_video_ids:
        return {}
    conn = get_db_readonly()
    try:
        _ensure_sync_state_table(conn)
        placeholders = ", ".join("?" for _ in short_video_ids)
        cols = table_columns(conn, "short_comment_sync_state")
        has_count_column = "last_comment_count" in cols
        has_observed_column = "observed_comment_count" in cols
        has_observed_at_column = "observed_comment_count_at" in cols
        select_comment_count = ", last_comment_count" if has_count_column else ""
        select_observed_count = ", observed_comment_count" if has_observed_column else ""
        select_observed_at = ", observed_comment_count_at" if has_observed_at_column else ""
        try:
            rows = conn.execute(
                f"""
                SELECT short_video_id, last_synced_at{select_comment_count}{select_observed_count}{select_observed_at}
                FROM short_comment_sync_state
                WHERE short_video_id IN ({placeholders})
                """,
                short_video_ids,
            ).fetchall()
        except Exception as exc:
            if "short_comment_sync_state" in str(exc).lower():
                return {}
            raise
        result: Dict[str, Dict[str, object]] = {}
        for row in rows:
            if not row or not row[0]:
                continue
            row_index = 2
            last_comment_count = None
            if has_count_column:
                last_comment_count = _normalize_comment_count(row[row_index])
                row_index += 1
            observed_comment_count = None
            if has_observed_column:
                observed_comment_count = _normalize_comment_count(row[row_index])
                row_index += 1
            observed_comment_count_at = None
            if has_observed_at_column:
                observed_comment_count_at = _parse_sync_ts(row[row_index])
            result[row[0]] = {
                "last_synced_at": _parse_sync_ts(row[1]),
                "last_comment_count": last_comment_count,
                "observed_comment_count": observed_comment_count,
                "observed_comment_count_at": observed_comment_count_at,
            }
        return result
    finally:
        conn.close()


def _update_sync_state(sync_updates: Dict[str, Dict[str, object]]) -> None:
    if not sync_updates:
        return
    conn = get_db()
    try:
        _ensure_sync_state_table(conn)
        for short_id, state in sync_updates.items():
            last_synced_at = state.get("last_synced_at")
            last_comment_count = _normalize_comment_count(state.get("last_comment_count"))
            observed_comment_count = _normalize_comment_count(state.get("observed_comment_count"))
            observed_comment_count_at = state.get("observed_comment_count_at")
            conn.execute(
                """
                INSERT INTO short_comment_sync_state (
                    short_video_id,
                    last_synced_at,
                    last_comment_count,
                    observed_comment_count,
                    observed_comment_count_at
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (short_video_id)
                DO UPDATE SET
                    last_synced_at = COALESCE(excluded.last_synced_at, short_comment_sync_state.last_synced_at),
                    last_comment_count = COALESCE(excluded.last_comment_count, short_comment_sync_state.last_comment_count),
                    observed_comment_count = COALESCE(excluded.observed_comment_count, short_comment_sync_state.observed_comment_count),
                    observed_comment_count_at = COALESCE(excluded.observed_comment_count_at, short_comment_sync_state.observed_comment_count_at)
                """,
                [
                    short_id,
                    last_synced_at,
                    last_comment_count,
                    observed_comment_count,
                    observed_comment_count_at,
                ],
            )
        conn.commit()
    finally:
        conn.close()


def _build_publish_sort_map(entries: List[Dict[str, object]]) -> Dict[str, Optional[datetime]]:
    result: Dict[str, Optional[datetime]] = {}
    for entry in entries:
        short_id = entry.get("short_video_id")
        if not short_id:
            continue
        result[short_id] = _parse_sync_ts(entry.get("publish_sort_key"))
    return result


def _build_title_map(entries: List[Dict[str, object]]) -> Dict[str, str]:
    result: Dict[str, str] = {}
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
        result[short_id] = str(title)
    return result


def _select_sync_ids(
    short_ids: List[str],
    publish_map: Dict[str, Optional[datetime]],
    sync_state: Dict[str, Dict[str, object]],
    *,
    recent_size: int,
    rotate_size: int,
) -> List[str]:
    recent_candidates: List[Tuple[datetime, str]] = []
    for short_id in short_ids:
        publish_dt = publish_map.get(short_id)
        recent_candidates.append((publish_dt or datetime.min.replace(tzinfo=timezone.utc), short_id))
    recent_candidates.sort(key=lambda item: item[0], reverse=True)
    recent_ids = [short_id for _, short_id in recent_candidates[:recent_size]]
    recent_set = set(recent_ids)
    rotate_candidates: List[Tuple[datetime, str]] = []
    for short_id in short_ids:
        if short_id in recent_set:
            continue
        sync_state_entry = sync_state.get(short_id) or {}
        synced_at = sync_state_entry.get("last_synced_at")
        rotate_candidates.append((synced_at or datetime.min.replace(tzinfo=timezone.utc), short_id))
    rotate_candidates.sort(key=lambda item: item[0])
    rotate_ids = [short_id for _, short_id in rotate_candidates[:rotate_size]]
    return recent_ids + rotate_ids


def _sync_youtube_comments_for_videos(
    owner_user_id: str,
    short_ids: List[str],
    latest_by_video: Dict[str, object],
    title_map: Dict[str, str],
    sync_state: Dict[str, Dict[str, object]],
    comment_counts: Dict[str, Optional[int]],
    cached_top_level_counts: Dict[str, int],
    sync_updates: Dict[str, Dict[str, object]],
    short_oauth_user_ids: Dict[str, Optional[str]],
) -> int:
    updated_count = 0
    for short_id in short_ids:
        current_comment_count = _normalize_comment_count(comment_counts.get(short_id))
        cached_top_level_count = cached_top_level_counts.get(short_id, 0)
        if not _should_fetch_youtube_comment_bodies(
            short_id,
            current_comment_count,
            sync_state.get(short_id),
            cached_top_level_count,
        ):
            continue
        comments: List[Dict[str, object]] = []
        any_success = False
        try:
            oauth_user_id = short_oauth_user_ids.get(short_id)
            if not oauth_user_id:
                logger.warning(
                    "Skipping YouTube comment fetch for short_id=%s owner_user_id=%s because no unambiguous OAuth token key could be resolved.",
                    short_id,
                    owner_user_id,
                )
                continue
            comments.extend(
                fetch_video_comments(
                    short_id,
                    max_results=YOUTUBE_COMMENT_FETCH_MAX_RESULTS,
                    moderation_status="heldForReview",
                    user_id=oauth_user_id,
                )
            )
            any_success = True
        except YoutubeApiError:
            pass
        try:
            comments.extend(
                fetch_video_comments(
                    short_id,
                    max_results=YOUTUBE_COMMENT_FETCH_MAX_RESULTS,
                    moderation_status=None,
                    user_id=oauth_user_id,
                    prefer_user_oauth=True,
                    allow_other_oauth_fallback=False,
                )
            )
            any_success = True
        except YoutubeApiError:
            pass
        if not any_success:
            continue
        sync_updates.setdefault(short_id, {}).update(
            {
                "last_synced_at": datetime.now(timezone.utc),
                "last_comment_count": current_comment_count,
            }
        )
        if not comments:
            updated_count += 1
            continue
        comments = _merge_youtube_comments(comments)
        latest_ts = latest_by_video.get(short_id) if latest_by_video else None
        if latest_ts:
            comments = [
                comment
                for comment in comments
                if not comment.get("published_at")
                or comment.get("published_at") > latest_ts
            ]
        if not comments:
            updated_count += 1
            continue
        moderation_entries = [
            {"id": str(comment.get("comment_id")), "text": comment.get("text") or ""}
            for comment in comments
            if comment.get("comment_id") and comment.get("text")
        ]
        moderation_map = (
            moderate_text_entries(moderation_entries, owner_user_id)
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
            title = title_map.get(short_id) or short_id
            records.append(
                {
                    "platform": "youtube",
                    "comment_id": str(comment_id),
                    "parent_id": comment.get("parent_id"),
                    "thread_id": comment.get("thread_id"),
                    "video_id": short_id,
                    "instagram_media_id": None,
                    "queue_id": None,
                    "owner_user_id": owner_user_id,
                    "video_title": title,
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
        updated_count += 1
    return updated_count


def _load_owner_user_ids() -> List[str]:
    conn = get_db_readonly()
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT owner_user_id
            FROM youtube_videos
            WHERE owner_user_id IS NOT NULL
              AND owner_user_id <> ''
            """
        ).fetchall()
        return [row[0] for row in rows if row and row[0]]
    finally:
        conn.close()


def main() -> int:
    app = create_app()
    with app.app_context():
        comment_sync_enabled = os.getenv("YT_COMMENT_SYNC_ENABLED", "0").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        full_sync_enabled = os.getenv("YT_COMMENT_FULL_SYNC_ENABLED", "0").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        try:
            if comment_sync_enabled:
                entries = _collect_short_broadcast_entries()
                all_short_video_ids = _unique_short_ids(entries)
                sync_state = _load_sync_state(all_short_video_ids)
                sync_updates: Dict[str, Dict[str, object]] = {}
                short_owner_context = _load_short_owner_context(entries) if entries else {}
                owner_short_ids = _group_short_ids_by_owner(short_owner_context)
                short_oauth_user_ids = _resolve_short_oauth_user_ids(short_owner_context)
                comment_count_map = (
                    {
                        short_id: _normalize_comment_count((stats or {}).get("comment_count"))
                        for short_id, stats in fetch_video_stats(all_short_video_ids).items()
                    }
                    if all_short_video_ids
                    else {}
                )
                recent_entries = _select_recent_entries(entries, COMMENT_COUNT_SYNC_RECENT_SIZE)
                if not all_short_video_ids:
                    logger.info("No short videos found for comment count sync.")
                else:
                    refreshed = _sync_youtube_comment_totals(
                        all_short_video_ids,
                        comment_count_map,
                        sync_updates,
                    )
                    logger.info(
                        "Refreshed comment totals for %s short videos.",
                        refreshed,
                    )
                ig_refreshed = _sync_instagram_comment_counts(recent_entries)
                fb_refreshed = _sync_facebook_comment_counts(recent_entries)
                logger.info(
                    "Refreshed Instagram comment counts for %s entries; Facebook for %s entries.",
                    ig_refreshed,
                    fb_refreshed,
                )
                if not full_sync_enabled:
                    logger.info("Full YouTube comment sync disabled; skipping comment fetch.")
                if full_sync_enabled and entries:
                    owner_user_ids = _load_owner_user_ids()
                    publish_map = _build_publish_sort_map(entries)
                    title_map = _build_title_map(entries)
                    conn = get_db()
                    try:
                        _ensure_sync_state_table(conn)
                    finally:
                        conn.close()
                    if not owner_user_ids:
                        logger.info("No YouTube owners found for comment sync.")
                    else:
                        total_records = 0
                        for owner_user_id in owner_user_ids:
                            candidate_ids = owner_short_ids.get(owner_user_id, [])
                            if not candidate_ids:
                                continue
                            selected_ids = _select_sync_ids(
                                candidate_ids,
                                publish_map,
                                sync_state,
                                recent_size=12,
                                rotate_size=24,
                            )
                            latest_by_video = fetch_latest_comment_timestamps(
                                owner_user_id,
                                platform="youtube",
                            )
                            cached_top_level_counts = fetch_top_level_comment_counts(
                                selected_ids,
                                platform="youtube",
                            )
                            try:
                                updated_count = _sync_youtube_comments_for_videos(
                                    owner_user_id,
                                    selected_ids,
                                    latest_by_video,
                                    title_map,
                                    sync_state,
                                    comment_count_map,
                                    cached_top_level_counts,
                                    sync_updates,
                                    short_oauth_user_ids,
                                )
                                total_records += updated_count
                            except Exception:
                                logger.exception(
                                    "Failed to sync YouTube comments for owner_user_id=%s",
                                    owner_user_id,
                                )
                        logger.info(
                            "Synced %s new YouTube comments across %s owners.",
                            total_records,
                            len(owner_user_ids),
                        )
                if sync_updates:
                    _update_sync_state(sync_updates)
            else:
                logger.info("YouTube comment sync disabled; skipping comment fetch.")
        except Exception:
            logger.exception("Comment sync failed; continuing with daily snapshots.")
        try:
            capture_daily_snapshot(quiet=True)
        except Exception:
            logger.exception("Failed to refresh daily video metrics snapshot after comment sync")
        try:
            capture_daily_subscriber_snapshot()
        except Exception:
            logger.exception("Failed to refresh subscriber snapshot after comment sync")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
