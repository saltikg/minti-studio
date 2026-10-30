from pathlib import Path

from app.video_shorts.config import SUBTITLE_PILL_FONT_MAP, SUBTITLE_PRESETS, SUB_FONT_CHOICES
from app.video_shorts.services.transcript_service import (
    _pillow_subtitle_font_path,
    _subtitle_font_path_for_key,
)


def test_every_pillow_preset_has_an_existing_font_file():
    pillow_presets = {
        key: preset
        for key, preset in SUBTITLE_PRESETS.items()
        if bool(preset.get("pillow_engine"))
    }
    assert pillow_presets
    for preset_key in pillow_presets:
        assert preset_key in SUBTITLE_PILL_FONT_MAP
        assert Path(SUBTITLE_PILL_FONT_MAP[preset_key]["font_path"]).is_file()


def test_every_caption_font_key_resolves_to_an_existing_pillow_font():
    for font_key, _label, _font_path, _font_name in SUB_FONT_CHOICES:
        resolved = _subtitle_font_path_for_key(font_key)
        assert resolved is not None
        assert resolved.is_file()


def test_explicit_caption_font_does_not_require_a_preset_font_map():
    font_key = SUB_FONT_CHOICES[0][0]
    expected = _subtitle_font_path_for_key(font_key)
    resolved, error = _pillow_subtitle_font_path(font_key, "preset-without-a-font-map")
    assert resolved == expected
    assert error is None


def test_effect_path_resolves_a_font_even_without_a_font_key_or_preset_map():
    resolved, error = _pillow_subtitle_font_path(None, "clean_black")
    assert resolved is not None
    assert resolved.is_file()
    assert error is None
