from __future__ import annotations

from datetime import date
from threading import Lock, Thread
from uuid import uuid4

from app import create_app
from app.video_shorts.services import db as db_service
from app.video_shorts.services import usage_metering


def _insert_user(user_id: str, plan_id: str = "plan_10gb") -> None:
    conn = db_service.get_db()
    try:
        usage_metering.ensure_usage_metering_schema(conn)
        conn.execute(
            """
            INSERT INTO shorts_users (id, name, email, username, role, plan_id)
            VALUES (?, ?, ?, ?, 'member', ?)
            """,
            [user_id, "Test User", f"{user_id}@example.com", f"user_{user_id[:8]}", plan_id],
        )
        conn.commit()
    finally:
        conn.close()


def _set_plan_limits(plan_id: str, export_limit: int, transcription_limit: int | None) -> None:
    conn = db_service.get_db()
    try:
        conn.execute(
            """
            UPDATE shorts_storage_plans
            SET monthly_export_limit = ?,
                monthly_transcription_minutes = ?
            WHERE plan_id = ?
            """,
            [export_limit, transcription_limit, plan_id],
        )
        conn.commit()
    finally:
        conn.close()


def test_usage_endpoint_auto_creates_current_period_and_returns_zeros(monkeypatch, tmp_path):
    db_path = tmp_path / "usage_endpoint.duckdb"
    monkeypatch.setattr(db_service, "VIDEO_SHORTS_DB_BACKEND", "duckdb")
    monkeypatch.setattr(db_service, "VIDEO_SHORTS_DB", str(db_path))

    user_id = str(uuid4())
    _insert_user(user_id, "plan_10gb")

    app = create_app()
    app.secret_key = "test-secret"
    client = app.test_client()
    with client.session_transaction() as session:
        session["vs_user_id"] = user_id

    response = client.get("/api/usage")
    assert response.status_code == 200
    payload = response.get_json()

    assert payload["plan"]["id"] == "plan_10gb"
    assert payload["exports"]["used"] == 0
    assert payload["transcription"]["used_minutes"] == 0
    assert payload["period"]["start"].endswith("-01")


def test_one_export_is_visible_in_usage_snapshot(monkeypatch, tmp_path):
    db_path = tmp_path / "single_export.duckdb"
    monkeypatch.setattr(db_service, "VIDEO_SHORTS_DB_BACKEND", "duckdb")
    monkeypatch.setattr(db_service, "VIDEO_SHORTS_DB", str(db_path))

    user_id = str(uuid4())
    _insert_user(user_id, "plan_free")

    usage_metering.increment_exports(user_id, 1)
    snapshot = usage_metering.get_usage_snapshot(user_id)

    assert snapshot["exports"]["used"] == 1
    assert snapshot["exports"]["limit"] == 30


def test_export_limit_is_enforced(monkeypatch, tmp_path):
    db_path = tmp_path / "limit_enforced.duckdb"
    monkeypatch.setattr(db_service, "VIDEO_SHORTS_DB_BACKEND", "duckdb")
    monkeypatch.setattr(db_service, "VIDEO_SHORTS_DB", str(db_path))

    user_id = str(uuid4())
    _insert_user(user_id, "plan_free")

    for _ in range(30):
        result = usage_metering.reserve_export(user_id, 1)
        assert result["allowed"] is True

    blocked = usage_metering.reserve_export(user_id, 1)
    assert blocked["allowed"] is False
    assert blocked["remaining"] == 0


def test_period_rollover_creates_new_month_row(monkeypatch, tmp_path):
    db_path = tmp_path / "period_rollover.duckdb"
    monkeypatch.setattr(db_service, "VIDEO_SHORTS_DB_BACKEND", "duckdb")
    monkeypatch.setattr(db_service, "VIDEO_SHORTS_DB", str(db_path))

    user_id = str(uuid4())
    _insert_user(user_id, "plan_10gb")

    monkeypatch.setattr(usage_metering, "_utc_today", lambda: date(2026, 6, 18))
    usage_metering.increment_exports(user_id, 3)
    usage_metering.add_transcription_minutes(user_id, 42)

    june = usage_metering.get_usage_snapshot(user_id)
    assert june["period"]["start"] == "2026-06-01"
    assert june["exports"]["used"] == 3
    assert june["transcription"]["used_minutes"] == 42

    monkeypatch.setattr(usage_metering, "_utc_today", lambda: date(2026, 7, 2))
    july = usage_metering.get_usage_snapshot(user_id)
    assert july["period"]["start"] == "2026-07-01"
    assert july["exports"]["used"] == 0
    assert july["transcription"]["used_minutes"] == 0


def test_concurrent_reservations_do_not_exceed_limit(monkeypatch, tmp_path):
    db_path = tmp_path / "concurrent_limit.duckdb"
    monkeypatch.setattr(db_service, "VIDEO_SHORTS_DB_BACKEND", "duckdb")
    monkeypatch.setattr(db_service, "VIDEO_SHORTS_DB", str(db_path))

    user_id = str(uuid4())
    _insert_user(user_id, "plan_10gb")
    _set_plan_limits("plan_10gb", 10, 600)
    usage_metering.get_current_period(user_id)

    successes: list[bool] = []
    errors: list[str] = []
    lock = Lock()

    def _worker() -> None:
        try:
            result = usage_metering.reserve_export(user_id, 1)
            with lock:
                successes.append(bool(result["allowed"]))
        except Exception as exc:  # pragma: no cover - diagnostic path
            with lock:
                errors.append(str(exc))

    threads = [Thread(target=_worker) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    assert sum(1 for item in successes if item) == 10

    snapshot = usage_metering.get_usage_snapshot(user_id)
    assert snapshot["exports"]["used"] == 10
    assert snapshot["exports"]["limit"] == 10
