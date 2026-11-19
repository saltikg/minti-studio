from datetime import datetime, timezone

from scripts.run_at_pacific_time import should_run


def test_pacific_guard_matches_standard_time_utc_hour():
    matches, now_pt = should_run(
        datetime(2026, 1, 12, 12, 10, tzinfo=timezone.utc), hour=4, minute=10, weekdays={0}
    )
    assert matches
    assert now_pt.hour == 4


def test_pacific_guard_matches_daylight_time_utc_hour():
    matches, now_pt = should_run(
        datetime(2026, 7, 13, 11, 10, tzinfo=timezone.utc), hour=4, minute=10, weekdays={0}
    )
    assert matches
    assert now_pt.hour == 4


def test_pacific_guard_rejects_other_utc_slot_and_wrong_weekday():
    assert not should_run(
        datetime(2026, 1, 12, 11, 10, tzinfo=timezone.utc), hour=4, minute=10, weekdays={0}
    )[0]
    assert not should_run(
        datetime(2026, 7, 14, 11, 10, tzinfo=timezone.utc), hour=4, minute=10, weekdays={0}
    )[0]
