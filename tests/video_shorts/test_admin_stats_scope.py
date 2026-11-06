from __future__ import annotations

from datetime import date

import pytest
from flask import Flask, g, jsonify, session

from app.video_shorts import video_shorts_bp
from app.video_shorts.routes import generation, stats
from app.video_shorts.services.admin_operation_scope import request_admin_operation_scope


class _Connection:
    def close(self):
        return None


@pytest.fixture()
def app(monkeypatch):
    test_app = Flask(__name__)
    test_app.secret_key = "test-secret"
    actor = {"role": "admin"}

    @test_app.before_request
    def _set_actor():
        g.vs_current_user = {"id": "admin-1", "role": actor["role"]}

    test_app.register_blueprint(video_shorts_bp, url_prefix="/video_shorts")

    def resolve_scope(_conn, *, current_user, brand_id, workspace_kind):
        if current_user.get("role") != "admin" or brand_id != "shelley-brand" or workspace_kind != "customer":
            return None
        return {
            "owner_user_id": "shelley-owner",
            "brand_id": "shelley-brand",
            "workspace_kind": "customer",
            "acting_admin_id": "admin-1",
        }

    monkeypatch.setattr(generation, "get_db_readonly", lambda: _Connection())
    monkeypatch.setattr(generation, "resolve_admin_operation_request_scope", resolve_scope)
    return test_app, actor


def test_admin_scope_reaches_page_widgets_and_top_videos(app, monkeypatch):
    test_app, _actor = app
    observed = []

    def render_stub(*, load_widgets):
        scope = request_admin_operation_scope()
        observed.append(("widgets" if load_widgets else "page", dict(scope or {})))
        return {"ok": True, "brand_id": (scope or {}).get("brand_id")}

    monkeypatch.setattr(stats, "_render_video_stats", render_stub)
    monkeypatch.setattr(stats, "get_db_readonly", lambda: _Connection())
    monkeypatch.setattr(
        stats,
        "_resolve_stats_tenant",
        lambda _conn: {
            "owner_user_id": request_admin_operation_scope()["owner_user_id"],
            "brand_id": request_admin_operation_scope()["brand_id"],
        },
    )
    top_brands = []

    def top_stub(
        _conn,
        _target_date,
        _channel_type,
        *,
        owner_user_id,
        brand_id,
        limit,
    ):
        top_brands.append((owner_user_id, brand_id, limit))
        return [{"video_id": "shelley-video"}]

    monkeypatch.setattr(stats, "_fetch_top_videos_for_date", top_stub)
    query = "admin_operation_brand=shelley-brand&admin_operation_kind=customer"
    client = test_app.test_client()
    assert client.get(f"/video_shorts/stats?{query}").status_code == 200
    assert client.get(f"/video_shorts/stats/widgets?{query}").status_code == 200
    top_response = client.get(f"/video_shorts/stats/top_videos?date=2026-11-05&{query}")
    assert top_response.status_code == 200
    assert top_response.get_json()["videos"] == [{"video_id": "shelley-video"}]
    assert [entry[1]["brand_id"] for entry in observed] == ["shelley-brand", "shelley-brand"]
    assert top_brands == [("shelley-owner", "shelley-brand", 6)]


def test_dashboard_scope_mismatch_and_non_admin_fail_with_403(app):
    test_app, actor = app
    client = test_app.test_client()
    paths = (
        "/video_shorts/stats",
        "/video_shorts/stats/widgets",
        "/video_shorts/stats/top_videos?date=2026-11-05&channel=all",
        "/video_shorts/stats/report?start=2026-11-01&end=2026-11-30",
        "/video_shorts/stats/report.pdf?start=2026-11-01&end=2026-11-30",
    )
    for path in paths:
        separator = "&" if "?" in path else "?"
        mismatch = client.get(
            f"{path}{separator}admin_operation_brand=hoca-brand&admin_operation_kind=customer"
        )
        assert mismatch.status_code == 403
    actor["role"] = "member"
    for path in paths:
        separator = "&" if "?" in path else "?"
        non_admin = client.get(
            f"{path}{separator}admin_operation_brand=shelley-brand&admin_operation_kind=customer"
        )
        assert non_admin.status_code == 403


def test_customer_stats_route_is_unchanged_without_admin_parameters(app, monkeypatch):
    test_app, actor = app
    actor["role"] = "member"
    monkeypatch.setattr(stats, "_render_video_stats", lambda *, load_widgets: jsonify(ok=True, scoped=False))
    response = test_app.test_client().get("/video_shorts/stats")
    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "scoped": False}


def test_admin_stats_tenant_never_falls_back_to_session_brand():
    class TenantConnection:
        def execute(self, sql, params=None):
            if "FROM shorts_brands" in sql:
                assert params == ["shelley-owner", "shelley-brand"]
                return self
            if "SELECT time_zone FROM shorts_users" in sql:
                assert params == ["shelley-owner"]
                return self
            raise AssertionError(sql)

        def fetchone(self):
            if not hasattr(self, "brand_returned"):
                self.brand_returned = True
                return ("shelley-brand", "shelley-owner", "Shelley", "shelley", False, None, None)
            return ("America/Los_Angeles",)

    test_app = Flask(__name__)
    test_app.secret_key = "test-secret"
    with test_app.test_request_context("/video_shorts/stats"):
        session["vs_brand_id"] = "hoca-brand"
        g.vs_current_user = {"id": "admin-1", "role": "admin"}
        g.vs_admin_operation_scope = {
            "owner_user_id": "shelley-owner",
            "brand_id": "shelley-brand",
            "workspace_kind": "customer",
            "acting_admin_id": "admin-1",
        }
        tenant = stats._resolve_stats_tenant(TenantConnection())
    assert tenant["owner_user_id"] == "shelley-owner"
    assert tenant["brand_id"] == "shelley-brand"
    assert tenant["brand_name"] == "Shelley"


def test_customer_stats_tenant_loads_the_owned_brand_name(monkeypatch):
    class TenantConnection:
        def execute(self, sql, params=None):
            assert "FROM shorts_brands" in sql
            assert params == ["customer-owner", "customer-brand"]
            return self

        def fetchone(self):
            return (
                "customer-brand", "customer-owner", "Customer Brand", "customer-brand",
                False, None, None,
            )

    test_app = Flask(__name__)
    test_app.secret_key = "test-secret"
    monkeypatch.setattr(stats, "current_brand_id", lambda: "customer-brand")
    with test_app.test_request_context("/video_shorts/stats"):
        g.vs_current_user = {"id": "customer-owner", "time_zone": "America/Los_Angeles"}
        g.vs_current_brand = None
        tenant = stats._resolve_stats_tenant(TenantConnection())
    assert tenant["brand_id"] == "customer-brand"
    assert tenant["brand_name"] == "Customer Brand"


def test_top_videos_query_is_explicitly_tenant_scoped(monkeypatch):
    observed = {}

    def loader(_conn, **kwargs):
        observed.update(kwargs)
        return []

    monkeypatch.setattr(stats, "load_top_videos_for_date", loader)
    assert stats._fetch_top_videos_for_date(
        object(),
        date(2026, 11, 5),
        "all",
        owner_user_id="shelley-owner",
        brand_id="shelley-brand",
        limit=6,
    ) == []
    assert observed == {
        "owner_user_id": "shelley-owner",
        "brand_id": "shelley-brand",
        "target_date": date(2026, 11, 5),
        "selected_platform": "all",
        "limit": 6,
    }
