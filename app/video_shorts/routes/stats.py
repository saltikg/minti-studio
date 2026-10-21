from __future__ import annotations
import json
from bisect import bisect_right
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from flask import current_app, g, jsonify, render_template, request, session
from markupsafe import Markup, escape
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.video_shorts import video_shorts_bp
from app.video_shorts.config import PRIVATE_STATE_DIR, SHORTS_DIR
from app.video_shorts.services.brands import current_brand_id
from app.video_shorts.services.db import get_db_readonly
from app.video_shorts.services.video_metrics import (
    SNAPSHOT_TABLE,
)
from app.video_shorts.services.subscriber_metrics import (
    SUBSCRIBER_SNAPSHOT_TABLE,
)
from app.video_shorts.services.timezones import DEFAULT_TIME_ZONE, TIMEZONE_OPTIONS
from app.video_shorts.services.youtube_token_store import has_refresh_token


CHANNEL_OPTIONS: Sequence[Mapping[str, str]] = [
    {"value": "all", "label": "All channels"},
    {"value": "youtube", "label": "YouTube"},
    {"value": "instagram", "label": "Instagram"},
    {"value": "facebook", "label": "Facebook"},
    {"value": "tiktok", "label": "TikTok"},
]

DEFAULT_WINDOW_DAYS = 30
VIDEO_LIST_PAGE_SIZE = 20
VIDEO_DAILY_DELTAS_TABLE = "analytics.fct_video_daily_deltas"


def _rows_to_dict(cursor) -> List[Mapping[str, object]]:
    cols = [desc[0] for desc in cursor.description]
    return [dict(zip(cols, row)) for row in cursor.fetchall()]


def _fragment_format_stat(value: object) -> str:
    return "0" if value is None else f"{value:,}"


def _fragment_top_video_rows(videos, show_facebook: bool = False) -> Markup:
    if not videos:
        colspan = 5 if show_facebook else 4
        return Markup(f'<tr><td colspan="{colspan}" class="text-center text-muted py-4">No videos found for this range.</td></tr>')
    parts: List[str] = []
    for video in videos:
        thumbnail_url = video.get("thumbnail_url")
        video_url = video.get("video_url")
        if thumbnail_url and video_url:
            thumbnail = f'<a href="{escape(video_url)}" target="_blank" rel="noopener noreferrer" title="Open video"><img src="{escape(thumbnail_url)}" alt="Video thumbnail"></a>'
        elif thumbnail_url:
            thumbnail = f'<img src="{escape(thumbnail_url)}" alt="Video thumbnail">'
        else:
            thumbnail = '<div class="video-thumb-placeholder"></div>'
        facebook_cell = ""
        if show_facebook:
            facebook_value = _fragment_format_stat(video.get("views")) if str(video.get("channel_type") or "").lower() == "facebook" else "—"
            facebook_cell = f'<td class="text-end">{facebook_value}</td>'
        parts.append(
            '<tr>'
            f'<td class="p-0 pe-3 video-thumb-cell">{thumbnail}</td>'
            f'<td><strong>{escape(video.get("video_title") or video.get("video_id") or "")}</strong></td>'
            f'<td class="text-end">{_fragment_format_stat(video.get("views"))}</td>'
            f'<td class="text-end">{_fragment_format_stat(video.get("comments"))}</td>'
            f'{facebook_cell}</tr>'
        )
    return Markup("".join(parts))


def _row_to_brand_dict(row: Optional[Sequence[object]]) -> Optional[Dict[str, object]]:
    if not row:
        return None
    return {
        "id": row[0],
        "owner_user_id": row[1],
        "name": row[2],
        "slug": row[3],
        "is_default": bool(row[4]),
        "created_at": row[5],
        "updated_at": row[6],
    }


def _load_owned_brand(
    conn,
    *,
    user_id: Optional[str],
    brand_id: Optional[str],
) -> Optional[Dict[str, object]]:
    owner_user_id = str(user_id or "").strip()
    requested_brand_id = str(brand_id or "").strip()
    if not owner_user_id or not requested_brand_id:
        return None
    row = conn.execute(
        """
        SELECT id, owner_user_id, name, slug, COALESCE(is_default, FALSE), created_at, updated_at
        FROM shorts_brands
        WHERE owner_user_id = ?
          AND id = ?
        LIMIT 1
        """,
        [owner_user_id, requested_brand_id],
    ).fetchone()
    return _row_to_brand_dict(row)


def _resolve_owned_brand_for_stats(conn, *, user_id: Optional[str]) -> Optional[Dict[str, object]]:
    owner_user_id = str(user_id or "").strip()
    if not owner_user_id:
        g.vs_current_brand = None
        g.vs_brands = []
        session.pop("vs_brand_id", None)
        return None

    brand_rows = conn.execute(
        """
        SELECT id, owner_user_id, name, slug, COALESCE(is_default, FALSE), created_at, updated_at
        FROM shorts_brands
        WHERE owner_user_id = ?
        ORDER BY COALESCE(is_default, FALSE) DESC, created_at ASC, id ASC
        """,
        [owner_user_id],
    ).fetchall()
    owned_brands = [brand for brand in (_row_to_brand_dict(row) for row in brand_rows) if brand]
    g.vs_brands = owned_brands

    requested_brand_id = (
        str((request.args.get("brand_id") or "").strip())
        or str((((getattr(g, "vs_current_brand", None) or {}).get("id")) or "")).strip()
        or str((session.get("vs_brand_id") or "")).strip()
    )
    brand = _load_owned_brand(conn, user_id=owner_user_id, brand_id=requested_brand_id)
    if brand is None:
        brand = owned_brands[0] if owned_brands else None

    g.vs_current_brand = brand
    if brand:
        session["vs_brand_id"] = brand["id"]
    else:
        session.pop("vs_brand_id", None)
    return brand


def _channel_filter_clause(channel_type: Optional[str]) -> Tuple[str, List[object]]:
    if channel_type and channel_type != "all":
        return " AND channel_type = ?", [channel_type]
    return "", []


def _parse_page_arg(name: str, default: int = 1) -> int:
    try:
        return max(int(request.args.get(name, str(default))), 1)
    except (TypeError, ValueError):
        return default


def _build_change_meta(current_value: int, previous_value: Optional[int]) -> Mapping[str, object]:
    if previous_value is None:
        return {"direction": "neutral", "change_label": "—"}
    if previous_value == 0:
        if current_value == 0:
            return {"direction": "neutral", "change_label": "0.0%"}
        return {
            "direction": "up" if current_value > 0 else "neutral",
            "change_label": "Prev day 0",
        }
    pct_change = ((current_value - previous_value) / abs(previous_value)) * 100
    if pct_change > 0:
        direction = "up"
    elif pct_change < 0:
        direction = "down"
    else:
        direction = "neutral"
    return {
        "direction": direction,
        "change_label": f"{pct_change:.1f}%",
    }


def _fetch_period_summary_totals(
    conn,
    *,
    start_date: date,
    end_date: date,
    brand_id: Optional[str] = None,
) -> Dict[str, Dict[str, int]]:
    snapshot_brand_clause, snapshot_brand_params = _brand_scope_clause(
        conn,
        SNAPSHOT_TABLE,
        brand_id=brand_id,
    )
    metric_rows: List[Mapping[str, object]] = []
    try:
        metric_cursor = conn.execute(
            f"""
            WITH ordered AS (
                SELECT
                    channel_type,
                    snapshot_date,
                    video_id,
                    CASE
                        WHEN channel_type = 'instagram' THEN COALESCE(reach, views, 0)
                        ELSE COALESCE(views, 0)
                    END AS current_views,
                    COALESCE(comments, 0) AS current_comments,
                    COALESCE(likes, 0) AS current_likes,
                    LAG(
                        CASE
                            WHEN channel_type = 'instagram' THEN COALESCE(reach, views, 0)
                            ELSE COALESCE(views, 0)
                        END
                    ) OVER (PARTITION BY channel_type, video_id ORDER BY snapshot_date) AS prev_views,
                    LAG(COALESCE(comments, 0)) OVER (PARTITION BY channel_type, video_id ORDER BY snapshot_date) AS prev_comments,
                    LAG(COALESCE(likes, 0)) OVER (PARTITION BY channel_type, video_id ORDER BY snapshot_date) AS prev_likes
                FROM {SNAPSHOT_TABLE}
                WHERE snapshot_date <= ?
                  {snapshot_brand_clause}
            )
            SELECT
                channel_type,
                SUM(
                    CASE
                        WHEN prev_views IS NULL THEN 0
                        ELSE GREATEST(COALESCE(current_views, 0) - COALESCE(prev_views, 0), 0)
                    END
                ) AS views,
                SUM(
                    CASE
                        WHEN prev_comments IS NULL THEN 0
                        ELSE GREATEST(COALESCE(current_comments, 0) - COALESCE(prev_comments, 0), 0)
                    END
                ) AS comments,
                SUM(
                    CASE
                        WHEN prev_likes IS NULL THEN 0
                        ELSE GREATEST(COALESCE(current_likes, 0) - COALESCE(prev_likes, 0), 0)
                    END
                ) AS likes
            FROM ordered
            WHERE snapshot_date BETWEEN ? AND ?
            GROUP BY channel_type
            """,
            [
                end_date.isoformat(),
                *snapshot_brand_params,
                start_date.isoformat(),
                end_date.isoformat(),
            ],
        )
        metric_rows = _rows_to_dict(metric_cursor)
    except Exception:
        metric_rows = []

    totals_by_channel: Dict[str, Dict[str, int]] = {}
    for row in metric_rows:
        channel_key = (row.get("channel_type") or "").lower() or "all"
        totals_by_channel[channel_key] = {
            "views": int(row.get("views") or 0),
            "comments": int(row.get("comments") or 0),
            "likes": int(row.get("likes") or 0),
        }

    for option in CHANNEL_OPTIONS:
        totals_by_channel.setdefault(
            option["value"],
            {
                "views": 0,
                "comments": 0,
                "likes": 0,
            },
        )

    totals_by_channel["all"] = {
        "views": sum(entry["views"] for key, entry in totals_by_channel.items() if key != "all"),
        "comments": sum(entry["comments"] for key, entry in totals_by_channel.items() if key != "all"),
        "likes": sum(entry["likes"] for key, entry in totals_by_channel.items() if key != "all"),
    }
    return totals_by_channel


def _fetch_dashboard_deltas(
    conn,
    *,
    previous_start: date,
    current_start: date,
    current_end: date,
    brand_id: Optional[str],
) -> Tuple[Dict[str, Dict[str, int]], Dict[str, Dict[str, int]], Dict[str, Dict[str, Dict[str, int]]]]:
    if not brand_id:
        return {}, {}, {}
    live_end = current_end == date.today()
    fact_end = current_end - timedelta(days=1) if live_end else current_end
    rows = _rows_to_dict(
        conn.execute(
            f"""
            WITH metric_keys AS MATERIALIZED (
                SELECT DISTINCT brand_id, channel_type, video_id
                FROM {VIDEO_DAILY_DELTAS_TABLE}
                WHERE brand_id = ?
                  AND snapshot_date BETWEEN ? AND ?
            ), first_seen AS MATERIALIZED (
                SELECT
                    metric_keys.*,
                    (
                        SELECT MIN(prior.snapshot_date)
                        FROM {SNAPSHOT_TABLE} prior
                        WHERE prior.brand_id = metric_keys.brand_id
                          AND prior.channel_type = metric_keys.channel_type
                          AND prior.video_id = metric_keys.video_id
                    ) AS first_snapshot_date
                FROM metric_keys
            ), fact_daily AS (
                SELECT
                    snapshot_date,
                    deltas.channel_type,
                    SUM(GREATEST(COALESCE(CASE WHEN deltas.channel_type = 'instagram' THEN reach ELSE views END, 0), 0)) AS views,
                    SUM(GREATEST(COALESCE(comments, 0), 0)) AS comments,
                    SUM(GREATEST(COALESCE(likes, 0), 0)) AS likes
                FROM {VIDEO_DAILY_DELTAS_TABLE} deltas
                JOIN first_seen
                  ON first_seen.brand_id = deltas.brand_id
                 AND first_seen.channel_type = deltas.channel_type
                 AND first_seen.video_id = deltas.video_id
                 AND deltas.snapshot_date > first_seen.first_snapshot_date
                WHERE deltas.brand_id = ?
                  AND deltas.snapshot_date BETWEEN ? AND ?
                GROUP BY snapshot_date, deltas.channel_type
            ), live_keys AS MATERIALIZED (
                SELECT DISTINCT channel_type, video_id
                FROM {SNAPSHOT_TABLE}
                WHERE ?
                  AND brand_id = ?
                  AND snapshot_date = ?
            ), live_rows AS (
                SELECT snapshots.*
                FROM {SNAPSHOT_TABLE} snapshots
                JOIN live_keys USING (channel_type, video_id)
                WHERE snapshots.brand_id = ?
                  AND snapshots.snapshot_date = ?
                UNION ALL
                SELECT prior.*
                FROM live_keys
                JOIN LATERAL (
                    SELECT snapshots.*
                    FROM {SNAPSHOT_TABLE} snapshots
                    WHERE snapshots.brand_id = ?
                      AND snapshots.channel_type = live_keys.channel_type
                      AND snapshots.video_id = live_keys.video_id
                      AND snapshots.snapshot_date < ?
                    ORDER BY snapshots.snapshot_date DESC, snapshots.effective_at DESC
                    LIMIT 1
                ) prior ON TRUE
            ), live_ordered AS (
                SELECT
                    snapshot_date,
                    channel_type,
                    video_id,
                    CASE WHEN channel_type = 'instagram' THEN COALESCE(reach, 0) ELSE COALESCE(views, 0) END AS current_views,
                    COALESCE(comments, 0) AS current_comments,
                    COALESCE(likes, 0) AS current_likes,
                    LAG(CASE WHEN channel_type = 'instagram' THEN COALESCE(reach, 0) ELSE COALESCE(views, 0) END)
                        OVER (PARTITION BY channel_type, video_id ORDER BY snapshot_date, effective_at) AS prev_views,
                    LAG(COALESCE(comments, 0))
                        OVER (PARTITION BY channel_type, video_id ORDER BY snapshot_date, effective_at) AS prev_comments,
                    LAG(COALESCE(likes, 0))
                        OVER (PARTITION BY channel_type, video_id ORDER BY snapshot_date, effective_at) AS prev_likes
                FROM live_rows
            ), live_daily AS (
                SELECT
                    snapshot_date,
                    channel_type,
                    SUM(CASE WHEN prev_views IS NULL THEN 0 ELSE GREATEST(current_views - prev_views, 0) END) AS views,
                    SUM(CASE WHEN prev_comments IS NULL THEN 0 ELSE GREATEST(current_comments - prev_comments, 0) END) AS comments,
                    SUM(CASE WHEN prev_likes IS NULL THEN 0 ELSE GREATEST(current_likes - prev_likes, 0) END) AS likes
                FROM live_ordered
                WHERE snapshot_date = ?
                GROUP BY snapshot_date, channel_type
            ), combined AS (
                SELECT * FROM fact_daily
                UNION ALL
                SELECT * FROM live_daily
            )
            SELECT
                snapshot_date,
                channel_type,
                SUM(views) AS views,
                SUM(comments) AS comments,
                SUM(likes) AS likes
            FROM combined
            GROUP BY snapshot_date, channel_type
            ORDER BY snapshot_date, channel_type
            """,
            [
                brand_id, previous_start.isoformat(), fact_end.isoformat(),
                brand_id, previous_start.isoformat(), fact_end.isoformat(),
                live_end, brand_id, current_end.isoformat(),
                brand_id, current_end.isoformat(),
                brand_id, current_end.isoformat(),
                current_end.isoformat(),
            ],
        )
    )
    current: Dict[str, Dict[str, int]] = {}
    previous: Dict[str, Dict[str, int]] = {}
    daily: Dict[str, Dict[str, Dict[str, int]]] = {}
    for row in rows:
        channel = str(row.get("channel_type") or "").lower()
        snapshot_date = row.get("snapshot_date")
        if not channel or not isinstance(snapshot_date, date):
            continue
        values = {key: row.get(key) or 0 for key in ("views", "comments", "likes")}
        target = current if snapshot_date >= current_start else previous
        channel_totals = target.setdefault(channel, {"views": 0, "comments": 0, "likes": 0})
        for key, value in values.items():
            channel_totals[key] += int(value)
        if snapshot_date >= current_start:
            daily.setdefault(channel, {})[snapshot_date.isoformat()] = values
    for target in (current, previous):
        for option in CHANNEL_OPTIONS:
            target.setdefault(option["value"], {"views": 0, "comments": 0, "likes": 0})
        target["all"] = {
            key: sum(values[key] for channel, values in target.items() if channel != "all")
            for key in ("views", "comments", "likes")
        }
    return current, previous, daily


def _brand_scope_clause(
    conn,
    table_name: str,
    *,
    alias: Optional[str] = None,
    brand_id: Optional[str] = None,
) -> Tuple[str, List[object]]:
    scoped_brand_id = brand_id if brand_id is not None else current_brand_id()
    if not scoped_brand_id:
        return " AND 1 = 0", []
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


def _build_filters(
    conn,
    channel_type: str,
    start: date,
    end: date,
    *,
    brand_id: Optional[str] = None,
):
    clause = ""
    params: List[object] = [start.isoformat(), end.isoformat()]
    if channel_type and channel_type != "all":
        clause = " AND channel_type = ?"
        params.append(channel_type)
    brand_clause, brand_params = _brand_scope_clause(conn, SNAPSHOT_TABLE, brand_id=brand_id)
    clause += brand_clause
    params.extend(brand_params)
    return clause, params


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
    if not brand_id:
        return {}
    rows = conn.execute(
        "SELECT id, video_id FROM youtube_videos WHERE brand_id = ?",
        [brand_id],
    ).fetchall()
    plan_files: Dict[str, Path] = {}
    for root in (PRIVATE_STATE_DIR, PRIVATE_STATE_DIR / "legacy", SHORTS_DIR):
        try:
            for entry in root.iterdir():
                if entry.name.endswith("_plan.json"):
                    plan_files.setdefault(entry.name, entry)
        except OSError:
            continue
    published_schedule: Dict[str, date] = {}
    for video_pk, source_video_id in rows:
        plan_path = plan_files.get(f"{video_pk}_plan.json") or plan_files.get(f"{source_video_id}_plan.json")
        if plan_path is None:
            continue
        try:
            raw = json.loads(plan_path.read_text())
        except (OSError, ValueError, TypeError):
            continue
        for entry in raw.get("plan") or raw.get("clips") or []:
            if not isinstance(entry, dict) or str(entry.get("publish_status") or "").lower() != "published":
                continue
            short_id = str(entry.get("yt_video_id") or entry.get("short_video_id") or "").strip()
            publish_date = _parse_publish_date(entry.get("publish_at_iso") or entry.get("publish_at"))
            if not short_id or publish_date is None:
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
    if not published_video_ids or not brand_id:
        return {}, {}, {}, {}, {}
    values_sql = ", ".join("(?, CAST(? AS DATE))" for _ in published_video_ids)
    published_params: List[object] = []
    for video_id, publish_date in (published_schedule or {}).items():
        published_params.extend([video_id, publish_date.isoformat()])
    cursor = conn.execute(
        f"""
        WITH published(video_id, publish_date) AS (
            VALUES {values_sql}
        ), seed_snapshots AS (
            SELECT
                seed.snapshot_date,
                published.video_id,
                seed.views,
                seed.likes,
                seed.effective_at
            FROM published
            JOIN LATERAL (
                SELECT snapshot_date, views, likes, effective_at
                FROM {SNAPSHOT_TABLE} s
                WHERE s.brand_id = ?
                  AND s.channel_type = 'youtube'
                  AND s.video_id = published.video_id
                  AND COALESCE(s.stats_source, '') NOT LIKE ?
                  AND s.snapshot_date < ?
                ORDER BY s.snapshot_date DESC, s.effective_at DESC
                LIMIT 1
            ) seed ON TRUE
        ), range_snapshots AS (
            SELECT
                s.snapshot_date,
                s.video_id,
                s.views,
                s.likes,
                s.effective_at
            FROM {SNAPSHOT_TABLE} s
            JOIN published ON published.video_id = s.video_id
            WHERE s.brand_id = ?
              AND s.channel_type = 'youtube'
              AND COALESCE(s.stats_source, '') NOT LIKE ?
              AND s.snapshot_date BETWEEN ? AND ?
        ), selected AS (
            SELECT * FROM seed_snapshots
            UNION ALL
            SELECT * FROM range_snapshots
        )
        SELECT
            snapshot_date,
            video_id,
            COALESCE(views, 0) AS views,
            COALESCE(likes, 0) AS likes
        FROM selected
        ORDER BY snapshot_date, video_id, effective_at
        """,
        [
            *published_params,
            brand_id,
            "estimated_gap_fill_from_%",
            start.isoformat(),
            brand_id,
            "estimated_gap_fill_from_%",
            start.isoformat(),
            end.isoformat(),
        ],
    )
    snapshots_by_video: Dict[str, List[Tuple[date, int, int]]] = {}
    for row in _rows_to_dict(cursor):
        snapshot_date = row.get("snapshot_date")
        video_id = str(row.get("video_id") or "").strip()
        if isinstance(snapshot_date, date) and video_id:
            snapshots_by_video.setdefault(video_id, []).append(
                (snapshot_date, int(row.get("views") or 0), int(row.get("likes") or 0))
            )
    snapshot_dates_by_video = {
        video_id: [snapshot[0] for snapshot in snapshots]
        for video_id, snapshots in snapshots_by_video.items()
    }
    total_views: Dict[str, int] = {}
    total_likes: Dict[str, int] = {}
    delta_views: Dict[str, int] = {}
    delta_likes: Dict[str, int] = {}
    video_counts: Dict[str, int] = {}
    current = start
    while current <= end:
        date_key = current.isoformat()
        previous_day = current - timedelta(days=1)
        day_total_views = day_total_likes = day_delta_views = day_delta_likes = day_video_count = 0
        for video_id, publish_date in (published_schedule or {}).items():
            if publish_date > current:
                continue
            day_video_count += 1
            snapshots = snapshots_by_video.get(video_id, [])
            snapshot_dates = snapshot_dates_by_video.get(video_id, [])
            current_index = bisect_right(snapshot_dates, current) - 1
            previous_index = bisect_right(snapshot_dates, previous_day) - 1
            current_snapshot = snapshots[current_index] if current_index >= 0 else None
            previous_snapshot = snapshots[previous_index] if previous_index >= 0 else None
            if current_snapshot is not None:
                day_total_views += current_snapshot[1]
                day_total_likes += current_snapshot[2]
            if current_snapshot is not None and previous_snapshot is not None:
                day_delta_views += max(current_snapshot[1] - previous_snapshot[1], 0)
                day_delta_likes += max(current_snapshot[2] - previous_snapshot[2], 0)
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
    if not brand_id:
        return [], 0
    offset = (page - 1) * page_size
    rows = conn.execute(
        """
        WITH base AS (
            SELECT
                gv.generated_video_id,
                gv.generated_title,
                gv.youtube_video_id,
                gv.instagram_media_id,
                COALESCE(gv.youtube_published_at, gv.published_at, gv.planned_publish_at) AS publish_at,
                COUNT(*) OVER () AS total_count
            FROM analytics.dim_generated_videos gv
            WHERE gv.publish_status = 'published'
              AND COALESCE(gv.youtube_published_at, gv.published_at, gv.planned_publish_at)::date BETWEEN ? AND ?
              AND gv.brand_id = ?
            ORDER BY publish_at DESC NULLS LAST, gv.generated_video_id DESC
            LIMIT ? OFFSET ?
        )
        SELECT
            b.generated_title,
            b.publish_at,
            b.youtube_video_id,
            COALESCE(yt.views, 0) AS youtube_views,
            COALESCE(ig.instagram_views, 0) AS instagram_views,
            b.total_count
        FROM base b
        LEFT JOIN LATERAL (
            SELECT s.views
            FROM main.shorts_video_daily_snapshots s
            WHERE s.brand_id = ?
              AND s.channel_type = 'youtube'
              AND s.video_id = b.youtube_video_id
            ORDER BY s.snapshot_date DESC, s.effective_at DESC
            LIMIT 1
        ) yt ON TRUE
        LEFT JOIN LATERAL (
            SELECT COALESCE(s.reach, s.views, 0) AS instagram_views
            FROM main.shorts_video_daily_snapshots s
            WHERE s.brand_id = ?
              AND s.channel_type = 'instagram'
              AND s.video_id = b.instagram_media_id
            ORDER BY s.snapshot_date DESC, s.effective_at DESC
            LIMIT 1
        ) ig ON TRUE
        ORDER BY b.publish_at DESC NULLS LAST, b.generated_video_id DESC
        """,
        [
            start_date.isoformat(),
            end_date.isoformat(),
            brand_id,
            page_size,
            offset,
            brand_id,
            brand_id,
        ],
    ).fetchall()

    items: List[Mapping[str, object]] = []
    total_count = int(rows[0][5] or 0) if rows else 0
    for title, publish_at, youtube_video_id, youtube_views, instagram_views, _total_count in rows:
        youtube_video_id_text = str(youtube_video_id or "").strip()
        youtube_views_value = int(youtube_views or 0)
        instagram_views_value = int(instagram_views or 0)
        items.append(
            {
                "video_title": (title or "").strip() or "Untitled video",
                "publish_at": publish_at,
                "youtube_views": youtube_views_value,
                "instagram_views": instagram_views_value,
                "total_views": youtube_views_value + instagram_views_value,
                "thumbnail_url": (
                    f"https://i.ytimg.com/vi/{youtube_video_id_text}/mqdefault.jpg"
                    if youtube_video_id_text
                    else None
                ),
            }
        )
    return items, total_count


@video_shorts_bp.route("/stats/widgets", methods=["GET"])
def video_stats_widgets():
    return jsonify(_render_video_stats(load_widgets=True))


@video_shorts_bp.route("/stats", methods=["GET"])
def video_stats_page():
    return _render_video_stats(load_widgets=False)


def _render_video_stats(*, load_widgets: bool):
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
    period_days = (end_date - start_date).days + 1
    previous_period_end = start_date - timedelta(days=1)
    previous_period_start = previous_period_end - timedelta(days=period_days - 1)

    if not load_widgets:
        return render_template(
            "video_metrics.html",
            dashboard_shell=True,
            start_date=start_date,
            end_date=end_date,
            channel_type=channel_type,
            selected_metric=selected_metric,
        )

    user_time_zone = _get_user_timezone()
    user_time_zone_label = _timezone_label(user_time_zone)
    current_user = getattr(g, "vs_current_user", None) or {}
    oauth_reauth_required = False
    active_brand_id = str(current_brand_id() or "").strip() or None
    youtube_connected = has_refresh_token(current_user.get("id"), brand_id=active_brand_id)
    conn = get_db_readonly()
    try:
        snapshot_brand_clause, snapshot_brand_params = _brand_scope_clause(
            conn,
            SNAPSHOT_TABLE,
            brand_id=active_brand_id,
        )
        period_totals_by_channel, previous_period_totals_by_channel, dashboard_deltas = _fetch_dashboard_deltas(
            conn,
            previous_start=previous_period_start,
            current_start=start_date,
            current_end=end_date,
            brand_id=active_brand_id,
        )
        analytics_daily_views = {
            channel: {day: values["views"] for day, values in days.items()}
            for channel, days in dashboard_deltas.items()
        }
        analytics_daily_likes = {
            channel: {day: values["likes"] for day, values in days.items()}
            for channel, days in dashboard_deltas.items()
        }
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

        filter_clause, params = _build_filters(
            conn,
            channel_type,
            start_date,
            end_date,
            brand_id=active_brand_id,
        )
        chart_clause = filter_clause
        chart_params = list(params)
        daily_cursor = conn.execute(
            f"""
            SELECT
                snapshot_date,
                channel_type,
                SUM(COALESCE(views, 0)) AS views,
                SUM(COALESCE(comments, 0)) AS comments,
                SUM(COALESCE(likes, 0)) AS likes,
                SUM(COALESCE(impressions, 0)) AS impressions,
                SUM(COALESCE(reach, 0)) AS reach,
                SUM(COALESCE(saved, 0)) AS saved,
                SUM(CASE WHEN channel_type = 'youtube' THEN COALESCE(views, 0) ELSE 0 END) AS youtube_views_total,
                SUM(CASE WHEN channel_type = 'youtube' THEN COALESCE(likes, 0) ELSE 0 END) AS youtube_likes_total,
                SUM(CASE WHEN channel_type = 'youtube' AND views IS NOT NULL THEN 1 ELSE 0 END) AS youtube_views_rows,
                MAX(effective_at) AS last_updated,
                (SELECT MIN(all_rows.snapshot_date) FROM {SNAPSHOT_TABLE} all_rows WHERE all_rows.brand_id = ?) AS earliest_snapshot
            FROM {SNAPSHOT_TABLE}
            WHERE snapshot_date BETWEEN ? AND ?{chart_clause}
            GROUP BY snapshot_date, channel_type
            ORDER BY snapshot_date ASC, channel_type ASC
            """,
            [active_brand_id, *chart_params],
        )
        channel_rows = _rows_to_dict(daily_cursor)
        earliest_snapshot = (
            channel_rows[0].get("earliest_snapshot") if channel_rows else None
        ) or start_date
        daily_by_date: Dict[str, Dict[str, object]] = {}
        for channel_row in channel_rows:
            date_key = _normalize_date(channel_row.get("snapshot_date"))
            aggregate = daily_by_date.setdefault(
                date_key,
                {
                    "snapshot_date": channel_row.get("snapshot_date"),
                    "views": 0,
                    "comments": 0,
                    "likes": 0,
                    "impressions": 0,
                    "reach": 0,
                    "saved": 0,
                    "youtube_views_total": 0,
                    "youtube_likes_total": 0,
                    "youtube_views_rows": 0,
                    "last_updated": None,
                },
            )
            for metric in (
                "views", "comments", "likes", "impressions", "reach", "saved",
                "youtube_views_total", "youtube_likes_total", "youtube_views_rows",
            ):
                aggregate[metric] = (aggregate.get(metric) or 0) + (channel_row.get(metric) or 0)
            row_updated = channel_row.get("last_updated")
            if row_updated is not None and (aggregate["last_updated"] is None or row_updated > aggregate["last_updated"]):
                aggregate["last_updated"] = row_updated
        daily_totals = list(daily_by_date.values())
        last_api_at = max(
            (row.get("last_updated") for row in daily_totals if row.get("last_updated") is not None),
            default=None,
        )
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

        prev_date = start_date - timedelta(days=1)
        scoped_channel_clause, scoped_channel_params = _channel_filter_clause(channel_type)
        totals_cursor = conn.execute(
            f"""
            WITH latest AS (
                SELECT channel_type, MAX(snapshot_date) AS snapshot_date
                FROM {SNAPSHOT_TABLE}
                WHERE snapshot_date BETWEEN ? AND ?
                  AND brand_id = ?
                  {scoped_channel_clause}
                GROUP BY channel_type
            ), selected AS (
                SELECT 'current' AS period_name, s.*
                FROM {SNAPSHOT_TABLE} s
                JOIN latest l
                  ON s.channel_type = l.channel_type
                 AND s.snapshot_date = l.snapshot_date
                WHERE s.brand_id = ?
                  {scoped_channel_clause}
                UNION ALL
                SELECT 'previous' AS period_name, s.*
                FROM {SNAPSHOT_TABLE} s
                WHERE s.snapshot_date = ?
                  AND s.brand_id = ?
                  {scoped_channel_clause}
            )
            SELECT
                period_name,
                channel_type,
                SUM(COALESCE(views, 0)) AS views,
                SUM(COALESCE(comments, 0)) AS comments,
                SUM(COALESCE(likes, 0)) AS likes,
                SUM(COALESCE(impressions, 0)) AS impressions,
                SUM(COALESCE(reach, 0)) AS reach,
                SUM(COALESCE(saved, 0)) AS saved
            FROM selected
            GROUP BY period_name, channel_type
            """,
            [
                start_date.isoformat(),
                end_date.isoformat(),
                active_brand_id,
                *scoped_channel_params,
                active_brand_id,
                *scoped_channel_params,
                prev_date.isoformat(),
                active_brand_id,
                *scoped_channel_params,
            ],
        )
        totals_rows = _rows_to_dict(totals_cursor)
        channel_totals: Dict[str, Mapping[str, int]] = {}
        prev_totals: Dict[str, Mapping[str, int]] = {}
        for row in totals_rows:
            key = row.get("channel_type") or "all"
            effective_views = (row.get("reach") or 0) if key == "instagram" else (row.get("views") or 0)
            target_totals = channel_totals if row.get("period_name") == "current" else prev_totals
            target_totals[key] = {
                "views": effective_views,
                "comments": row.get("comments") or 0,
                "likes": row.get("likes") or 0,
                "impressions": row.get("impressions") or 0,
                "reach": row.get("reach") or 0,
                "saved": row.get("saved") or 0,
            }
        def _empty_totals() -> Dict[str, int]:
            return {
                "views": 0,
                "comments": 0,
                "likes": 0,
                "impressions": 0,
                "reach": 0,
                "saved": 0,
            }
        for totals in (channel_totals, prev_totals):
            totals.setdefault(
                "all",
                {
                    metric: sum(entry[metric] for entry in totals.values())
                    for metric in ("views", "comments", "likes", "impressions", "reach", "saved")
                },
            )
        for option in CHANNEL_OPTIONS:
            channel_totals.setdefault(option["value"], _empty_totals())
        # These legacy collections are no longer rendered by video_metrics.html.
        top_videos: List[Mapping[str, object]] = []
        top_comment_video: List[Mapping[str, object]] = []
        top_table_rows: List[Mapping[str, object]] = []
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
        channel_map: Dict[str, Dict[str, Mapping[str, object]]] = {}
        for row in channel_rows:
            key = row.get("channel_type") or "all"
            date_key = _normalize_date(row["snapshot_date"])
            channel_map.setdefault(key, {})[date_key] = row
        instagram_total_reach_map = {
            date_key: 0
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

        selected_channel = channel_type if channel_type in chart_series else "all"
        chart_points = chart_series.get(selected_channel, chart_series["all"])
        summary_card_specs = [
            ("Total views", "views", "visibility"),
            ("Total comments", "comments", "comment"),
            ("Total likes", "likes", "thumb_up"),
        ]
        summary_cards_by_channel: Dict[str, Dict[str, Mapping[str, object]]] = {}
        for channel_key in ["all", *[option["value"] for option in CHANNEL_OPTIONS if option["value"] != "all"]]:
            current_totals = period_totals_by_channel.get(channel_key) or period_totals_by_channel.get("all", {})
            previous_totals = previous_period_totals_by_channel.get(channel_key) or previous_period_totals_by_channel.get("all", {})
            card_map: Dict[str, Mapping[str, object]] = {}
            for title, key, icon in summary_card_specs:
                current_value = int(current_totals.get(key) or 0)
                previous_value = previous_totals.get(key)
                change_meta = _build_change_meta(current_value, int(previous_value) if previous_value is not None else None)
                card_map[key] = {
                    "key": key,
                    "title": title,
                    "icon": icon,
                    "display_value": current_value,
                    "change_label": change_meta["change_label"],
                    "direction": change_meta["direction"],
                }
            summary_cards_by_channel[channel_key] = card_map
        summary_cards = [
            summary_cards_by_channel.get(selected_channel, summary_cards_by_channel["all"])[key]
            for _title, key, _icon in summary_card_specs
        ]

        subscriber_totals: Dict[str, int] = {
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
                WITH series AS (
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
                      AND brand_id = ?
                    GROUP BY snapshot_date, channel_type
                ), latest_dates AS (
                    SELECT channel_type, MAX(snapshot_date) AS snapshot_date
                    FROM {SUBSCRIBER_SNAPSHOT_TABLE}
                    WHERE brand_id = ?
                    GROUP BY channel_type
                ), latest_totals AS (
                    SELECT
                        COALESCE(SUM(CASE WHEN s.channel_type = 'youtube' THEN COALESCE(s.subscriber_count_exact, 0) ELSE 0 END), 0) AS latest_youtube,
                        COALESCE(SUM(CASE WHEN s.channel_type = 'instagram' THEN COALESCE(s.subscriber_count_exact, s.subscriber_count, 0) ELSE 0 END), 0) AS latest_instagram,
                        COALESCE(SUM(CASE WHEN s.channel_type = 'facebook' THEN COALESCE(s.subscriber_count_exact, s.subscriber_count, 0) ELSE 0 END), 0) AS latest_facebook,
                        COALESCE(SUM(CASE WHEN s.channel_type = 'tiktok' THEN COALESCE(s.subscriber_count_exact, s.subscriber_count, 0) ELSE 0 END), 0) AS latest_tiktok
                    FROM {SUBSCRIBER_SNAPSHOT_TABLE} s
                    JOIN latest_dates l
                      ON l.channel_type = s.channel_type
                     AND l.snapshot_date = s.snapshot_date
                    WHERE s.brand_id = ?
                ), baseline_candidates AS (
                    SELECT baseline_date, baseline_subscribers_exact, 0 AS priority
                    FROM youtube_channels
                    WHERE baseline_date IS NOT NULL
                      AND baseline_subscribers_exact IS NOT NULL
                      AND brand_id = ?
                    ORDER BY baseline_date DESC
                    LIMIT 1
                ), fallback_candidate AS (
                    SELECT snapshot_date AS baseline_date, subscriber_count_exact AS baseline_subscribers_exact, 1 AS priority
                    FROM {SUBSCRIBER_SNAPSHOT_TABLE}
                    WHERE channel_type = 'youtube'
                      AND subscriber_count_exact IS NOT NULL
                      AND brand_id = ?
                    ORDER BY snapshot_date ASC
                    LIMIT 1
                ), baseline AS (
                    SELECT baseline_date, baseline_subscribers_exact
                    FROM (
                        SELECT * FROM baseline_candidates
                        UNION ALL
                        SELECT * FROM fallback_candidate
                    ) candidates
                    ORDER BY priority
                    LIMIT 1
                ), baseline_meta AS (
                    SELECT
                        b.baseline_date,
                        b.baseline_subscribers_exact,
                        COALESCE(SUM(s.subscribers_net) FILTER (
                            WHERE s.snapshot_date > b.baseline_date AND s.snapshot_date < ?
                        ), 0) AS seed_sum
                    FROM baseline b
                    LEFT JOIN {SUBSCRIBER_SNAPSHOT_TABLE} s
                      ON s.brand_id = ?
                     AND s.channel_type = 'youtube'
                     AND s.subscribers_net IS NOT NULL
                    GROUP BY b.baseline_date, b.baseline_subscribers_exact
                )
                SELECT
                    series.*,
                    latest_totals.*,
                    baseline_meta.*,
                    EXISTS (
                        SELECT 1
                        FROM youtube_oauth_tokens_v2 oauth
                        WHERE oauth.user_id = ?
                          AND COALESCE(oauth.reauth_required, 0) <> 0
                    ) AS oauth_reauth_required
                FROM latest_totals
                LEFT JOIN baseline_meta ON TRUE
                LEFT JOIN series ON TRUE
                ORDER BY series.snapshot_date ASC, series.channel_type ASC
                """,
                [
                    start_date.isoformat(), end_date.isoformat(), active_brand_id,
                    active_brand_id, active_brand_id,
                    active_brand_id, active_brand_id, start_date.isoformat(), active_brand_id,
                    current_user.get("id"),
                ],
            )
            subscriber_rows = _rows_to_dict(subscriber_cursor)
            if subscriber_rows:
                first_subscriber_row = subscriber_rows[0]
                oauth_reauth_required = bool(first_subscriber_row.get("oauth_reauth_required"))
                for platform in subscriber_totals:
                    subscriber_totals[platform] = int(first_subscriber_row.get(f"latest_{platform}") or 0)
                baseline_date_obj = first_subscriber_row.get("baseline_date")
                baseline_total = (
                    int(first_subscriber_row.get("baseline_subscribers_exact"))
                    if first_subscriber_row.get("baseline_subscribers_exact") is not None
                    else None
                )
                seed_sum = int(first_subscriber_row.get("seed_sum") or 0)
            else:
                baseline_date_obj = None
                baseline_total = None
                seed_sum = 0
            subscriber_rows = [row for row in subscriber_rows if row.get("snapshot_date") is not None]
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

    template_context = dict(
        start_date=start_date,
        end_date=end_date,
        channel_type=channel_type,
        selected_metric=selected_metric,
        channel_options=CHANNEL_OPTIONS,
        daily_totals=daily_totals,
        range_totals=period_totals_by_channel.get(selected_channel) or period_totals_by_channel.get("all", {}),
        summary_cards=summary_cards,
        summary_cards_by_channel=summary_cards_by_channel,
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
            youtube_subscriber_data_missing or oauth_reauth_required
        ),
        last_api_at=last_api_at,
        stats_connect_empty=bool(not youtube_connected and not channel_rows),
    )
    template = current_app.jinja_env.get_template("video_metrics.html")
    fragment_context = template.new_context(
        {
            "dashboard_shell": False,
            "dashboard_fragment": True,
            "format_stat": _fragment_format_stat,
            "render_top_video_rows": _fragment_top_video_rows,
            **template_context,
        }
    )
    rendered_page = "".join(template.blocks["content"](fragment_context))
    rendered_page += "".join(template.blocks["scripts"](fragment_context))
    return {"html": rendered_page, "cached_for_seconds": 0}


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
