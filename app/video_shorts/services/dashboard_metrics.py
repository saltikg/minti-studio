from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

from app.video_shorts.services.brands import brand_scoped_user_id


MART = "analytics.fct_short_daily_gains"
YOUTUBE_INSIGHTS_MART = "analytics.fct_short_youtube_insights_daily"
YOUTUBE_CONTENT_TYPE_MART = "analytics.fct_brand_youtube_content_type_daily"
SUBSCRIBER_TABLE = "main.shorts_channel_subscriber_daily"
PLATFORMS = ("youtube", "instagram", "facebook")
YOUTUBE_TRAFFIC_SOURCES = (
    ("shorts_feed_views", "Shorts feed"),
    ("search_views", "Search"),
    ("subscriber_views", "Subscribers"),
    ("notification_views", "Notifications"),
    ("channel_page_views", "Channel page"),
    ("external_views", "External"),
    ("other_views", "Other"),
)


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


def _as_date(value: object) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _as_pt_date(value: object) -> Optional[date]:
    if isinstance(value, datetime):
        moment = value
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(ZoneInfo("America/Los_Angeles")).date()
    return _as_date(value)


def _calendar_month_before(value: date) -> tuple[date, date]:
    month_start = value.replace(day=1)
    previous_end = month_start - timedelta(days=1)
    return previous_end.replace(day=1), previous_end


def _known_sum(values: Sequence[Optional[int]]) -> Optional[int]:
    known = [int(value) for value in values if value is not None]
    return sum(known) if known else None


FACEBOOK_INSIGHTS_UNAVAILABLE = "Views unavailable — reconnect Facebook"


def load_platform_state(
    conn,
    *,
    owner_user_id: str,
    brand_id: str,
) -> Dict[str, object]:
    scoped_owner = brand_scoped_user_id(owner_user_id, brand_id=brand_id)
    if not scoped_owner:
        return {
            "connections": {platform: False for platform in PLATFORMS},
            "metric_connections": {platform: False for platform in PLATFORMS},
            "facebook_insights_status": "not_connected",
            "facebook_insights_error": None,
            "facebook_unavailable_message": None,
        }
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
            ) AS facebook_connected,
            (
                SELECT insights_status FROM facebook_page_tokens
                WHERE user_id = ?
                LIMIT 1
            ) AS facebook_insights_status,
            (
                SELECT insights_error FROM facebook_page_tokens
                WHERE user_id = ?
                LIMIT 1
            ) AS facebook_insights_error
        """,
        [scoped_owner, scoped_owner, scoped_owner, scoped_owner, scoped_owner],
    ).fetchone()
    connections = {
        "youtube": bool(row and row[0]),
        "instagram": bool(row and row[1]),
        "facebook": bool(row and row[2]),
    }
    facebook_status = str((row and row[3]) or "active").strip().lower()
    facebook_unavailable = connections["facebook"] and facebook_status in {
        "needs_reconnect", "error"
    }
    metric_connections = dict(connections)
    if facebook_unavailable:
        metric_connections["facebook"] = False
    return {
        "connections": connections,
        "metric_connections": metric_connections,
        "facebook_insights_status": facebook_status,
        "facebook_insights_error": row[4] if row else None,
        "facebook_unavailable_message": (
            FACEBOOK_INSIGHTS_UNAVAILABLE if facebook_unavailable else None
        ),
    }


def load_platform_connections(
    conn,
    *,
    owner_user_id: str,
    brand_id: str,
) -> Dict[str, bool]:
    return load_platform_state(
        conn,
        owner_user_id=owner_user_id,
        brand_id=brand_id,
    )["connections"]


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


def _youtube_insights_highlight(
    *,
    traffic_sources: Sequence[Mapping[str, object]],
    analytics_views: int,
    subscribers_gained: int,
    top_subscriber_short: Optional[Mapping[str, object]],
    top_view_short: Optional[Mapping[str, object]],
    current_totals: Mapping[str, Mapping[str, Optional[int]]],
    previous_totals: Mapping[str, Mapping[str, Optional[int]]],
) -> Optional[str]:
    candidates: List[tuple[float, str]] = []
    platform_views = sorted(
        (
            (platform, int((current_totals.get(platform) or {}).get("views") or 0))
            for platform in PLATFORMS
        ),
        key=lambda item: item[1],
        reverse=True,
    )
    if len(platform_views) > 1 and platform_views[1][1] > 0:
        ratio = platform_views[0][1] / platform_views[1][1]
        if ratio >= 2:
            candidates.append(
                (
                    ratio / 2,
                    f"{platform_views[0][0].title()} delivered {ratio:.1f}× more views "
                    f"than {platform_views[1][0].title()}.",
                )
            )
    if top_view_short and analytics_views > 0:
        share = int(top_view_short.get("views") or 0) * 100.0 / analytics_views
        if share >= 30:
            candidates.append(
                (
                    share / 30,
                    f"{top_view_short.get('title') or 'The top Short'} generated "
                    f"{share:.0f}% of YouTube views in this period.",
                )
            )
    if subscribers_gained > 0 and top_subscriber_short:
        candidates.append(
            (
                2 + min(subscribers_gained / 100.0, 3),
                f"Shorts gained {subscribers_gained:,} YouTube subscriber"
                f"{'s' if subscribers_gained != 1 else ''}; "
                f"{top_subscriber_short.get('title') or 'the top Short'} led with "
                f"{int(top_subscriber_short.get('subscribers') or 0):,}.",
            )
        )
    current_views = int((current_totals.get("all") or {}).get("views") or 0)
    previous_views = int((previous_totals.get("all") or {}).get("views") or 0)
    if previous_views > 0:
        growth = (current_views - previous_views) * 100.0 / previous_views
        if growth >= 20:
            candidates.append(
                (growth / 20, f"Views grew {growth:.0f}% versus the previous period.")
            )
    if traffic_sources:
        dominant = max(traffic_sources, key=lambda source: float(source.get("percent") or 0))
        dominant_pct = float(dominant.get("percent") or 0)
        if dominant_pct >= 50:
            candidates.append(
                (
                    dominant_pct / 50,
                    f"{dominant.get('label')} drove {dominant_pct:.0f}% of attributed "
                    "YouTube views.",
                )
            )
    return max(candidates, key=lambda candidate: candidate[0])[1] if candidates else None


def _summarize_youtube_insight_rows(
    rows: Sequence[Mapping[str, object]],
    *,
    start_date: date,
    end_date: date,
    current_totals: Mapping[str, Mapping[str, Optional[int]]],
    previous_totals: Mapping[str, Mapping[str, Optional[int]]],
) -> Dict[str, object]:
    current_rows = [
        row
        for row in rows
        if isinstance(row.get("metric_date"), date)
        and start_date <= row["metric_date"] <= end_date
    ]
    if not current_rows:
        return {
            "status": "Pending",
            "traffic_sources": [],
            "analytics_views": None,
            "source_attributed_views": None,
            "coverage_percent": None,
            "coverage_note": None,
            "average_view_percentage": None,
            "average_view_duration_seconds": None,
            "subscribers_gained": None,
            "top_subscriber_short": None,
            "latest_date": None,
            "highlight": None,
        }

    known_views = [int(row["views"]) for row in current_rows if row.get("views") is not None]
    analytics_views = sum(known_views) if known_views else 0
    attributed_values = [
        int(row["source_attributed_views"])
        for row in current_rows
        if row.get("source_attributed_views") is not None
    ]
    attributed_views = sum(attributed_values) if attributed_values else None
    traffic_sources: List[Dict[str, object]] = []
    if attributed_views is not None and attributed_views > 0:
        for field, label in YOUTUBE_TRAFFIC_SOURCES:
            value = sum(int(row.get(field) or 0) for row in current_rows)
            traffic_sources.append(
                {
                    "key": field,
                    "label": label,
                    "views": value,
                    "percent": round(value * 100.0 / attributed_views, 1),
                }
            )

    pct_numerator = 0.0
    pct_denominator = 0
    duration_numerator = 0.0
    duration_denominator = 0
    subscribers_known = False
    subscribers_gained = 0
    subscriber_by_short: Dict[object, Dict[str, object]] = {}
    views_by_short: Dict[object, Dict[str, object]] = {}
    for row in current_rows:
        views = int(row["views"]) if row.get("views") is not None else None
        short_id = row.get("generated_video_id")
        title = str(row.get("generated_title") or "Untitled Short")
        if views is not None:
            item = views_by_short.setdefault(short_id, {"title": title, "views": 0})
            item["views"] = int(item["views"]) + views
        if views is not None and views > 5 and row.get("average_view_percentage") is not None:
            pct_numerator += float(row["average_view_percentage"]) * views
            pct_denominator += views
        if views is not None and views > 5 and row.get("average_view_duration_seconds") is not None:
            duration_numerator += float(row["average_view_duration_seconds"]) * views
            duration_denominator += views
        if row.get("subscribers_gained") is not None:
            subscribers_known = True
            gained = int(row["subscribers_gained"])
            subscribers_gained += gained
            item = subscriber_by_short.setdefault(
                short_id,
                {"title": title, "subscribers": 0},
            )
            item["subscribers"] = int(item["subscribers"]) + gained

    top_subscriber_short = (
        max(subscriber_by_short.values(), key=lambda item: int(item["subscribers"]))
        if subscriber_by_short
        else None
    )
    top_view_short = (
        max(views_by_short.values(), key=lambda item: int(item["views"]))
        if views_by_short
        else None
    )
    coverage_percent = (
        round(attributed_views * 100.0 / analytics_views, 1)
        if attributed_views is not None and analytics_views > 0
        else None
    )
    coverage_note = (
        f"Traffic sources cover {coverage_percent:.1f}% of Analytics views."
        if coverage_percent is not None and coverage_percent < 90
        else None
    )
    result: Dict[str, object] = {
        "status": "Ready",
        "traffic_sources": traffic_sources,
        "analytics_views": analytics_views,
        "source_attributed_views": attributed_views,
        "coverage_percent": coverage_percent,
        "coverage_note": coverage_note,
        "average_view_percentage": (
            round(pct_numerator / pct_denominator, 1) if pct_denominator else None
        ),
        "average_view_duration_seconds": (
            round(duration_numerator / duration_denominator, 1)
            if duration_denominator
            else None
        ),
        "subscribers_gained": subscribers_gained if subscribers_known else None,
        "top_subscriber_short": top_subscriber_short,
        "latest_date": max(
            row["metric_date"]
            for row in current_rows
            if isinstance(row.get("metric_date"), date)
        ),
    }
    result["highlight"] = _youtube_insights_highlight(
        traffic_sources=traffic_sources,
        analytics_views=analytics_views,
        subscribers_gained=subscribers_gained if subscribers_known else 0,
        top_subscriber_short=top_subscriber_short,
        top_view_short=top_view_short,
        current_totals=current_totals,
        previous_totals=previous_totals,
    )
    return result


def load_youtube_insights(
    conn,
    *,
    owner_user_id: str,
    brand_id: str,
    start_date: date,
    end_date: date,
    previous_start: date,
    current_totals: Mapping[str, Mapping[str, Optional[int]]],
    previous_totals: Mapping[str, Mapping[str, Optional[int]]],
) -> Dict[str, object]:
    rows = _rows(
        conn.execute(
            f"""
            SELECT
                metric_date,
                generated_video_id,
                generated_title,
                views,
                shorts_feed_views,
                search_views,
                subscriber_views,
                notification_views,
                channel_page_views,
                external_views,
                other_views,
                source_attributed_views,
                subscribers_gained,
                average_view_percentage,
                average_view_duration_seconds
            FROM {YOUTUBE_INSIGHTS_MART}
            WHERE owner_user_id = ?
              AND brand_id = ?
              AND metric_date BETWEEN ? AND ?
            ORDER BY metric_date, generated_video_id
            """,
            [owner_user_id, brand_id, previous_start.isoformat(), end_date.isoformat()],
        )
    )
    return _summarize_youtube_insight_rows(
        rows,
        start_date=start_date,
        end_date=end_date,
        current_totals=current_totals,
        previous_totals=previous_totals,
    )


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


def load_long_form_effect_metrics(
    conn,
    *,
    owner_user_id: str,
    brand_id: str,
    start_date: date,
    end_date: date,
) -> Dict[str, object]:
    """Return tenant-scoped VOD views for a range and its pre-Minti baseline."""
    # Provider timestamps can be historical imports (and therefore predate the
    # customer's Minti workflow). A publication close to the Minti record creation
    # is authoritative; otherwise creation marks the start of the imported series.
    first_publish_row = conn.execute(
        """
        SELECT
            MIN(created_at) AS first_created_at,
            MIN(LEAST(
                CASE WHEN youtube_published_at BETWEEN created_at AND created_at + INTERVAL '45 days'
                     THEN youtube_published_at END,
                CASE WHEN instagram_published_at BETWEEN created_at AND created_at + INTERVAL '45 days'
                     THEN instagram_published_at END,
                CASE WHEN facebook_published_at BETWEEN created_at AND created_at + INTERVAL '45 days'
                     THEN facebook_published_at END,
                CASE WHEN published_at BETWEEN created_at AND created_at + INTERVAL '45 days'
                     THEN published_at END
            )) AS first_workflow_publish_at
        FROM shorts_generated_videos
        WHERE CAST(user_id AS VARCHAR) = CAST(? AS VARCHAR)
          AND CAST(brand_id AS VARCHAR) = CAST(? AS VARCHAR)
          AND lower(COALESCE(publish_status, '')) = 'published'
          AND COALESCE(
              youtube_published_at,
              instagram_published_at,
              facebook_published_at,
              published_at
          ) IS NOT NULL
        """,
        [owner_user_id, brand_id],
    ).fetchone()
    first_publish_value = (
        (first_publish_row[1] or first_publish_row[0])
        if first_publish_row
        else None
    )
    first_publish_date = _as_pt_date(first_publish_value)
    if first_publish_date is None:
        return {
            "status": "Pending",
            "range_start": start_date,
            "range_end": end_date,
            "long_form_views": None,
            "long_form_views_per_day": None,
            "days_in_range": 0,
            "baseline_start": None,
            "baseline_end": None,
            "baseline_long_form_views": None,
            "baseline_views_per_day": None,
            "baseline_days": 0,
            "change_percent": None,
            "data_through_date": None,
            "first_minti_publication_date": None,
            "days_after_first_publication": 0,
            "trend_status": "too_early",
        }

    baseline_start, baseline_end = _calendar_month_before(first_publish_date)
    row = conn.execute(
        f"""
        SELECT
            SUM(vod_views) FILTER (WHERE metric_date BETWEEN ? AND ?) AS range_views,
            COUNT(DISTINCT metric_date) FILTER (
                WHERE metric_date BETWEEN ? AND ?
            ) AS range_days,
            SUM(vod_views) FILTER (
                WHERE metric_date BETWEEN ? AND ?
            ) AS baseline_views,
            COUNT(DISTINCT metric_date) FILTER (
                WHERE metric_date BETWEEN ? AND ?
            ) AS baseline_days,
            MAX(metric_date) FILTER (
                WHERE metric_date BETWEEN ? AND ?
            ) AS data_through_date
        FROM {YOUTUBE_CONTENT_TYPE_MART}
        WHERE CAST(owner_user_id AS VARCHAR) = CAST(? AS VARCHAR)
          AND CAST(brand_id AS VARCHAR) = CAST(? AS VARCHAR)
          AND metric_date BETWEEN ? AND ?
        """,
        [
            start_date.isoformat(), end_date.isoformat(),
            start_date.isoformat(), end_date.isoformat(),
            baseline_start.isoformat(), baseline_end.isoformat(),
            baseline_start.isoformat(), baseline_end.isoformat(),
            start_date.isoformat(), end_date.isoformat(),
            owner_user_id, brand_id,
            min(start_date, baseline_start).isoformat(),
            max(end_date, baseline_end).isoformat(),
        ],
    ).fetchone()
    range_views = int(row[0]) if row and row[0] is not None else None
    range_days = int(row[1] or 0) if row else 0
    baseline_views = int(row[2]) if row and row[2] is not None else None
    baseline_days = int(row[3] or 0) if row else 0
    range_average = (
        range_views / range_days
        if range_views is not None and range_days > 0
        else None
    )
    baseline_average = (
        baseline_views / baseline_days
        if baseline_views is not None and baseline_days > 0
        else None
    )
    change_percent = (
        ((range_average - baseline_average) / baseline_average) * 100.0
        if range_average is not None
        and baseline_average is not None
        and baseline_average > 0
        else None
    )
    data_through_date = _as_date(row[4] if row else None)
    observed_end = min(end_date, data_through_date) if data_through_date else None
    observed_start = max(start_date, first_publish_date)
    days_after_first_publication = (
        (observed_end - observed_start).days + 1
        if observed_end is not None and observed_end >= observed_start
        else 0
    )
    return {
        "status": "ready" if range_average is not None else "Pending",
        "range_start": start_date,
        "range_end": end_date,
        "long_form_views": range_views,
        "long_form_views_per_day": range_average,
        "days_in_range": range_days,
        "baseline_start": baseline_start,
        "baseline_end": baseline_end,
        "baseline_long_form_views": baseline_views,
        "baseline_views_per_day": baseline_average,
        "baseline_days": baseline_days,
        "change_percent": change_percent,
        "data_through_date": data_through_date,
        "first_minti_publication_date": first_publish_date,
        "days_after_first_publication": days_after_first_publication,
        "trend_status": (
            "too_early"
            if days_after_first_publication
            < int(REPORT_VERDICT_THRESHOLDS["long_form_min_days"])
            else "ready"
        ),
    }


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
    platform_state = load_platform_state(
        conn,
        owner_user_id=owner_user_id,
        brand_id=brand_id,
    )
    connections = platform_state["connections"]
    metric_connections = platform_state["metric_connections"]
    facebook_unavailable_message = platform_state["facebook_unavailable_message"]
    connected_platforms = [
        platform for platform in PLATFORMS if metric_connections[platform]
    ]
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
        connections=metric_connections,
    )
    previous_totals = _period_totals(
        daily_map,
        start_date=previous_start,
        end_date=previous_end,
        connections=metric_connections,
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
                if not metric_connections.get(platform):
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
                if metric_connections.get(platform)
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
                    per_platform["youtube"]["views"], connected=metric_connections["youtube"]
                ),
                "instagram_daily_views": _display_metric(
                    per_platform["instagram"]["views"], connected=metric_connections["instagram"]
                ),
                "facebook_daily_views": _display_metric(
                    per_platform["facebook"]["views"], connected=metric_connections["facebook"]
                ),
                "youtube_daily_likes": _display_metric(
                    per_platform["youtube"]["likes"], connected=metric_connections["youtube"]
                ),
                "instagram_daily_likes": _display_metric(
                    per_platform["instagram"]["likes"], connected=metric_connections["instagram"]
                ),
                "facebook_daily_likes": _display_metric(
                    per_platform["facebook"]["likes"], connected=metric_connections["facebook"]
                ),
                "last_updated": last_updated,
                "last_updated_epoch": int(last_updated.timestamp()) if last_updated else 0,
            }
        )
    selected_platforms = (
        connected_platforms
        if selected_platform == "all"
        else ([selected_platform] if metric_connections.get(selected_platform) else [])
    )
    video_rows = _video_rollup(
        conn,
        brand_id=brand_id,
        start_date=start_date,
        end_date=end_date,
        platforms=selected_platforms,
        connections=metric_connections,
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
        connections=metric_connections,
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
        connections=metric_connections,
        limit=5,
    )
    week_rows = _video_rollup(
        conn,
        brand_id=brand_id,
        start_date=today - timedelta(days=6),
        end_date=today,
        platforms=selected_platforms,
        connections=metric_connections,
        limit=5,
    )
    audience_totals, audience_series, youtube_pending = _load_audience(
        conn,
        brand_id=brand_id,
        start_date=start_date,
        end_date=end_date,
        connections=connections,
    )
    youtube_insights = (
        load_youtube_insights(
            conn,
            owner_user_id=owner_user_id,
            brand_id=brand_id,
            start_date=start_date,
            end_date=end_date,
            previous_start=previous_start,
            current_totals=current_totals,
            previous_totals=previous_totals,
        )
        if connections.get("youtube")
        else {
            "status": "Not connected",
            "traffic_sources": [],
            "subscribers_gained": None,
            "highlight": None,
        }
    )
    long_form_effect = load_long_form_effect_metrics(
        conn,
        owner_user_id=owner_user_id,
        brand_id=brand_id,
        start_date=start_date,
        end_date=end_date,
    )
    if facebook_unavailable_message:
        for row in daily_totals:
            row["facebook_daily_views"] = facebook_unavailable_message
            row["facebook_daily_likes"] = facebook_unavailable_message
        for rows_for_view in (
            video_rows,
            range_top_rows,
            today_rows,
            week_rows,
        ):
            for row in rows_for_view:
                row["facebook_views"] = facebook_unavailable_message
    return {
        "connections": connections,
        "metric_connections": metric_connections,
        "facebook_insights_status": platform_state["facebook_insights_status"],
        "facebook_insights_error": platform_state["facebook_insights_error"],
        "facebook_unavailable_message": facebook_unavailable_message,
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
        "youtube_insights": youtube_insights,
        "long_form_effect": long_form_effect,
        "last_api_at": last_updated,
        "stats_connect_empty": not any(connections.values()),
    }


REPORT_TIPS = (
    "Keep the first sentence direct: name the problem or promise before adding context.",
    "A strong on-screen hook in the first two seconds helps viewers decide to stay.",
    "Reply to early comments while a Short is fresh; it can turn viewers into regulars.",
    "Repeat the topics your audience finishes most often, then test a new opening line.",
)

REPORT_VERDICT_THRESHOLDS = {
    "platform_ratio": 2.0,
    "average_watched_percent": 60.0,
    "dominant_traffic_percent": 40.0,
    "mom_views_percent": 20.0,
    "shorts_feed_opportunity_percent": 30.0,
    "platform_low_share_percent": 10.0,
    "analytics_lag_days": 2,
    "long_form_min_days": 7,
}

REPORT_PLATFORM_LABELS = {
    "youtube": "YouTube",
    "instagram": "Instagram",
    "facebook": "Facebook",
}


def _mom_result(
    current: Optional[int],
    previous: Optional[int],
    *,
    previous_label: str,
) -> Dict[str, object]:
    if previous is None or previous <= 0:
        return {
            "current": current,
            "previous": previous,
            "percent": None,
            "direction": "first",
            "label": "First month",
        }
    current_value = int(current or 0)
    percent = (current_value - previous) * 100.0 / previous
    if percent > 0:
        direction = "up"
        label = f"↑ {abs(percent):.0f}% vs {previous_label}"
    elif percent < 0:
        direction = "down"
        label = f"↓ {abs(percent):.0f}% vs {previous_label}"
    else:
        direction = "flat"
        label = f"0% vs {previous_label}"
    return {
        "current": current_value,
        "previous": int(previous),
        "percent": round(percent, 1),
        "direction": direction,
        "label": label,
    }


def build_monthly_report_summary(
    *,
    current_totals: Mapping[str, Mapping[str, Optional[int]]],
    connections: Mapping[str, bool],
    youtube_insights: Mapping[str, object],
    monthly_comparison: Mapping[str, Mapping[str, object]],
    start_date: date,
    end_date: date,
    today: date,
    metric_connections: Optional[Mapping[str, bool]] = None,
) -> Dict[str, object]:
    """Build reusable rule-based report headline and verdict rows."""
    lag_days = int(REPORT_VERDICT_THRESHOLDS["analytics_lag_days"])
    analytics_target_end = min(end_date, today - timedelta(days=lag_days))
    analytics_latest = _as_date(youtube_insights.get("latest_date"))
    analytics_complete = bool(
        analytics_latest
        and analytics_target_end >= start_date
        and analytics_latest >= analytics_target_end
    )

    available = metric_connections or connections
    strengths: List[Dict[str, object]] = []
    opportunities: List[Dict[str, object]] = []
    connected_views = sorted(
        (
            (
                platform,
                int((current_totals.get(platform) or {}).get("views") or 0),
            )
            for platform in PLATFORMS
            if connections.get(platform) and available.get(platform)
        ),
        key=lambda item: item[1],
        reverse=True,
    )
    platform_fact: Optional[Dict[str, object]] = None
    if len(connected_views) >= 2 and connected_views[1][1] > 0:
        leader, leader_views = connected_views[0]
        runner, runner_views = connected_views[1]
        ratio = leader_views / runner_views
        if ratio >= float(REPORT_VERDICT_THRESHOLDS["platform_ratio"]):
            platform_fact = {
                "kind": "strength",
                "tag": "Strength",
                "metric": f"{REPORT_PLATFORM_LABELS[leader]} is leading",
                "value": f"{ratio:.1f}×",
                "context": f"your {REPORT_PLATFORM_LABELS[runner]} views",
                "score": 100.0 + ratio,
                "fact": "platform_ratio",
                "platform": leader,
                "runner": runner,
            }
            strengths.append(platform_fact)

    traffic = [
        item
        for item in (youtube_insights.get("traffic_sources") or [])
        if float(item.get("percent") or 0) > 0
    ]
    dominant = max(
        traffic,
        key=lambda item: float(item.get("percent") or 0),
        default=None,
    )
    dominant_fact: Optional[Dict[str, object]] = None
    dominant_percent = float((dominant or {}).get("percent") or 0)
    if (
        analytics_complete
        and dominant
        and dominant_percent
        >= float(REPORT_VERDICT_THRESHOLDS["dominant_traffic_percent"])
    ):
        dominant_fact = {
            "kind": "strength",
            "tag": "Strength",
            "metric": f"{dominant.get('label')} is working",
            "value": f"{dominant_percent:.0f}%",
            "context": "of attributed YouTube views",
            "score": 80.0 + dominant_percent / 10.0,
            "fact": "traffic_source",
            "source_key": dominant.get("key"),
            "source_label": dominant.get("label"),
        }
        strengths.append(dominant_fact)

    average_watched = youtube_insights.get("average_view_percentage")
    if (
        analytics_complete
        and average_watched is not None
        and float(average_watched)
        >= float(REPORT_VERDICT_THRESHOLDS["average_watched_percent"])
    ):
        strengths.append(
            {
                "kind": "strength",
                "tag": "Strength",
                "metric": "Viewers keep watching",
                "value": f"{float(average_watched):.0f}%",
                "context": "watched on average",
                "score": 60.0 + float(average_watched) / 10.0,
                "fact": "retention",
            }
        )

    views_mom = monthly_comparison.get("views") or {}
    views_growth = views_mom.get("percent")
    if (
        views_growth is not None
        and float(views_growth)
        >= float(REPORT_VERDICT_THRESHOLDS["mom_views_percent"])
    ):
        strengths.append(
            {
                "kind": "strength",
                "tag": "Strength",
                "metric": "Views grew",
                "value": f"+{float(views_growth):.0f}%",
                "context": str(views_mom.get("label") or "month over month").lstrip("↑ "),
                "score": 70.0 + min(float(views_growth), 200.0) / 20.0,
                "fact": "mom_views",
            }
        )

    subscribers = youtube_insights.get("subscribers_gained")
    if analytics_complete and subscribers is not None and int(subscribers) > 0:
        strengths.append(
            {
                "kind": "strength",
                "tag": "Strength",
                "metric": "Shorts gained subscribers",
                "value": f"+{int(subscribers):,}",
                "context": "new YouTube subscribers from Shorts",
                "score": 50.0 + min(int(subscribers), 500) / 100.0,
                "fact": "subscribers",
            }
        )

    shorts_feed = next(
        (
            item
            for item in traffic
            if str(item.get("key") or "") == "shorts_feed_views"
        ),
        None,
    )
    shorts_feed_percent = float((shorts_feed or {}).get("percent") or 0)
    if (
        analytics_complete
        and shorts_feed is not None
        and shorts_feed_percent
        < float(REPORT_VERDICT_THRESHOLDS["shorts_feed_opportunity_percent"])
    ):
        opportunities.append(
            {
                "kind": "opportunity",
                "tag": "Opportunity",
                "metric": "Shorts feed",
                "value": f"{shorts_feed_percent:.0f}%",
                "context": "the feed is still learning your channel",
                "score": 90.0 - shorts_feed_percent,
                "fact": "shorts_feed",
            }
        )
    if analytics_complete and subscribers is not None and int(subscribers) == 0:
        opportunities.append(
            {
                "kind": "opportunity",
                "tag": "Opportunity",
                "metric": "Subscriber conversion",
                "value": "0",
                "context": "a ‘follow for more’ line can help",
                "score": 85.0,
                "fact": "subscriber_conversion",
            }
        )
    for platform in ("facebook", "instagram", "youtube"):
        if not connections.get(platform):
            opportunities.append(
                {
                    "kind": "opportunity",
                    "tag": "Opportunity",
                    "metric": f"{REPORT_PLATFORM_LABELS[platform]} reach",
                    "value": "Not connected",
                    "context": f"connect {REPORT_PLATFORM_LABELS[platform]} to reach more viewers",
                    "score": 50.0,
                    "fact": "not_connected",
                }
            )
            break

    total_views = int((current_totals.get("all") or {}).get("views") or 0)
    existing_opportunity_facts = {
        (item.get("fact"), item.get("platform")) for item in opportunities
    }
    for platform in PLATFORMS:
        if not connections.get(platform) or not available.get(platform):
            continue
        platform_views = (current_totals.get(platform) or {}).get("views")
        label = REPORT_PLATFORM_LABELS[platform]
        if platform_views is None or int(platform_views) == 0:
            fact = "platform_pending" if platform_views is None else "platform_zero"
            if (fact, platform) not in existing_opportunity_facts:
                opportunities.append(
                    {
                        "kind": "opportunity",
                        "tag": "Opportunity",
                        "metric": f"{label} reach",
                        "value": "Pending" if platform_views is None else "0",
                        "context": (
                            "views are still pending for this month"
                            if platform_views is None
                            else "no measured views this month"
                        ),
                        "score": 65.0 if platform_views is None else 70.0,
                        "fact": fact,
                        "platform": platform,
                    }
                )
            continue
        share = (int(platform_views) * 100.0 / total_views) if total_views else 0.0
        if share < float(REPORT_VERDICT_THRESHOLDS["platform_low_share_percent"]):
            opportunities.append(
                {
                    "kind": "opportunity",
                    "tag": "Opportunity",
                    "metric": f"{label} reach",
                    "value": f"{share:.0f}%",
                    "context": "of your views",
                    "score": 60.0 + (10.0 - share),
                    "fact": "platform_low_share",
                    "platform": platform,
                }
            )

    strengths.sort(key=lambda item: float(item["score"]), reverse=True)
    opportunities.sort(key=lambda item: float(item["score"]), reverse=True)
    selected_strengths = strengths[:3]
    selected_opportunities = opportunities[:2]

    if platform_fact:
        platform_name = REPORT_PLATFORM_LABELS[str(platform_fact["platform"])]
        if dominant_fact:
            source = str(dominant_fact.get("source_label") or "YouTube").lower()
            if dominant_fact.get("source_key") == "shorts_feed_views":
                headline = (
                    f"{platform_name} is your growth engine — and the Shorts feed "
                    "is bringing people in"
                )
            else:
                headline = (
                    f"{platform_name} is your growth engine — and people find you "
                    f"through {source}"
                )
        else:
            headline = f"{platform_name} is your growth engine this month"
    elif dominant_fact:
        headline = f"People find your Shorts through {str(dominant_fact['source_label']).lower()}"
    elif selected_strengths:
        top = selected_strengths[0]
        if top.get("fact") == "retention":
            headline = f"People watched {top['value']} of your Shorts on average"
        elif top.get("fact") == "mom_views":
            headline = f"Your Shorts views grew {top['value']} month over month"
        elif top.get("fact") == "subscribers":
            headline = f"Your Shorts brought you {top['value']} new subscribers"
        else:
            headline = "Your Shorts kept building your audience this month"
    else:
        headline = "Your Shorts kept building your audience this month"

    return {
        "headline": headline,
        "verdict_rows": [*selected_strengths, *selected_opportunities],
        "analytics_complete_through_range": analytics_complete,
        "analytics_target_end": analytics_target_end,
    }


def _audience_change(series: Sequence[Mapping[str, object]], platform: str) -> Optional[int]:
    values = [
        int(row[f"{platform}_net"])
        for row in series
        if row.get(f"{platform}_net") is not None
    ]
    return sum(values) if values else None


def _as_utc(value: object) -> Optional[datetime]:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def load_monthly_report_metrics(
    conn,
    *,
    owner_user_id: str,
    brand_id: str,
    brand_name: str,
    start_date: date,
    end_date: date,
    today: date,
) -> Dict[str, object]:
    """Compose report-only presentation data from the canonical dashboard payload.

    Metric values are never redefined here: views, engagement, audience, rankings,
    and YouTube insights use the same canonical loaders as ``load_dashboard_metrics``.
    Extra reads cover the prior calendar month, publication counts, and future schedules.
    """
    dashboard = load_dashboard_metrics(
        conn,
        owner_user_id=owner_user_id,
        brand_id=brand_id,
        start_date=start_date,
        end_date=end_date,
        selected_platform="all",
        page=1,
        page_size=500,
        today=today,
    )
    previous_month_start, previous_month_end = _calendar_month_before(start_date)
    connected_platforms = [
        platform for platform in PLATFORMS
        if dashboard["metric_connections"].get(platform)
    ]
    previous_rows = _daily_metric_rows(
        conn,
        brand_id=brand_id,
        start_date=previous_month_start,
        end_date=previous_month_end,
        platforms=connected_platforms,
    )
    previous_daily: Dict[str, Dict[str, Dict[str, object]]] = {}
    for row in previous_rows:
        platform = str(row.get("platform") or "")
        metric_date = _as_date(row.get("metric_date"))
        if platform and metric_date:
            previous_daily.setdefault(platform, {})[metric_date.isoformat()] = row
    previous_calendar_totals = _period_totals(
        previous_daily,
        start_date=previous_month_start,
        end_date=previous_month_end,
        connections=dashboard["metric_connections"],
    )
    _, previous_audience_series, _ = _load_audience(
        conn,
        brand_id=brand_id,
        start_date=previous_month_start,
        end_date=previous_month_end,
        connections=dashboard["connections"],
    )
    publication = conn.execute(
        f"""
        SELECT
            COUNT(DISTINCT generated_video_id) FILTER (
                WHERE canonical_publish_date BETWEEN ? AND ?
            ) AS shorts_published,
            COUNT(DISTINCT CAST(generated_video_id AS VARCHAR) || ':' || platform) FILTER (
                WHERE platform_publish_date BETWEEN ? AND ?
            ) AS platform_posts,
            COUNT(DISTINCT generated_video_id) FILTER (
                WHERE canonical_publish_date BETWEEN ? AND ?
            ) AS previous_shorts_published
        FROM {MART}
        WHERE brand_id = ?
        """,
        [
            start_date.isoformat(), end_date.isoformat(),
            start_date.isoformat(), end_date.isoformat(),
            previous_month_start.isoformat(), previous_month_end.isoformat(),
            brand_id,
        ],
    ).fetchone()
    platform_post_rows = _rows(
        conn.execute(
            f"""
            SELECT platform, COUNT(DISTINCT generated_video_id) AS posts
            FROM {MART}
            WHERE brand_id = ?
              AND platform_publish_date BETWEEN ? AND ?
            GROUP BY platform
            ORDER BY platform
            """,
            [brand_id, start_date.isoformat(), end_date.isoformat()],
        )
    )

    effective_range_end = min(end_date, today)
    schedule_start = max(today, effective_range_end)
    schedule_end = schedule_start + timedelta(days=30)
    pt = ZoneInfo("America/Los_Angeles")
    now_pt = datetime.now(pt)
    schedule_start_at = (
        now_pt.astimezone(timezone.utc).replace(tzinfo=None)
        if now_pt.date() == today
        else datetime.combine(schedule_start, datetime.min.time())
    )
    schedule_end_at = schedule_start_at + timedelta(days=30)
    scheduled_rows = _rows(
        conn.execute(
            """
            WITH scoped_shorts AS (
                SELECT id, source_video_id, clip_filename, generated_title,
                       planned_publish_at, publish_status
                FROM shorts_generated_videos
                WHERE CAST(user_id AS VARCHAR) = CAST(? AS VARCHAR)
                  AND CAST(brand_id AS VARCHAR) = CAST(? AS VARCHAR)
            ), schedule_candidates AS (
                SELECT id, generated_title, planned_publish_at
                FROM scoped_shorts
                WHERE planned_publish_at IS NOT NULL
                  AND lower(coalesce(publish_status, '')) IN
                      ('scheduled', 'pending', 'retry', 'uploading', 'uploaded', 'queued')
                UNION ALL
                SELECT generated.id, COALESCE(generated.generated_title, queue.plan_title),
                       CAST(NULLIF(queue.publish_at, '') AS TIMESTAMP)
                FROM scoped_shorts generated
                JOIN shorts_instagram_queue queue
                  ON CAST(queue.user_id AS VARCHAR) = CAST(? AS VARCHAR)
                 AND CAST(queue.brand_id AS VARCHAR) = CAST(? AS VARCHAR)
                 AND CAST(queue.video_id AS VARCHAR) = CAST(generated.source_video_id AS VARCHAR)
                 AND queue.clip_filename = generated.clip_filename
                WHERE lower(coalesce(queue.status, '')) IN
                      ('scheduled', 'pending', 'retry', 'uploading', 'queued')
                  AND NULLIF(queue.publish_at, '') IS NOT NULL
                UNION ALL
                SELECT generated.id, COALESCE(generated.generated_title, queue.plan_title),
                       CAST(NULLIF(queue.publish_at, '') AS TIMESTAMP)
                FROM scoped_shorts generated
                JOIN shorts_facebook_queue queue
                  ON CAST(queue.user_id AS VARCHAR) = CAST(? AS VARCHAR)
                 AND CAST(queue.brand_id AS VARCHAR) = CAST(? AS VARCHAR)
                 AND CAST(queue.video_id AS VARCHAR) = CAST(generated.source_video_id AS VARCHAR)
                 AND queue.clip_filename = generated.clip_filename
                WHERE lower(coalesce(queue.status, '')) IN
                      ('scheduled', 'pending', 'retry', 'uploading', 'queued')
                  AND NULLIF(queue.publish_at, '') IS NOT NULL
            )
            SELECT id, MAX(generated_title) AS generated_title,
                   MIN(planned_publish_at) AS planned_publish_at
            FROM schedule_candidates
            WHERE planned_publish_at >= ?
              AND planned_publish_at < ?
            GROUP BY id
            ORDER BY planned_publish_at, id
            """,
            [
                owner_user_id,
                brand_id,
                owner_user_id,
                brand_id,
                owner_user_id,
                brand_id,
                schedule_start_at,
                schedule_end_at,
            ],
        )
    )
    scheduled: List[Dict[str, object]] = []
    for row in scheduled_rows:
        planned = _as_utc(row.get("planned_publish_at"))
        if planned is None:
            continue
        local = planned.astimezone(pt)
        scheduled.append(
            {
                "id": row.get("id"),
                "title": str(row.get("generated_title") or "Untitled Short"),
                "date": local.date(),
                "time": local.strftime("%-I:%M %p PT"),
            }
        )

    audience_changes = {
        platform: _audience_change(dashboard["subscriber_series"], platform)
        for platform in PLATFORMS
    }
    previous_audience_changes = {
        platform: _audience_change(previous_audience_series, platform)
        for platform in PLATFORMS
    }
    platform_posts = {
        str(row.get("platform")): int(row.get("posts") or 0)
        for row in platform_post_rows
    }
    current_subscribers = _known_sum(
        [
            audience_changes.get(platform)
            for platform in ("youtube", "instagram")
            if dashboard["connections"].get(platform)
        ]
    )
    previous_subscribers = _known_sum(
        [
            previous_audience_changes.get(platform)
            for platform in ("youtube", "instagram")
            if dashboard["connections"].get(platform)
        ]
    )
    previous_label = previous_month_start.strftime("%b")
    monthly_comparison = {
        "shorts_published": _mom_result(
            int((publication or [0, 0, 0])[0] or 0),
            int((publication or [0, 0, 0])[2] or 0),
            previous_label=previous_label,
        ),
        "views": _mom_result(
            dashboard["current_totals"]["all"].get("views"),
            previous_calendar_totals["all"].get("views"),
            previous_label=previous_label,
        ),
        "subscribers": _mom_result(
            current_subscribers,
            previous_subscribers,
            previous_label=previous_label,
        ),
        "likes": _mom_result(
            dashboard["current_totals"]["all"].get("likes"),
            previous_calendar_totals["all"].get("likes"),
            previous_label=previous_label,
        ),
    }
    if int((publication or [0, 0, 0])[2] or 0) == 0:
        monthly_comparison = {
            key: _mom_result(item.get("current"), None, previous_label=previous_label)
            for key, item in monthly_comparison.items()
        }
    report_summary = build_monthly_report_summary(
        current_totals=dashboard["current_totals"],
        connections=dashboard["connections"],
        youtube_insights=dashboard["youtube_insights"],
        monthly_comparison=monthly_comparison,
        start_date=start_date,
        end_date=end_date,
        today=today,
        metric_connections=dashboard["metric_connections"],
    )
    month_index = (start_date.year * 12 + start_date.month - 1) % len(REPORT_TIPS)
    return {
        **dashboard,
        "brand_name": brand_name,
        "shorts_published": int((publication or [0, 0, 0])[0] or 0),
        "platform_posts_total": int((publication or [0, 0, 0])[1] or 0),
        "platform_posts": platform_posts,
        "audience_changes": audience_changes,
        "previous_month_start": previous_month_start,
        "previous_month_end": previous_month_end,
        "previous_calendar_totals": previous_calendar_totals,
        "previous_audience_changes": previous_audience_changes,
        "monthly_comparison": monthly_comparison,
        "report_summary": report_summary,
        "scheduled_shorts": scheduled,
        "schedule_start": schedule_start,
        "schedule_end": schedule_end,
        "tip": REPORT_TIPS[month_index],
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
    platform_state = load_platform_state(
        conn,
        owner_user_id=owner_user_id,
        brand_id=brand_id,
    )
    connections = platform_state["metric_connections"]
    platforms = (
        [platform for platform in PLATFORMS if connections[platform]]
        if selected_platform == "all"
        else ([selected_platform] if connections.get(selected_platform) else [])
    )
    rows = _video_rollup(
        conn,
        brand_id=brand_id,
        start_date=target_date,
        end_date=target_date,
        platforms=platforms,
        connections=connections,
        limit=limit,
    )
    if platform_state["facebook_unavailable_message"]:
        for row in rows:
            row["facebook_views"] = platform_state["facebook_unavailable_message"]
    return rows
