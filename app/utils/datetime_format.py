from __future__ import annotations

from datetime import date, datetime, time, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import g


DEFAULT_TIME_ZONE = "America/Los_Angeles"


def _resolve_timezone(tz_name: Optional[str]) -> ZoneInfo:
    name = tz_name
    if not name:
        user = getattr(g, "vs_current_user", None)
        name = (user or {}).get("time_zone") or DEFAULT_TIME_ZONE
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo(DEFAULT_TIME_ZONE)


def _normalize_datetime(value: object) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo=timezone.utc)
    if isinstance(value, str):
        normalized = value.replace("Z", "+00:00")
        if normalized[-5:] in {"+0000", "-0000"}:
            normalized = f"{normalized[:-5]}{normalized[-5:-2]}:{normalized[-2:]}"
        try:
            return datetime.fromisoformat(normalized)
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S%z"):
                try:
                    return datetime.strptime(normalized, fmt)
                except ValueError:
                    continue
    return None


def format_datetime(value: object, tz_name: Optional[str] = None) -> str:
    dt = _normalize_datetime(value)
    if not dt:
        return "-"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    zone = _resolve_timezone(tz_name)
    localized = dt.astimezone(zone)
    label = localized.strftime("%Y-%m-%d %I:%M:%S %p %Z")
    return label
