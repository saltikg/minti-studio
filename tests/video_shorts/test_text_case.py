from pathlib import Path

from flask import Flask, g

from app.video_shorts.config import (
    CAPTION_EFFECT_OUTLINE_SHADOW,
    DEFAULT_SUBTITLE_PRESET,
    STYLE_TEMPLATES,
    SUBTITLE_PRESETS,
    SUB_FONT_CHOICES,
    TITLE_FONTS,
    caption_effect_values,
)
from app.video_shorts.routes.generation import design_layout_metrics
from app.video_shorts.services.text_case import uppercase_text


def test_turkish_uppercase_text():
    assert uppercase_text("bir şey yapacağım", "tr") == "BİR ŞEY YAPACAĞIM"
    assert uppercase_text("kısa", "turkish") == "KISA"


def test_non_turkish_uppercase_text():
    assert uppercase_text("mixed Case", "en") == "MIXED CASE"


def test_yellow_punch_style_and_caption_effect_values():
    style = next(item for item in STYLE_TEMPLATES if item["key"] == "yellow_punch")
    preset = SUBTITLE_PRESETS[style["subtitle_preset"]]

    assert style["caption_effect"] == CAPTION_EFFECT_OUTLINE_SHADOW
    assert preset["active_color"] == preset["inactive_color"] == "#FFE600"
    assert preset["force_uppercase"] is True
    assert caption_effect_values(style["caption_effect"], preset) == {
        "outline_width": 3,
        "shadow_color": "#000000",
        "shadow_opacity": 1.0,
        "shadow_offset_x": 3,
        "shadow_offset_y": 3,
        "shadow_blur_radius": 0,
    }


def test_design_layout_endpoint_uses_turkish_uppercase_for_title_and_caption():
    title_font = next(key for key, item in TITLE_FONTS.items() if Path(item["path"]).exists())
    caption_font = next(key for key, _label, path, _name in SUB_FONT_CHOICES if Path(path).exists())
    data = {
        "width": 720,
        "height": 1280,
        "title": "bir şey yapacağım",
        "caption": "bir şey yapacağım",
        "caption_source": "bir şey yapacağım",
        "title_font": title_font,
        "caption_font": caption_font,
        "title_size": 44,
        "caption_size": 40,
        "title_top": 40,
        "caption_margin": 520,
        "caption_anchor": "top",
        "line_spacing": -4,
        "preset": DEFAULT_SUBTITLE_PRESET,
        "caption_style": "karaoke",
        "language": "tr",
        "title_uppercase": True,
        "subtitle_uppercase": True,
        "subtitle_highlight_color": "#FFE600",
        "subtitle_effect": "outline_shadow",
        "active_word": 0,
    }
    app = Flask(__name__)
    with app.test_request_context("/api/design-layout", method="POST", json=data):
        g.vs_current_user = {"id": "test-user"}
        payload = design_layout_metrics().get_json()

    assert " ".join(run["text"] for run in payload["title"]["runs"]) == "BİR ŞEY YAPACAĞIM"
    assert payload["caption"]["text"] == "BİR ŞEY YAPACAĞIM"
    assert payload["caption"]["highlight_color"] == "#FFE600"
    assert payload["caption"]["effect"] == "outline_shadow"
    assert payload["caption"]["effect_values"]["outline_width"] == 3
