from datetime import datetime, timezone

from app.video_shorts.services import outreach_reporting


def _report():
    return {
        "today_pt": "2026-11-19",
        "dates": [f"2026-11-{day:02d}" for day in range(13, 20)],
        "rows": [
            {
                "label": "Channels discovered",
                "key": "channels_discovered",
                "values": [1, 2, 3, 4, 5, 6, 7],
                "total": 28,
            }
        ],
        "status": {
            "promotable_ready_pool": 3,
            "awaiting_email_enrichment": 2,
            "discovery_jobs_queued": 1,
            "discovery_jobs_processing": 0,
            "leads_stuck_over_48h": 1,
            "root_disk_pct": 61,
            "first_email_queue": 0,
        },
        "clicked_today": [{"channel_name": "A Doctor", "category": "Doctors"}],
        "warnings": ["first-email queue empty"],
    }


def test_render_outreach_report_has_html_and_plain_text():
    rendered = outreach_reporting.render_outreach_report(_report())

    assert rendered["subject"] == "Minti Outreach — 2026-11-19 · ⚠️ first-email queue empty"
    assert "Channels discovered | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 28" in rendered["text"]
    assert "A Doctor — Doctors" in rendered["text"]
    assert "overflow-x:auto" in rendered["html"]
    assert "<td" in rendered["html"]


def test_next_report_run_is_dst_aware():
    before_dst = datetime(2026, 3, 7, 20, tzinfo=timezone.utc)
    after_dst = datetime(2026, 3, 9, 20, tzinfo=timezone.utc)

    first = outreach_reporting.next_report_run_pt(now=before_dst)
    second = outreach_reporting.next_report_run_pt(now=after_dst)

    assert first.hour == 18 and first.utcoffset().total_seconds() == -8 * 3600
    assert second.hour == 18 and second.utcoffset().total_seconds() == -7 * 3600


def test_scheduled_report_catches_up_after_18_and_dedupes(monkeypatch):
    sent = []
    claimed = []
    monkeypatch.setattr(
        outreach_reporting,
        "_claim_report_day",
        lambda report_date, _now: claimed.append(report_date) or len(claimed) == 1,
    )
    monkeypatch.setattr(outreach_reporting, "collect_outreach_report", lambda **_kwargs: _report())
    monkeypatch.setattr(outreach_reporting, "send_outreach_report", lambda report: sent.append(report))

    now = datetime(2026, 11, 20, 4, 30, tzinfo=timezone.utc)  # Nov 19, 20:30 PT
    assert outreach_reporting.process_scheduled_outreach_report(now=now) is True
    assert outreach_reporting.process_scheduled_outreach_report(now=now) is False
    assert len(sent) == 1


def test_shared_account_filter_keeps_known_exclusions():
    sql = outreach_reporting.outreach_account_filter_sql(
        "sl", email_column="recipient_email", name_column="recipient_name", id_column="id"
    )

    assert "mintistudio.com" in sql
    assert "99, 171, 194" in sql
    assert "+test@" in sql
