from __future__ import annotations

from flask import render_template, request

from app.video_shorts import video_shorts_bp
from app.video_shorts.services.brands import current_brand_id
from app.video_shorts.services.db import get_db_readonly


def _rows_to_dict(cursor):
    columns = [desc[0] for desc in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


@video_shorts_bp.route("/video-analytics", methods=["GET"])
def video_analytics_page():
    brand_id = current_brand_id()
    conn = get_db_readonly()
    try:
        sql = """
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
        WHERE total_views > 0
        """
        params = []
        if brand_id:
            sql += " AND brand_id = ?"
            params.append(brand_id)
        sql += " ORDER BY total_views DESC"
        cursor = conn.execute(sql, params)
        rows = _rows_to_dict(cursor)
        return render_template("video_analytics.html", videos=rows, brand_id=brand_id)
    finally:
        conn.close()
