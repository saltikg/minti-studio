from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Dict, List, Mapping, Optional, Sequence

from app.video_shorts.services.brands import brand_scoped_user_id


MART = "analytics.fct_short_daily_gains"
SUBSCRIBER_TABLE = "main.shorts_channel_subscriber_daily"
PLATFORMS = ("youtube", "instagram", "facebook")


def _rows(cursor) -> List[Dict[str, object]]:
    columns = [description[0] for description in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _date_sequence(start_date: date, end_date: date) -> List[str]:
    values: List[str] = []
    current = start_date
    while current <= end_date:
        values.append(current.isoformat())
        current += timedelta(days=1)
    return values


def _known_sum(values: Sequence[Optional[int]]) -> Optional[int]:
    known = [int(value) for value in values if value is not None]
    return sum(known) if known else None


def load_platform_connections(
    conn,
    *,
    owner_user_id: str,
    brand_id: str,
) -> Dict[str, bool]:
    scoped_owner = brand_scoped_user_id(owner_user_id, brand_id=brand_id)
    if not scoped_owner:
        return {platform: False for platform in PLATFORMS}
    row = conn.execute(
        """
        SELECT
            EXISTS (
                SELECT 1 FROM youtube_oauth_tokens_v2
                WHERE user_id = ? AND NULLIF(BTRIM(refresh_token), '') IS NOT NULL
            ) AS youtube_connected,
            EXISTS (
                SELECT 1 FROM instagram_oauth_tokens
                WHERE user_id = ?
                  AND NULLIF(BTRIM(page_access_token), '') IS NOT NULL
                  AND NULLIF(BTRIM(instagram_business_account_id), '') IS NOT NULL
            ) AS instagram_connected,
            EXISTS (
                SELECT 1 FROM facebook_page_tokens
                WHERE user_id = ?
                  AND NULLIF(BTRIM(page_access_token), '') IS NOT NULL
                  AND NULLIF(BTRIM(page_id), '') IS NOT NULL
            ) AS facebook_connected
        """,
        [scoped_owner, scoped_owner, scoped_owner],
    ).fetchone()
    return {
        "youtube": bool(row and row[0]),
        "instagram": bool(row and row[1]),
        "facebook": bool(row and row[2]),
    }


def _daily_metric_rows(
    conn,
    *,
    brand_id: str,
    start_date: date,
    end_date: date,
    platforms: Sequence[str],
) -> List[Dict[str, object]]:
    if not platforms:
        return []
    placeholders = ", ".join("?" for _ in platforms)
    return _rows(
        conn.execute(
            f"""
            SELECT
                metric_date,
                platform,
                SUM(views_gain) FILTER (WHERE views_gain IS NOT NULL) AS views,
                SUM(likes_gain) FILTER (WHERE likes_gain IS NOT NULL) AS likes,
                SUM(comments_gain) FILTER (WHERE comments_gain IS NOT NULL) AS comments,
                COUNT(*) AS short_rows,
                COUNT(views_gain) AS views_rows,
                COUNT(likes_gain) AS likes_rows,
                COUNT(comments_gain) AS comments_rows,
                BOOL_OR(completeness_status <> 'complete') AS has_incomplete_rows,
                MAX(effective_at) AS last_updated
            FROM {MART}
            WHERE brand_id = ?
              AND metric_date BETWEEN ? AND ?
              AND platform IN ({placeholders})
            GROUP BY metric_date, platform
            ORDER BY metric_date, platform
            """,
            [brand_id, start_date.isoformat(), end_date.isoformat(), *platforms],
        )
    )


def _period_totals(
    daily: Mapping[str, Mapping[str, Mapping[str, object]]],
    *,
    start_date: date,
    end_date: date,
    connections: Mapping[str, bool],
) -> Dict[str, Dict[str, Optional[int]]]:
    dates = _date_sequence(start_date, end_date)
    result: Dict[str, Dict[str, Optional[int]]] = {}
    for platform in PLATFORMS:
        if not connections.get(platform):
            result[platform] = {"views": None, "likes": None, "comments": None}
            continue
        values: Dict[str, Optional[int]] = {}
        for metric in ("views", "likes", "comments"):
            metric_values: List[Optional[int]] = []
            has_active_rows = False
            has_known_active_value = False
            for date_key in dates:
                row = (daily.get(platform) or {}).get(date_key)
                if row is None:
                    metric_values.append(0)
                else:
                    has_active_rows = True
                    if int(row.get(f"{metric}_rows") or 0) == 0:
                        metric_values.append(None)
                    else:
                        has_known_active_value = True
                        metric_values.append(
                            int(row[metric]) if row.get(metric) is not None else None
                        )
            values[metric] = (
                None
                if has_active_rows and not has_known_active_value
                else _known_sum(metric_values)
            )
        result[platform] = values

    result["all"] = {}
    for metric in ("views", "likes", "comments"):
        connected_values = [
            result[platform][metric]
            for platform in PLATFORMS
            if connections.get(platform)
        ]
        result["all"][metric] = _known_sum(connected_values)
    return result


def _display_metric(value: Optional[int], *, connected: bool) -> object:
    if not connected:
        return "Not connected"
    if value is None:
        return "Pending"
    return int(value)


def _audience_net_by_date(
    platform: str,
    platform_rows: Mapping[str, Mapping[str, object]],
) -> Dict[str, Optional[int]]:
    result: Dict[str, Optional[int]] = {}
    previous_total: Optional[int] = None
    for date_key in sorted(platform_rows):
        row = platform_rows[date_key]
        total_value = row.get("audience_total")
        current_total = int(total_value) if total_value is not None else None
        if platform == "youtube":
            result[date_key] = (
                int(row["audience_gain"])
                if int(row.get("gain_rows") or 0) > 0
                and row.get("audience_gain") is not None
                else None
            )
        else:
            result[date_key] = (
                current_total - previous_total
                if current_total is not None and previous_total is not None
                else None
            )
        if current_total is not None:
            previous_total = current_total
    return result


def _video_rollup(
    conn,
    *,
    brand_id: str,
    start_date: date,
    end_date: date,
    platforms: Sequence[str],
    connections: Mapping[str, bool],
    limit: Optional[int] = None,
    offset: int = 0,
) -> List[Dict[str, object]]:
    if not platforms:
        return []
    placeholders = ", ".join("?" for _ in platforms)
    limit_sql = ""
    params: List[object] = [
        brand_id,
        end_date.isoformat(),
        *platforms,
        brand_id,
        start_date.isoformat(),
        end_date.isoformat(),
        *platforms,
    ]
    params.extend([start_date.isoformat(), end_date.isoformat()])
    if limit is not None:
        limit_sql = " LIMIT ? OFFSET ?"
        params.extend([limit, offset])
    rows = _rows(
        conn.execute(
            f"""
            WITH latest_ranked AS (
                SELECT
                    generated_video_id,
                    platform,
                    lifetime_views,
                    ROW_NUMBER() OVER (
                        PARTITION BY generated_video_id, platform
                        ORDER BY metric_date DESC, effective_at DESC NULLS LAST
                    ) AS rn
                FROM {MART}
                WHERE brand_id = ?
                  AND metric_date <= ?
                  AND platform IN ({placeholders})
                  AND lifetime_views IS NOT NULL
            ), latest AS (
                SELECT generated_video_id, platform, lifetime_views
                FROM latest_ranked
                WHERE rn = 1
            ), period AS (
                SELECT
                    generated_video_id,
                    MAX(source_video_id) AS source_video_id,
                    MAX(generated_title) AS generated_title,
                    MAX(youtube_video_id) AS youtube_video_id,
                    MAX(instagram_media_id) AS instagram_media_id,
                    MAX(instagram_permalink) AS instagram_permalink,
                    MIN(canonical_publish_date) AS canonical_publish_date,
                    SUM(views_gain) FILTER (WHERE platform = 'youtube' AND views_gain IS NOT NULL) AS youtube_views,
                    SUM(views_gain) FILTER (WHERE platform = 'instagram' AND views_gain IS NOT NULL) AS instagram_views,
                    SUM(views_gain) FILTER (WHERE platform = 'facebook' AND views_gain IS NOT NULL) AS facebook_views,
                    SUM(comments_gain) FILTER (WHERE comments_gain IS NOT NULL) AS comments,
                    SUM(views_gain) FILTER (WHERE views_gain IS NOT NULL) AS period_views
                FROM {MART}
                WHERE brand_id = ?
                  AND metric_date BETWEEN ? AND ?
                  AND platform IN ({placeholders})
                GROUP BY generated_video_id
            ), lifetime AS (
                SELECT
                    generated_video_id,
                    SUM(lifetime_views) AS lifetime_views
                FROM latest
                GROUP BY generated_video_id
            )
            SELECT
                period.*,
                lifetime.lifetime_views,
                COUNT(*) OVER () AS total_count
            FROM period
            LEFT JOIN lifetime USING (generated_video_id)
            WHERE period.period_views IS NOT NULL
               OR period.canonical_publish_date BETWEEN ? AND ?
            ORDER BY period.period_views DESC NULLS LAST,
                     period.canonical_publish_date DESC,
                     period.generated_video_id DESC
            {limit_sql}
            """,
            params,
        )
    )
    items: List[Dict[str, object]] = []
    for row in rows:
        youtube_id = str(row.get("youtube_video_id") or "").strip()
        source_video_id = str(row.get("source_video_id") or "").strip()
        instagram_permalink = str(row.get("instagram_permalink") or "").strip()
        youtube_views = row.get("youtube_views")
        instagram_views = row.get("instagram_views")
        facebook_views = row.get("facebook_views")
        item = dict(row)
        item.update(
            {
                "video_id": str(row.get("generated_video_id") or ""),
                "video_title": str(row.get("generated_title") or "").strip()
                or "Untitled video",
                "publish_at": row.get("canonical_publish_date"),
                "views": row.get("period_views"),
                "views_delta": row.get("period_views"),
                "total_views": row.get("period_views"),
                "lifetime_views": row.get("lifetime_views"),
                "youtube_views": _display_metric(
                    int(youtube_views) if youtube_views is not None else None,
                    connected=bool(connections.get("youtube")),
                ),
                "instagram_views": _display_metric(
                    int(instagram_views) if instagram_views is not None else None,
                    connected=bool(connections.get("instagram")),
                ),
                "facebook_views": _display_metric(
                    int(facebook_views) if facebook_views is not None else None,
                    connected=bool(connections.get("facebook")),
                ),
                "thumbnail_url": (
                    f"https://i.ytimg.com/vi/{youtube_id}/mqdefault.jpg"
                    if youtube_id
                    else (
                        f"https://i.ytimg.com/vi/{source_video_id}/mqdefault.jpg"
                        if source_video_id
                        else None
                    )
                ),
                "video_url": (
                    f"https://www.youtube.com/watch?v={youtube_id}"
                    if youtube_id
                    else (instagram_permalink or None)
                ),
            }
        )
        items.append(item)
    return items


def _load_audience(
    conn,
    *,
    brand_id: str,
    start_date: date,
    end_date: date,
    connections: Mapping[str, bool],
) -> tuple[Dict[str, object], List[Dict[str, object]], bool]:
    rows = _rows(
        conn.execute(
            f"""
            SELECT
                snapshot_date,
                channel_type AS platform,
                SUM(
                    CASE
                        WHEN channel_type = 'youtube' THEN subscriber_count_exact
                        ELSE COALESCE(subscriber_count_exact, subscriber_count)
                    END
                ) AS audience_total,
                SUM(subscribers_net) FILTER (WHERE subscribers_net IS NOT NULL) AS audience_gain,
                COUNT(subscribers_net) AS gain_rows
            FROM {SUBSCRIBER_TABLE}
            WHERE brand_id = ?
              AND snapshot_date <= ?
              AND channel_type IN ('youtube', 'instagram', 'facebook')
            GROUP BY snapshot_date, channel_type
            ORDER BY snapshot_date, channel_type
            """,
            [brand_id, end_date.isoformat()],
        )
    )
    by_platform: Dict[str, Dict[str, Mapping[str, object]]] = {}
    for row in rows:
        platform = str(row.get("platform") or "")
        snapshot_date = row.get("snapshot_date")
        if platform and isinstance(snapshot_date, date):
            by_platform.setdefault(platform, {})[snapshot_date.isoformat()] = row

    net_by_platform = {
        platform: _audience_net_by_date(platform, by_platform.get(platform) or {})
        for platform in PLATFORMS
    }

    totals: Dict[str, object] = {}
    for platform in PLATFORMS:
        if not connections.get(platform):
            totals[platform] = "Not connected"
            continue
        platform_rows = by_platform.get(platform) or {}
        latest = platform_rows[max(platform_rows)] if platform_rows else None
        totals[platform] = (
            int(latest["audience_total"])
            if latest and latest.get("audience_total") is not None
            else "Pending"
        )

    series: List[Dict[str, object]] = []
    for date_key in _date_sequence(start_date, end_date):
        item: Dict[str, object] = {"date": date_key}
        for platform in PLATFORMS:
            row = (by_platform.get(platform) or {}).get(date_key)
            if not connections.get(platform) or row is None:
                item[f"{platform}_total"] = None
                item[f"{platform}_net"] = None
            else:
                item[f"{platform}_total"] = row.get("audience_total")
                item[f"{platform}_net"] = (net_by_platform.get(platform) or {}).get(date_key)
        series.append(item)
    youtube_pending = bool(connections.get("youtube")) and totals.get("youtube") == "Pending"
    return totals, series, youtube_pending


def load_dashboard_metrics(
    conn,
    *,
    owner_user_id: str,
    brand_id: str,
    start_date: date,
    end_date: date,
    selected_platform: str,
    page: int,
    page_size: int,
    today: date,
) -> Dict[str, object]:
    connections = load_platform_connections(
        conn,
        owner_user_id=owner_user_id,
        brand_id=brand_id,
    )
    connected_platforms = [platform for platform in PLATFORMS if connections[platform]]
    period_days = (end_date - start_date).days + 1
    previous_end = start_date - timedelta(days=1)
    previous_start = previous_end - timedelta(days=period_days - 1)
    rows = _daily_metric_rows(
        conn,
        brand_id=brand_id,
        start_date=previous_start,
        end_date=end_date,
        platforms=connected_platforms,
    )
    daily_map: Dict[str, Dict[str, Dict[str, object]]] = {}
    last_updated: Optional[datetime] = None
    for row in rows:
        platform = str(row.get("platform") or "")
        metric_date = row.get("metric_date")
        if not platform or not isinstance(metric_date, date):
            continue
        daily_map.setdefault(platform, {})[metric_date.isoformat()] = row
        effective_at = row.get("last_updated")
        if isinstance(effective_at, datetime) and (
            last_updated is None or effective_at > last_updated
        ):
            last_updated = effective_at

    current_totals = _period_totals(
        daily_map,
        start_date=start_date,
        end_date=end_date,
        connections=connections,
    )
    previous_totals = _period_totals(
        daily_map,
        start_date=previous_start,
        end_date=previous_end,
        connections=connections,
    )

    chart_series: Dict[str, List[Dict[str, object]]] = {
        key: [] for key in ("all", *PLATFORMS)
    }
    daily_totals: List[Dict[str, object]] = []
    for date_key in _date_sequence(start_date, end_date):
        per_platform: Dict[str, Dict[str, Optional[int]]] = {}
        for platform in PLATFORMS:
            source = (daily_map.get(platform) or {}).get(date_key)
            values: Dict[str, Optional[int]] = {}
            for metric in ("views", "likes", "comments"):
                if not connections.get(platform):
                    values[metric] = None
                elif source is None:
                    values[metric] = 0
                elif int(source.get(f"{metric}_rows") or 0) == 0:
                    values[metric] = None
                else:
                    values[metric] = (
                        int(source[metric]) if source.get(metric) is not None else None
                    )
            per_platform[platform] = values
            chart_series[platform].append(
                {
                    "date": date_key,
                    "views_delta": values["views"],
                    "likes_delta": values["likes"],
                    "comments_delta": values["comments"],
                }
            )

        all_values: Dict[str, Optional[int]] = {}
        for metric in ("views", "likes", "comments"):
            values = [
                per_platform[platform][metric]
                for platform in PLATFORMS
                if connections.get(platform)
            ]
            all_values[metric] = _known_sum(values)
        chart_series["all"].append(
            {
                "date": date_key,
                "views_delta": all_values["views"],
                "likes_delta": all_values["likes"],
                "comments_delta": all_values["comments"],
                "youtube_daily_views": per_platform["youtube"]["views"],
                "instagram_daily_views": per_platform["instagram"]["views"],
                "facebook_daily_views": per_platform["facebook"]["views"],
            }
        )
        daily_totals.append(
            {
                "snapshot_date": date.fromisoformat(date_key),
                "views": all_values["views"],
                "comments": all_values["comments"],
                "youtube_daily_views": _display_metric(
                    per_platform["youtube"]["views"], connected=connections["youtube"]
                ),
                "instagram_daily_views": _display_metric(
                    per_platform["instagram"]["views"], connected=connections["instagram"]
                ),
                "facebook_daily_views": _display_metric(
                    per_platform["facebook"]["views"], connected=connections["facebook"]
                ),
                "youtube_daily_likes": _display_metric(
                    per_platform["youtube"]["likes"], connected=connections["youtube"]
                ),
                "instagram_daily_likes": _display_metric(
                    per_platform["instagram"]["likes"], connected=connections["instagram"]
                ),
                "facebook_daily_likes": _display_metric(
                    per_platform["facebook"]["likes"], connected=connections["facebook"]
                ),
                "last_updated": last_updated,
                "last_updated_epoch": int(last_updated.timestamp()) if last_updated else 0,
            }
        )
    selected_platforms = (
        connected_platforms
        if selected_platform == "all"
        else ([selected_platform] if connections.get(selected_platform) else [])
    )
    video_rows = _video_rollup(
        conn,
        brand_id=brand_id,
        start_date=start_date,
        end_date=end_date,
        platforms=selected_platforms,
        connections=connections,
        limit=page_size,
        offset=(page - 1) * page_size,
    )
    video_total = int(video_rows[0].get("total_count") or 0) if video_rows else 0
    range_top_rows = _video_rollup(
        conn,
        brand_id=brand_id,
        start_date=start_date,
        end_date=end_date,
        platforms=selected_platforms,
        connections=connections,
        limit=6,
    )
    first_publish_row = conn.execute(
        f"""
        SELECT MIN(canonical_publish_date)
        FROM {MART}
        WHERE brand_id = ?
          AND canonical_publish_date BETWEEN ? AND ?
        """,
        [brand_id, start_date.isoformat(), end_date.isoformat()],
    ).fetchone()
    first_publish_date = first_publish_row[0] if first_publish_row else None
    if isinstance(first_publish_date, datetime):
        first_publish_date = first_publish_date.date()
    if isinstance(first_publish_date, date):
        daily_totals = [
            row for row in daily_totals if row["snapshot_date"] >= first_publish_date
        ]
    daily_totals.reverse()
    today_rows = _video_rollup(
        conn,
        brand_id=brand_id,
        start_date=today,
        end_date=today,
        platforms=selected_platforms,
        connections=connections,
        limit=5,
    )
    week_rows = _video_rollup(
        conn,
        brand_id=brand_id,
        start_date=today - timedelta(days=6),
        end_date=today,
        platforms=selected_platforms,
        connections=connections,
        limit=5,
    )
    audience_totals, audience_series, youtube_pending = _load_audience(
        conn,
        brand_id=brand_id,
        start_date=start_date,
        end_date=end_date,
        connections=connections,
    )
    return {
        "connections": connections,
        "current_totals": current_totals,
        "previous_totals": previous_totals,
        "previous_start": previous_start,
        "previous_end": previous_end,
        "chart_series_delta": chart_series,
        "daily_totals": daily_totals,
        "video_rows": video_rows,
        "video_rows_total": video_total,
        "range_top_videos": range_top_rows,
        "today_top_videos": today_rows,
        "week_top_videos": week_rows,
        "subscriber_totals": audience_totals,
        "subscriber_series": audience_series,
        "youtube_subscriber_data_missing": youtube_pending,
        "last_api_at": last_updated,
        "stats_connect_empty": not any(connections.values()),
    }


def load_top_videos_for_date(
    conn,
    *,
    owner_user_id: str,
    brand_id: str,
    target_date: date,
    selected_platform: str,
    limit: int,
) -> List[Dict[str, object]]:
    connections = load_platform_connections(
        conn,
        owner_user_id=owner_user_id,
        brand_id=brand_id,
    )
    platforms = (
        [platform for platform in PLATFORMS if connections[platform]]
        if selected_platform == "all"
        else ([selected_platform] if connections.get(selected_platform) else [])
    )
    return _video_rollup(
        conn,
        brand_id=brand_id,
        start_date=target_date,
        end_date=target_date,
        platforms=platforms,
        connections=connections,
        limit=limit,
    )
