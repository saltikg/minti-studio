from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo


PACIFIC_TZ = ZoneInfo("America/Los_Angeles")
DISCOVERY_BACKFILL_QUOTA_FEATURE = "backfill"


def discovery_backfill_block_reason(now: datetime | None = None) -> str:
    current = now or datetime.now(PACIFIC_TZ)
    if current.tzinfo is None:
        current = current.replace(tzinfo=PACIFIC_TZ)
    current_pt = current.astimezone(PACIFIC_TZ)
    if 0 <= current_pt.hour < 4:
        return (
            "Discovery backfills are disabled from 12:00 AM through 3:59 AM PT "
            "so scheduled discovery cannot be starved."
        )
    return ""


def require_discovery_backfill_window(now: datetime | None = None) -> None:
    reason = discovery_backfill_block_reason(now)
    if reason:
        raise RuntimeError(reason)
