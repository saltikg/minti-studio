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
)
from app.video_shorts.services.comment_store import fetch_latest_comment_timestamps
from app.video_shorts.services.comment_moderation import moderate_text_entries
from app.video_shorts.services.comment_store import upsert_comment_records
from app.video_shorts.services.db import get_db, get_db_readonly, ensure_channel_owner_schema
from app.video_shorts.services.facebook_queue import update_facebook_queue_metrics, load_facebook_queue_map
from app.video_shorts.services.instagram_queue import load_instagram_queue_map
from app.video_shorts.services.instagram_api import refresh_instagram_media, InstagramActionError
from app.video_shorts.services.ai_video_workspace import list_ai_broadcast_entries
from app.video_shorts.tasks.daily_video_metrics_snapshot import capture_daily_snapshot
from app.video_shorts.tasks.daily_subscriber_snapshot import capture_daily_subscriber_snapshot
from app.video_shorts.youtube_api import YoutubeApiError, fetch_video_comments, fetch_video_stats
from app.video_shorts.tasks.sync_instagram_metrics import _fetch_facebook_metrics
from src.trends.facebook_page_tokens import get_facebook_page_data

logger = logging.getLogger(__name__)

COMMENT_COUNT_SYNC_RECENT_SIZE = int(os.getenv("SHORT_COMMENT_COUNT_SYNC_LIMIT", "100"))


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


def _sync_youtube_comment_totals(short_ids: List[str]) -> int:
    if not short_ids:
        return 0
    stats = fetch_video_stats(short_ids)
    refreshed = 0
    for short_id in short_ids:
        stats_entry = stats.get(short_id) or {}
        comment_count = stats_entry.get("comment_count")
        if comment_count is None:
            continue
        try:
            total = max(0, int(comment_count))
        except (TypeError, ValueError):
            continue
        pending_count = 0
        try:
            pending_comments = fetch_video_comments(
                short_id,
                max_results=50,
                moderation_status="heldForReview",
                user_id=None,
            )
            pending_count = sum(
                1 for comment in pending_comments if not comment.get("is_reply")
            )
        except YoutubeApiError:
            pending_count = 0
        except Exception:
            logger.exception(
                "Failed to fetch heldForReview comments for %s",
                short_id,
            )
        _upsert_short_comment_counts(
            short_id,
            {"pending": pending_count, "published": total, "rejected": 0},
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


def _load_owner_short_ids(entries: List[Dict[str, object]]) -> Dict[str, List[str]]:
    video_ids = sorted({entry.get("video_id") for entry in entries if entry.get("video_id")})
    if not video_ids:
        return {}
    conn = get_db_readonly()
    try:
        ensure_channel_owner_schema(conn)
        placeholders = ", ".join("?" for _ in video_ids)
        rows = conn.execute(
            f"""
            SELECT video_id, owner_user_id
            FROM youtube_videos
            WHERE video_id IN ({placeholders})
            """,
            video_ids,
        ).fetchall()
    finally:
        conn.close()
    owner_by_video = {row[0]: row[1] for row in rows if row[0] and row[1]}
    for item in list_ai_broadcast_entries(brand_id=None):
        video_id = item.get("video_id")
        owner_id = item.get("user_id")
        if video_id and owner_id:
            owner_by_video.setdefault(video_id, owner_id)
    result: Dict[str, List[str]] = {}
    for entry in entries:
        short_id = entry.get("short_video_id")
        source_video_id = entry.get("video_id")
        owner_user_id = owner_by_video.get(source_video_id)
        if not short_id or not owner_user_id:
            continue
        result.setdefault(owner_user_id, []).append(short_id)
    return result


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
                last_synced_at TIMESTAMP
            )
            """
        )
    except Exception as exc:
        if "read-only" in str(exc).lower():
            return
        raise


def _load_sync_state(short_video_ids: List[str]) -> Dict[str, Optional[datetime]]:
    if not short_video_ids:
        return {}
    conn = get_db_readonly()
    try:
        _ensure_sync_state_table(conn)
        placeholders = ", ".join("?" for _ in short_video_ids)
        try:
            rows = conn.execute(
                f"""
                SELECT short_video_id, last_synced_at
                FROM short_comment_sync_state
                WHERE short_video_id IN ({placeholders})
                """,
                short_video_ids,
            ).fetchall()
        except Exception as exc:
            if "short_comment_sync_state" in str(exc).lower():
                return {}
            raise
        return {row[0]: _parse_sync_ts(row[1]) for row in rows if row[0]}
    finally:
        conn.close()


def _update_sync_state(short_video_ids: List[str]) -> None:
    if not short_video_ids:
        return
    conn = get_db()
    try:
        _ensure_sync_state_table(conn)
        now = datetime.now(timezone.utc)
        for short_id in short_video_ids:
            conn.execute(
                """
                INSERT INTO short_comment_sync_state (short_video_id, last_synced_at)
                VALUES (?, ?)
                ON CONFLICT (short_video_id)
                DO UPDATE SET last_synced_at = excluded.last_synced_at
                """,
                [short_id, now],
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
    sync_state: Dict[str, Optional[datetime]],
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
        synced_at = sync_state.get(short_id)
        rotate_candidates.append((synced_at or datetime.min.replace(tzinfo=timezone.utc), short_id))
    rotate_candidates.sort(key=lambda item: item[0])
    rotate_ids = [short_id for _, short_id in rotate_candidates[:rotate_size]]
    return recent_ids + rotate_ids


def _sync_youtube_comments_for_videos(
    owner_user_id: str,
    short_ids: List[str],
    latest_by_video: Dict[str, object],
    title_map: Dict[str, str],
) -> Set[str]:
    updated_ids: Set[str] = set()
    for short_id in short_ids:
        comments: List[Dict[str, object]] = []
        any_success = False
        try:
            comments.extend(
                fetch_video_comments(
                    short_id,
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
                    short_id,
                    max_results=50,
                    moderation_status=None,
                    user_id=owner_user_id,
                )
            )
            any_success = True
        except YoutubeApiError:
            pass
        if not any_success:
            continue
        if not comments:
            updated_ids.add(short_id)
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
            updated_ids.add(short_id)
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
        updated_ids.add(short_id)
    return updated_ids


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
                recent_entries = _select_recent_entries(entries, COMMENT_COUNT_SYNC_RECENT_SIZE)
                short_video_ids = _unique_short_ids(recent_entries)
                if not short_video_ids:
                    logger.info("No recent short videos found for comment count sync.")
                else:
                    refreshed = _sync_youtube_comment_totals(short_video_ids)
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
                    owner_short_ids = _load_owner_short_ids(entries)
                    publish_map = _build_publish_sort_map(entries)
                    title_map = _build_title_map(entries)
                    conn = get_db()
                    try:
                        _ensure_sync_state_table(conn)
                    finally:
                        conn.close()
                    sync_state = _load_sync_state(all_short_video_ids)
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
                            try:
                                updated_ids = _sync_youtube_comments_for_videos(
                                    owner_user_id,
                                    selected_ids,
                                    latest_by_video,
                                    title_map,
                                )
                                _update_sync_state(list(updated_ids))
                                total_records += len(updated_ids)
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
