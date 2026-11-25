from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from flask import current_app, has_app_context

from app.video_shorts.services.db import get_db, table_columns


PACIFIC_TZ = ZoneInfo("America/Los_Angeles")
TABLE_NAME = "youtube_data_api_unit_usage"
DEFAULT_RESEARCH_DAILY_CAP = 2000
DISCOVERY_QUOTA_FEATURES = {"discovery_search", "discovery_enrichment"}
YOUTUBE_SEARCH_DAILY_LIMIT = 100
DISCOVERY_SEARCH_DAILY_CAP = 70
RESEARCH_SEARCH_DAILY_CAP = 20
OTHER_SEARCH_DAILY_CAP = 10

YOUTUBE_DATA_API_UNIT_COSTS = {
    "channels.list": 1,
    "commentThreads.list": 1,
    "comments.delete": 50,
    "comments.insert": 50,
    "comments.setModerationStatus": 50,
    "playlistItems.list": 1,
    # Google accounts search.list in its separate 100-search daily bucket.
    "search.list": 0,
    "videos.insert": 1600,
    "videos.list": 1,
    "videos.delete": 50,
    "videos.update": 50,
}


def quota_date_pt(now: Optional[datetime] = None) -> str:
    """Return the YouTube quota day, which resets at midnight Pacific time."""
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    return instant.astimezone(PACIFIC_TZ).date().isoformat()


def today_pt() -> str:
    return quota_date_pt()


def unit_cost(method: str) -> int:
    return int(YOUTUBE_DATA_API_UNIT_COSTS.get(str(method or "").strip(), 1))


class YoutubeQuotaCapExceeded(RuntimeError):
    pass


def research_daily_cap() -> int:
    try:
        return max(0, int(os.getenv("YT_QUOTA_RESEARCH_CAP", str(DEFAULT_RESEARCH_DAILY_CAP)) or 0))
    except (TypeError, ValueError):
        return DEFAULT_RESEARCH_DAILY_CAP


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


def total_units_for_date(
    conn,
    date_pt: Optional[str] = None,
    *,
    exclude_features: Optional[set[str]] = None,
    include_features: Optional[set[str]] = None,
) -> int:
    if not table_columns(conn, TABLE_NAME):
        return 0
    excluded = sorted(str(item).strip() for item in (exclude_features or set()) if str(item).strip())
    exclusion_sql = ""
    params = [date_pt or today_pt()]
    if excluded:
        exclusion_sql = f" AND feature NOT IN ({', '.join(['?'] * len(excluded))})"
        params.extend(excluded)
    included = sorted(str(item).strip() for item in (include_features or set()) if str(item).strip())
    inclusion_sql = ""
    if included:
        inclusion_sql = f" AND feature IN ({', '.join(['?'] * len(included))})"
        params.extend(included)
    row = conn.execute(
        f"SELECT COALESCE(SUM(units), 0) FROM {TABLE_NAME} WHERE date_pt = ?{exclusion_sql}{inclusion_sql}",
        params,
    ).fetchone()
    try:
        return int((row or [0])[0] or 0)
    except Exception:
        return 0


def research_units_for_date(conn, date_pt: Optional[str] = None) -> int:
    if not table_columns(conn, TABLE_NAME):
        return 0
    row = conn.execute(
        f"""
        SELECT COALESCE(SUM(units), 0)
        FROM {TABLE_NAME}
        WHERE date_pt = ?
          AND (feature = 'research' OR feature LIKE '%%_research')
        """,
        [date_pt or today_pt()],
    ).fetchone()
    return int((row or [0])[0] or 0)


def search_calls_for_date(
    conn,
    date_pt: Optional[str] = None,
    *,
    include_features: Optional[set[str]] = None,
) -> int:
    if not table_columns(conn, TABLE_NAME):
        return 0
    included = sorted(str(item).strip() for item in (include_features or set()) if str(item).strip())
    inclusion_sql = ""
    params: list[Any] = [date_pt or quota_date_pt()]
    if included:
        inclusion_sql = f" AND feature IN ({', '.join(['?'] * len(included))})"
        params.extend(included)
    row = conn.execute(
        f"""
        SELECT COALESCE(SUM(calls), 0)
        FROM {TABLE_NAME}
        WHERE date_pt = ?
          AND method = 'search.list'
          {inclusion_sql}
        """,
        params,
    ).fetchone()
    return int((row or [0])[0] or 0)


def _is_research_feature(feature: str) -> bool:
    clean_feature = str(feature or "other").strip().lower()
    return clean_feature == "research" or clean_feature.endswith("_research")


def _search_share(feature: str) -> tuple[str, int]:
    clean_feature = str(feature or "other").strip().lower()
    if clean_feature in DISCOVERY_QUOTA_FEATURES:
        return "discovery", DISCOVERY_SEARCH_DAILY_CAP
    if _is_research_feature(clean_feature):
        return "research", RESEARCH_SEARCH_DAILY_CAP
    return "other", OTHER_SEARCH_DAILY_CAP


def search_calls_for_share(conn, share: str, date_pt: Optional[str] = None) -> int:
    if not table_columns(conn, TABLE_NAME):
        return 0
    clean_share = str(share or "other").strip().lower()
    params: list[Any] = [date_pt or quota_date_pt()]
    if clean_share == "discovery":
        features = sorted(DISCOVERY_QUOTA_FEATURES)
        predicate = f"feature IN ({', '.join(['?'] * len(features))})"
        params.extend(features)
    elif clean_share == "research":
        predicate = "(feature = 'research' OR feature LIKE '%%_research')"
    else:
        features = sorted(DISCOVERY_QUOTA_FEATURES)
        predicate = (
            f"feature NOT IN ({', '.join(['?'] * len(features))}) "
            "AND feature <> 'research' AND feature NOT LIKE '%%_research'"
        )
        params.extend(features)
    row = conn.execute(
        f"""
        SELECT COALESCE(SUM(calls), 0)
        FROM {TABLE_NAME}
        WHERE date_pt = ?
          AND method = 'search.list'
          AND {predicate}
        """,
        params,
    ).fetchone()
    return int((row or [0])[0] or 0)


def require_youtube_quota_available(*, feature: str, method: str, calls: int = 1) -> None:
    clean_feature = str(feature or "other").strip().lower()
    clean_method = str(method or "unknown").strip()
    requested_calls = max(0, int(calls or 0))
    if requested_calls <= 0:
        return
    is_research = _is_research_feature(clean_feature)
    if clean_method != "search.list" and not is_research:
        return
    conn = get_db()
    try:
        ensure_youtube_quota_table(conn)
        if clean_method == "search.list":
            share_name, search_cap = _search_share(clean_feature)
            search_used = search_calls_for_share(conn, share_name)
            total_search_used = search_calls_for_date(conn)
            if search_used + requested_calls > search_cap:
                raise YoutubeQuotaCapExceeded(
                    f"YouTube {share_name} search quota reached: used={search_used} "
                    f"requested={requested_calls} cap={search_cap} date_pt={quota_date_pt()}"
                )
            if total_search_used + requested_calls > YOUTUBE_SEARCH_DAILY_LIMIT:
                raise YoutubeQuotaCapExceeded(
                    f"YouTube total search quota reached: used={total_search_used} "
                    f"requested={requested_calls} cap={YOUTUBE_SEARCH_DAILY_LIMIT} date_pt={quota_date_pt()}"
                )
        if is_research:
            cap = research_daily_cap()
            used = research_units_for_date(conn)
            requested_units = requested_calls * unit_cost(clean_method)
            if cap and used + requested_units > cap:
                raise YoutubeQuotaCapExceeded(
                    f"YouTube research quota cap reached: used={used} requested={requested_units} "
                    f"cap={cap} date_pt={quota_date_pt()}"
                )
        conn.commit()
    finally:
        conn.close()


def youtube_quota_runtime_budgets(conn=None, *, discovery_budget: int = 4000) -> dict[str, Any]:
    owns_conn = conn is None
    if owns_conn:
        conn = get_db()
    try:
        ensure_youtube_quota_table(conn)
        payload = {
            "date_pt": quota_date_pt(),
            "total_units_used": total_units_for_date(conn),
            "discovery_budget": max(0, int(discovery_budget)),
            "discovery_used": total_units_for_date(conn, include_features=DISCOVERY_QUOTA_FEATURES),
            "discovery_search_cap": DISCOVERY_SEARCH_DAILY_CAP,
            "discovery_search_used": search_calls_for_date(conn, include_features=DISCOVERY_QUOTA_FEATURES),
            "research_cap": research_daily_cap(),
            "research_used": research_units_for_date(conn),
            "research_search_cap": RESEARCH_SEARCH_DAILY_CAP,
            "research_search_used": search_calls_for_share(conn, "research"),
            "other_search_cap": OTHER_SEARCH_DAILY_CAP,
            "other_search_used": search_calls_for_share(conn, "other"),
            "total_search_cap": YOUTUBE_SEARCH_DAILY_LIMIT,
            "total_search_used": search_calls_for_date(conn),
        }
        if owns_conn:
            conn.commit()
        return payload
    finally:
        if owns_conn:
            conn.close()
