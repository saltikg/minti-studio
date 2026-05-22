from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import List, Mapping, Optional, Tuple
from zoneinfo import ZoneInfo

from flask import current_app, g, render_template, request

from app.video_shorts import video_shorts_bp
from app.video_shorts.services.brands import current_brand_id
from app.video_shorts.services.db import get_db_readonly


logger = logging.getLogger(__name__)

GROWTH_TABLE = "analytics.fct_channel_daily_growth"
VIDEO_DAILY_DELTAS_TABLE = "analytics.fct_video_daily_deltas"
DEFAULT_WINDOW_DAYS = 7
MAX_TABLE_ROWS = 100
MAX_TOP_VIDEOS = 8
DASHBOARD_VIEW_PLATFORMS = ("youtube", "instagram", "facebook")
PLATFORM_OPTIONS = [
    {"value": "all", "label": "All Platforms"},
    {"value": "instagram", "label": "Instagram"},
    {"value": "youtube", "label": "YouTube"},
    {"value": "facebook", "label": "Facebook"},
    {"value": "tiktok", "label": "TikTok"},
]
PLATFORM_LABELS = {option["value"]: option["label"] for option in PLATFORM_OPTIONS}
PLATFORM_DISPLAY_ORDER = {
    "instagram": 0,
    "youtube": 1,
    "facebook": 2,
    "tiktok": 3,
}
AUDIENCE_PLATFORMS = ("youtube", "instagram", "facebook")
DBT_RUN_RESULTS_PATH = Path("/home/ubuntu/apps/dbt/minti_dbt/target/run_results.json")
DBT_LOG_PATH = Path("/home/ubuntu/apps/dbt/logs/dbt.log")
DEFAULT_TIME_ZONE = "America/Los_Angeles"


def _rows_to_dict(cursor) -> List[Mapping[str, object]]:
    columns = [desc[0] for desc in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


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


def _latest_dbt_update_utc() -> Optional[datetime]:
    latest: Optional[datetime] = None
    for path in (DBT_RUN_RESULTS_PATH, DBT_LOG_PATH):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        candidate = datetime.fromtimestamp(mtime, tz=timezone.utc)
        if latest is None or candidate > latest:
            latest = candidate
    return latest


def _resolve_timezone(tz_name: Optional[str]) -> ZoneInfo:
    try:
        return ZoneInfo((tz_name or DEFAULT_TIME_ZONE).strip() or DEFAULT_TIME_ZONE)
    except Exception:
        return ZoneInfo(DEFAULT_TIME_ZONE)


def _format_last_updated_label(timestamp: Optional[datetime], tz_name: Optional[str]) -> str:
    if timestamp is None:
        return "Last updated: unavailable"
    user_tz = _resolve_timezone(tz_name)
    local_time = timestamp.astimezone(user_tz)
    return f"Last updated: {local_time.strftime('%Y-%m-%d %H:%M %Z')}"


def _normalize_filters() -> Tuple[str, date, date]:
    today = date.today()
    end_date = (
        _parse_date_param(request.args.get("end_date"))
        or _parse_date_param(request.args.get("end"))
        or today
    )
    start_date = (
        _parse_date_param(request.args.get("start_date"))
        or _parse_date_param(request.args.get("start"))
        or (
        end_date - timedelta(days=DEFAULT_WINDOW_DAYS - 1)
    )
    )

    if start_date > end_date:
        start_date, end_date = end_date, start_date

    selected_platform = (
        (request.args.get("channel_type") or "").strip().lower()
        or (request.args.get("channel") or "").strip().lower()
        or next(
            (
                value.strip().lower()
                for value in request.args.getlist("channels")
                if value and value.strip()
            ),
            "",
        )
        or "all"
    )
    valid_values = {option["value"] for option in PLATFORM_OPTIONS}
    if selected_platform not in valid_values:
        selected_platform = "all"
    return selected_platform, start_date, end_date


def _platform_clause(selected_platform: str) -> Tuple[str, List[object]]:
    if selected_platform == "all":
        return "", []
    return " AND channel_type = ?", [selected_platform]


def _brand_clause(brand_id: Optional[str]) -> Tuple[str, List[object]]:
    if not brand_id:
        return "", []
    return " AND brand_id = ?", [brand_id]


def _relation_exists(conn, relation_name: str) -> bool:
    try:
        cursor = conn.execute("SELECT to_regclass(?)", [relation_name])
        row = cursor.fetchone()
        return bool(row and row[0])
    except Exception:
        return False


def _relation_has_column(conn, relation_name: str, column_name: str) -> bool:
    try:
        if "." in relation_name:
            schema_name, table_name = relation_name.split(".", 1)
        else:
            schema_name, table_name = "public", relation_name
        cursor = conn.execute(
            """
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = ?
              AND table_name = ?
              AND column_name = ?
            LIMIT 1
            """,
            [schema_name, table_name, column_name],
        )
        return cursor.fetchone() is not None
    except Exception:
        return False


def _compute_platform_kpis(
    conn,
    *,
    selected_platform: str,
    start_date: date,
    end_date: date,
    brand_clause: str,
    brand_params: List[object],
) -> List[Mapping[str, object]]:
    # Inclusive range length so 2026-03-10..2026-04-07 counts every day in the
    # selected window. The previous comparison window is the immediately
    # preceding period with the exact same inclusive length.
    current_start = start_date
    current_end = end_date
    period_length_days = (current_end - current_start).days + 1
    previous_end = current_start - timedelta(days=1)
    previous_start = previous_end - timedelta(days=period_length_days - 1)

    platform_clause, platform_params = _platform_clause(selected_platform)
    cursor = conn.execute(
        f"""
        WITH periods AS (
            SELECT
                channel_type,
                CASE
                    WHEN snapshot_date BETWEEN ? AND ? THEN 'current'
                    WHEN snapshot_date BETWEEN ? AND ? THEN 'previous'
                END AS period_name,
                daily_subscriber_delta
            FROM {GROWTH_TABLE}
            WHERE (
                    snapshot_date BETWEEN ? AND ?
                 OR snapshot_date BETWEEN ? AND ?
                  )
              {brand_clause}
              {platform_clause}
              AND daily_subscriber_delta IS NOT NULL
        )
        SELECT
            channel_type,
            SUM(CASE WHEN period_name = 'current' THEN daily_subscriber_delta ELSE 0 END) AS current_growth,
            SUM(CASE WHEN period_name = 'previous' THEN daily_subscriber_delta ELSE 0 END) AS previous_growth
        FROM periods
        WHERE period_name IS NOT NULL
        GROUP BY channel_type
        ORDER BY channel_type ASC
        """,
        [
            current_start.isoformat(),
            current_end.isoformat(),
            previous_start.isoformat(),
            previous_end.isoformat(),
            current_start.isoformat(),
            current_end.isoformat(),
            previous_start.isoformat(),
            previous_end.isoformat(),
            *brand_params,
            *platform_params,
        ],
    )
    rows = _rows_to_dict(cursor)

    kpis: List[Mapping[str, object]] = []
    for row in rows:
        platform = (row.get("channel_type") or "").lower()
        if not platform:
            continue
        current_growth = int(row.get("current_growth") or 0)
        previous_growth = int(row.get("previous_growth") or 0)
        pct_change: Optional[float]
        pct_label: str
        direction: str

        if previous_growth == 0:
            if current_growth == 0:
                pct_change = 0.0
                pct_label = "0.0%"
                direction = "neutral"
            elif current_growth > 0:
                pct_change = None
                pct_label = "New"
                direction = "up"
            else:
                pct_change = None
                pct_label = "New"
                direction = "down"
        else:
            pct_change = ((current_growth - previous_growth) / abs(previous_growth)) * 100
            pct_label = f"{pct_change:.1f}%"
            if pct_change > 0:
                direction = "up"
            elif pct_change < 0:
                direction = "down"
            else:
                direction = "neutral"

        kpis.append(
            {
                "platform": platform,
                "platform_label": PLATFORM_LABELS.get(platform, platform.title()),
                "current_growth": current_growth,
                "previous_growth": previous_growth,
                "pct_change": pct_change,
                "pct_label": pct_label,
                "change_label": pct_label,
                "direction": direction,
                "current_period_label": f"{current_start.isoformat()} to {current_end.isoformat()}",
                "previous_period_label": f"{previous_start.isoformat()} to {previous_end.isoformat()}",
            }
        )
    kpis.sort(key=lambda item: (PLATFORM_DISPLAY_ORDER.get(item["platform"], 99), item["platform"]))
    return kpis


def _fetch_top_videos(
    conn,
    *,
    start_date: date,
    end_date: date,
    brand_id: Optional[str],
) -> List[Mapping[str, object]]:
    has_dim_source_videos = _relation_exists(conn, "analytics.dim_source_videos")
    has_brand_column = _relation_has_column(conn, VIDEO_DAILY_DELTAS_TABLE, "brand_id")
    title_join_sql = """
        LEFT JOIN title_latest t
          ON t.source_video_id = n.source_video_id
    """
    title_select_sql = "MAX(t.video_title) AS video_title"
    if has_dim_source_videos:
        title_join_sql = """
        LEFT JOIN title_latest t
          ON t.source_video_id = n.source_video_id
        LEFT JOIN analytics.dim_source_videos AS dim
          ON dim.video_id = n.source_video_id
        """
        title_select_sql = "COALESCE(MAX(NULLIF(dim.video_title, '')), MAX(t.video_title)) AS video_title"

    metrics_brand_clause = ""
    params: List[object] = []
    if brand_id and has_brand_column:
        metrics_brand_clause = " AND brand_id = ?"

    map_brand_clause = ""
    title_brand_clause = ""
    if brand_id:
        map_brand_clause = " AND brand_id = ?"
        title_brand_clause = " AND s.brand_id = ?"

    params.extend([start_date.isoformat(), end_date.isoformat()])
    if brand_id and has_brand_column:
        params.append(brand_id)

    if brand_id:
        params.append(brand_id)  # map_youtube_from_instagram
        params.append(brand_id)  # map_instagram_from_instagram
        params.append(brand_id)  # map_youtube_from_ai

    params.extend([start_date.isoformat(), end_date.isoformat()])
    if brand_id:
        params.append(brand_id)  # title_source

    cursor = conn.execute(
        f"""
        WITH metric_base AS (
            SELECT
                snapshot_date,
                video_id,
                channel_type,
                SUM(COALESCE(views, 0)) AS views,
                SUM(COALESCE(reach, 0)) AS reach
            FROM {VIDEO_DAILY_DELTAS_TABLE}
            WHERE snapshot_date BETWEEN ? AND ?
              {metrics_brand_clause}
              AND channel_type IN ('youtube', 'instagram')
              AND video_id IS NOT NULL
            GROUP BY snapshot_date, video_id, channel_type
        ),
        map_union AS (
            SELECT
                'youtube' AS channel_type,
                CAST(youtube_short_id AS VARCHAR) AS platform_video_id,
                CAST(video_id AS VARCHAR) AS source_video_id
            FROM main.shorts_instagram_queue
            WHERE youtube_short_id IS NOT NULL
              AND video_id IS NOT NULL
              {map_brand_clause}
            UNION ALL
            SELECT
                'instagram' AS channel_type,
                CAST(instagram_media_id AS VARCHAR) AS platform_video_id,
                CAST(video_id AS VARCHAR) AS source_video_id
            FROM main.shorts_instagram_queue
            WHERE instagram_media_id IS NOT NULL
              AND video_id IS NOT NULL
              {map_brand_clause}
            UNION ALL
            SELECT
                'youtube' AS channel_type,
                CAST(youtube_video_id AS VARCHAR) AS platform_video_id,
                CAST(video_id AS VARCHAR) AS source_video_id
            FROM main.shorts_ai_videos
            WHERE youtube_video_id IS NOT NULL
              AND video_id IS NOT NULL
              {map_brand_clause}
        ),
        map_resolved AS (
            SELECT
                channel_type,
                platform_video_id,
                MAX(source_video_id) AS source_video_id
            FROM map_union
            GROUP BY channel_type, platform_video_id
        ),
        normalized AS (
            SELECT
                COALESCE(m.source_video_id, b.video_id) AS source_video_id,
                b.channel_type,
                b.video_id AS platform_video_id,
                b.views,
                b.reach
            FROM metric_base b
            LEFT JOIN map_resolved m
              ON m.channel_type = b.channel_type
             AND m.platform_video_id = b.video_id
        ),
        title_source AS (
            SELECT
                COALESCE(m.source_video_id, s.video_id) AS source_video_id,
                NULLIF(BTRIM(video_title), '') AS video_title,
                s.snapshot_date,
                ROW_NUMBER() OVER (
                    PARTITION BY COALESCE(m.source_video_id, s.video_id)
                    ORDER BY s.snapshot_date DESC
                ) AS rn
            FROM main.shorts_video_daily_snapshots s
            LEFT JOIN map_resolved m
              ON m.channel_type = s.channel_type
             AND m.platform_video_id = s.video_id
            WHERE s.snapshot_date BETWEEN ? AND ?
              {title_brand_clause}
              AND s.channel_type IN ('youtube', 'instagram')
              AND s.video_id IS NOT NULL
        ),
        title_latest AS (
            SELECT source_video_id, video_title
            FROM title_source
            WHERE rn = 1
              AND video_title IS NOT NULL
        )
        SELECT
            n.source_video_id AS video_id,
            {title_select_sql},
            MAX(CASE WHEN n.channel_type = 'youtube' THEN n.platform_video_id END) AS youtube_video_id,
            SUM(CASE WHEN n.channel_type = 'youtube' THEN n.views ELSE 0 END) AS youtube_views,
            SUM(CASE WHEN n.channel_type = 'instagram' THEN n.reach ELSE 0 END) AS instagram_reach,
            SUM(
                CASE
                    WHEN n.channel_type = 'youtube' THEN n.views
                    WHEN n.channel_type = 'instagram' THEN n.reach
                    ELSE 0
                END
            ) AS total_exposure
        FROM normalized n
        {title_join_sql}
        GROUP BY n.source_video_id
        HAVING SUM(
            CASE
                WHEN n.channel_type = 'youtube' THEN n.views
                WHEN n.channel_type = 'instagram' THEN n.reach
                ELSE 0
            END
        ) > 0
        ORDER BY total_exposure DESC, n.source_video_id ASC
        LIMIT {MAX_TOP_VIDEOS}
        """,
        params,
    )
    rows = _rows_to_dict(cursor)

    top_videos: List[Mapping[str, object]] = []
    for row in rows:
        source_video_id = str(row.get("video_id") or "").strip()
        youtube_video_id = str(row.get("youtube_video_id") or "").strip()
        video_id = source_video_id
        if not video_id:
            continue
        youtube_views = int(row.get("youtube_views") or 0)
        instagram_reach = int(row.get("instagram_reach") or 0)
        total_exposure = int(row.get("total_exposure") or 0)

        thumbnail_url: Optional[str] = None
        if youtube_views > 0 and youtube_video_id:
            thumbnail_url = f"https://i.ytimg.com/vi/{youtube_video_id}/hqdefault.jpg"
        video_url: Optional[str] = None
        if youtube_views > 0 and youtube_video_id:
            video_url = f"https://www.youtube.com/watch?v={youtube_video_id}"

        top_videos.append(
            {
                "video_id": source_video_id,
                "video_title": (row.get("video_title") or "").strip(),
                "thumbnail_url": thumbnail_url,
                "video_url": video_url,
                "youtube_views": youtube_views,
                "instagram_reach": instagram_reach,
                "total_exposure": total_exposure,
            }
        )
    return top_videos


@video_shorts_bp.route("/dashboard-v2", methods=["GET"])
def dashboard_v2_page():
    selected_platform, start_date, end_date = _normalize_filters()
    brand_id = current_brand_id()

    conn = get_db_readonly()
    try:
        brand_clause, brand_params = _brand_clause(brand_id)

        platform_clause, platform_params = _platform_clause(selected_platform)
        base_params: List[object] = [start_date.isoformat(), end_date.isoformat()]
        base_params.extend(brand_params)
        base_params.extend(platform_params)

        chart_cursor = conn.execute(
            f"""
            SELECT
                snapshot_date,
                channel_type,
                channel_id,
                channel_name,
                brand_id,
                subscriber_count,
                prev_subscriber_count,
                daily_subscriber_delta
            FROM {GROWTH_TABLE}
            WHERE snapshot_date BETWEEN ? AND ?
              {brand_clause}
              {platform_clause}
              AND daily_subscriber_delta IS NOT NULL
            ORDER BY snapshot_date ASC, channel_name ASC
            """,
            base_params,
        )
        chart_rows = _rows_to_dict(chart_cursor)

        daily_views_platform_clause, daily_views_platform_params = _platform_clause(selected_platform)
        daily_views_cursor = conn.execute(
            f"""
            SELECT
                snapshot_date,
                channel_type,
                SUM(
                    CASE
                        WHEN channel_type = 'youtube' THEN views
                        ELSE reach
                    END
                ) AS daily_views
            FROM {VIDEO_DAILY_DELTAS_TABLE}
            WHERE snapshot_date BETWEEN ? AND ?
              {daily_views_platform_clause}
            GROUP BY snapshot_date, channel_type
            HAVING SUM(
                CASE
                    WHEN channel_type = 'youtube' THEN views
                    ELSE reach
                END
            ) IS NOT NULL
            ORDER BY snapshot_date ASC, channel_type ASC
            """,
            [
                start_date.isoformat(),
                end_date.isoformat(),
                *daily_views_platform_params,
            ],
        )
        daily_channel_views_rows = _rows_to_dict(daily_views_cursor)

        table_cursor = conn.execute(
            f"""
            SELECT
                snapshot_date,
                channel_name,
                daily_subscriber_delta
            FROM {GROWTH_TABLE}
            WHERE snapshot_date BETWEEN ? AND ?
              {brand_clause}
              {platform_clause}
              AND daily_subscriber_delta IS NOT NULL
            ORDER BY snapshot_date DESC, channel_name ASC
            LIMIT {MAX_TABLE_ROWS}
            """,
            base_params,
        )
        summary_rows = _rows_to_dict(table_cursor)

        earliest_cursor = conn.execute(
            f"""
            SELECT MIN(snapshot_date)
            FROM {GROWTH_TABLE}
            WHERE 1 = 1
              {brand_clause}
            """,
            brand_params,
        )
        earliest_row = earliest_cursor.fetchone()
        earliest_snapshot = (
            earliest_row[0]
            if earliest_row and earliest_row[0] is not None
            else start_date
        )
        platform_kpis = _compute_platform_kpis(
            conn,
            selected_platform=selected_platform,
            start_date=start_date,
            end_date=end_date,
            brand_clause=brand_clause,
            brand_params=brand_params,
        )
        top_videos = _fetch_top_videos(
            conn,
            start_date=start_date,
            end_date=end_date,
            brand_id=brand_id,
        )
    finally:
        conn.close()

    chart_dates = sorted(
        {
            row["snapshot_date"].isoformat()
            if hasattr(row["snapshot_date"], "isoformat")
            else str(row["snapshot_date"])
            for row in chart_rows
        }
    )

    platform_keys_for_chart = sorted(
        {
            (row.get("channel_type") or "").lower()
            for row in chart_rows
            if row.get("channel_type")
        },
        key=lambda item: (PLATFORM_DISPLAY_ORDER.get(item, 99), item),
    )

    channel_series = []
    for platform_key in platform_keys_for_chart:
        row_map = {
            (
                row["snapshot_date"].isoformat()
                if hasattr(row["snapshot_date"], "isoformat")
                else str(row["snapshot_date"])
            ): row
            for row in chart_rows
            if (row.get("channel_type") or "").lower() == platform_key
        }
        points = []
        has_data = False
        for date_key in chart_dates:
            row = row_map.get(date_key)
            value = row.get("daily_subscriber_delta") if row else None
            if value is not None:
                has_data = True
            points.append(value)
        if has_data:
            channel_series.append(
                {
                    "channel_name": PLATFORM_LABELS.get(platform_key, platform_key.title()),
                    "channel_type": platform_key,
                    "data": points,
                }
            )

    daily_views_dates = sorted(
        {
            row["snapshot_date"].isoformat()
            if hasattr(row["snapshot_date"], "isoformat")
            else str(row["snapshot_date"])
            for row in daily_channel_views_rows
        }
    )

    daily_views_platform_keys = []
    if selected_platform == "all":
        daily_views_platform_keys.extend(DASHBOARD_VIEW_PLATFORMS)
    elif selected_platform:
        daily_views_platform_keys.append(selected_platform)
    discovered_platforms = sorted(
        {
            (row.get("channel_type") or "").lower()
            for row in daily_channel_views_rows
            if row.get("channel_type")
        },
        key=lambda item: (PLATFORM_DISPLAY_ORDER.get(item, 99), item),
    )
    for platform in discovered_platforms:
        if platform not in daily_views_platform_keys:
            daily_views_platform_keys.append(platform)

    daily_views_by_channel = []
    for platform_key in daily_views_platform_keys:
        row_map = {
            (
                row["snapshot_date"].isoformat()
                if hasattr(row["snapshot_date"], "isoformat")
                else str(row["snapshot_date"])
            ): row
            for row in daily_channel_views_rows
            if (row.get("channel_type") or "").lower() == platform_key
        }
        points = []
        for date_key in daily_views_dates:
            row = row_map.get(date_key)
            value = row.get("daily_views") if row else None
            points.append(int(value) if value is not None else 0)
        daily_views_by_channel.append(
            {
                "channel_name": PLATFORM_LABELS.get(platform_key, platform_key.title()),
                "channel_type": platform_key,
                "data": points,
            }
        )

    audience_map = {}
    for row in chart_rows:
        snapshot_value = row.get("snapshot_date")
        if snapshot_value is None:
            continue
        date_key = snapshot_value.isoformat() if hasattr(snapshot_value, "isoformat") else str(snapshot_value)
        platform_key = (row.get("channel_type") or "").lower()
        if platform_key not in AUDIENCE_PLATFORMS:
            continue
        audience_map.setdefault(date_key, {
            "date": date_key,
            "youtube_net": None,
            "instagram_net": None,
            "facebook_net": None,
        })
        current_value = audience_map[date_key].get(f"{platform_key}_net")
        delta_value = row.get("daily_subscriber_delta")
        if delta_value is None:
            continue
        audience_map[date_key][f"{platform_key}_net"] = (current_value or 0) + int(delta_value)
    audience_rows = [
        audience_map[date_key]
        for date_key in chart_dates
        if date_key in audience_map
    ]

    log_message = (
        "dashboard_v2 filters channels=%s start_date=%s end_date=%s rows=%s"
    )
    current_app.logger.info(
        log_message,
        len(platform_keys_for_chart),
        start_date.isoformat(),
        end_date.isoformat(),
        len(chart_rows),
    )
    logger.info(
        log_message,
        len(platform_keys_for_chart),
        start_date.isoformat(),
        end_date.isoformat(),
        len(chart_rows),
    )
    current_user = getattr(g, "vs_current_user", None) or {}
    user_tz_name = current_user.get("time_zone")
    last_updated_display = _format_last_updated_label(
        _latest_dbt_update_utc(),
        user_tz_name,
    )

    return render_template(
        "dashboard_v2.html",
        platform_options=PLATFORM_OPTIONS,
        selected_platform=selected_platform,
        start_date=start_date,
        end_date=end_date,
        earliest_snapshot=earliest_snapshot,
        chart_dates=chart_dates,
        channel_series=channel_series,
        daily_views_dates=daily_views_dates,
        daily_views_by_channel=daily_views_by_channel,
        platform_kpis=platform_kpis,
        top_videos=top_videos,
        audience_rows=audience_rows,
        summary_rows=summary_rows,
        chart_row_count=len(chart_rows),
        last_updated_display=last_updated_display,
    )
