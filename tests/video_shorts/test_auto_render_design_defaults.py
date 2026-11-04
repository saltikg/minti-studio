from __future__ import annotations

import re

import pytest

from app.video_shorts.routes import generation
from app.video_shorts.services import autopilot_leads, tenant_media


class _Result:
    def __init__(self, row=None):
        self._row = row

    def fetchone(self):
        return self._row


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


class _PreferenceConnection:
    def __init__(self):
        self.statements = []

    def execute(self, sql, params=None):
        self.statements.append((" ".join(str(sql).split()), list(params or [])))
        return _Result()


def test_bold_pop_defaults_are_saved_for_the_specific_brand(monkeypatch):
    conn = _PreferenceConnection()
    monkeypatch.setattr(autopilot_leads, "_enable_outreach_face_aware_layout", lambda *_args: None)

    autopilot_leads._save_bold_pop_short_editor_defaults(conn, "owner-1", "brand-1")

    preference_writes = [params for sql, params in conn.statements if "INSERT INTO shorts_user_preferences" in sql]
    assert preference_writes
    assert preference_writes[-1][2] == "short_editor_defaults:brand-1"
