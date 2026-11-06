from __future__ import annotations
import json
import io
import re
from bisect import bisect_right
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from flask import abort, current_app, g, jsonify, render_template, request, session, url_for, send_file
from markupsafe import Markup, escape
from werkzeug.exceptions import HTTPException
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.video_shorts import video_shorts_bp
from app.video_shorts.config import PRIVATE_STATE_DIR, SHORTS_DIR
from app.video_shorts.services.brands import current_brand_id
from app.video_shorts.services.admin_operation_scope import request_admin_operation_scope
from app.video_shorts.services.db import get_db_readonly
from app.video_shorts.services.dashboard_metrics import (
    load_dashboard_metrics,
    load_monthly_report_metrics,
    load_top_videos_for_date,
)
from app.video_shorts.services.stats_report import (
    REPORT_RENDER_ROUTE,
    ReportGrantError,
    ReportRenderBusy,
    consume_report_grant,
    issue_report_grant,
    render_report_pdf,
)
from app.video_shorts.services.feature_flags import tiktok_enabled
from app.video_shorts.services.video_metrics import (
    SNAPSHOT_TABLE,
)
from app.video_shorts.services.timezones import DEFAULT_TIME_ZONE, TIMEZONE_OPTIONS


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
    if value is None:
        return "Pending"
    if isinstance(value, str):
        return value
    return f"{value:,}"


def _fragment_top_video_rows(videos, show_facebook: bool = False) -> Markup:
    if not videos:
        return Markup('<tr><td colspan="4" class="text-center text-muted py-4">No videos found for this range.</td></tr>')
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
        views = _fragment_format_stat(video.get("views"))
        lifetime = _fragment_format_stat(video.get("lifetime_views"))
        parts.append(
            '<tr>'
            f'<td class="p-0 pe-3 video-thumb-cell">{thumbnail}</td>'
            f'<td><strong>{escape(video.get("video_title") or video.get("video_id") or "")}</strong></td>'
            f'<td class="text-end">{views}<div class="small text-muted">{lifetime} lifetime</div></td>'
            f'<td class="text-end">{_fragment_format_stat(video.get("comments"))}</td>'
            '</tr>'
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


def _resolve_stats_tenant(conn) -> Dict[str, object]:
    operation_scope = request_admin_operation_scope()
    if operation_scope:
        if operation_scope["workspace_kind"] != "customer":
            abort(403)
        owner_user_id = operation_scope["owner_user_id"]
        brand_id = operation_scope["brand_id"]
        brand = _load_owned_brand(conn, user_id=owner_user_id, brand_id=brand_id)
        if not brand:
            abort(403)
        timezone_row = conn.execute(
            "SELECT time_zone FROM shorts_users WHERE CAST(id AS VARCHAR) = CAST(? AS VARCHAR) LIMIT 1",
            [owner_user_id],
        ).fetchone()
        return {
            "owner_user_id": owner_user_id,
            "brand_id": brand_id,
            "brand_name": str(brand.get("name") or "Customer").strip() or "Customer",
            "time_zone": str((timezone_row[0] if timezone_row else "") or DEFAULT_TIME_ZONE).strip() or DEFAULT_TIME_ZONE,
            "is_admin_operation": True,
        }

    current_user = getattr(g, "vs_current_user", None) or {}
    active_brand_id = str(current_brand_id() or "").strip() or None
    current_brand = getattr(g, "vs_current_brand", None) or {}
    owner_user_id = str(current_user.get("id") or "").strip()
    if owner_user_id and active_brand_id and str(current_brand.get("id") or "") != active_brand_id:
        current_brand = _load_owned_brand(
            conn,
            user_id=owner_user_id,
            brand_id=active_brand_id,
        ) or {}
    return {
        "owner_user_id": owner_user_id,
        "brand_id": active_brand_id,
        "brand_name": str(current_brand.get("name") or "").strip(),
        "time_zone": str(current_user.get("time_zone") or DEFAULT_TIME_ZONE).strip() or DEFAULT_TIME_ZONE,
        "is_admin_operation": False,
    }


def _stats_url(endpoint: str, **values: object) -> str:
    operation_scope = request_admin_operation_scope()
    if operation_scope:
        values.setdefault("admin_operation_brand", operation_scope["brand_id"])
        values.setdefault("admin_operation_kind", operation_scope["workspace_kind"])
    endpoint_name = endpoint if "." in endpoint else f"video_shorts_bp.{endpoint}"
    return url_for(endpoint_name, **values)


def _stats_scope_query() -> Dict[str, str]:
    operation_scope = request_admin_operation_scope()
    if not operation_scope:
        return {}
    return {
        "admin_operation_brand": operation_scope["brand_id"],
        "admin_operation_kind": operation_scope["workspace_kind"],
    }


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
        return {
            "direction": "up" if current_value > 0 else "neutral",
            "change_label": "New this period",
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
        "change_label": f"{pct_change:.0f}%",
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
    *,
    brand_id: Optional[str],
) -> Dict[str, Dict[str, int]]:
    brand_clause, brand_params = _brand_scope_clause(
        conn,
        "social_comment_cache",
        brand_id=brand_id,
    )
    try:
        cursor = conn.execute(
            f"""
            WITH raw AS (
                SELECT
                    LOWER(platform) AS platform,
                    CASE
                        WHEN published_at IS NULL THEN NULL
                        WHEN CAST(published_at AS VARCHAR) LIKE '%Z' THEN replace(CAST(published_at AS VARCHAR), 'Z', '+00:00')
                        ELSE CAST(published_at AS VARCHAR)
                    END AS published_norm
                FROM social_comment_cache
                WHERE 1 = 1
                  {brand_clause}
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
            [*brand_params, start.isoformat(), end.isoformat()],
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


def _fetch_top_videos_for_date(
    conn,
    target_date: date,
    channel_type: str,
    *,
    owner_user_id: str,
    brand_id: Optional[str],
    limit: int = 5,
):
    if not owner_user_id or not brand_id:
        return []
    return load_top_videos_for_date(
        conn,
        owner_user_id=owner_user_id,
        brand_id=brand_id,
        target_date=target_date,
        selected_platform=channel_type,
        limit=limit,
    )


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


def _parse_report_range() -> Tuple[date, date]:
    today = datetime.now(ZoneInfo(DEFAULT_TIME_ZONE)).date()
    try:
        start_date = _parse_date_param(request.args.get("start"))
        end_date = _parse_date_param(request.args.get("end"))
    except ValueError:
        abort(400, description="Invalid report date range")
    if start_date is None or end_date is None:
        start_date = today.replace(day=1)
        end_date = today
    if start_date > end_date:
        abort(400, description="Report start must not be after its end")
    if (end_date - start_date).days > 62:
        abort(400, description="Report range cannot exceed 63 days")
    return start_date, end_date


def _report_context(conn, tenant: Mapping[str, object], start_date: date, end_date: date) -> Dict[str, object]:
    owner_user_id = str(tenant.get("owner_user_id") or "").strip()
    brand_id = str(tenant.get("brand_id") or "").strip()
    brand_name = str(tenant.get("brand_name") or "Customer").strip() or "Customer"
    if not owner_user_id or not brand_id:
        abort(403)
    today = datetime.now(ZoneInfo(DEFAULT_TIME_ZONE)).date()
    report = load_monthly_report_metrics(
        conn,
        owner_user_id=owner_user_id,
        brand_id=brand_id,
        brand_name=brand_name,
        start_date=start_date,
        end_date=end_date,
        today=today,
    )
    connected = report["connections"]
    connected_platforms = [platform for platform in ("youtube", "instagram", "facebook") if connected.get(platform)]
    platform_labels = {"youtube": "YouTube", "instagram": "Instagram", "facebook": "Facebook"}
    current = report["current_totals"]
    total_views = current.get("all", {}).get("views")
    view_segments = []
    known_total = int(total_views or 0)
    for platform in connected_platforms:
        value = current.get(platform, {}).get("views")
        view_segments.append(
            {
                "key": platform,
                "label": platform_labels[platform],
                "value": value,
                "percent": round(int(value or 0) * 100 / known_total, 1) if value is not None and known_total else 0,
            }
        )

    chart_by_date = {
        str(row["date"]): row for row in report["chart_series_delta"]["all"]
    }
    raw_daily = []
    for date_key, row in chart_by_date.items():
        metric_date = date.fromisoformat(date_key)
        if metric_date > min(end_date, today):
            continue
        values = {
            platform: row.get(f"{platform}_daily_views")
            for platform in ("youtube", "instagram", "facebook")
        }
        total = sum(int(value) for value in values.values() if value is not None)
        raw_daily.append({"date": metric_date, "values": values, "total": total})
    daily_max = max((item["total"] for item in raw_daily), default=0) or 1
    daily_bars = []
    for item in raw_daily:
        segments = []
        for platform in connected_platforms:
            value = item["values"].get(platform)
            segments.append(
                {
                    "key": platform,
                    "height": round(int(value or 0) * 100 / daily_max, 2),
                    "value": value,
                }
            )
        daily_bars.append({**item, "segments": segments})

    cumulative_total = 0
    cumulative_values = []
    for item in raw_daily:
        cumulative_total += int(item["total"] or 0)
        cumulative_values.append(
            {"date": item["date"], "value": cumulative_total}
        )
    cumulative_max = max(
        (int(item["value"]) for item in cumulative_values),
        default=0,
    ) or 1
    cumulative_points = []
    point_count = len(cumulative_values)
    for index, item in enumerate(cumulative_values):
        x = 12 + (index * 616 / max(point_count - 1, 1))
        y = 174 - (int(item["value"]) * 140 / cumulative_max)
        cumulative_points.append(
            {**item, "x": round(x, 2), "y": round(y, 2)}
        )
    cumulative_polyline = " ".join(
        f"{item['x']},{item['y']}" for item in cumulative_points
    )
    cumulative_area = (
        f"12,174 {cumulative_polyline} 628,174" if cumulative_points else ""
    )
    cumulative_labels = []
    if cumulative_points:
        label_indexes = sorted({0, len(cumulative_points) // 2, len(cumulative_points) - 1})
        cumulative_labels = [cumulative_points[index] for index in label_indexes]

    insights = report.get("youtube_insights") or {}
    traffic = sorted(
        [
            item
            for item in (insights.get("traffic_sources") or [])
            if float(item.get("percent") or 0) > 0
        ],
        key=lambda item: float(item.get("percent") or 0),
        reverse=True,
    )
    dominant = max(traffic, key=lambda item: float(item.get("percent") or 0), default=None)
    source_meanings = {
        "shorts_feed_views": "Your Shorts are being discovered in the Shorts feed, where strong openings help earn more watch time.",
        "search_views": "Search is your leading discovery source, so clear topics and searchable titles are helping people find you.",
        "subscriber_views": "Your existing subscribers are driving the most views, showing that your Shorts are reaching a loyal audience.",
        "notification_views": "Notifications are driving discovery, which means your returning audience is responding quickly.",
        "channel_page_views": "People are finding these Shorts from your channel page, often after exploring more of your content.",
        "external_views": "External links are your leading discovery source, so sharing Shorts beyond YouTube is working.",
        "other_views": "Your views are spread across several YouTube discovery surfaces rather than one dominant source.",
    }
    best = (report.get("range_top_videos") or [None])[0]
    if start_date.year == end_date.year and start_date.month == end_date.month:
        month_label = start_date.strftime("%B %Y")
    else:
        month_label = f"{start_date.strftime('%b %-d')} – {end_date.strftime('%b %-d, %Y')}"
    audience_changes = report["audience_changes"]
    subscriber_parts = [
        {"key": platform, "label": platform_labels[platform], "value": audience_changes.get(platform)}
        for platform in connected_platforms
        if platform in {"youtube", "instagram"}
    ]
    subscriber_total_parts = [
        {
            "key": platform,
            "label": platform_labels[platform],
            "value": report["subscriber_totals"].get(platform),
        }
        for platform in connected_platforms
        if platform in {"youtube", "instagram"}
    ]
    long_form = dict(report.get("long_form_effect") or {})
    long_form_values = [
        float(value)
        for value in (
            long_form.get("baseline_views_per_day"),
            long_form.get("long_form_views_per_day"),
        )
        if value is not None
    ]
    long_form_max = max(long_form_values, default=0.0) or 1.0
    long_form["baseline_bar_percent"] = round(
        float(long_form.get("baseline_views_per_day") or 0) * 100 / long_form_max,
        1,
    )
    long_form["range_bar_percent"] = round(
        float(long_form.get("long_form_views_per_day") or 0) * 100 / long_form_max,
        1,
    )
    if long_form.get("trend_status") == "too_early":
        long_form["trend_line"] = "Too early to read a trend"
    elif long_form.get("change_percent") is None:
        long_form["trend_line"] = "Long-form trend data is pending"
    else:
        change = float(long_form["change_percent"])
        prefix = "+" if change >= 0 else "−"
        long_form["trend_line"] = (
            f"{prefix}{abs(change):.0f}% vs before Minti. Many things affect long-form "
            "views; this shows the trend, not a direct effect."
        )
    report.update(
        {
            "owner_user_id": owner_user_id,
            "brand_id": brand_id,
            "start_date": start_date,
            "end_date": end_date,
            "month_label": month_label,
            "connected_platforms": connected_platforms,
            "platform_labels": platform_labels,
            "view_segments": view_segments,
            "daily_bars": daily_bars,
            "cumulative_points": cumulative_points,
            "cumulative_polyline": cumulative_polyline,
            "cumulative_area": cumulative_area,
            "cumulative_labels": cumulative_labels,
            "cumulative_end_value": cumulative_total,
            "best_short": best,
            "traffic_meaning": source_meanings.get(str((dominant or {}).get("key") or "")),
            "traffic_sources_sorted": traffic,
            "subscriber_parts": subscriber_parts,
            "subscriber_total_parts": subscriber_total_parts,
            "long_form_report": long_form,
            "report_generated_at": datetime.now(ZoneInfo(DEFAULT_TIME_ZONE)),
        }
    )
    return report


@video_shorts_bp.route("/stats/report", methods=["GET"])
def stats_report_page():
    start_date, end_date = _parse_report_range()
    conn = get_db_readonly()
    try:
        tenant = _resolve_stats_tenant(conn)
        context = _report_context(conn, tenant, start_date, end_date)
    finally:
        conn.close()
    return render_template("stats_report.html", **context)


@video_shorts_bp.route("/stats/report/render", methods=["GET"])
def stats_report_render():
    start_date, end_date = _parse_report_range()
    brand_id = str(request.args.get("brand_id") or "").strip()
    try:
        claims = consume_report_grant(
            str(request.args.get("token") or ""),
            brand_id=brand_id,
            start=start_date.isoformat(),
            end=end_date.isoformat(),
            route=REPORT_RENDER_ROUTE,
        )
    except ReportGrantError as exc:
        abort(403, description=str(exc))
    conn = get_db_readonly()
    try:
        brand = _load_owned_brand(
            conn,
            user_id=claims["owner_user_id"],
            brand_id=claims["brand_id"],
        )
        if not brand:
            abort(403)
        tenant = {
            "owner_user_id": claims["owner_user_id"],
            "brand_id": claims["brand_id"],
            "brand_name": brand.get("name") or "Customer",
            "is_admin_operation": False,
        }
        context = _report_context(conn, tenant, start_date, end_date)
    finally:
        conn.close()
    return render_template("stats_report.html", **context)


@video_shorts_bp.route("/stats/report.pdf", methods=["GET"])
def stats_report_pdf():
    start_date, end_date = _parse_report_range()
    conn = get_db_readonly()
    try:
        tenant = _resolve_stats_tenant(conn)
        owner_user_id = str(tenant.get("owner_user_id") or "").strip()
        brand_id = str(tenant.get("brand_id") or "").strip()
        brand_name = str(tenant.get("brand_name") or "Customer").strip() or "Customer"
        if not owner_user_id or not brand_id or not _load_owned_brand(conn, user_id=owner_user_id, brand_id=brand_id):
            abort(403)
    finally:
        conn.close()
    token = issue_report_grant(
        owner_user_id=owner_user_id,
        brand_id=brand_id,
        start_date=start_date,
        end_date=end_date,
    )
    try:
        pdf, elapsed = render_report_pdf(
            token=token,
            brand_id=brand_id,
            start_date=start_date,
            end_date=end_date,
        )
    except ReportRenderBusy as exc:
        return jsonify({"error": str(exc)}), 429
    current_app.logger.info(
        "stats_report_rendered owner_user_id=%s brand_id=%s start=%s end=%s seconds=%.3f bytes=%s",
        owner_user_id, brand_id, start_date, end_date, elapsed, len(pdf),
    )
    safe_brand = re.sub(r"[^A-Za-z0-9]+", "-", brand_name).strip("-") or "Brand"
    month_part = start_date.strftime("%Y-%m")
    return send_file(
        io.BytesIO(pdf),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"Minti-{safe_brand}-{month_part}.pdf",
        max_age=0,
    )


def _render_video_stats(*, load_widgets: bool):
    tiktok_feature_enabled = tiktok_enabled()
    end_param = request.args.get("end")
    start_param = request.args.get("start")
    channel_type = request.args.get("channel", "all")
    if not tiktok_feature_enabled and channel_type == "tiktok":
        channel_type = "all"
    selected_metric = request.args.get("metric", "views")
    videos_page = _parse_page_arg("videos_page", 1)

    today = datetime.now(ZoneInfo(DEFAULT_TIME_ZONE)).date()
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
    conn = get_db_readonly()
    try:
        tenant = _resolve_stats_tenant(conn)
    except Exception:
        conn.close()
        raise
    active_brand_id = str(tenant.get("brand_id") or "").strip() or None
    target_owner_user_id = str(tenant.get("owner_user_id") or "").strip()
    is_admin_operation = bool(tenant.get("is_admin_operation"))
    shared_template_context = {
        "stats_url": _stats_url,
        "stats_scope_query": _stats_scope_query(),
        "stats_admin_operation": is_admin_operation,
        "stats_admin_brand_name": str(tenant.get("brand_name") or "Customer"),
        "stats_today": today,
    }

    if not load_widgets:
        conn.close()
        return render_template(
            "video_metrics.html",
            dashboard_shell=True,
            tiktok_enabled=tiktok_feature_enabled,
            start_date=start_date,
            end_date=end_date,
            channel_type=channel_type,
            selected_metric=selected_metric,
            **shared_template_context,
        )

    # The rendered dashboard has one data path: the canonical daily-gains mart.
    # Keep the tenant values explicit here so an admin-scoped request can never
    # fall back to a session brand inside a shared query helper.
    try:
        canonical = load_dashboard_metrics(
            conn,
            owner_user_id=target_owner_user_id,
            brand_id=active_brand_id or "",
            start_date=start_date,
            end_date=end_date,
            selected_platform=channel_type,
            page=videos_page,
            page_size=VIDEO_LIST_PAGE_SIZE,
            today=today,
        )
    finally:
        conn.close()

    platform_connections = canonical["connections"]
    period_totals_by_channel = canonical["current_totals"]
    previous_period_totals_by_channel = canonical["previous_totals"]
    selected_channel = (
        channel_type
        if channel_type in ("all", "youtube", "instagram", "facebook")
        else "all"
    )
    previous_period_label = (
        f"vs previous period ({canonical['previous_start'].isoformat()}–"
        f"{canonical['previous_end'].isoformat()})"
    )
    summary_specs = (
        ("Total views", "views", "visibility"),
        ("Total comments", "comments", "comment"),
        ("Total likes", "likes", "thumb_up"),
    )
    summary_cards_by_channel: Dict[str, Dict[str, Mapping[str, object]]] = {}
    for channel_key in ("all", "youtube", "instagram", "facebook"):
        current_totals = period_totals_by_channel.get(channel_key, {})
        previous_totals = previous_period_totals_by_channel.get(channel_key, {})
        is_connected = (
            any(platform_connections.values())
            if channel_key == "all"
            else bool(platform_connections.get(channel_key))
        )
        card_map: Dict[str, Mapping[str, object]] = {}
        for title, metric_key, icon in summary_specs:
            current_value = current_totals.get(metric_key)
            previous_value = previous_totals.get(metric_key)
            if not is_connected:
                display_value: object = "Not connected"
                direction = "neutral"
                delta_label = previous_period_label
            elif current_value is None:
                display_value = "Pending"
                direction = "neutral"
                delta_label = f"Pending · {previous_period_label}"
            else:
                display_value = int(current_value)
                change = _build_change_meta(
                    int(current_value),
                    int(previous_value) if previous_value is not None else None,
                )
                direction = str(change["direction"])
                delta_label = str(change["change_label"])
                if previous_value != 0:
                    delta_label = f"{delta_label} {previous_period_label}"
            card_map[metric_key] = {
                "key": metric_key,
                "title": title,
                "icon": icon,
                "display_value": display_value,
                "change_label": delta_label,
                "direction": direction,
            }
        summary_cards_by_channel[channel_key] = card_map

    video_rows = canonical["video_rows"]
    video_rows_total = int(canonical["video_rows_total"] or 0)
    video_rows_total_pages = max(
        (video_rows_total + VIDEO_LIST_PAGE_SIZE - 1) // VIDEO_LIST_PAGE_SIZE,
        1,
    )
    videos_page = min(videos_page, video_rows_total_pages)
    summary_cards = [
        summary_cards_by_channel[selected_channel][key]
        for _title, key, _icon in summary_specs
    ]
    template_context = dict(
        start_date=start_date,
        end_date=end_date,
        channel_type=channel_type,
        selected_metric=selected_metric,
        channel_options=[
            option for option in CHANNEL_OPTIONS
            if tiktok_feature_enabled or option["value"] != "tiktok"
        ],
        daily_totals=canonical["daily_totals"],
        range_totals=period_totals_by_channel.get(selected_channel, {}),
        summary_cards=summary_cards,
        summary_cards_by_channel=summary_cards_by_channel,
        chart_series=canonical["chart_series_delta"],
        chart_series_delta=canonical["chart_series_delta"],
        channel_totals=period_totals_by_channel,
        selected_channel=selected_channel,
        video_rows=video_rows,
        videos_page=videos_page,
        video_rows_total=video_rows_total,
        video_rows_total_pages=video_rows_total_pages,
        today_top_videos=canonical["today_top_videos"],
        week_top_videos=canonical["week_top_videos"],
        user_time_zone_label=_timezone_label(str(tenant.get("time_zone") or DEFAULT_TIME_ZONE)),
        earliest_snapshot=start_date,
        default_insights=canonical["range_top_videos"],
        subscriber_totals=canonical["subscriber_totals"],
        subscriber_series=canonical["subscriber_series"],
        youtube_subscriber_data_missing=canonical["youtube_subscriber_data_missing"],
        youtube_insights=canonical["youtube_insights"],
        last_api_at=canonical["last_api_at"],
        stats_connect_empty=canonical["stats_connect_empty"],
        platform_connections=platform_connections,
        tiktok_enabled=tiktok_feature_enabled,
        **shared_template_context,
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
        tenant = _resolve_stats_tenant(conn)
        videos = _fetch_top_videos_for_date(
            conn,
            parsed_date,
            channel_type,
            owner_user_id=str(tenant.get("owner_user_id") or "").strip(),
            brand_id=str(tenant.get("brand_id") or "").strip() or None,
            limit=6,
        )
        return jsonify({"videos": videos, "date": parsed_date.isoformat(), "channel": channel_type})
    except HTTPException:
        raise
    except Exception:
        return jsonify({"videos": [], "date": parsed_date.isoformat(), "channel": channel_type})
    finally:
        conn.close()
