from __future__ import annotations

from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from flask import current_app, has_app_context

from app.video_shorts.services.db import get_db, table_columns


PACIFIC_TZ = ZoneInfo("America/Los_Angeles")
TABLE_NAME = "youtube_data_api_unit_usage"

YOUTUBE_DATA_API_UNIT_COSTS = {
    "channels.list": 1,
    "commentThreads.list": 1,
    "comments.delete": 50,
    "comments.insert": 50,
    "comments.setModerationStatus": 50,
    "playlistItems.list": 1,
    "search.list": 100,
    "videos.insert": 1600,
    "videos.list": 1,
    "videos.update": 50,
}


def today_pt() -> str:
    return datetime.now(PACIFIC_TZ).date().isoformat()


def unit_cost(method: str) -> int:
    return int(YOUTUBE_DATA_API_UNIT_COSTS.get(str(method or "").strip(), 1))


def ensure_youtube_quota_table(conn) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
            date_pt DATE NOT NULL,
            feature TEXT NOT NULL,
            method TEXT NOT NULL,
            calls INTEGER NOT NULL DEFAULT 0,
            units INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (date_pt, feature, method)
        )
        """
    )


def record_youtube_data_api_call(
    *,
    feature: str,
    method: str,
    calls: int = 1,
    units_per_call: Optional[int] = None,
) -> None:
    clean_feature = str(feature or "other").strip() or "other"
    clean_method = str(method or "unknown").strip() or "unknown"
    call_count = max(0, int(calls or 0))
    if call_count <= 0:
        return
    units = call_count * int(units_per_call if units_per_call is not None else unit_cost(clean_method))
    conn = None
    try:
        conn = get_db()
        ensure_youtube_quota_table(conn)
        date_pt = today_pt()
        conn.execute(
            f"""
            INSERT INTO {TABLE_NAME} (date_pt, feature, method, calls, units, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT (date_pt, feature, method)
            DO UPDATE SET
                calls = {TABLE_NAME}.calls + EXCLUDED.calls,
                units = {TABLE_NAME}.units + EXCLUDED.units,
                updated_at = CURRENT_TIMESTAMP
            """,
            [date_pt, clean_feature, clean_method, call_count, units],
        )
        conn.commit()
    except Exception:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        if has_app_context():
            current_app.logger.exception("Could not record YouTube Data API usage")
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def total_units_for_date(conn, date_pt: Optional[str] = None) -> int:
    if not table_columns(conn, TABLE_NAME):
        return 0
    row = conn.execute(
        f"SELECT COALESCE(SUM(units), 0) FROM {TABLE_NAME} WHERE date_pt = ?",
        [date_pt or today_pt()],
    ).fetchone()
    try:
        return int((row or [0])[0] or 0)
    except Exception:
        return 0
