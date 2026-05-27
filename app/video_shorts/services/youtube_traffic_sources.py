from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from flask import current_app, has_app_context
from googleapiclient.errors import HttpError

from app.video_shorts.services.db import get_db, get_db_readonly
from app.video_shorts.services.youtube_oauth import (
    build_authenticated_youtube_analytics,
    list_stored_refresh_tokens,
)

logger = logging.getLogger(__name__)

RAW_TRAFFIC_TABLE = "raw_yt_traffic_sources"
RAW_RETENTION_TABLE = "raw_yt_video_retention"
BATCH_SIZE = 20
MAX_RESULTS = 200
INTER_BATCH_SLEEP_SECONDS = 0.8
RETRYABLE_STATUS_CODES = {403, 429}
UPSERT_SQL = f"""
INSERT INTO {RAW_TRAFFIC_TABLE} (
    snapshot_date,
    video_id,
    traffic_source_type,
    views,
    fetched_at
)
VALUES (?, ?, ?, ?, ?)
ON CONFLICT (snapshot_date, video_id, traffic_source_type)
DO UPDATE SET
    views = EXCLUDED.views,
    fetched_at = EXCLUDED.fetched_at
"""
RETENTION_UPSERT_SQL = f"""
INSERT INTO {RAW_RETENTION_TABLE} (
    snapshot_date,
    video_id,
    views,
    average_view_duration_seconds,
    average_view_percentage,
    subscribers_gained,
    fetched_at
)
VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (snapshot_date, video_id)
DO UPDATE SET
    views = EXCLUDED.views,
    average_view_duration_seconds = EXCLUDED.average_view_duration_seconds,
    average_view_percentage = EXCLUDED.average_view_percentage,
    subscribers_gained = EXCLUDED.subscribers_gained,
    fetched_at = EXCLUDED.fetched_at
"""


def _log() -> logging.Logger:
    if has_app_context():
        return current_app.logger
    return logger


def _chunked(values: Sequence[str], size: int) -> List[List[str]]:
    return [list(values[index : index + size]) for index in range(0, len(values), size)]


def _resolve_window(start_date: Optional[date] = None) -> Tuple[date, date]:
    end_date = date.today() - timedelta(days=1)
    if start_date is None:
        return end_date - timedelta(days=3), end_date
    if start_date > end_date:
        raise ValueError("start_date must be on or before yesterday")
    return start_date, end_date


def _load_video_ids() -> List[str]:
    conn = get_db_readonly()
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT youtube_video_id
            FROM shorts_generated_videos
            WHERE youtube_video_id IS NOT NULL
              AND TRIM(youtube_video_id) <> ''
              AND publish_status = 'published'
            """
        ).fetchall()
    finally:
        conn.close()
    return [str(row[0]).strip() for row in rows if row and str(row[0] or "").strip()]


def _build_analytics_client():
    token_infos = list_stored_refresh_tokens()
    for token_info in token_infos:
        if token_info.get("reauth_required"):
            continue
        scoped_user_id = token_info.get("user_id")
        refresh_token = token_info.get("refresh_token")
        try:
            analytics = build_authenticated_youtube_analytics(
                refresh_token=refresh_token,
                user_id=scoped_user_id,
            )
        except Exception as exc:
            _log().warning(
                "YouTube traffic sources analytics init failed user_id=%s: %s",
                scoped_user_id,
                exc,
            )
            continue
        if analytics:
            return analytics
    return None


def _execute_query_with_retry(
    analytics,
    *,
    start_date: date,
    end_date: date,
    batch: Sequence[str],
    start_index: int,
    metrics: str,
    dimensions: str,
):
    attempt = 0
    while True:
        try:
            return (
                analytics.reports()
                .query(
                    ids="channel==MINE",
                    startDate=start_date.isoformat(),
                    endDate=end_date.isoformat(),
                    metrics=metrics,
                    dimensions=dimensions,
                    filters="video==" + ",".join(batch),
                    maxResults=MAX_RESULTS,
                    startIndex=start_index,
                )
                .execute()
            )
        except HttpError as exc:
            status_code = int(getattr(getattr(exc, "resp", None), "status", 0) or 0)
            if status_code not in RETRYABLE_STATUS_CODES or attempt >= 4:
                raise
            sleep_seconds = 2 ** attempt
            _log().warning(
                "YouTube traffic sources retry status=%s attempt=%s sleep=%ss",
                status_code,
                attempt + 1,
                sleep_seconds,
            )
            time.sleep(sleep_seconds)
            attempt += 1


def _parse_int(value: object) -> Optional[int]:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_float(value: object) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_rows(payload_rows: Sequence[Sequence[object]], fetched_at: datetime) -> List[Tuple[object, ...]]:
    rows: List[Tuple[object, ...]] = []
    for raw_row in payload_rows:
        if len(raw_row) < 4:
            continue
        snapshot_date, video_id, traffic_source_type, views = raw_row[:4]
        if not snapshot_date or not video_id or not traffic_source_type:
            continue
        rows.append(
            (
                date.fromisoformat(str(snapshot_date)),
                str(video_id).strip(),
                str(traffic_source_type).strip(),
                _parse_int(views),
                fetched_at,
            )
        )
    return rows


def _normalize_retention_rows(
    payload_rows: Sequence[Sequence[object]],
    fetched_at: datetime,
) -> List[Tuple[object, ...]]:
    rows: List[Tuple[object, ...]] = []
    for raw_row in payload_rows:
        if len(raw_row) < 6:
            continue
        snapshot_date, video_id, views, avg_duration, avg_percentage, subscribers_gained = raw_row[:6]
        if not snapshot_date or not video_id:
            continue
        rows.append(
            (
                date.fromisoformat(str(snapshot_date)),
                str(video_id).strip(),
                _parse_int(views),
                _parse_float(avg_duration),
                _parse_float(avg_percentage),
                _parse_int(subscribers_gained),
                fetched_at,
            )
        )
    return rows


def ingest_traffic_sources(start_date: Optional[date] = None) -> Dict[str, object]:
    log = _log()
    resolved_start_date, end_date = _resolve_window(start_date)
    video_ids = _load_video_ids()
    if not video_ids:
        result = {
            "videos": 0,
            "rows_written": 0,
            "retention_rows_written": 0,
            "window": f"{resolved_start_date.isoformat()}..{end_date.isoformat()}",
        }
        log.info("YouTube traffic sources skipped result=%s", result)
        return result

    analytics = _build_analytics_client()
    if not analytics:
        result = {
            "videos": len(video_ids),
            "rows_written": 0,
            "retention_rows_written": 0,
            "window": f"{resolved_start_date.isoformat()}..{end_date.isoformat()}",
        }
        log.warning("YouTube traffic sources skipped: no valid OAuth token result=%s", result)
        return result

    fetched_at = datetime.utcnow()
    write_rows: List[Tuple[object, ...]] = []
    retention_write_rows: List[Tuple[object, ...]] = []
    for batch in _chunked(video_ids, BATCH_SIZE):
        start_index = 1
        while True:
            response = _execute_query_with_retry(
                analytics,
                start_date=resolved_start_date,
                end_date=end_date,
                batch=batch,
                start_index=start_index,
                metrics="views",
                dimensions="day,video,insightTrafficSourceType",
            )
            payload_rows = response.get("rows") or []
            write_rows.extend(_normalize_rows(payload_rows, fetched_at))
            if len(payload_rows) < MAX_RESULTS:
                break
            start_index += len(payload_rows)
        start_index = 1
        while True:
            retention_response = _execute_query_with_retry(
                analytics,
                start_date=resolved_start_date,
                end_date=end_date,
                batch=batch,
                start_index=start_index,
                metrics="views,averageViewDuration,averageViewPercentage,subscribersGained",
                dimensions="day,video",
            )
            retention_payload_rows = retention_response.get("rows") or []
            retention_write_rows.extend(_normalize_retention_rows(retention_payload_rows, fetched_at))
            if len(retention_payload_rows) < MAX_RESULTS:
                break
            start_index += len(retention_payload_rows)
        time.sleep(INTER_BATCH_SLEEP_SECONDS)

    conn = get_db()
    try:
        if write_rows:
            conn.executemany(UPSERT_SQL, write_rows)
        if retention_write_rows:
            conn.executemany(RETENTION_UPSERT_SQL, retention_write_rows)
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()

    result = {
        "videos": len(video_ids),
        "rows_written": len(write_rows),
        "retention_rows_written": len(retention_write_rows),
        "window": f"{resolved_start_date.isoformat()}..{end_date.isoformat()}",
    }
    log.info("YouTube traffic sources ingest result=%s", result)
    return result
