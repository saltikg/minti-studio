from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, Dict, Any
from zoneinfo import ZoneInfo

from app.video_shorts.services.db import table_columns


def get_shorts_overview_quota_state(conn) -> Dict[str, Any]:
    state = {
        "active": False,
        "until": None,
        "until_utc": None,
        "until_pst": None,
        "last_error_code": None,
        "last_error_reason": None,
        "last_error_message": None,
        "last_error_domain": None,
        "last_error_at": None,
        "last_error_at_utc": None,
        "last_error_at_pst": None,
        "cache_last_fetched_at": None,
        "cache_last_fetched_utc": None,
        "cache_last_fetched_pst": None,
        "legacy_record": False,
    }
    cols = _table_columns(conn, "shorts_overview_quota_state")
    if not cols:
        return state
    try:
        select_cols = ["exhausted_until"]
        if "last_error_code" in cols:
            select_cols.append("last_error_code")
        if "last_error_reason" in cols:
            select_cols.append("last_error_reason")
        if "last_error_message" in cols:
            select_cols.append("last_error_message")
        if "last_error_domain" in cols:
            select_cols.append("last_error_domain")
        if "last_error_at" in cols:
            select_cols.append("last_error_at")
        row = conn.execute(
            f"""
            SELECT {', '.join(select_cols)}
            FROM shorts_overview_quota_state
            WHERE id = 1
            """
        ).fetchone()
        if row and row[0]:
            exhausted_until = row[0]
            state["until"] = exhausted_until
            state["until_utc"] = _format_timestamp(exhausted_until, "UTC")
            state["until_pst"] = _format_timestamp(exhausted_until, "America/Los_Angeles")
            state["active"] = _is_future(exhausted_until)
            idx = 1
            if "last_error_code" in cols:
                state["last_error_code"] = row[idx]
                idx += 1
            if "last_error_reason" in cols:
                state["last_error_reason"] = row[idx]
                idx += 1
            if "last_error_message" in cols:
                state["last_error_message"] = row[idx]
                idx += 1
            if "last_error_domain" in cols:
                state["last_error_domain"] = row[idx]
                idx += 1
            if "last_error_at" in cols:
                state["last_error_at"] = row[idx]
                if row[idx]:
                    state["last_error_at_utc"] = _format_timestamp(row[idx], "UTC")
                    state["last_error_at_pst"] = _format_timestamp(
                        row[idx], "America/Los_Angeles"
                    )
            if state["until"] and not any(
                [
                    state["last_error_code"],
                    state["last_error_reason"],
                    state["last_error_message"],
                ]
            ):
                state["legacy_record"] = True
    except Exception:
        return state

    try:
        fetched_row = conn.execute(
            "SELECT MAX(fetched_at) FROM shorts_overview_stats_cache"
        ).fetchone()
        if fetched_row and fetched_row[0]:
            state["cache_last_fetched_at"] = fetched_row[0]
            state["cache_last_fetched_utc"] = _format_timestamp(fetched_row[0], "UTC")
            state["cache_last_fetched_pst"] = _format_timestamp(
                fetched_row[0], "America/Los_Angeles"
            )
    except Exception:
        return state
    return state


def _format_timestamp(value, tz_name: str) -> Optional[str]:
    if not value:
        return None
    dt = _coerce_utc(value)
    if not dt:
        return None
    tz = ZoneInfo(tz_name)
    return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M %Z")


def _coerce_utc(value) -> Optional[datetime]:
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value))
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt


def _is_future(value) -> bool:
    dt = _coerce_utc(value)
    if not dt:
        return False
    return dt > datetime.now(timezone.utc)


def _table_columns(conn, table_name: str) -> set:
    return table_columns(conn, table_name)
