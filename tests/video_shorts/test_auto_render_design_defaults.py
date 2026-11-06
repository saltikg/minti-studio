from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from flask import Flask, g, jsonify
from werkzeug.exceptions import Forbidden

from app.video_shorts.routes import generation
from app.video_shorts.services import tenant_media


ROOT = Path(__file__).resolve().parents[2]


class _Result:
    def __init__(self, row=None):
        self._row = row

    def fetchone(self):
        return self._row


class _UserConnection:
    def execute(self, _sql, _params=None):
        return _Result(("owner-1", "owner@example.com", "user", "starter"))

    def close(self):
        pass


def test_auto_render_drops_stale_admin_scope_from_worker_context(monkeypatch):
    app = Flask(__name__)
    captured = {}
    monkeypatch.setattr(generation, "get_db_readonly", lambda: _UserConnection())

    def _autoclip(_video_pk):
        captured["admin_scope"] = generation.request_admin_operation_scope()
        captured["owner_user_id"] = g.vs_current_user["id"]
        captured["brand_id"] = g.vs_current_brand["id"]
        return jsonify(success=True, job_id="render-job-1")

    monkeypatch.setattr(generation, "autoclip_video", _autoclip)

    with app.app_context():
        g.vs_admin_operation_scope = {
            "owner_user_id": "stale-owner",
            "brand_id": "stale-brand",
            "workspace_kind": "customer",
            "acting_admin_id": "admin-1",
        }
        result = generation._enqueue_auto_render_for_plan_entry(
            owner_user_id="owner-1",
            brand_id="brand-1",
            video_pk=7937,
            plan_index=1,
        )
        restored_scope = dict(g.vs_admin_operation_scope)

    assert result["ok"] is True
    assert captured == {
        "admin_scope": None,
        "owner_user_id": "owner-1",
        "brand_id": "brand-1",
    }
    assert restored_scope["owner_user_id"] == "stale-owner"


def test_lead_render_restores_previous_admin_scope(monkeypatch):
    app = Flask(__name__)
    captured = {}
    monkeypatch.setattr(generation, "get_db_readonly", lambda: _UserConnection())

    def _autoclip(_video_pk):
        captured["scope"] = dict(g.vs_admin_operation_scope)
        return jsonify(success=True, job_id="lead-render-job")

    monkeypatch.setattr(generation, "autoclip_video", _autoclip)

    with app.app_context():
        g.vs_admin_operation_scope = {"owner_user_id": "previous-owner"}
        result = generation._enqueue_selected_lead_render(
            lead_id="lead-1",
            owner_user_id="owner-1",
            brand_id="brand-1",
            video_pk=7937,
            plan_index=1,
        )
        restored_scope = dict(g.vs_admin_operation_scope)

    assert result["ok"] is True
    assert captured["scope"]["owner_user_id"] == "owner-1"
    assert captured["scope"]["workspace_kind"] == "lead"
    assert restored_scope == {"owner_user_id": "previous-owner"}


def test_editor_context_fails_closed_outside_request():
    with pytest.raises(tenant_media.TenantMediaConflict, match="request-only"):
        generation._active_editor_context()


class _DesignDefaultsConnection:
    def __init__(self, current):
        self.current = current
        self.updated = {}
        self.committed = False

    def execute(self, sql, params=None):
        normalized = " ".join(str(sql).split())
        if normalized.startswith("SELECT "):
            columns = normalized.removeprefix("SELECT ").split(" FROM youtube_videos", 1)[0].split(", ")
            return _Result(tuple(self.current.get(column) for column in columns))
        if normalized.startswith("UPDATE youtube_videos SET "):
            assignment_sql = normalized.split(" SET ", 1)[1].split(" WHERE ", 1)[0]
            columns = [re.match(r"([a-z_]+) = \?", item).group(1) for item in assignment_sql.split(", ")]
            self.updated = dict(zip(columns, list(params or [])[: len(columns)]))
            return _Result()
        raise AssertionError(f"Unexpected SQL: {normalized}")

    def commit(self):
        self.committed = True

    def rollback(self):
        pass

    def close(self):
        pass


def test_brand_scoped_defaults_never_merge_legacy_fields(monkeypatch):
    scoped_key = "short_editor_defaults:brand-1"
    values = {
        scoped_key: '{"title_bg_color":"#FFFFFF","title_bg_alpha":100}',
        "short_editor_defaults": '{"title_bg_color":"#14532D","font":"montserrat_black"}',
    }
    calls = []

    def _load(_user_id, key):
        calls.append(key)
        return values.get(key)

    monkeypatch.setattr(generation, "load_user_preference", _load)

    loaded = generation._load_short_editor_defaults("owner-1", "brand-1")

    assert loaded == {"title_bg_color": "#FFFFFF", "title_bg_alpha": 100}
    assert calls == [scoped_key]
    resolved = generation._apply_short_editor_defaults(
        {"title_bg_color": None, "title_bg_alpha": None, "font": None},
        loaded,
    )
    assert resolved["title_bg_color"] == "#FFFFFF"
    assert resolved["title_bg_alpha"] == 100
    assert resolved["font"] == generation.DEFAULT_EDITOR_TITLE_FONT_KEY


def test_legacy_defaults_are_used_only_without_scoped_record(monkeypatch):
    values = {
        "short_editor_defaults": '{"title_bg_color":"#14532D","title_bg_alpha":0}',
    }
    calls = []

    def _load(_user_id, key):
        calls.append(key)
        return values.get(key)

    monkeypatch.setattr(generation, "load_user_preference", _load)

    loaded = generation._load_short_editor_defaults("owner-1", "brand-without-default")

    assert loaded["title_bg_color"] == "#14532D"
    assert loaded["title_bg_alpha"] == 0
    assert calls == ["short_editor_defaults:brand-without-default", "short_editor_defaults"]


def test_scoped_snapshot_replaces_values_previously_copied_from_legacy():
    legacy = {
        "font": "montserrat_black",
        "title_bg_color": "#14532D",
        "title_bg_alpha": 0,
    }
    scoped = {
        "font": "anton",
        "title_bg_color": "#FFFFFF",
        "title_bg_alpha": 100,
    }
    stored_video = {
        "font": "montserrat_black",
        "title_bg_color": "#14532D",
        "title_bg_alpha": 0,
    }

    without_legacy = generation._discard_legacy_inherited_editor_values(stored_video, legacy)
    resolved = generation._apply_short_editor_defaults(without_legacy, scoped)

    assert resolved["font"] == "anton"
    assert resolved["title_bg_color"] == "#FFFFFF"
    assert resolved["title_bg_alpha"] == 100


@pytest.mark.parametrize(
    ("source_height", "caption_top", "block_height", "subscribe_top", "expected"),
    [
        (1920, 1334, 136, 1470, (889.333, 90.667, 980.0)),
        (1920, 488, 136, None, (325.333, 90.667, None)),
        (1920, 491, 136, None, (327.333, 90.667, None)),
        (1280, 319, 90, None, (319.0, 90.0, None)),
    ],
)
def test_auto_layout_segment_coordinates_are_stored_in_canonical_frame(
    source_height, caption_top, block_height, subscribe_top, expected
):
    normalized = generation._normalize_auto_layout_segment_coordinates(
        {
            "mode": "fill",
            "caption_top": caption_top,
            "caption_block_height": block_height,
            "subscribe_top": subscribe_top,
        },
        source_height=source_height,
    )

    assert normalized["coordinate_height"] == 1280
    assert normalized["caption_top"] == expected[0]
    assert normalized["caption_block_height"] == expected[1]
    assert normalized["subscribe_top"] == expected[2]


def test_preview_scales_new_and_legacy_auto_layout_coordinates_through_one_helper():
    template = (ROOT / "app/video_shorts/templates/generate_short.html").read_text(encoding="utf-8")

    assert "const scaleAutoLayoutPreviewSegmentCoordinates" in template
    assert "segment.coordinate_height" in template
    assert "card?.dataset.renderHeight" in template
    assert "const activeHeight = getActiveRenderFrameHeight();" in template
    assert "return scaleAutoLayoutPreviewSegmentCoordinates(activeSegment, card);" in template


def test_auto_render_applies_explicit_saved_design_over_database_defaults(monkeypatch):
    current = {
        "title_font_key": "fjalla_one",
        "title_font_size": None,
        "subtitle_font_key": None,
        "subtitle_font_size": None,
        "subtitle_margin": 405,
        "subtitle_margin_custom": False,
        "caption_anchor": "top",
        "subtitle_style": "karaoke",
        "subtitle_preset": "amber_gold",
        "subtitle_text_color": "#FFFFFF",
        "subtitle_highlight_color": None,
        "subtitle_bg_color": "#000000",
        "subtitle_bg_alpha": 85,
        "subtitle_effect": None,
        "subtitle_uppercase": None,
        "overlay_shapes": None,
    }
    saved = {
        "font": "montserrat_black",
        "title_font_size": 44,
        "sub_font": "montserrat_black",
        "sub_font_size": 40,
        "sub_margin": 269,
        "caption_anchor": "bottom",
        "subtitle_style": "karaoke",
        "subtitle_preset": "yellow_punch",
        "subtitle_text_color": "#FFE600",
        "subtitle_highlight_color": "#FFE600",
        "subtitle_bg_color": "#000000",
        "subtitle_bg_alpha": 0,
        "subtitle_effect": "outline_shadow",
        "subtitle_uppercase": True,
        "overlay_shapes": [{"id": "brand-shape", "x": 0, "y": 0, "w": 100, "h": 20}],
    }
    conn = _DesignDefaultsConnection(current)
    monkeypatch.setattr(generation, "get_db", lambda: conn)
    monkeypatch.setattr(generation, "_ensure_video_crop_schema", lambda _conn: set(current))
    monkeypatch.setattr(generation, "table_columns", lambda _conn, _table: set(current))
    monkeypatch.setattr(generation, "_load_short_editor_defaults", lambda _owner, _brand: saved)

    result = generation.apply_short_editor_defaults_to_video_if_null(
        owner_user_id="owner-1",
        brand_id="brand-1",
        video_pk=584,
    )

    assert result["updated"] is True
    assert conn.committed is True
    assert conn.updated["title_font_key"] == "montserrat_black"
    assert conn.updated["subtitle_preset"] == "yellow_punch"
    assert conn.updated["subtitle_text_color"] == "#FFE600"
    assert conn.updated["subtitle_highlight_color"] == "#FFE600"
    assert conn.updated["subtitle_bg_alpha"] == 0
    assert conn.updated["subtitle_effect"] == "outline_shadow"
    assert conn.updated["subtitle_uppercase"] is True
    assert conn.updated["caption_anchor"] == "bottom"
    assert conn.updated["subtitle_margin"] == 269
    assert "brand-shape" in conn.updated["overlay_shapes"]


class _AutopilotLookupConnection:
    def __init__(self, row):
        self.row = row

    def execute(self, _sql, _params=None):
        return _Result(self.row)

    def close(self):
        pass


def test_autopilot_design_uses_saved_brand_default(monkeypatch):
    saved = {"font": "montserrat_black_italic", "subtitle_preset": "podcast", "enable_subscribe_overlay": False}
    captured = {}
    monkeypatch.setattr(generation, "get_db_readonly", lambda: _AutopilotLookupConnection([json.dumps(saved)]))
    monkeypatch.setattr(
        generation,
        "apply_short_editor_defaults_to_video_if_null",
        lambda **kwargs: captured.update(kwargs) or {"updated": True, "fields": ["title_font_key"]},
    )

    result = generation.apply_autopilot_design_to_video(
        owner_user_id="owner-1", brand_id="brand-1", video_pk=7928
    )

    assert result["source"] == "saved_default"
    assert captured["defaults_override"] == saved
    assert captured["force"] is True


def test_autopilot_design_without_saved_default_uses_podcast_fallback(monkeypatch):
    captured = {}
    monkeypatch.setattr(generation, "get_db_readonly", lambda: _AutopilotLookupConnection([None]))
    monkeypatch.setattr(
        generation,
        "apply_short_editor_defaults_to_video_if_null",
        lambda **kwargs: captured.update(kwargs) or {"updated": True, "fields": ["subtitle_preset"]},
    )

    result = generation.apply_autopilot_design_to_video(
        owner_user_id="owner-1", brand_id="brand-1", video_pk=101
    )

    assert result["source"] == "podcast_fallback"
    assert captured["defaults_override"]["subtitle_preset"] == "podcast"
    assert captured["defaults_override"]["enable_subscribe_overlay"] is False


def test_new_self_serve_video_copies_current_brand_default(monkeypatch):
    saved = {"caption_anchor": "top", "sub_margin": 520, "subtitle_preset": "podcast"}
    captured = {}
    monkeypatch.setattr(
        generation,
        "get_db_readonly",
        lambda: _AutopilotLookupConnection([json.dumps(saved), "self_serve"]),
    )
    monkeypatch.setattr(
        generation,
        "apply_short_editor_defaults_to_video_if_null",
        lambda **kwargs: captured.update(kwargs) or {"updated": True, "fields": ["caption_anchor"]},
    )

    result = generation.apply_autopilot_design_to_video(
        owner_user_id="owner-1", brand_id="brand-1", video_pk=9001
    )

    assert result["source"] == "saved_default"
    assert captured["force"] is True
    assert captured["defaults_override"] == saved


@pytest.mark.parametrize("role", ["member", "admin"])
def test_reset_to_default_route_is_video_scoped_for_customer_and_admin(monkeypatch, role):
    app = Flask(__name__)
    captured = {}
    saved = {"caption_anchor": "top", "sub_margin": 520, "subtitle_preset": "podcast"}
    monkeypatch.setattr(
        generation,
        "_active_editor_context",
        lambda: {"owner_user_id": "owner-1", "brand_id": "brand-1"},
    )
    monkeypatch.setattr(
        generation,
        "get_db_readonly",
        lambda: _AutopilotLookupConnection(("owner-1", "brand-1")),
    )
    monkeypatch.setattr(generation, "load_user_preference", lambda *_args: json.dumps(saved))
    monkeypatch.setattr(
        generation,
        "apply_short_editor_defaults_to_video_if_null",
        lambda **kwargs: captured.update(kwargs) or {"updated": True, "fields": ["caption_anchor"]},
    )

    with app.test_request_context("/video_shorts/generate/7956/settings/reset-default", method="POST"):
        g.vs_current_user = {"id": "admin-1" if role == "admin" else "owner-1", "role": role}
        response = generation.reset_short_editor_design_to_default(7956)
        payload = response.get_json()

    assert payload["success"] is True
    assert captured == {
        "owner_user_id": "owner-1",
        "brand_id": "brand-1",
        "video_pk": 7956,
        "force": True,
        "defaults_override": saved,
    }


def test_reset_to_default_route_fails_closed_on_video_tenant_mismatch(monkeypatch):
    app = Flask(__name__)
    monkeypatch.setattr(
        generation,
        "_active_editor_context",
        lambda: {"owner_user_id": "owner-1", "brand_id": "brand-1"},
    )
    monkeypatch.setattr(
        generation,
        "get_db_readonly",
        lambda: _AutopilotLookupConnection(("owner-1", "other-brand")),
    )
    with app.test_request_context("/video_shorts/generate/7956/settings/reset-default", method="POST"):
        g.vs_current_user = {"id": "owner-1", "role": "member"}
        with pytest.raises(Forbidden):
            generation.reset_short_editor_design_to_default(7956)


def test_force_copy_uses_saved_default_instead_of_existing_video_values(monkeypatch):
    current = {
        "subtitle_margin": 871,
        "subtitle_margin_custom": True,
        "caption_anchor": "bottom",
        "subtitle_preset": "yellow_punch",
    }
    saved = {
        "sub_margin": 520,
        "caption_anchor": "top",
        "subtitle_preset": "podcast",
    }
    conn = _DesignDefaultsConnection(current)
    monkeypatch.setattr(generation, "get_db", lambda: conn)
    monkeypatch.setattr(generation, "_ensure_video_crop_schema", lambda _conn: set(current))
    monkeypatch.setattr(generation, "table_columns", lambda _conn, _table: set(current))

    generation.apply_short_editor_defaults_to_video_if_null(
        owner_user_id="owner-1",
        brand_id="brand-1",
        video_pk=7956,
        force=True,
        defaults_override=saved,
    )

    assert conn.updated["caption_anchor"] == "top"
    assert conn.updated["subtitle_margin"] == 520
    assert conn.updated["subtitle_preset"] == "podcast"


def test_saved_render_snapshot_hash_detects_design_change():
    base = {
        "plan_index": 4,
        "brand_id": "brand-1",
        "caption_anchor": "top",
        "subtitle_margin": 520,
        "subtitle_highlight_color": "#45B89E",
        "subtitle_effect": "outline_shadow",
        "subscribe_overlay_key": "system:subscribe.png",
    }
    rendered_hash = generation._render_settings_hash_from_options(base)
    changed_hash = generation._render_settings_hash_from_options(
        {**base, "caption_anchor": "bottom", "subtitle_margin": 270}
    )

    assert rendered_hash != changed_hash


def test_save_top_then_render_builder_immediately_uses_persisted_top(monkeypatch):
    monkeypatch.setattr(generation, "_load_subscribe_overlay_key", lambda *_args: None)
    monkeypatch.setattr(generation, "choose_deterministic_system_subscribe", lambda *_args: None)
    monkeypatch.setattr(generation, "load_background_preference", lambda *_args: None)
    monkeypatch.setattr(
        generation,
        "resolve_export_resolution",
        lambda *_args: type("Resolution", (), {"resolution": "1080"})(),
    )
    video = {
        "id": 7956,
        "video_id": "source-7956",
        "owner_user_id": "owner-1",
        "brand_id": "brand-1",
        "title": "Peptides for Women 40+",
        "duration_seconds": 120,
        "caption_anchor": "top",
        "subtitle_margin": 520,
        "subtitle_margin_custom": True,
        "subtitle_preset": "podcast",
        "subtitle_style": "plain",
        "layout_mode": "manual",
        "show_title": True,
        "show_subtitle": True,
    }
    plan = {"plan_index": 4, "start": 10.0, "end": 30.0, "title": "Doctor's Warning"}

    options = generation._render_settings_options_for_plan_entry(
        video_pk=7956,
        video=video,
        plan_entry=plan,
        segments=[],
        brand_id="brand-1",
        fallback_title=video["title"],
    )

    assert options["caption_anchor"] == "top"
    assert options["subtitle_margin"] == 520
    assert options["export_resolution"] == "1080"


def test_editor_template_exposes_reset_and_visible_design_changed_state():
    template = (ROOT / "app/video_shorts/templates/generate_short.html").read_text(encoding="utf-8")

    assert "Replace this video's design with your saved default?" in template
    assert 'id="generate-v2-editor-reset-default"' in template
    assert 'id="crop-reset-default-btn"' in template
    assert "Design changed — Regenerate to apply" in template
    assert "{% if has_short_editor_default %}" in template


def test_worker_render_route_requires_and_receives_internal_snapshot_marker():
    worker_source = (ROOT / "app/video_shorts/worker.py").read_text(encoding="utf-8")
    generation_source = (ROOT / "app/video_shorts/routes/generation.py").read_text(encoding="utf-8")

    assert '"vs_render_worker_job": job' in worker_source
    assert "Queued render context is invalid." in generation_source
    assert "Queued render settings failed validation." in generation_source
    assert "persisted_video_design = _load_video_design_mapping_for_render" in generation_source
