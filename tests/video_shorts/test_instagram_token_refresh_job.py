from datetime import datetime, timedelta, timezone

from app.video_shorts.tasks import refresh_instagram_tokens as job


def test_near_expiry_without_refresh_would_alert():
    now = datetime(2026, 11, 15, tzinfo=timezone.utc)

    assert job._alert_reason(
        action="skipped: outside refresh window",
        expires_at=now + timedelta(days=6),
        now=now,
    ) == "expires within 7 days and was not refreshed"


def test_refreshed_near_expiry_does_not_alert():
    now = datetime(2026, 11, 15, tzinfo=timezone.utc)

    assert job._alert_reason(
        action="refreshed",
        expires_at=now + timedelta(days=6),
        now=now,
    ) is None


def test_failure_always_alerts():
    now = datetime(2026, 11, 15, tzinfo=timezone.utc)

    assert job._alert_reason(
        action="failed",
        expires_at=now + timedelta(days=30),
        now=now,
    ) == "refresh failed"


def test_job_checks_every_listed_token_and_reports_skip(monkeypatch):
    current = {
        "user_id": "owner::brand-id",
        "page_access_token": "secret-token",
        "instagram_business_account_id": "ig-id",
        "instagram_username": "account_name",
        "token_refreshed_at": "2026-11-01T00:00:00+00:00",
        "expires_at": "2027-01-01T00:00:00+00:00",
    }
    calls = []
    monkeypatch.setattr(job, "_brand_names", lambda: {"brand-id": "Brand Name"})
    monkeypatch.setattr(job, "list_instagram_credentials", lambda: [{"user_id": "owner::brand-id"}])
    monkeypatch.setattr(job, "get_instagram_data", lambda *args, **kwargs: dict(current))

    def fake_refresh(**kwargs):
        calls.append(kwargs)
        return dict(current)

    monkeypatch.setattr(job, "refresh_instagram_token_if_needed", fake_refresh)

    results = job.run_instagram_token_refresh_job(send_alerts=False)

    assert len(calls) == 1
    assert results[0]["brand"] == "Brand Name"
    assert results[0]["account"] == "account_name"
    assert results[0]["action"] == "skipped: outside refresh window"
    assert results[0]["alert"] == "none"


def test_job_failure_is_sanitized_and_would_alert(monkeypatch):
    current = {
        "user_id": "owner::brand-id",
        "page_access_token": "secret-token",
        "instagram_business_account_id": "ig-id",
        "instagram_username": "account_name",
        "token_refreshed_at": "2026-11-01T00:00:00+00:00",
        "expires_at": "2027-01-01T00:00:00+00:00",
    }
    monkeypatch.setattr(job, "_brand_names", lambda: {"brand-id": "Brand Name"})
    monkeypatch.setattr(job, "list_instagram_credentials", lambda: [{"user_id": "owner::brand-id"}])
    monkeypatch.setattr(job, "get_instagram_data", lambda *args, **kwargs: dict(current))
    monkeypatch.setattr(
        job,
        "refresh_instagram_token_if_needed",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("secret-token")),
    )

    results = job.run_instagram_token_refresh_job(send_alerts=False)

    assert results[0]["action"] == "failed"
    assert results[0]["alert"] == "would fire"
    assert "secret-token" not in str(results[0]["error"])
