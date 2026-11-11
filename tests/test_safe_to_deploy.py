from datetime import datetime, timedelta, timezone

from scripts import safe_to_deploy


def test_waits_when_schedule_is_due_within_twenty_minutes(monkeypatch, capsys):
    now = datetime.now(timezone.utc)
    item = safe_to_deploy.ScheduledPublish(
        platform='youtube',
        schedule_id='schedule-near',
        brand_id='brand',
        status='pending',
        due_at=now + timedelta(minutes=19),
        due_kind='upload_due_at',
    )
    monkeypatch.setattr(safe_to_deploy, '_load_env_file', lambda: None)
    monkeypatch.setattr(safe_to_deploy, '_fetch_processing_jobs', lambda: [])
    monkeypatch.setattr(safe_to_deploy, '_fetch_imminent_schedules', lambda _now: [item])
    assert safe_to_deploy.main() == 1
    output = capsys.readouterr().out
    assert 'WAIT' in output
    assert 'schedule-near' in output
    assert 'within 20 minutes' in output
