from __future__ import annotations

from datetime import date

import pytest
from flask import Flask

from app.video_shorts.services import stats_report


@pytest.fixture()
def app(tmp_path, monkeypatch):
    test_app = Flask(__name__)
    test_app.secret_key = "report-test-secret"
    monkeypatch.setattr(stats_report, "_TOKEN_DIR", tmp_path / "tokens")
    monkeypatch.setattr(stats_report, "_LOCK_PATH", tmp_path / "render.lock")
    return test_app


def test_report_grant_is_tenant_range_route_bound_and_single_use(app):
    start = date(2026, 11, 1)
    end = date(2026, 11, 30)
    with app.app_context():
        token = stats_report.issue_report_grant(
            owner_user_id="owner-a",
            brand_id="brand-a",
            start_date=start,
            end_date=end,
        )
        with pytest.raises(stats_report.ReportGrantError, match="does not match"):
            stats_report.consume_report_grant(
                token,
                brand_id="brand-b",
                start=start.isoformat(),
                end=end.isoformat(),
                route=stats_report.REPORT_RENDER_ROUTE,
            )
        claims = stats_report.consume_report_grant(
            token,
            brand_id="brand-a",
            start=start.isoformat(),
            end=end.isoformat(),
            route=stats_report.REPORT_RENDER_ROUTE,
        )
        assert claims == {
            "owner_user_id": "owner-a",
            "brand_id": "brand-a",
            "start": "2026-11-01",
            "end": "2026-11-30",
        }
        with pytest.raises(stats_report.ReportGrantError, match="already been used"):
            stats_report.consume_report_grant(
                token,
                brand_id="brand-a",
                start=start.isoformat(),
                end=end.isoformat(),
                route=stats_report.REPORT_RENDER_ROUTE,
            )


def test_report_grant_rejects_another_range_without_consuming(app):
    with app.app_context():
        token = stats_report.issue_report_grant(
            owner_user_id="owner-a",
            brand_id="brand-a",
            start_date=date(2026, 10, 1),
            end_date=date(2026, 10, 31),
        )
        with pytest.raises(stats_report.ReportGrantError):
            stats_report.consume_report_grant(
                token,
                brand_id="brand-a",
                start="2026-11-01",
                end="2026-11-30",
                route=stats_report.REPORT_RENDER_ROUTE,
            )
        assert stats_report.consume_report_grant(
            token,
            brand_id="brand-a",
            start="2026-10-01",
            end="2026-10-31",
            route=stats_report.REPORT_RENDER_ROUTE,
        )["owner_user_id"] == "owner-a"
