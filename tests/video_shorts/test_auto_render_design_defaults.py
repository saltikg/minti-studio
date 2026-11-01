from __future__ import annotations

import re

from app.video_shorts.routes import generation


class _Result:
    def __init__(self, row=None):
        self._row = row

    def fetchone(self):
        return self._row


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
