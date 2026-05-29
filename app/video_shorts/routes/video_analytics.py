from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from flask import abort, jsonify, render_template, request

from app.video_shorts import video_shorts_bp
from app.video_shorts.services.brands import current_brand_id
from app.video_shorts.services.db import get_db_readonly


PAGE_SIZE = 25
DISCOVERY_LABEL_OPTIONS = {"algorithm_driven", "demand_driven", "mixed"}
SORT_COLUMN_MAP = {
    "total_views": "total_views",
    "avg_retention_pct": "avg_retention_pct",
    "effective_publish_at": "effective_publish_at",
    "algorithm_pct": "algorithm_pct",
    "search_pct": "search_pct",
    "subscriber_pct": "subscriber_pct",
}
VIDEO_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


def _rows_to_dict(cursor):
    columns = [desc[0] for desc in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _parse_date_param(value):
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        try:
            return datetime.fromisoformat(value).date()
        except ValueError:
            return None


def _normalize_filters():
    today = date.today()
    end_date = _parse_date_param(request.args.get("end_date")) or today
    start_date = _parse_date_param(request.args.get("start_date")) or (today - timedelta(days=90))
    if start_date > end_date:
        start_date, end_date = end_date, start_date

    discovery_label = (request.args.get("discovery_label") or "").strip().lower()
    if discovery_label not in DISCOVERY_LABEL_OPTIONS:
        discovery_label = ""

    sort_by = (request.args.get("sort_by") or "total_views").strip().lower()
    if sort_by not in SORT_COLUMN_MAP:
        sort_by = "total_views"

    sort_dir = (request.args.get("sort_dir") or "desc").strip().lower()
    if sort_dir not in {"asc", "desc"}:
        sort_dir = "desc"

    try:
        page = max(int(request.args.get("page", "1")), 1)
    except ValueError:
        page = 1

    return start_date, end_date, discovery_label, sort_by, sort_dir, page


@video_shorts_bp.route("/video-analytics", methods=["GET"])
def video_analytics_page():
    brand_id = current_brand_id()
    start_date, end_date, discovery_label, sort_by, sort_dir, page = _normalize_filters()
    sort_col = SORT_COLUMN_MAP[sort_by]

    conn = get_db_readonly()
    try:
        where_clauses = [
            "total_views > 0",
            "effective_publish_at::date BETWEEN ? AND ?",
        ]
        params = [start_date.isoformat(), end_date.isoformat()]

        if brand_id:
            where_clauses.append("brand_id = ?")
            params.append(brand_id)
        if discovery_label:
            where_clauses.append("discovery_label = ?")
            params.append(discovery_label)

        where_sql = " AND ".join(where_clauses)

        count_sql = f"""
        SELECT COUNT(*)
        FROM analytics.fct_video_performance_summary
        WHERE {where_sql}
        """
        total_count = conn.execute(count_sql, params).fetchone()[0]
        total_pages = max((total_count + PAGE_SIZE - 1) // PAGE_SIZE, 1)
        page = min(page, total_pages)
        offset = (page - 1) * PAGE_SIZE

        sql = f"""
        SELECT
            youtube_video_id,
            generated_title,
            effective_publish_at::date AS publish_date,
            total_views,
            algorithm_pct,
            search_pct,
            subscriber_pct,
            discovery_label,
            avg_retention_pct,
            avg_duration_sec,
            retention_tier,
            ever_looped,
            total_subs_gained,
            video_duration_sec
        FROM analytics.fct_video_performance_summary
        WHERE {where_sql}
        ORDER BY {sort_col} {sort_dir}
        LIMIT ? OFFSET ?
        """
        cursor = conn.execute(sql, params + [PAGE_SIZE, offset])
        rows = _rows_to_dict(cursor)

        return render_template(
            "video_analytics.html",
            videos=rows,
            brand_id=brand_id,
            start_date=start_date,
            end_date=end_date,
            discovery_label=discovery_label,
            sort_by=sort_by,
            sort_dir=sort_dir,
            page=page,
            total_count=total_count,
            total_pages=total_pages,
            page_size=PAGE_SIZE,
        )
    finally:
        conn.close()


@video_shorts_bp.route("/video-analytics/lifecycle/<video_id>", methods=["GET"])
def video_lifecycle_api(video_id):
    if not VIDEO_ID_PATTERN.fullmatch(video_id or ""):
        abort(400)

    conn = get_db_readonly()
    try:
        cursor = conn.execute(
            """
            SELECT days_since_publish, daily_views, snapshot_date::text
            FROM analytics.fct_video_lifecycle
            WHERE video_id = ?
            ORDER BY days_since_publish
            """,
            [video_id],
        )
        lifecycle_rows = _rows_to_dict(cursor)

        cursor2 = conn.execute(
            """
            SELECT
                l.days_since_publish,
                t.traffic_source_type,
                SUM(t.views) AS views
            FROM analytics.fct_video_lifecycle l
            JOIN main.raw_yt_traffic_sources t
                ON t.video_id = l.video_id
               AND t.snapshot_date = l.snapshot_date
            WHERE l.video_id = ?
            GROUP BY l.days_since_publish, t.traffic_source_type
            ORDER BY l.days_since_publish, views DESC
            """,
            [video_id],
        )
        traffic_rows = _rows_to_dict(cursor2)
    finally:
        conn.close()

    return jsonify({
        "lifecycle": lifecycle_rows,
        "traffic": traffic_rows,
    })
