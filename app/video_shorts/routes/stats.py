from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from flask import g, jsonify, render_template, request
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.video_shorts import video_shorts_bp
from app.video_shorts.config import SHORTS_DIR
from app.video_shorts.services.brands import current_brand_id
from app.video_shorts.services.db import get_db_readonly, table_columns
from app.video_shorts.services.video_metrics import (
    SNAPSHOT_TABLE,
    ensure_snapshot_table,
)
from app.video_shorts.services.subscriber_metrics import (
    SUBSCRIBER_SNAPSHOT_TABLE,
    ensure_subscriber_snapshot_table,
)
from app.video_shorts.services.youtube_oauth import is_reauth_required
from app.video_shorts.routes.auth import DEFAULT_TIME_ZONE, TIMEZONE_OPTIONS


CHANNEL_OPTIONS: Sequence[Mapping[str, str]] = [
    {"value": "all", "label": "All channels"},
    {"value": "youtube", "label": "YouTube"},
    {"value": "instagram", "label": "Instagram"},
    {"value": "facebook", "label": "Facebook"},
    {"value": "tiktok", "label": "TikTok"},
]

DEFAULT_WINDOW_DAYS = 30
VIDEO_LIST_PAGE_SIZE = 20


def _rows_to_dict(cursor) -> List[Mapping[str, object]]:
    cols = [desc[0] for desc in cursor.description]
    return [dict(zip(cols, row)) for row in cursor.fetchall()]


def _channel_filter_clause(channel_type: Optional[str]) -> Tuple[str, List[object]]:
    if channel_type and channel_type != "all":
        return " AND channel_type = ?", [channel_type]
    return "", []


def _parse_page_arg(name: str, default: int = 1) -> int:
    try:
        return max(int(request.args.get(name, str(default))), 1)
    except (TypeError, ValueError):
        return default


def _brand_scope_clause(
    conn,
    table_name: str,
    *,
    alias: Optional[str] = None,
    brand_id: Optional[str] = None,
) -> Tuple[str, List[object]]:
    scoped_brand_id = brand_id if brand_id is not None else current_brand_id()
    if not scoped_brand_id:
        return "", []
    if "brand_id" not in table_columns(conn, table_name):
        return "", []
    prefix = f"{alias}." if alias else ""
    return f" AND {prefix}brand_id = ?", [scoped_brand_id]


def _thumbnail_url_for_row(row: Mapping[str, object]) -> Optional[str]:
    video_id = row.get("video_id")
    if not video_id:
        return None
    channel_type = (row.get("channel_type") or "").lower()
    if channel_type == "youtube":
        return f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"
    if channel_type == "instagram":
        return f"https://www.instagram.com/p/{video_id}/media/?size=l"
    if channel_type in {"facebook", "tiktok"}:
        return None
    return f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"


def _video_url_for_row(row: Mapping[str, object]) -> Optional[str]:
    video_id = row.get("video_id")
    if not video_id:
        return None
    channel_type = (row.get("channel_type") or "").lower()
    if channel_type == "youtube":
        return f"https://www.youtube.com/shorts/{video_id}"
    if channel_type == "instagram":
        return f"https://www.instagram.com/p/{video_id}/"
    if channel_type == "facebook":
        permalink = row.get("permalink")
        if isinstance(permalink, str) and permalink:
            if permalink.startswith("http"):
                return permalink
            return f"https://www.facebook.com{permalink}"
    return f"https://www.youtube.com/watch?v={video_id}"


def _fetch_facebook_queue_rows(
    conn,
    start: date,
    end: date,
    brand_id: Optional[str] = None,
) -> List[Mapping[str, object]]:
    brand_clause, brand_params = _brand_scope_clause(conn, "shorts_facebook_queue", brand_id=brand_id)
    try:
        cursor = conn.execute(
            f"""
            WITH fb AS (
                SELECT
                    facebook_video_id,
                    plan_title,
                    page_name,
                    view_count,
                    comment_count,
                    permalink,
                    published_at,
                    CASE
                        WHEN published_at IS NULL THEN NULL
                        WHEN CAST(published_at AS VARCHAR) LIKE '%Z' THEN replace(CAST(published_at AS VARCHAR), 'Z', '+00:00')
                        ELSE CAST(published_at AS VARCHAR)
                    END AS published_norm
                FROM shorts_facebook_queue
                WHERE status = 'published'
                {brand_clause}
            ),
            parsed AS (
                SELECT
                    *,
                    COALESCE(
                        try_strptime(published_norm, '%Y-%m-%dT%H:%M:%S%z'),
                        try_strptime(published_norm, '%Y-%m-%dT%H:%M%z'),
                        try_strptime(published_norm, '%Y-%m-%d %H:%M:%S'),
                        try_strptime(published_norm, '%Y-%m-%d %H:%M')
                    ) AS published_ts
                FROM fb
            )
            SELECT
                facebook_video_id,
                plan_title,
                page_name,
                view_count,
                comment_count,
                permalink
            FROM parsed
            WHERE published_ts IS NOT NULL
              AND CAST(published_ts AS DATE) BETWEEN ? AND ?
            """,
            [*brand_params, start.isoformat(), end.isoformat()],
        )
    except Exception:
        return []
    rows = _rows_to_dict(cursor)
    return [
        {
            "video_id": row.get("facebook_video_id"),
            "channel_type": "facebook",
            "channel_name": row.get("page_name"),
            "video_title": row.get("plan_title") or row.get("facebook_video_id"),
            "views": row.get("view_count") or 0,
            "comments": row.get("comment_count") or 0,
            "views_delta": row.get("view_count") or 0,
            "comments_delta": row.get("comment_count") or 0,
            "permalink": row.get("permalink"),
        }
        for row in rows
        if row.get("facebook_video_id")
    ]


def _populate_thumbnails(rows: Sequence[Mapping[str, object]]) -> None:
    for row in rows:
        row["thumbnail_url"] = _thumbnail_url_for_row(row)
        row["video_url"] = _video_url_for_row(row)


def _build_filters(conn, channel_type: str, start: date, end: date):
    clause = ""
    params: List[object] = [start.isoformat(), end.isoformat()]
    if channel_type and channel_type != "all":
        clause = " AND channel_type = ?"
        params.append(channel_type)
    brand_clause, brand_params = _brand_scope_clause(conn, SNAPSHOT_TABLE)
    clause += brand_clause
    params.extend(brand_params)
    return clause, params


def _load_published_youtube_short_ids(
    conn,
    *,
    brand_id: Optional[str] = None,
) -> List[str]:
    if not SHORTS_DIR.exists():
        return []
    plan_suffix = "_plan.json"
    plan_paths = [path for path in SHORTS_DIR.glob(f"*{plan_suffix}") if path.is_file()]
    if not plan_paths:
        return []
    source_video_ids = [
        path.name[: -len(plan_suffix)]
        for path in plan_paths
        if path.name.endswith(plan_suffix)
    ]
    if not source_video_ids:
        return []
    placeholders = ", ".join("?" for _ in source_video_ids)
    params: List[object] = list(source_video_ids)
    query = f"""
        SELECT DISTINCT video_id
        FROM youtube_videos
        WHERE video_id IN ({placeholders})
    """
    if brand_id and "brand_id" in table_columns(conn, "youtube_videos"):
        query += " AND brand_id = ?"
        params.append(brand_id)
    rows = conn.execute(query, params).fetchall()
    allowed_source_ids = {str(row[0]) for row in rows if row and row[0]}
    if not allowed_source_ids:
        return []
    published_ids: List[str] = []
    for plan_path in plan_paths:
        source_video_id = plan_path.name[: -len(plan_suffix)]
        if source_video_id not in allowed_source_ids:
            continue
        try:
            raw = json.loads(plan_path.read_text())
        except Exception:
            continue
        entries = raw.get("plan") or raw.get("clips") or []
        for entry in entries:
            if str(entry.get("publish_status") or "").lower() != "published":
                continue
            short_id = str(entry.get("yt_video_id") or entry.get("short_video_id") or "").strip()
            if short_id:
                published_ids.append(short_id)
    return list(dict.fromkeys(published_ids))


def _parse_publish_date(value: object) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return _parse_date_param(text)


def _load_published_youtube_short_schedule(
    conn,
    *,
    brand_id: Optional[str] = None,
) -> Dict[str, date]:
    if not SHORTS_DIR.exists():
        return {}
    plan_suffix = "_plan.json"
    plan_paths = [path for path in SHORTS_DIR.glob(f"*{plan_suffix}") if path.is_file()]
    if not plan_paths:
        return {}
    source_video_ids = [
        path.name[: -len(plan_suffix)]
        for path in plan_paths
        if path.name.endswith(plan_suffix)
    ]
    if not source_video_ids:
        return {}
    placeholders = ", ".join("?" for _ in source_video_ids)
    params: List[object] = list(source_video_ids)
    query = f"""
        SELECT DISTINCT video_id
        FROM youtube_videos
        WHERE video_id IN ({placeholders})
    """
    if brand_id and "brand_id" in table_columns(conn, "youtube_videos"):
        query += " AND brand_id = ?"
        params.append(brand_id)
    rows = conn.execute(query, params).fetchall()
    allowed_source_ids = {str(row[0]) for row in rows if row and row[0]}
    if not allowed_source_ids:
        return {}
    published_schedule: Dict[str, date] = {}
    for plan_path in plan_paths:
        source_video_id = plan_path.name[: -len(plan_suffix)]
        if source_video_id not in allowed_source_ids:
            continue
        try:
            raw = json.loads(plan_path.read_text())
        except Exception:
            continue
        entries = raw.get("plan") or raw.get("clips") or []
        for entry in entries:
            if str(entry.get("publish_status") or "").lower() != "published":
                continue
            short_id = str(entry.get("yt_video_id") or entry.get("short_video_id") or "").strip()
            if not short_id:
                continue
            publish_date = _parse_publish_date(entry.get("publish_at_iso") or entry.get("publish_at"))
            if publish_date is None:
                continue
            existing = published_schedule.get(short_id)
            if existing is None or publish_date < existing:
                published_schedule[short_id] = publish_date
    return published_schedule


def _parse_date_param(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        try:
            return datetime.fromisoformat(value).date()
        except ValueError:
            return None


def _normalize_date(value: object) -> str:
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, datetime):
        return value.date().isoformat()
    return str(value)


def _get_user_timezone() -> str:
    user = getattr(g, "vs_current_user", None)
    return (user or {}).get("time_zone") or DEFAULT_TIME_ZONE


def _timezone_label(tz_name: str) -> str:
    for value, label in TIMEZONE_OPTIONS:
        if value == tz_name:
            return label
    return tz_name


def _localize_datetime(value: Optional[datetime], tz_name: str) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    try:
        zone = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        zone = ZoneInfo("UTC")
    return value.astimezone(zone)


def _fetch_daily_comment_counts(
    conn,
    start: date,
    end: date,
) -> Dict[str, Dict[str, int]]:
    try:
        cursor = conn.execute(
            """
            WITH raw AS (
                SELECT
                    LOWER(platform) AS platform,
                    CASE
                        WHEN published_at IS NULL THEN NULL
                        WHEN CAST(published_at AS VARCHAR) LIKE '%Z' THEN replace(CAST(published_at AS VARCHAR), 'Z', '+00:00')
                        ELSE CAST(published_at AS VARCHAR)
                    END AS published_norm
                FROM social_comment_cache
            ),
            parsed AS (
                SELECT
                    platform,
                    COALESCE(
                        try_strptime(published_norm, '%Y-%m-%dT%H:%M:%S%z'),
                        try_strptime(published_norm, '%Y-%m-%dT%H:%M%z'),
                        try_strptime(published_norm, '%Y-%m-%d %H:%M:%S'),
                        try_strptime(published_norm, '%Y-%m-%d %H:%M'),
                        try_strptime(published_norm, '%Y-%m-%d')
                    ) AS published_ts
                FROM raw
            )
            SELECT
                CAST(published_ts AS DATE) AS comment_date,
                platform,
                COUNT(*) AS comment_count
            FROM parsed
            WHERE published_ts IS NOT NULL
              AND CAST(published_ts AS DATE) BETWEEN ? AND ?
            GROUP BY comment_date, platform
            """,
            [start.isoformat(), end.isoformat()],
        )
    except Exception:
        return {}
    rows = _rows_to_dict(cursor)
    counts: Dict[str, Dict[str, int]] = {}
    for row in rows:
        platform = (row.get("platform") or "").lower()
        if not platform:
            continue
        date_key = _normalize_date(row.get("comment_date"))
        if not date_key:
            continue
        counts.setdefault(platform, {})[date_key] = row.get("comment_count") or 0
    return counts


def _fetch_daily_analytics_views(
    conn,
    start: date,
    end: date,
    brand_id: Optional[str] = None,
) -> Tuple[Dict[str, Dict[str, int]], Dict[str, int]]:
    brand_clause, brand_params = _brand_scope_clause(
        conn,
        SNAPSHOT_TABLE,
        brand_id=brand_id,
    )
    try:
        daily_cursor = conn.execute(
            f"""
            WITH ordered AS (
                SELECT
                    channel_type,
                    snapshot_date,
                    video_id,
                    CASE
                        WHEN channel_type = 'instagram' THEN COALESCE(reach, views)
                        ELSE views
                    END AS current_views,
                    LAG(
                        CASE
                            WHEN channel_type = 'instagram' THEN COALESCE(reach, views)
                            ELSE views
                        END
                    ) OVER (PARTITION BY channel_type, video_id ORDER BY snapshot_date) AS prev_views
                FROM {SNAPSHOT_TABLE}
                WHERE snapshot_date <= ?
                  {brand_clause}
            )
            SELECT
                channel_type,
                snapshot_date,
                SUM(
                    CASE
                        WHEN prev_views IS NULL THEN 0
                        ELSE GREATEST(COALESCE(current_views, 0) - COALESCE(prev_views, 0), 0)
                    END
                ) AS views
            FROM ordered
            WHERE snapshot_date BETWEEN ? AND ?
            GROUP BY channel_type, snapshot_date
            """,
            [
                end.isoformat(),
                *brand_params,
                start.isoformat(),
                end.isoformat(),
            ],
        )
        rows = _rows_to_dict(daily_cursor)
    except Exception:
        return {}, {}
    daily_map: Dict[str, Dict[str, int]] = {}
    for row in rows:
        channel = (row.get("channel_type") or "").lower()
        if not channel:
            continue
        date_key = _normalize_date(row.get("snapshot_date"))
        if not date_key:
            continue
        daily_map.setdefault(channel, {})[date_key] = row.get("views") or 0
    return daily_map, {}


def _fetch_daily_analytics_likes(
    conn,
    start: date,
    end: date,
    brand_id: Optional[str] = None,
) -> Dict[str, Dict[str, int]]:
    brand_clause, brand_params = _brand_scope_clause(
        conn,
        SNAPSHOT_TABLE,
        brand_id=brand_id,
    )
    try:
        cursor = conn.execute(
            f"""
            WITH ordered AS (
                SELECT
                    channel_type,
                    snapshot_date,
                    video_id,
                    likes,
                    LAG(likes) OVER (PARTITION BY channel_type, video_id ORDER BY snapshot_date) AS prev_likes
                FROM {SNAPSHOT_TABLE}
                WHERE snapshot_date <= ?
                  {brand_clause}
            )
            SELECT
                channel_type,
                snapshot_date,
                SUM(
                    CASE
                        WHEN prev_likes IS NULL THEN 0
                        ELSE GREATEST(COALESCE(likes, 0) - COALESCE(prev_likes, 0), 0)
                    END
                ) AS likes
            FROM ordered
            WHERE snapshot_date BETWEEN ? AND ?
            GROUP BY channel_type, snapshot_date
            """,
            [
                end.isoformat(),
                *brand_params,
                start.isoformat(),
                end.isoformat(),
            ],
        )
    except Exception:
        return {}
    rows = _rows_to_dict(cursor)
    daily_map: Dict[str, Dict[str, int]] = {}
    for row in rows:
        channel = (row.get("channel_type") or "").lower()
        if not channel:
            continue
        date_key = _normalize_date(row.get("snapshot_date"))
        if not date_key:
            continue
        daily_map.setdefault(channel, {})[date_key] = row.get("likes") or 0
    return daily_map


def _fetch_youtube_daily_deltas(
    conn,
    start: date,
    end: date,
    brand_id: Optional[str] = None,
    published_video_ids: Optional[Sequence[str]] = None,
) -> Tuple[Dict[str, int], Dict[str, int]]:
    brand_clause, brand_params = _brand_scope_clause(
        conn,
        SNAPSHOT_TABLE,
        brand_id=brand_id,
    )
    published_clause = ""
    published_params: List[object] = []
    if published_video_ids is not None:
        cleaned_ids = [str(video_id).strip() for video_id in published_video_ids if str(video_id or "").strip()]
        if not cleaned_ids:
            return {}, {}
        published_clause = " AND video_id IN ({})".format(", ".join("?" for _ in cleaned_ids))
        published_params.extend(cleaned_ids)
    try:
        cursor = conn.execute(
            f"""
            WITH ordered AS (
                SELECT
                    snapshot_date,
                    video_id,
                    views,
                    likes,
                    LAG(views) OVER (PARTITION BY video_id ORDER BY snapshot_date) AS prev_views,
                    LAG(likes) OVER (PARTITION BY video_id ORDER BY snapshot_date) AS prev_likes
                FROM {SNAPSHOT_TABLE}
                WHERE channel_type = 'youtube'
                  {brand_clause}
                  AND COALESCE(stats_source, '') NOT LIKE 'estimated_gap_fill_from_%'
                  {published_clause}
                  AND snapshot_date <= ?
            )
            SELECT
                snapshot_date,
                SUM(
                    CASE
                        WHEN prev_views IS NULL THEN 0
                        ELSE GREATEST(COALESCE(views, 0) - COALESCE(prev_views, 0), 0)
                    END
                ) AS views_delta,
                SUM(
                    CASE
                        WHEN prev_likes IS NULL THEN 0
                        ELSE GREATEST(COALESCE(likes, 0) - COALESCE(prev_likes, 0), 0)
                    END
                ) AS likes_delta
            FROM ordered
            WHERE snapshot_date BETWEEN ? AND ?
            GROUP BY snapshot_date
            ORDER BY snapshot_date
            """,
            [*brand_params, *published_params, end.isoformat(), start.isoformat(), end.isoformat()],
        )
    except Exception:
        return {}, {}
    rows = _rows_to_dict(cursor)
    view_map: Dict[str, int] = {}
    like_map: Dict[str, int] = {}
    for row in rows:
        date_key = _normalize_date(row.get("snapshot_date"))
        if not date_key:
            continue
        view_map[date_key] = int(row.get("views_delta") or 0)
        like_map[date_key] = int(row.get("likes_delta") or 0)
    return view_map, like_map


def _fetch_youtube_published_daily_metrics(
    conn,
    start: date,
    end: date,
    *,
    brand_id: Optional[str] = None,
    published_schedule: Optional[Mapping[str, date]] = None,
) -> Tuple[Dict[str, int], Dict[str, int], Dict[str, int], Dict[str, int], Dict[str, int]]:
    if published_schedule is not None and not published_schedule:
        return {}, {}, {}, {}, {}
    published_video_ids = list((published_schedule or {}).keys())
    if not published_video_ids:
        return {}, {}, {}, {}, {}
    placeholders = ", ".join("?" for _ in published_video_ids)
    brand_clause, brand_params = _brand_scope_clause(
        conn,
        SNAPSHOT_TABLE,
        brand_id=brand_id,
    )
    cursor = conn.execute(
        f"""
        SELECT snapshot_date, video_id, COALESCE(views, 0) AS views, COALESCE(likes, 0) AS likes
        FROM {SNAPSHOT_TABLE}
        WHERE channel_type = 'youtube'
          {brand_clause}
          AND COALESCE(stats_source, '') NOT LIKE ?
          AND snapshot_date <= ?
          AND video_id IN ({placeholders})
        ORDER BY snapshot_date, video_id
        """,
        [
            *brand_params,
            "estimated_gap_fill_from_%",
            end.isoformat(),
            *published_video_ids,
        ],
    )
    rows = _rows_to_dict(cursor)
    snapshots_by_video: Dict[str, List[Tuple[date, int, int]]] = {}
    for row in rows:
        snapshot_date_value = row.get("snapshot_date")
        snapshot_date = snapshot_date_value if isinstance(snapshot_date_value, date) else _parse_date_param(str(snapshot_date_value))
        if snapshot_date is None:
            continue
        video_id = str(row.get("video_id") or "").strip()
        if not video_id:
            continue
        snapshots_by_video.setdefault(video_id, []).append(
            (
                snapshot_date,
                int(row.get("views") or 0),
                int(row.get("likes") or 0),
            )
        )
    total_views: Dict[str, int] = {}
    total_likes: Dict[str, int] = {}
    delta_views: Dict[str, int] = {}
    delta_likes: Dict[str, int] = {}
    video_counts: Dict[str, int] = {}
    current = start
    while current <= end:
        date_key = current.isoformat()
        day_total_views = 0
        day_total_likes = 0
        day_delta_views = 0
        day_delta_likes = 0
        day_video_count = 0
        prev_day = current - timedelta(days=1)
        for video_id, publish_date in (published_schedule or {}).items():
            if publish_date > current:
                continue
            day_video_count += 1
            snapshots = snapshots_by_video.get(video_id) or []
            current_snapshot: Optional[Tuple[date, int, int]] = None
            prev_snapshot: Optional[Tuple[date, int, int]] = None
            for snapshot in snapshots:
                snapshot_day = snapshot[0]
                if snapshot_day <= current:
                    current_snapshot = snapshot
                if snapshot_day <= prev_day:
                    prev_snapshot = snapshot
                if snapshot_day > current:
                    break
            if current_snapshot is not None:
                day_total_views += current_snapshot[1]
                day_total_likes += current_snapshot[2]
            if current_snapshot is not None and prev_snapshot is not None:
                day_delta_views += max(current_snapshot[1] - prev_snapshot[1], 0)
                day_delta_likes += max(current_snapshot[2] - prev_snapshot[2], 0)
        total_views[date_key] = day_total_views
        total_likes[date_key] = day_total_likes
        delta_views[date_key] = day_delta_views
        delta_likes[date_key] = day_delta_likes
        video_counts[date_key] = day_video_count
        current += timedelta(days=1)
    return total_views, total_likes, delta_views, delta_likes, video_counts


def _fetch_top_videos_for_date(conn, target_date: date, channel_type: str, limit: int = 5):
    channel_clause, channel_params = _channel_filter_clause(channel_type)
    prev_date = target_date - timedelta(days=1)
    params = [
        target_date.isoformat(),
        *channel_params,
        prev_date.isoformat(),
        *channel_params,
        limit,
    ]
    cursor = conn.execute(
        f"""
        WITH target AS (
            SELECT
                video_id,
                channel_type,
                channel_name,
                video_title,
                SUM(COALESCE(views, 0)) AS views,
                SUM(COALESCE(comments, 0)) AS comments
            FROM {SNAPSHOT_TABLE}
            WHERE snapshot_date = ?{channel_clause}
            GROUP BY video_id, channel_type, channel_name, video_title
        ),
        prev_values AS (
            SELECT
                video_id,
                channel_type,
                SUM(COALESCE(views, 0)) AS views,
                SUM(COALESCE(comments, 0)) AS comments
            FROM {SNAPSHOT_TABLE}
            WHERE snapshot_date = ?{channel_clause}
            GROUP BY video_id, channel_type
        )
        SELECT
            t.video_id,
            t.channel_type,
            t.channel_name,
            t.video_title,
            t.views,
            t.comments,
            COALESCE(t.views, 0) - COALESCE(p.views, 0) AS views_delta,
            COALESCE(t.comments, 0) - COALESCE(p.comments, 0) AS comments_delta
        FROM target t
        LEFT JOIN prev_values p
          ON t.video_id = p.video_id AND t.channel_type = p.channel_type
        ORDER BY views_delta DESC NULLS LAST, t.views DESC
        LIMIT ?
        """,
        params,
    )
    rows = _rows_to_dict(cursor)
    _populate_thumbnails(rows)
    return rows


def _fetch_stats_video_rows(
    conn,
    *,
    start_date: date,
    end_date: date,
    brand_id: Optional[str],
    page: int,
    page_size: int = VIDEO_LIST_PAGE_SIZE,
) -> Tuple[List[Mapping[str, object]], int]:
    base_brand_clause = " AND gv.brand_id = ?" if brand_id else ""
    snapshot_brand_clause = " AND s.brand_id = ?" if brand_id else ""
    brand_params = [brand_id] if brand_id else []
    offset = (page - 1) * page_size

    count_row = conn.execute(
        f"""
        SELECT COUNT(*)
        FROM analytics.dim_generated_videos gv
        WHERE gv.publish_status = 'published'
          AND COALESCE(gv.youtube_published_at, gv.published_at, gv.planned_publish_at)::date BETWEEN ? AND ?
          {base_brand_clause}
        """,
        [
            start_date.isoformat(),
            end_date.isoformat(),
            *brand_params,
        ],
    ).fetchone()
    total_count = int((count_row or [0])[0] or 0)

    rows = conn.execute(
        f"""
        WITH base AS (
            SELECT
                gv.generated_video_id,
                gv.generated_title,
                gv.youtube_video_id,
                gv.instagram_media_id,
                COALESCE(gv.youtube_published_at, gv.published_at, gv.planned_publish_at) AS publish_at
            FROM analytics.dim_generated_videos gv
            WHERE gv.publish_status = 'published'
              AND COALESCE(gv.youtube_published_at, gv.published_at, gv.planned_publish_at)::date BETWEEN ? AND ?
              {base_brand_clause}
        ),
        latest_youtube AS (
            SELECT
                s.video_id,
                s.views,
                ROW_NUMBER() OVER (
                    PARTITION BY s.video_id
                    ORDER BY s.snapshot_date DESC, s.effective_at DESC
                ) AS rn
            FROM main.shorts_video_daily_snapshots s
            JOIN base b
              ON b.youtube_video_id = s.video_id
            WHERE s.channel_type = 'youtube'
              {snapshot_brand_clause}
        ),
        latest_instagram AS (
            SELECT
                s.video_id,
                COALESCE(s.reach, s.views, 0) AS instagram_views,
                ROW_NUMBER() OVER (
                    PARTITION BY s.video_id
                    ORDER BY s.snapshot_date DESC, s.effective_at DESC
                ) AS rn
            FROM main.shorts_video_daily_snapshots s
            JOIN base b
              ON b.instagram_media_id = s.video_id
            WHERE s.channel_type = 'instagram'
              {snapshot_brand_clause}
        )
        SELECT
            b.generated_title,
            b.publish_at,
            COALESCE(yt.views, 0) AS youtube_views,
            COALESCE(ig.instagram_views, 0) AS instagram_views
        FROM base b
        LEFT JOIN latest_youtube yt
          ON yt.video_id = b.youtube_video_id
         AND yt.rn = 1
        LEFT JOIN latest_instagram ig
          ON ig.video_id = b.instagram_media_id
         AND ig.rn = 1
        ORDER BY b.publish_at DESC NULLS LAST, b.generated_video_id DESC
        LIMIT ? OFFSET ?
        """,
        [
            start_date.isoformat(),
            end_date.isoformat(),
            *brand_params,
            *brand_params,
            *brand_params,
            page_size,
            offset,
        ],
    ).fetchall()

    items: List[Mapping[str, object]] = []
    for title, publish_at, youtube_views, instagram_views in rows:
        items.append(
            {
                "video_title": (title or "").strip() or "Untitled video",
                "publish_at": publish_at,
                "youtube_views": int(youtube_views or 0),
                "instagram_views": int(instagram_views or 0),
            }
        )
    return items, total_count


@video_shorts_bp.route("/stats", methods=["GET"])
def video_stats_page():
    end_param = request.args.get("end")
    start_param = request.args.get("start")
    channel_type = request.args.get("channel", "all")
    selected_metric = request.args.get("metric", "views")
    videos_page = _parse_page_arg("videos_page", 1)

    today = date.today()
    try:
        parsed_end = _parse_date_param(end_param)
        end_date = parsed_end if parsed_end else today
    except ValueError:
        end_date = today
    if end_date > today:
        end_date = today
    try:
        parsed_start = _parse_date_param(start_param)
        start_date = parsed_start if parsed_start else end_date - timedelta(days=DEFAULT_WINDOW_DAYS - 1)
    except ValueError:
        start_date = end_date - timedelta(days=DEFAULT_WINDOW_DAYS - 1)

    if start_date > end_date:
        start_date, end_date = end_date, start_date
    if (end_date - start_date).days > 60:
        start_date = end_date - timedelta(days=60)

    user_time_zone = _get_user_timezone()
    user_time_zone_label = _timezone_label(user_time_zone)
    conn = get_db_readonly()
    try:
        ensure_snapshot_table(conn)
        ensure_subscriber_snapshot_table(conn)
        active_brand_id = current_brand_id()
        snapshot_brand_clause, snapshot_brand_params = _brand_scope_clause(
            conn,
            SNAPSHOT_TABLE,
            brand_id=active_brand_id,
        )
        subscriber_brand_clause, subscriber_brand_params = _brand_scope_clause(
            conn,
            SUBSCRIBER_SNAPSHOT_TABLE,
            brand_id=active_brand_id,
        )
        youtube_channels_brand_clause, youtube_channels_brand_params = _brand_scope_clause(
            conn,
            "youtube_channels",
            brand_id=active_brand_id,
        )

        analytics_daily_views, analytics_baseline = _fetch_daily_analytics_views(
            conn,
            start_date,
            end_date,
            brand_id=active_brand_id,
        )
        analytics_daily_likes = _fetch_daily_analytics_likes(
            conn,
            start_date,
            end_date,
            brand_id=active_brand_id,
        )
        published_youtube_short_schedule = _load_published_youtube_short_schedule(
            conn,
            brand_id=active_brand_id,
        )
        youtube_total_views_map, youtube_total_likes_map, youtube_daily_views_map, youtube_daily_likes_map, _youtube_video_count_map = _fetch_youtube_published_daily_metrics(
            conn,
            start_date,
            end_date,
            brand_id=active_brand_id,
            published_schedule=published_youtube_short_schedule,
        )
        analytics_daily_likes_all: Dict[str, int] = {}
        for channel_map in analytics_daily_likes.values():
            for date_key, value in channel_map.items():
                analytics_daily_likes_all[date_key] = analytics_daily_likes_all.get(date_key, 0) + (value or 0)
        for date_key, value in youtube_daily_likes_map.items():
            analytics_daily_likes_all[date_key] = analytics_daily_likes_all.get(date_key, 0) + (value or 0)

        comment_start = start_date - timedelta(days=1)
        comment_counts_by_platform = _fetch_daily_comment_counts(
            conn,
            comment_start,
            end_date,
        )
        comment_counts_all: Dict[str, int] = {}
        for platform_counts in comment_counts_by_platform.values():
            for date_key, count in platform_counts.items():
                comment_counts_all[date_key] = comment_counts_all.get(date_key, 0) + (count or 0)

        filter_clause, params = _build_filters(conn, channel_type, start_date, end_date)
        chart_clause = filter_clause
        chart_params = list(params)
        daily_cursor = conn.execute(
            f"""
            SELECT
                snapshot_date,
                SUM(COALESCE(views, 0)) AS views,
                SUM(COALESCE(comments, 0)) AS comments,
                SUM(COALESCE(likes, 0)) AS likes,
                SUM(COALESCE(impressions, 0)) AS impressions,
                SUM(COALESCE(reach, 0)) AS reach,
                SUM(COALESCE(saved, 0)) AS saved,
                SUM(CASE WHEN channel_type = 'youtube' THEN COALESCE(views, 0) ELSE 0 END) AS youtube_views_total,
                SUM(CASE WHEN channel_type = 'youtube' THEN COALESCE(likes, 0) ELSE 0 END) AS youtube_likes_total,
                SUM(CASE WHEN channel_type = 'youtube' AND views IS NOT NULL THEN 1 ELSE 0 END) AS youtube_views_rows,
                MAX(effective_at) AS last_updated
            FROM {SNAPSHOT_TABLE}
            WHERE snapshot_date BETWEEN ? AND ?{chart_clause}
            GROUP BY snapshot_date
            ORDER BY snapshot_date ASC
            """,
            chart_params,
        )
        daily_totals = _rows_to_dict(daily_cursor)
        last_api_row = conn.execute(
            f"""
            SELECT MAX(effective_at) AS last_api_at
            FROM {SNAPSHOT_TABLE}
            WHERE snapshot_date BETWEEN ? AND ?{chart_clause}
            """,
            chart_params,
        ).fetchone()
        last_api_at = last_api_row[0] if last_api_row else None
        prev_views = None
        prev_comments = None
        prev_impressions = None
        prev_likes = None
        for row in daily_totals:
            date_key = _normalize_date(row.get("snapshot_date"))
            localized = _localize_datetime(row.get("last_updated"), user_time_zone)
            if localized:
                row["last_updated_local"] = localized.strftime("%Y-%m-%d %I:%M:%S %p %Z")
                row["last_updated_epoch"] = int(localized.timestamp())
            else:
                row["last_updated_local"] = None
                row["last_updated_epoch"] = 0
            row["instagram_daily_likes"] = analytics_daily_likes.get("instagram", {}).get(date_key, 0)

            youtube_total = youtube_total_views_map.get(date_key, 0)
            instagram_total = int(row.get("reach") or 0)
            youtube_daily = analytics_daily_views.get("youtube", {}).get(date_key, 0)

            youtube_daily_likes = youtube_daily_likes_map.get(date_key, 0)
            row["youtube_daily_likes"] = youtube_daily_likes

            instagram_daily_views = max(instagram_total - prev_views, 0) if channel_type == "instagram" and prev_views is not None else analytics_daily_views.get("instagram", {}).get(date_key, 0)
            row["youtube_daily_views"] = youtube_daily
            row["instagram_daily_views"] = instagram_daily_views or 0

            if channel_type == "all":
                current_views = youtube_total + instagram_total
            elif channel_type == "youtube":
                current_views = youtube_total
            elif channel_type == "instagram":
                current_views = instagram_total
            else:
                current_views = row.get("views") or 0

            row["views"] = current_views
            if channel_type and channel_type != "all":
                current_comments = (
                    comment_counts_by_platform.get(channel_type, {}).get(date_key, 0)
                )
            else:
                current_comments = comment_counts_all.get(date_key, 0)
            row["comments"] = current_comments
            current_impressions = row.get("impressions") or 0
            current_likes = row.get("likes") or 0
            row["views_change"] = None if prev_views is None else current_views - prev_views
            row["comments_change"] = None if prev_comments is None else current_comments - prev_comments
            row["impressions_change"] = None if prev_impressions is None else current_impressions - prev_impressions
            row["likes_change"] = None if prev_likes is None else current_likes - prev_likes
            row["views_delta"] = row["views_change"]
            row["comments_delta"] = row["comments_change"]
            row["likes_delta"] = row["likes_change"]
            if current_views is not None:
                prev_views = current_views
            prev_comments = current_comments
            prev_impressions = current_impressions
            prev_likes = current_likes

        # Expose newest first for the table.
        daily_totals = list(reversed(daily_totals))

        total_cursor = conn.execute(
            f"""
            WITH latest AS (
                SELECT channel_type, MAX(snapshot_date) AS snapshot_date
                FROM {SNAPSHOT_TABLE}
                WHERE snapshot_date BETWEEN ? AND ?{chart_clause}
                GROUP BY channel_type
            )
            SELECT
                s.channel_type,
                SUM(COALESCE(s.views, 0)) AS views,
                SUM(COALESCE(s.comments, 0)) AS comments,
                SUM(COALESCE(s.likes, 0)) AS likes,
                SUM(COALESCE(s.impressions, 0)) AS impressions,
                SUM(COALESCE(s.reach, 0)) AS reach,
                SUM(COALESCE(s.saved, 0)) AS saved
            FROM {SNAPSHOT_TABLE} s
            JOIN latest l
              ON s.channel_type = l.channel_type
             AND s.snapshot_date = l.snapshot_date
            WHERE 1 = 1{chart_clause}
            GROUP BY s.channel_type
            """,
            [*chart_params, *chart_params[2:]],
        )
        totals_list = _rows_to_dict(total_cursor)
        channel_totals: Dict[str, Mapping[str, int]] = {}
        for row in totals_list:
            key = row.get("channel_type") or "all"
            effective_views = (row.get("reach") or 0) if key == "instagram" else (row.get("views") or 0)
            channel_totals[key] = {
                "views": effective_views,
                "comments": row.get("comments") or 0,
                "likes": row.get("likes") or 0,
                "impressions": row.get("impressions") or 0,
                "reach": row.get("reach") or 0,
                "saved": row.get("saved") or 0,
            }
        aggregated = {
            "views": sum(entry["views"] for entry in channel_totals.values()),
            "comments": sum(entry["comments"] for entry in channel_totals.values()),
            "likes": sum(entry["likes"] for entry in channel_totals.values()),
            "impressions": sum(entry["impressions"] for entry in channel_totals.values()),
            "reach": sum(entry["reach"] for entry in channel_totals.values()),
            "saved": sum(entry["saved"] for entry in channel_totals.values()),
        }
        channel_totals.setdefault("all", aggregated)
        def _empty_totals() -> Dict[str, int]:
            return {
                "views": 0,
                "comments": 0,
                "likes": 0,
                "impressions": 0,
                "reach": 0,
                "saved": 0,
            }
        for option in CHANNEL_OPTIONS:
            channel_totals.setdefault(option["value"], _empty_totals())

        prev_date = start_date - timedelta(days=1)
        prev_params = [prev_date.isoformat()]
        if channel_type and channel_type != "all":
            prev_params.append(channel_type)
        prev_params.extend(snapshot_brand_params)
        prev_totals_cursor = conn.execute(
            f"""
            SELECT
                channel_type,
                SUM(COALESCE(views, 0)) AS views,
                SUM(COALESCE(comments, 0)) AS comments,
                SUM(COALESCE(likes, 0)) AS likes,
                SUM(COALESCE(impressions, 0)) AS impressions,
                SUM(COALESCE(reach, 0)) AS reach,
                SUM(COALESCE(saved, 0)) AS saved
            FROM {SNAPSHOT_TABLE}
            WHERE snapshot_date = ?{filter_clause}
            GROUP BY channel_type
            """,
            prev_params,
        )
        prev_totals_list = _rows_to_dict(prev_totals_cursor)
        prev_totals: Dict[str, Mapping[str, int]] = {}
        for row in prev_totals_list:
            key = row.get("channel_type") or "all"
            effective_views = (row.get("reach") or 0) if key == "instagram" else (row.get("views") or 0)
            prev_totals[key] = {
                "views": effective_views,
                "comments": row.get("comments") or 0,
                "likes": row.get("likes") or 0,
                "impressions": row.get("impressions") or 0,
                "reach": row.get("reach") or 0,
                "saved": row.get("saved") or 0,
            }
        aggregated_prev = {
            "views": sum(entry["views"] for entry in prev_totals.values()),
            "comments": sum(entry["comments"] for entry in prev_totals.values()),
            "likes": sum(entry["likes"] for entry in prev_totals.values()),
            "impressions": sum(entry["impressions"] for entry in prev_totals.values()),
            "reach": sum(entry["reach"] for entry in prev_totals.values()),
            "saved": sum(entry["saved"] for entry in prev_totals.values()),
        }
        prev_totals.setdefault("all", aggregated_prev)
        top_views_cursor = conn.execute(
            f"""
            WITH target AS (
                SELECT
                    video_id,
                    channel_type,
                    channel_name,
                    video_title,
                    SUM(COALESCE(views, 0)) AS views,
                    SUM(COALESCE(comments, 0)) AS comments,
                    SUM(COALESCE(likes, 0)) AS likes,
                    SUM(COALESCE(impressions, 0)) AS impressions,
                    SUM(COALESCE(reach, 0)) AS reach,
                    SUM(COALESCE(saved, 0)) AS saved,
                    MAX(stats_source) AS stats_source
                FROM {SNAPSHOT_TABLE}
                WHERE snapshot_date BETWEEN ? AND ?{filter_clause}
                GROUP BY video_id, channel_type, channel_name, video_title
            ),
            prev_values AS (
                SELECT
                    video_id,
                    channel_type,
                    SUM(COALESCE(views, 0)) AS views,
                    SUM(COALESCE(comments, 0)) AS comments,
                    SUM(COALESCE(likes, 0)) AS likes
                FROM {SNAPSHOT_TABLE}
                WHERE snapshot_date = ?{filter_clause}
                GROUP BY video_id, channel_type
            )
            SELECT
                t.video_id,
                t.channel_type,
                t.channel_name,
                t.video_title,
                t.views,
                t.comments,
                t.likes,
                t.impressions,
                t.reach,
                t.saved,
                COALESCE(t.views, 0) - COALESCE(p.views, 0) AS views_delta,
                COALESCE(t.comments, 0) - COALESCE(p.comments, 0) AS comments_delta,
                COALESCE(t.likes, 0) - COALESCE(p.likes, 0) AS likes_delta,
                t.stats_source
            FROM target t
            LEFT JOIN prev_values p
              ON t.video_id = p.video_id AND t.channel_type = p.channel_type
            ORDER BY views_delta DESC NULLS LAST, t.views DESC
            LIMIT 10
            """,
            params + prev_params,
        )
        top_videos = _rows_to_dict(top_views_cursor)

        top_comments_cursor = conn.execute(
            f"""
            SELECT
                video_id,
                channel_type,
                channel_name,
                video_title,
                SUM(COALESCE(views, 0)) AS views,
                SUM(COALESCE(comments, 0)) AS comments
                ,SUM(COALESCE(likes, 0)) AS likes
                ,SUM(COALESCE(impressions, 0)) AS impressions
                ,SUM(COALESCE(reach, 0)) AS reach
                ,SUM(COALESCE(saved, 0)) AS saved
                ,MAX(stats_source) AS stats_source
            FROM {SNAPSHOT_TABLE}
            WHERE snapshot_date BETWEEN ? AND ?{filter_clause}
            GROUP BY video_id, channel_type, channel_name, video_title
            ORDER BY comments DESC
            LIMIT 1
            """,
            params,
        )
        top_comment_video = _rows_to_dict(top_comments_cursor)

        top_table_cursor = conn.execute(
            f"""
            SELECT
                video_id,
                channel_type,
                channel_name,
                video_title,
                SUM(COALESCE(views, 0)) AS views,
                SUM(COALESCE(comments, 0)) AS comments,
                SUM(COALESCE(likes, 0)) AS likes,
                SUM(COALESCE(impressions, 0)) AS impressions,
                SUM(COALESCE(reach, 0)) AS reach,
                SUM(COALESCE(saved, 0)) AS saved
            FROM {SNAPSHOT_TABLE}
            WHERE snapshot_date BETWEEN ? AND ?{chart_clause}
            GROUP BY video_id, channel_type, channel_name, video_title
            ORDER BY views DESC
            LIMIT 40
            """,
            chart_params,
        )
        top_table_rows = _rows_to_dict(top_table_cursor)
        channel_clause, channel_params = _channel_filter_clause(channel_type)
        channel_scope_clause = f"{channel_clause}{snapshot_brand_clause}"
        channel_scope_params = [*channel_params, *snapshot_brand_params]
        today_prev_date = today - timedelta(days=1)
        today_cursor = conn.execute(
            f"""
            WITH target AS (
                SELECT
                    video_id,
                    channel_type,
                    channel_name,
                    video_title,
                    SUM(COALESCE(views, 0)) AS views,
                    SUM(COALESCE(comments, 0)) AS comments
                FROM {SNAPSHOT_TABLE}
                WHERE snapshot_date = ?{channel_scope_clause}
                GROUP BY video_id, channel_type, channel_name, video_title
            ),
            prev_values AS (
                SELECT
                    video_id,
                    channel_type,
                    SUM(COALESCE(views, 0)) AS views,
                    SUM(COALESCE(comments, 0)) AS comments
                FROM {SNAPSHOT_TABLE}
                WHERE snapshot_date = ?{channel_scope_clause}
                GROUP BY video_id, channel_type
            )
            SELECT
                t.video_id,
                t.channel_type,
                t.channel_name,
                t.video_title,
                t.views,
                t.comments,
                COALESCE(t.views, 0) - COALESCE(p.views, 0) AS views_delta,
                COALESCE(t.comments, 0) - COALESCE(p.comments, 0) AS comments_delta
            FROM target t
            LEFT JOIN prev_values p
              ON t.video_id = p.video_id AND t.channel_type = p.channel_type
            ORDER BY views_delta DESC NULLS LAST, t.views DESC
            LIMIT 5
            """,
            [today.isoformat(), *channel_scope_params, today_prev_date.isoformat(), *channel_scope_params],
        )
        today_top_videos = _rows_to_dict(today_cursor)
        fb_today_rows = _fetch_facebook_queue_rows(conn, today, today, brand_id=active_brand_id)
        if fb_today_rows:
            today_top_videos.extend(fb_today_rows)
        today_top_videos.sort(
            key=lambda row: (row.get("views_delta") or row.get("views") or 0),
            reverse=True,
        )
        _populate_thumbnails(today_top_videos)
        week_start = today - timedelta(days=6)
        week_end = today
        prev_week_start = week_start - timedelta(days=7)
        prev_week_end = week_start - timedelta(days=1)
        week_cursor = conn.execute(
            f"""
            WITH target AS (
                SELECT
                    video_id,
                    channel_type,
                    channel_name,
                    video_title,
                    MAX(views) AS views,
                    MAX(comments) AS comments
                FROM {SNAPSHOT_TABLE}
                WHERE snapshot_date BETWEEN ? AND ?{channel_scope_clause}
                GROUP BY video_id, channel_type, channel_name, video_title
            ),
            prev_values AS (
                SELECT
                    video_id,
                    channel_type,
                    MAX(views) AS views,
                    MAX(comments) AS comments
                FROM {SNAPSHOT_TABLE}
                WHERE snapshot_date BETWEEN ? AND ?{channel_scope_clause}
                GROUP BY video_id, channel_type
            )
            SELECT
                t.video_id,
                t.channel_type,
                t.channel_name,
                t.video_title,
                t.views,
                t.comments,
                COALESCE(t.views, 0) - COALESCE(p.views, 0) AS views_delta,
                COALESCE(t.comments, 0) - COALESCE(p.comments, 0) AS comments_delta
            FROM target t
            LEFT JOIN prev_values p
              ON t.video_id = p.video_id AND t.channel_type = p.channel_type
            ORDER BY views_delta DESC NULLS LAST, t.views DESC
            LIMIT 5
            """,
            [
                week_start.isoformat(),
                week_end.isoformat(),
                *channel_scope_params,
                prev_week_start.isoformat(),
                prev_week_end.isoformat(),
                *channel_scope_params,
            ],
        )
        week_top_videos = _rows_to_dict(week_cursor)
        fb_week_rows = _fetch_facebook_queue_rows(
            conn,
            week_start,
            week_end,
            brand_id=active_brand_id,
        )
        if fb_week_rows:
            week_top_videos.extend(fb_week_rows)
        week_top_videos.sort(
            key=lambda row: (row.get("views_delta") or row.get("views") or 0),
            reverse=True,
        )
        _populate_thumbnails(week_top_videos)
        video_rows, video_rows_total = _fetch_stats_video_rows(
            conn,
            start_date=start_date,
            end_date=end_date,
            brand_id=active_brand_id,
            page=videos_page,
        )

        date_sequence = []
        current = start_date
        while current <= end_date:
            date_sequence.append(current.isoformat())
            current += timedelta(days=1)
        daily_map = {_normalize_date(row["snapshot_date"]): row for row in daily_totals}
        channel_cursor = conn.execute(
            f"""
            SELECT
                snapshot_date,
                channel_type,
                SUM(COALESCE(views, 0)) AS views,
                SUM(COALESCE(comments, 0)) AS comments,
                SUM(COALESCE(likes, 0)) AS likes
            FROM {SNAPSHOT_TABLE}
            WHERE snapshot_date BETWEEN ? AND ?{chart_clause}
            GROUP BY snapshot_date, channel_type
            """,
            chart_params,
        )
        channel_rows = _rows_to_dict(channel_cursor)
        channel_map: Dict[str, Dict[str, Mapping[str, object]]] = {}
        for row in channel_rows:
            key = row.get("channel_type") or "all"
            date_key = _normalize_date(row["snapshot_date"])
            channel_map.setdefault(key, {})[date_key] = row
        instagram_total_reach_map = {
            date_key: int((row or {}).get("reach") or 0)
            for date_key, row in channel_map.get("instagram", {}).items()
        }

        chart_series: Dict[str, List[Mapping[str, object]]] = {}
        chart_series_delta: Dict[str, List[Mapping[str, object]]] = {}
        channel_keys = list({opt["value"] for opt in CHANNEL_OPTIONS} | {"all"})
        comment_prev_date = _normalize_date(comment_start)
        for key in channel_keys:
            source_map = daily_map if key == "all" else channel_map.get(key, {})
            prev_values = {"views": None, "comments": None, "likes": None}
            channel_prev = prev_totals.get(key) or prev_totals.get("all", {})
            prev_values["views"] = channel_prev.get("views")
            if key == "all":
                prev_values["comments"] = comment_counts_all.get(comment_prev_date)
            else:
                prev_values["comments"] = comment_counts_by_platform.get(key, {}).get(comment_prev_date)
            prev_values["likes"] = channel_prev.get("likes")
            chart_series[key] = []
            chart_series_delta[key] = []
            for date_key in date_sequence:
                entry = source_map.get(date_key, {})
                if key == "all":
                    views = youtube_total_views_map.get(date_key, 0) + instagram_total_reach_map.get(date_key, 0)
                elif key == "youtube":
                    views = youtube_total_views_map.get(date_key, 0)
                elif key == "instagram":
                    views = instagram_total_reach_map.get(date_key, 0)
                else:
                    views = entry.get("views", 0)
                if key == "all":
                    comments = comment_counts_all.get(date_key, 0)
                else:
                    comments = comment_counts_by_platform.get(key, {}).get(date_key, 0)
                likes = entry.get("likes", 0)
                chart_series[key].append(
                    {
                        "date": date_key,
                        "views": views if views is not None else 0,
                        "comments": comments,
                        "likes": likes,
                    }
                )
                delta_entry = {
                    "date": date_key,
                    "views_delta": None,
                    "comments_delta": comments,
                    "likes_delta": None,
                }
                if key == "all":
                    delta_entry["views_delta"] = (
                        youtube_daily_views_map.get(date_key, 0)
                        + analytics_daily_views.get("instagram", {}).get(date_key, 0)
                    )
                    delta_entry["likes_delta"] = analytics_daily_likes_all.get(date_key, 0)
                    delta_entry["youtube_daily_views"] = youtube_daily_views_map.get(date_key, 0)
                    delta_entry["instagram_daily_views"] = analytics_daily_views.get("instagram", {}).get(date_key, 0)
                elif key == "youtube":
                    delta_entry["views_delta"] = youtube_daily_views_map.get(date_key, 0)
                    delta_entry["likes_delta"] = youtube_daily_likes_map.get(date_key, 0)
                elif key == "instagram":
                    delta_entry["views_delta"] = analytics_daily_views.get(key, {}).get(date_key, 0)
                    delta_entry["likes_delta"] = analytics_daily_likes.get(key, {}).get(date_key, 0)
                else:
                    delta_entry["views_delta"] = None if prev_values["views"] is None else views - prev_values["views"]
                    delta_entry["comments_delta"] = None if prev_values["comments"] is None else comments - prev_values["comments"]
                    delta_entry["likes_delta"] = None if prev_values["likes"] is None else likes - prev_values["likes"]
                chart_series_delta[key].append(delta_entry)
                prev_values["views"] = views
                prev_values["comments"] = comments
                prev_values["likes"] = likes

        earliest_cursor = conn.execute(
            f"SELECT MIN(snapshot_date) FROM {SNAPSHOT_TABLE} WHERE 1 = 1{snapshot_brand_clause}",
            snapshot_brand_params,
        )
        earliest_row = earliest_cursor.fetchone()
        earliest_snapshot = earliest_row[0] if earliest_row and earliest_row[0] else start_date

        selected_channel = channel_type if channel_type in chart_series else "all"
        chart_points = chart_series.get(selected_channel, chart_series["all"])

        subscriber_totals: Dict[str, int] = {
            "youtube": 0,
            "instagram": 0,
            "facebook": 0,
            "tiktok": 0,
        }
        try:
            subscriber_totals_cursor = conn.execute(
                f"""
                WITH latest AS (
                    SELECT channel_type, MAX(snapshot_date) AS snapshot_date
                    FROM {SUBSCRIBER_SNAPSHOT_TABLE}
                    WHERE 1 = 1{subscriber_brand_clause}
                    GROUP BY channel_type
                )
                SELECT
                    s.channel_type,
                    SUM(
                        CASE
                            WHEN s.channel_type = 'youtube' THEN COALESCE(s.subscriber_count_exact, 0)
                            ELSE COALESCE(s.subscriber_count_exact, s.subscriber_count, 0)
                        END
                    ) AS subscribers
                FROM {SUBSCRIBER_SNAPSHOT_TABLE} s
                JOIN latest l
                  ON s.channel_type = l.channel_type
                 AND s.snapshot_date = l.snapshot_date
                WHERE 1 = 1{subscriber_brand_clause}
                GROUP BY s.channel_type
                """,
                [*subscriber_brand_params, *subscriber_brand_params],
            )
            for row in _rows_to_dict(subscriber_totals_cursor):
                key = (row.get("channel_type") or "").lower()
                if key in subscriber_totals:
                    subscriber_totals[key] = row.get("subscribers") or 0
        except Exception:
            subscriber_totals = {
                "youtube": 0,
                "instagram": 0,
                "facebook": 0,
                "tiktok": 0,
            }

        subscriber_series: List[Mapping[str, object]] = []
        youtube_subscriber_data_missing = False
        try:
            subscriber_cursor = conn.execute(
                f"""
                SELECT
                    snapshot_date,
                    channel_type,
                    SUM(
                        CASE
                            WHEN channel_type = 'youtube' THEN COALESCE(subscriber_count_exact, 0)
                            ELSE COALESCE(subscriber_count_exact, subscriber_count, 0)
                        END
                    ) AS subscribers_exact,
                    SUM(CASE WHEN subscribers_gained IS NOT NULL THEN subscribers_gained ELSE 0 END) AS subscribers_gained,
                    SUM(CASE WHEN subscribers_lost IS NOT NULL THEN subscribers_lost ELSE 0 END) AS subscribers_lost,
                    SUM(CASE WHEN subscribers_net IS NOT NULL THEN subscribers_net ELSE 0 END) AS subscribers_net,
                    SUM(CASE WHEN subscribers_gained IS NOT NULL THEN 1 ELSE 0 END) AS subscribers_gained_count,
                    SUM(CASE WHEN subscribers_lost IS NOT NULL THEN 1 ELSE 0 END) AS subscribers_lost_count,
                    SUM(CASE WHEN subscribers_net IS NOT NULL THEN 1 ELSE 0 END) AS subscribers_net_count
                FROM {SUBSCRIBER_SNAPSHOT_TABLE}
                WHERE snapshot_date BETWEEN ? AND ?
                  {subscriber_brand_clause}
                GROUP BY snapshot_date, channel_type
                ORDER BY snapshot_date ASC
                """,
                [start_date.isoformat(), end_date.isoformat(), *subscriber_brand_params],
            )
            subscriber_rows = _rows_to_dict(subscriber_cursor)
            youtube_subscriber_data_missing = not any(
                (row.get("channel_type") or "").lower() == "youtube"
                for row in subscriber_rows
            )
            subscriber_map: Dict[str, Dict[str, Dict[str, object]]] = {}
            for row in subscriber_rows:
                date_key = _normalize_date(row["snapshot_date"])
                channel_key = (row.get("channel_type") or "").lower()
                if channel_key not in {"youtube", "instagram", "facebook", "tiktok"}:
                    continue
                gained_count = row.get("subscribers_gained_count") or 0
                lost_count = row.get("subscribers_lost_count") or 0
                net_count = row.get("subscribers_net_count") or 0
                subscriber_map.setdefault(date_key, {})[channel_key] = {
                    "total": row.get("subscribers_exact") or 0,
                    "net": row.get("subscribers_net") if net_count else None,
                    "gained": row.get("subscribers_gained") if gained_count else None,
                    "lost": row.get("subscribers_lost") if lost_count else None,
                    "has_data": True,
                }
            baseline_total = None
            baseline_date_obj: Optional[date] = None
            seed_sum = 0
            try:
                baseline_row = conn.execute(
                    f"""
                    SELECT baseline_date, baseline_subscribers_exact
                    FROM youtube_channels
                    WHERE baseline_date IS NOT NULL
                      AND baseline_subscribers_exact IS NOT NULL
                      {youtube_channels_brand_clause}
                    ORDER BY baseline_date DESC
                    LIMIT 1
                    """,
                    youtube_channels_brand_params,
                ).fetchone()
                if baseline_row and baseline_row[1] is not None:
                    baseline_date_obj = baseline_row[0]
                    baseline_total = int(baseline_row[1])
            except Exception:
                baseline_total = None
                baseline_date_obj = None
            if baseline_total is None or baseline_date_obj is None:
                try:
                    baseline_row = conn.execute(
                        f"""
                        SELECT snapshot_date, subscriber_count_exact
                        FROM shorts_channel_subscriber_daily
                        WHERE channel_type = 'youtube'
                          AND subscriber_count_exact IS NOT NULL
                          {subscriber_brand_clause}
                        ORDER BY snapshot_date ASC
                        LIMIT 1
                        """,
                        subscriber_brand_params,
                    ).fetchone()
                    if baseline_row and baseline_row[1] is not None:
                        baseline_date_obj = baseline_row[0]
                        baseline_total = int(baseline_row[1])
                except Exception:
                    baseline_total = None
                    baseline_date_obj = None
            if baseline_total is not None and baseline_date_obj is not None and start_date > baseline_date_obj:
                try:
                    seed_row = conn.execute(
                        f"""
                        SELECT COALESCE(SUM(subscribers_net), 0)
                        FROM shorts_channel_subscriber_daily
                        WHERE channel_type = 'youtube'
                          AND subscribers_net IS NOT NULL
                          {subscriber_brand_clause}
                          AND snapshot_date > ?
                          AND snapshot_date < ?
                        """,
                        [
                            *subscriber_brand_params,
                            baseline_date_obj.isoformat(),
                            start_date.isoformat(),
                        ],
                    ).fetchone()
                    seed_sum = int(seed_row[0] or 0) if seed_row else 0
                except Exception:
                    seed_sum = 0
            prev_youtube_total: Optional[int] = None
            prev_youtube_has_data = False
            prev_instagram_total: Optional[int] = None
            prev_instagram_has_data = False
            prev_facebook_total: Optional[int] = None
            prev_facebook_has_data = False
            prev_tiktok_total: Optional[int] = None
            prev_tiktok_has_data = False
            computed_youtube_total: Optional[int] = None
            computed_youtube_end_total: Optional[int] = None
            for date_key in date_sequence:
                current_date = date.fromisoformat(date_key)
                per_date = subscriber_map.get(date_key, {})
                youtube = per_date.get("youtube", {})
                instagram = per_date.get("instagram", {})
                facebook = per_date.get("facebook", {})
                tiktok = per_date.get("tiktok", {})
                youtube_has_data = bool(youtube.get("has_data"))
                youtube_total = youtube.get("total", 0) if youtube_has_data else 0
                youtube_gained = youtube.get("gained")
                youtube_lost = youtube.get("lost")
                youtube_net_override = youtube.get("net")
                youtube_net: Optional[int]
                if youtube_net_override is not None:
                    youtube_net = youtube_net_override
                elif youtube_gained is not None or youtube_lost is not None:
                    youtube_net = (youtube_gained or 0) - (youtube_lost or 0)
                else:
                    # YouTube subscriber totals can be rounded/jittery when analytics deltas are missing.
                    # Avoid deriving daily net from raw total differences to prevent false +/-100 swings.
                    youtube_net = None
                if baseline_total is not None and baseline_date_obj is not None:
                    if current_date == baseline_date_obj:
                        computed_youtube_total = baseline_total
                    elif current_date > baseline_date_obj and computed_youtube_total is None:
                        computed_youtube_total = baseline_total + seed_sum
                    if current_date > baseline_date_obj and computed_youtube_total is not None:
                        if youtube_net is not None:
                            computed_youtube_total += youtube_net
                if youtube_has_data:
                    prev_youtube_total = youtube_total
                    prev_youtube_has_data = True
                if computed_youtube_total is not None:
                    youtube_total = computed_youtube_total
                    if current_date == end_date:
                        computed_youtube_end_total = computed_youtube_total
                instagram_has_data = bool(instagram.get("has_data"))
                instagram_total = instagram.get("total", 0) if instagram_has_data else 0
                instagram_net_override = instagram.get("net")
                instagram_net: Optional[int]
                if instagram_net_override is not None:
                    instagram_net = instagram_net_override
                elif not instagram_has_data or not prev_instagram_has_data:
                    instagram_net = None
                else:
                    instagram_net = instagram_total - (prev_instagram_total or 0)
                if instagram_has_data:
                    prev_instagram_total = instagram_total
                    prev_instagram_has_data = True
                facebook_has_data = bool(facebook.get("has_data"))
                facebook_total = facebook.get("total", 0) if facebook_has_data else 0
                facebook_net_override = facebook.get("net")
                facebook_net: Optional[int]
                if facebook_net_override is not None:
                    facebook_net = facebook_net_override
                elif not facebook_has_data or not prev_facebook_has_data:
                    facebook_net = None
                else:
                    facebook_net = facebook_total - (prev_facebook_total or 0)
                if facebook_has_data:
                    prev_facebook_total = facebook_total
                    prev_facebook_has_data = True
                tiktok_has_data = bool(tiktok.get("has_data"))
                tiktok_total = tiktok.get("total", 0) if tiktok_has_data else 0
                tiktok_net_override = tiktok.get("net")
                tiktok_net: Optional[int]
                if tiktok_net_override is not None:
                    tiktok_net = tiktok_net_override
                elif not tiktok_has_data or not prev_tiktok_has_data:
                    tiktok_net = None
                else:
                    tiktok_net = tiktok_total - (prev_tiktok_total or 0)
                if tiktok_has_data:
                    prev_tiktok_total = tiktok_total
                    prev_tiktok_has_data = True
                subscriber_series.append(
                    {
                        "date": date_key,
                        "youtube_total": youtube_total,
                        "youtube_gained": youtube_gained,
                        "youtube_lost": youtube_lost,
                        "youtube_net": youtube_net,
                        "instagram_total": instagram_total,
                        "instagram_net": instagram_net,
                        "facebook_total": facebook_total,
                        "facebook_net": facebook_net,
                        "tiktok_total": tiktok_total,
                        "tiktok_net": tiktok_net,
                    }
                )
            if computed_youtube_end_total is not None:
                subscriber_totals["youtube"] = computed_youtube_end_total
        except Exception:
            subscriber_series = [
                {
                    "date": date_key,
                    "youtube_total": 0,
                    "youtube_gained": 0,
                    "youtube_lost": 0,
                    "youtube_net": 0,
                    "instagram_total": 0,
                    "instagram_net": None,
                    "facebook_total": 0,
                    "facebook_net": None,
                    "tiktok_total": 0,
                    "tiktok_net": None,
                }
                for date_key in date_sequence
            ]
            youtube_subscriber_data_missing = True

    finally:
        conn.close()

    video_rows_total_pages = max((video_rows_total + VIDEO_LIST_PAGE_SIZE - 1) // VIDEO_LIST_PAGE_SIZE, 1)
    if videos_page > video_rows_total_pages:
        videos_page = video_rows_total_pages
        conn = get_db_readonly()
        try:
            video_rows, video_rows_total = _fetch_stats_video_rows(
                conn,
                start_date=start_date,
                end_date=end_date,
                brand_id=current_brand_id(),
                page=videos_page,
            )
        finally:
            conn.close()

    return render_template(
        "video_metrics.html",
        start_date=start_date,
        end_date=end_date,
        channel_type=channel_type,
        selected_metric=selected_metric,
        channel_options=CHANNEL_OPTIONS,
        daily_totals=daily_totals,
        range_totals=aggregated,
        top_videos=top_videos,
        top_view_video=top_videos[0] if top_videos else None,
        top_comment_video=top_comment_video[0] if top_comment_video else None,
        chart_series=chart_series,
        channel_totals=channel_totals,
        selected_channel=selected_channel,
        top_table_rows=top_table_rows,
        video_rows=video_rows,
        videos_page=videos_page,
        video_rows_total=video_rows_total,
        video_rows_total_pages=video_rows_total_pages,
        today_top_videos=today_top_videos,
        week_top_videos=week_top_videos,
        user_time_zone_label=user_time_zone_label,
        chart_series_delta=chart_series_delta,
        earliest_snapshot=earliest_snapshot,
        default_insights=today_top_videos,
        today=today,
        subscriber_totals=subscriber_totals,
        subscriber_series=subscriber_series,
        youtube_subscriber_data_missing=(
            youtube_subscriber_data_missing or _safe_reauth_required()
        ),
        last_api_at=last_api_at,
    )


def _safe_reauth_required() -> bool:
    try:
        current_user = getattr(g, "vs_current_user", None) or {}
        return is_reauth_required(current_user.get("id"))
    except Exception:
        return False


@video_shorts_bp.route("/stats/top_videos", methods=["GET"])
def stats_top_videos():
    date_param = request.args.get("date")
    channel_type = request.args.get("channel", "all")
    if not date_param:
        return jsonify({"videos": [], "date": None, "channel": channel_type})
    parsed_date = _parse_date_param(date_param)
    if not parsed_date:
        return jsonify({"videos": [], "date": None, "channel": channel_type})
    conn = get_db_readonly()
    try:
        videos = _fetch_top_videos_for_date(conn, parsed_date, channel_type, limit=6)
        return jsonify({"videos": videos, "date": parsed_date.isoformat(), "channel": channel_type})
    except Exception:
        return jsonify({"videos": [], "date": parsed_date.isoformat(), "channel": channel_type})
    finally:
        conn.close()
