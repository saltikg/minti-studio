from app.video_shorts.config import SUBTITLE_PRESETS
from app.video_shorts.services.autopilot_leads import (
    OUTREACH_SUBTITLE_PRESET,
    _bold_pop_short_editor_defaults,
    _bold_pop_video_style_values,
)


def test_outreach_video_style_is_fixed_and_isolated_from_product_defaults():
    style = _bold_pop_video_style_values()

    assert style == style | {
        "subtitle_font_key": "arimo",
        "subtitle_font_size": 45,
        "subtitle_margin": 520,
        "caption_anchor": "bottom",
        "subtitle_style": "karaoke",
        "subtitle_preset": "outreach_opus",
        "subtitle_text_color": "#FFFFFF",
        "subtitle_bg_color": "#000000",
        "subtitle_bg_alpha": 85,
        "subtitle_text_alpha": 100,
    }
    assert OUTREACH_SUBTITLE_PRESET != "opus"
    assert SUBTITLE_PRESETS[OUTREACH_SUBTITLE_PRESET] == SUBTITLE_PRESETS["opus"]


def test_outreach_editor_defaults_match_fixed_video_style():
    style = _bold_pop_video_style_values()
    defaults = _bold_pop_short_editor_defaults()

    assert defaults["sub_font"] == style["subtitle_font_key"]
    assert defaults["sub_font_size"] == style["subtitle_font_size"]
    assert defaults["sub_margin"] == style["subtitle_margin"]
    assert defaults["caption_anchor"] == style["caption_anchor"]
    assert defaults["subtitle_preset"] == style["subtitle_preset"]
    assert defaults["subtitle_text_color"] == style["subtitle_text_color"]
    assert defaults["subtitle_bg_color"] == style["subtitle_bg_color"]
    assert defaults["subtitle_bg_alpha"] == style["subtitle_bg_alpha"]
