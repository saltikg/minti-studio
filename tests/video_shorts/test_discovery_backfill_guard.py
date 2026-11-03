from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.video_shorts.services.discovery_backfill_guard import (
    discovery_backfill_block_reason,
    require_discovery_backfill_window,
)


PT = ZoneInfo("America/Los_Angeles")


def test_backfill_is_blocked_during_discovery_window():
    now = datetime(2026, 11, 3, 2, 30, tzinfo=PT)
    assert "12:00 AM" in discovery_backfill_block_reason(now)
    with pytest.raises(RuntimeError):
        require_discovery_backfill_window(now)


def test_backfill_is_allowed_at_four_am_pt():
    now = datetime(2026, 11, 3, 4, 0, tzinfo=PT)
    assert discovery_backfill_block_reason(now) == ""
    require_discovery_backfill_window(now)
