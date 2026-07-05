from __future__ import annotations

import logging
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import DefaultDict, Dict, List, Mapping, Optional, Sequence, Tuple

from flask import current_app, has_app_context
from googleapiclient.errors import HttpError

from app.video_shorts.services.db import get_db, table_columns
from app.video_shorts.services.generated_video_lifecycle import ensure_generated_videos_schema
from app.video_shorts.services.youtube_oauth import (
    build_authenticated_youtube_analytics,
    resolve_token_lookup_user_id,
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
    generated_video_id,
    owner_user_id,
    brand_id,
    traffic_source_type,
    views,
    fetched_at
)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (snapshot_date, video_id, traffic_source_type)
DO UPDATE SET
    generated_video_id = COALESCE(EXCLUDED.generated_video_id, {RAW_TRAFFIC_TABLE}.generated_video_id),
    owner_user_id = COALESCE(EXCLUDED.owner_user_id, {RAW_TRAFFIC_TABLE}.owner_user_id),
    brand_id = COALESCE(EXCLUDED.brand_id, {RAW_TRAFFIC_TABLE}.brand_id),
    views = EXCLUDED.views,
    fetched_at = EXCLUDED.fetched_at
"""
RETENTION_UPSERT_SQL = f"""
INSERT INTO {RAW_RETENTION_TABLE} (
    snapshot_date,
    video_id,
    generated_video_id,
    owner_user_id,
    brand_id,
    views,
    average_view_duration_seconds,
    average_view_percentage,
    subscribers_gained,
    fetched_at
)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (snapshot_date, video_id)
DO UPDATE SET
    generated_video_id = COALESCE(EXCLUDED.generated_video_id, {RAW_RETENTION_TABLE}.generated_video_id),
    owner_user_id = COALESCE(EXCLUDED.owner_user_id, {RAW_RETENTION_TABLE}.owner_user_id),
    brand_id = COALESCE(EXCLUDED.brand_id, {RAW_RETENTION_TABLE}.brand_id),
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


def _ensure_raw_analytics_schema(conn) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {RAW_TRAFFIC_TABLE} (
            snapshot_date DATE NOT NULL,
            video_id VARCHAR NOT NULL,
            generated_video_id BIGINT,
            owner_user_id VARCHAR,
            brand_id VARCHAR,
            traffic_source_type VARCHAR NOT NULL,
            views BIGINT,
            fetched_at TIMESTAMP,
            PRIMARY KEY (snapshot_date, video_id, traffic_source_type)
        )
        """
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {RAW_RETENTION_TABLE} (
            snapshot_date DATE NOT NULL,
            video_id VARCHAR NOT NULL,
            generated_video_id BIGINT,
            owner_user_id VARCHAR,
            brand_id VARCHAR,
            views BIGINT,
            average_view_duration_seconds DOUBLE PRECISION,
            average_view_percentage DOUBLE PRECISION,
            subscribers_gained BIGINT,
            fetched_at TIMESTAMP,
            PRIMARY KEY (snapshot_date, video_id)
        )
        """
    )
    for table_name in (RAW_TRAFFIC_TABLE, RAW_RETENTION_TABLE):
        cols = table_columns(conn, table_name)
        for col_name, col_type in (
            ("generated_video_id", "BIGINT"),
            ("owner_user_id", "VARCHAR"),
            ("brand_id", "VARCHAR"),
        ):
            if col_name in cols:
                continue
            try:
                conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_type}")
            except Exception:
                pass
    try:
        conn.commit()
    except Exception:
        pass


def _date_chunks(start: date, end: date, chunk_days: int = 30):
    current = start
    while current <= end:
        chunk_end = min(current + timedelta(days=chunk_days - 1), end)
        yield current, chunk_end
        current = chunk_end + timedelta(days=1)


def _load_published_generated_videos() -> List[Dict[str, object]]:
    conn = get_db()
    try:
        ensure_generated_videos_schema(conn)
        rows = conn.execute(
            """
            SELECT
                gv.id,
                gv.youtube_video_id,
                COALESCE(NULLIF(TRIM(CAST(gv.user_id AS VARCHAR)), ''), b.owner_user_id) AS owner_user_id,
                gv.brand_id
            FROM shorts_generated_videos AS gv
            LEFT JOIN shorts_brands AS b
              ON b.id = gv.brand_id
            WHERE youtube_video_id IS NOT NULL
              AND TRIM(youtube_video_id) <> ''
              AND publish_status = 'published'
            ORDER BY gv.created_at DESC, gv.id DESC
            """
        ).fetchall()
    finally:
        conn.close()
    by_video_id: Dict[str, Dict[str, object]] = {}
    for row in rows:
        if not row:
            continue
        youtube_video_id = str(row[1] or "").strip()
        if not youtube_video_id or youtube_video_id in by_video_id:
            continue
        by_video_id[youtube_video_id] = {
            "generated_video_id": row[0],
            "youtube_video_id": youtube_video_id,
            "user_id": str(row[2] or "").strip() or None,
            "brand_id": str(row[3] or "").strip() or None,
        }
    return list(by_video_id.values())


def _build_analytics_client(scoped_user_id: str):
    try:
        return build_authenticated_youtube_analytics(user_id=scoped_user_id)
    except Exception as exc:
        _log().warning(
            "YouTube traffic sources analytics init failed user_id=%s: %s",
            scoped_user_id,
            exc,
        )
        return None


def _execute_query_with_retry(
    analytics,
    *,
    start_date: date,
    end_date: date,
    batch: Sequence[str],
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


def _group_videos_by_owner(
    generated_videos: Sequence[Dict[str, object]],
) -> Tuple[
    DefaultDict[Tuple[str, Optional[str], str], List[Dict[str, object]]],
    List[Dict[str, object]],
]:
    grouped: DefaultDict[Tuple[str, Optional[str], str], List[Dict[str, object]]] = defaultdict(list)
    skipped: List[Dict[str, object]] = []
    for item in generated_videos:
        user_id = str(item.get("user_id") or "").strip() or None
        brand_id = str(item.get("brand_id") or "").strip() or None
        youtube_video_id = str(item.get("youtube_video_id") or "").strip()
        generated_video_id = item.get("generated_video_id")
        if not user_id or not youtube_video_id:
            skipped.append(
                {
                    "generated_video_id": generated_video_id,
                    "youtube_video_id": youtube_video_id,
                    "reason": "missing_owner_or_video_id",
                }
            )
            continue
        resolved_lookup_user_id, lookup_mode = resolve_token_lookup_user_id(user_id, brand_id)
        if not resolved_lookup_user_id:
            skipped.append(
                {
                    "generated_video_id": generated_video_id,
                    "youtube_video_id": youtube_video_id,
                    "user_id": user_id,
                    "brand_id": brand_id,
                    "reason": lookup_mode,
                }
            )
            continue
        grouped[(resolved_lookup_user_id, brand_id, user_id)].append(
            dict(item)
        )
    return grouped, skipped


def _video_meta_map(group_items: Sequence[Dict[str, object]]) -> Dict[str, Dict[str, object]]:
    return {
        str(item.get("youtube_video_id") or "").strip(): item
        for item in group_items
        if str(item.get("youtube_video_id") or "").strip()
    }


def _with_video_metadata(
    rows: Sequence[Tuple[object, ...]],
    video_meta: Mapping[str, Dict[str, object]],
) -> List[Tuple[object, ...]]:
    enriched: List[Tuple[object, ...]] = []
    for row in rows:
        video_id = str(row[1] or "").strip() if len(row) > 1 else ""
        meta = video_meta.get(video_id)
        if not meta:
            continue
        enriched.append(
            (
                row[0],
                row[1],
                meta.get("generated_video_id"),
                meta.get("user_id"),
                meta.get("brand_id"),
                *row[2:],
            )
        )
    return enriched


def ingest_traffic_sources(start_date: Optional[date] = None) -> Dict[str, object]:
    log = _log()
    resolved_start_date, end_date = _resolve_window(start_date)
    generated_videos = _load_published_generated_videos()
    if not generated_videos:
        result = {
            "videos": 0,
            "groups": 0,
            "groups_skipped": 0,
            "rows_written": 0,
            "retention_rows_written": 0,
            "window": f"{resolved_start_date.isoformat()}..{end_date.isoformat()}",
        }
        log.info("YouTube traffic sources skipped result=%s", result)
        return result

    grouped_videos, skipped_videos = _group_videos_by_owner(generated_videos)
    fetched_at = datetime.utcnow()
    write_rows: List[Tuple[object, ...]] = []
    retention_write_rows: List[Tuple[object, ...]] = []
    groups_skipped = 0
    for skipped in skipped_videos:
        log.warning(
            "YouTube traffic sources skipped video generated_video_id=%s youtube_video_id=%s reason=%s",
            skipped.get("generated_video_id"),
            skipped.get("youtube_video_id"),
            skipped.get("reason"),
        )
    for (lookup_user_id, brand_id, owner_user_id), group_items in grouped_videos.items():
        analytics = _build_analytics_client(lookup_user_id)
        if not analytics:
            groups_skipped += 1
            log.warning(
                "YouTube traffic sources skipped owner group user_id=%s brand_id=%s lookup_user_id=%s reason=no_valid_oauth",
                owner_user_id,
                brand_id,
                lookup_user_id,
            )
            continue
        video_meta = _video_meta_map(group_items)
        video_ids = list(video_meta.keys())
        # Future per-owner quota budget hooks belong here, before the API loop runs.
        for chunk_start, chunk_end in _date_chunks(resolved_start_date, end_date):
            for batch in _chunked(video_ids, BATCH_SIZE):
                response = _execute_query_with_retry(
                    analytics,
                    start_date=chunk_start,
                    end_date=chunk_end,
                    batch=batch,
                    metrics="views",
                    dimensions="day,video,insightTrafficSourceType",
                )
                payload_rows = response.get("rows") or []
                write_rows.extend(
                    _with_video_metadata(_normalize_rows(payload_rows, fetched_at), video_meta)
                )
                retention_response = _execute_query_with_retry(
                    analytics,
                    start_date=chunk_start,
                    end_date=chunk_end,
                    batch=batch,
                    metrics="views,averageViewDuration,averageViewPercentage,subscribersGained",
                    dimensions="day,video",
                )
                retention_payload_rows = retention_response.get("rows") or []
                retention_write_rows.extend(
                    _with_video_metadata(
                        _normalize_retention_rows(retention_payload_rows, fetched_at),
                        video_meta,
                    )
                )
                time.sleep(INTER_BATCH_SLEEP_SECONDS)

    conn = get_db()
    try:
        _ensure_raw_analytics_schema(conn)
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
        "videos": len(generated_videos),
        "groups": len(grouped_videos),
        "groups_skipped": groups_skipped,
        "rows_written": len(write_rows),
        "retention_rows_written": len(retention_write_rows),
        "window": f"{resolved_start_date.isoformat()}..{end_date.isoformat()}",
    }
    log.info("YouTube traffic sources ingest result=%s", result)
    return result
