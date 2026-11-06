"""Collect channel-wide YouTube views split by creator content type."""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Mapping, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from app.video_shorts.services.db import get_db, get_db_readonly
from app.video_shorts.services.publish_target_guard import (
    PublishTargetMismatch,
    require_target_match,
    require_token_owner,
)
from app.video_shorts.services.youtube_oauth import (
    build_authenticated_youtube_analytics,
    get_connected_channel_info,
    resolve_token_lookup_user_id,
)


logger = logging.getLogger(__name__)
RAW_TABLE = "raw_yt_channel_content_type_daily"
PT = ZoneInfo("America/Los_Angeles")
ROLLING_DAYS = 4
CHUNK_DAYS = 30

CREATE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {RAW_TABLE} (
    brand_id VARCHAR NOT NULL,
    owner_user_id VARCHAR NOT NULL,
    channel_id VARCHAR NOT NULL,
    metric_date DATE NOT NULL,
    creator_content_type VARCHAR NOT NULL,
    views BIGINT NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (brand_id, channel_id, metric_date, creator_content_type)
)
"""

UPSERT_SQL = f"""
INSERT INTO {RAW_TABLE} (
    brand_id,
    owner_user_id,
    channel_id,
    metric_date,
    creator_content_type,
    views,
    fetched_at
)
VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (brand_id, channel_id, metric_date, creator_content_type)
DO UPDATE SET
    owner_user_id = EXCLUDED.owner_user_id,
    views = EXCLUDED.views,
    fetched_at = EXCLUDED.fetched_at
"""


def _pt_today() -> date:
    return datetime.now(PT).date()


def _normalize_content_type(value: object) -> str:
    text = str(value or "").strip()
    aliases = {
        "shorts": "SHORTS",
        "videoondemand": "VIDEO_ON_DEMAND",
        "livestream": "LIVE_STREAM",
        "story": "STORY",
        "unspecified": "UNSPECIFIED",
    }
    compact = text.replace("_", "").replace("-", "").lower()
    return aliases.get(compact, text.replace("-", "_").upper())


def _previous_calendar_month(first_publish_date: date) -> Tuple[date, date]:
    current_month_start = first_publish_date.replace(day=1)
    previous_end = current_month_start - timedelta(days=1)
    return previous_end.replace(day=1), previous_end


def _date_chunks(start_date: date, end_date: date):
    current = start_date
    while current <= end_date:
        chunk_end = min(current + timedelta(days=CHUNK_DAYS - 1), end_date)
        yield current, chunk_end
        current = chunk_end + timedelta(days=1)


def _load_connected_brands() -> List[Dict[str, str]]:
    conn = get_db_readonly()
    try:
        rows = conn.execute(
            """
            SELECT
                CAST(b.id AS VARCHAR),
                CAST(b.owner_user_id AS VARCHAR),
                CAST(t.user_id AS VARCHAR),
                CAST(t.youtube_channel_id AS VARCHAR)
            FROM shorts_brands b
            JOIN youtube_oauth_tokens_v2 t
              ON CAST(t.user_id AS VARCHAR) =
                 CAST(b.owner_user_id AS VARCHAR) || '::' || CAST(b.id AS VARCHAR)
            WHERE NULLIF(BTRIM(t.refresh_token), '') IS NOT NULL
              AND NULLIF(BTRIM(t.youtube_channel_id), '') IS NOT NULL
              AND COALESCE(t.reauth_required, 0) = 0
            ORDER BY b.id
            """
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "brand_id": str(row[0]).strip(),
            "owner_user_id": str(row[1]).strip(),
            "token_owner": str(row[2]).strip(),
            "channel_id": str(row[3]).strip(),
        }
        for row in rows
        if row and all(str(value or "").strip() for value in row[:4])
    ]


def _first_minti_publish_dates(brand_ids: Sequence[str]) -> Dict[str, date]:
    if not brand_ids:
        return {}
    placeholders = ", ".join("?" for _ in brand_ids)
    conn = get_db_readonly()
    try:
        rows = conn.execute(
            f"""
            SELECT CAST(brand_id AS VARCHAR),
                   MIN(LEAST(
                       youtube_published_at,
                       instagram_published_at,
                       facebook_published_at,
                       published_at
                   )) AS first_published_at
            FROM shorts_generated_videos
            WHERE CAST(brand_id AS VARCHAR) IN ({placeholders})
              AND COALESCE(
                  youtube_published_at,
                  instagram_published_at,
                  facebook_published_at,
                  published_at
              ) IS NOT NULL
              AND lower(COALESCE(publish_status, '')) = 'published'
            GROUP BY brand_id
            """,
            list(brand_ids),
        ).fetchall()
    finally:
        conn.close()
    result: Dict[str, date] = {}
    for brand_id, value in rows:
        if isinstance(value, datetime):
            published_at = value
            if published_at.tzinfo is None:
                published_at = published_at.replace(tzinfo=timezone.utc)
            result[str(brand_id)] = published_at.astimezone(PT).date()
        elif isinstance(value, date):
            result[str(brand_id)] = value
    return result


def _parse_rows(
    payload_rows: Sequence[Sequence[object]],
    *,
    connection: Mapping[str, str],
    fetched_at: datetime,
) -> List[Tuple[object, ...]]:
    parsed: List[Tuple[object, ...]] = []
    for row in payload_rows:
        if len(row) < 3:
            continue
        try:
            metric_date = date.fromisoformat(str(row[0]))
            views = int(row[2])
        except (TypeError, ValueError):
            continue
        content_type = _normalize_content_type(row[1])
        if not content_type or views < 0:
            continue
        parsed.append(
            (
                connection["brand_id"],
                connection["owner_user_id"],
                connection["channel_id"],
                metric_date,
                content_type,
                views,
                fetched_at,
            )
        )
    return parsed


def _collect_brand_rows(
    connection: Mapping[str, str],
    *,
    start_date: date,
    end_date: date,
    fetched_at: datetime,
) -> Dict[str, object]:
    brand_id = connection["brand_id"]
    owner_user_id = connection["owner_user_id"]
    token_owner = connection["token_owner"]
    expected_channel_id = connection["channel_id"]
    try:
        require_token_owner(
            token_owner=token_owner,
            owner_user_id=owner_user_id,
            brand_id=brand_id,
        )
        lookup_user_id, lookup_mode = resolve_token_lookup_user_id(
            owner_user_id,
            brand_id=brand_id,
        )
        if lookup_user_id != token_owner:
            raise PublishTargetMismatch(
                platform="youtube_analytics_token",
                schedule_id="channel-content-type",
                brand_id=brand_id,
                expected=token_owner,
                actual=lookup_user_id or lookup_mode,
            )
        channel_info, channel_error = get_connected_channel_info(user_id=lookup_user_id)
        if channel_error:
            logger.warning(
                "YouTube content-type identity check failed brand_id=%s reason=%s",
                brand_id,
                channel_error,
            )
            return {
                "status": "failed",
                "reason": channel_error,
                "rows": [],
                "requests": 0,
            }
        actual_channel_id = str((channel_info or {}).get("id") or "").strip()
        require_target_match(
            platform="youtube_analytics_channel",
            schedule_id="channel-content-type",
            brand_id=brand_id,
            expected=expected_channel_id,
            actual=actual_channel_id,
        )
    except PublishTargetMismatch as exc:
        logger.warning(
            "YouTube content-type collection skipped brand_id=%s reason=credential_channel_mismatch expected=%s actual=%s",
            brand_id,
            exc.expected,
            exc.actual,
        )
        return {"status": "skipped", "reason": "credential_channel_mismatch", "rows": [], "requests": 0}
    except Exception:
        logger.exception(
            "YouTube content-type identity check failed brand_id=%s",
            brand_id,
        )
        return {"status": "failed", "reason": "identity_check_failed", "rows": [], "requests": 0}

    try:
        analytics = build_authenticated_youtube_analytics(user_id=token_owner)
        if not analytics:
            return {"status": "failed", "reason": "analytics_credentials_unavailable", "rows": [], "requests": 0}
        rows: List[Tuple[object, ...]] = []
        requests = 0
        for chunk_start, chunk_end in _date_chunks(start_date, end_date):
            response = (
                analytics.reports()
                .query(
                    ids="channel==MINE",
                    startDate=chunk_start.isoformat(),
                    endDate=chunk_end.isoformat(),
                    metrics="views",
                    dimensions="day,creatorContentType",
                    sort="day,creatorContentType",
                    maxResults=200,
                )
                .execute()
            )
            requests += 1
            rows.extend(
                _parse_rows(
                    response.get("rows") or [],
                    connection=connection,
                    fetched_at=fetched_at,
                )
            )
        return {"status": "ok", "reason": None, "rows": rows, "requests": requests}
    except Exception:
        logger.exception(
            "YouTube content-type Analytics query failed brand_id=%s channel_id=%s",
            brand_id,
            expected_channel_id,
        )
        return {"status": "failed", "reason": "analytics_query_failed", "rows": [], "requests": 0}


def ingest_channel_content_types(*, backfill: bool = False) -> Dict[str, object]:
    connections = _load_connected_brands()
    today = _pt_today()
    first_publish_dates = (
        _first_minti_publish_dates([item["brand_id"] for item in connections])
        if backfill
        else {}
    )
    fetched_at = datetime.now(timezone.utc)
    write_rows: List[Tuple[object, ...]] = []
    brand_results: List[Dict[str, object]] = []
    request_count = 0
    for connection in connections:
        if backfill:
            first_publish_date = first_publish_dates.get(connection["brand_id"])
            if first_publish_date is None:
                result = {
                    "brand_id": connection["brand_id"],
                    "channel_id": connection["channel_id"],
                    "status": "skipped",
                    "reason": "no_minti_publication",
                    "rows": 0,
                    "requests": 0,
                }
                brand_results.append(result)
                logger.info("YouTube content-type backfill skipped result=%s", result)
                continue
            start_date, _ = _previous_calendar_month(first_publish_date)
        else:
            start_date = today - timedelta(days=ROLLING_DAYS - 1)
        collected = _collect_brand_rows(
            connection,
            start_date=start_date,
            end_date=today,
            fetched_at=fetched_at,
        )
        rows = list(collected.pop("rows", []))
        write_rows.extend(rows)
        request_count += int(collected.get("requests") or 0)
        result = {
            "brand_id": connection["brand_id"],
            "channel_id": connection["channel_id"],
            "start_date": start_date.isoformat(),
            "end_date": today.isoformat(),
            "rows": len(rows),
            **collected,
        }
        brand_results.append(result)
        logger.info("YouTube content-type brand result=%s", result)

    conn = get_db()
    try:
        conn.execute(CREATE_TABLE_SQL)
        if write_rows:
            conn.executemany(UPSERT_SQL, write_rows)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    summary = {
        "mode": "backfill" if backfill else "rolling",
        "brands_connected": len(connections),
        "brands_succeeded": sum(item.get("status") == "ok" for item in brand_results),
        "brands_skipped": sum(item.get("status") == "skipped" for item in brand_results),
        "brands_failed": sum(item.get("status") == "failed" for item in brand_results),
        "rows_written": len(write_rows),
        "analytics_requests": request_count,
        "brands": brand_results,
    }
    logger.info("YouTube content-type ingest result=%s", summary)
    return summary
