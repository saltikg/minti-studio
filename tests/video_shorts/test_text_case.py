from pathlib import Path

from flask import Flask, g

from app.video_shorts.config import DEFAULT_SUBTITLE_PRESET, SUB_FONT_CHOICES, TITLE_FONTS
from app.video_shorts.routes.generation import design_layout_metrics
from app.video_shorts.services.text_case import uppercase_text


def test_turkish_uppercase_text():
    assert uppercase_text("bir şey yapacağım", "tr") == "BİR ŞEY YAPACAĞIM"
    assert uppercase_text("kısa", "turkish") == "KISA"


def test_non_turkish_uppercase_text():
    assert uppercase_text("mixed Case", "en") == "MIXED CASE"


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
        "active_word": 0,
    }
    app = Flask(__name__)
    with app.test_request_context("/api/design-layout", method="POST", json=data):
        g.vs_current_user = {"id": "test-user"}
        payload = design_layout_metrics().get_json()

    assert " ".join(run["text"] for run in payload["title"]["runs"]) == "BİR ŞEY YAPACAĞIM"
    assert payload["caption"]["text"] == "BİR ŞEY YAPACAĞIM"
