from datetime import datetime, timezone

from app.video_shorts.routes import generation
from app.video_shorts.services import outreach_email_send


def _utc_from_pt(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=generation.PST_ZONE).astimezone(timezone.utc)


def test_outreach_caps_use_safe_defaults_and_env_overrides(monkeypatch):
    monkeypatch.delenv("OUTREACH_DAILY_NEW_CAP", raising=False)
    monkeypatch.delenv("OUTREACH_DAILY_TOTAL_CAP", raising=False)
    assert outreach_email_send.outreach_daily_new_cap() == 8
    assert outreach_email_send.outreach_daily_total_cap() == 40

    monkeypatch.setenv("OUTREACH_DAILY_NEW_CAP", "20")
    monkeypatch.setenv("OUTREACH_DAILY_TOTAL_CAP", "35")
    assert outreach_email_send.outreach_daily_new_cap() == 20
    assert outreach_email_send.outreach_daily_total_cap() == 35


def test_total_cap_reserves_first_touch_capacity_before_followups():
    assert outreach_email_send.outreach_send_capacity(
        first_sent=0,
        total_sent=20,
        reserved_first=20,
        new_cap=20,
        total_cap=40,
    ) == (True, False)
    assert outreach_email_send.outreach_send_capacity(
        first_sent=20,
        total_sent=20,
        reserved_first=0,
        new_cap=20,
        total_cap=40,
    ) == (False, True)
    assert outreach_email_send.outreach_send_capacity(
        first_sent=20,
        total_sent=40,
        reserved_first=0,
        new_cap=20,
        total_cap=40,
    ) == (False, False)


def test_first_touch_slots_are_spaced_inside_pacific_window(monkeypatch):
    monkeypatch.setenv("OUTREACH_DAILY_NEW_CAP", "20")
    first = generation.allocate_next_outreach_slot(
        now=_utc_from_pt(2026, 11, 23, 6, 50),
        scheduled_datetimes=[],
        gap_minutes=10,
    )
    second = generation.allocate_next_outreach_slot(
        now=_utc_from_pt(2026, 11, 23, 6, 50),
        scheduled_datetimes=[first],
        gap_minutes=25,
    )
    assert first.astimezone(generation.PST_ZONE).strftime("%Y-%m-%d %H:%M") == "2026-11-23 07:10"
    assert second.astimezone(generation.PST_ZONE).strftime("%Y-%m-%d %H:%M") == "2026-11-23 07:35"


def test_first_touch_slots_roll_over_at_daily_cap_or_window_end(monkeypatch):
    monkeypatch.setenv("OUTREACH_DAILY_NEW_CAP", "2")
    existing = [
        _utc_from_pt(2026, 11, 23, 7, 10),
        _utc_from_pt(2026, 11, 23, 7, 30),
    ]
    capped = generation.allocate_next_outreach_slot(
        now=_utc_from_pt(2026, 11, 23, 6, 50),
        scheduled_datetimes=existing,
        gap_minutes=10,
    )
    assert capped.astimezone(generation.PST_ZONE).strftime("%Y-%m-%d %H:%M") == "2026-11-24 07:10"

    monkeypatch.setenv("OUTREACH_DAILY_NEW_CAP", "20")
    after_window = generation.allocate_next_outreach_slot(
        now=_utc_from_pt(2026, 11, 23, 15, 50),
        scheduled_datetimes=[],
        gap_minutes=25,
    )
    assert after_window.astimezone(generation.PST_ZONE).strftime("%Y-%m-%d %H:%M") == "2026-11-24 07:25"
