from __future__ import annotations

from app.video_shorts.config import DEFAULT_EDITOR_TITLE_FONT_KEY, TITLE_FONTS
from app.video_shorts.services.clip_covers import (
    COVER_TEXT_MAX_WIDTH,
    MIN_COVER_FONT_SIZE,
    _contrast_ratio,
    _fit_cover_layout,
    _highlight_word_indexes,
    _resolve_cover_colors,
)
from app.video_shorts.services.compositor import _title_font_path_for_text


def test_light_cover_box_uses_solid_contrast_fallback_for_matching_highlight():
    base, highlight, box = _resolve_cover_colors(
        {
            "title_bg_color": "#FFFFFF",
            "title_text_color": "#000000",
            "subtitle_highlight_color": "#000000",
        }
    )

    assert base == "#000000"
    assert highlight == "#E11D48"
    assert highlight != base
    assert _contrast_ratio(highlight, box) >= 4.5


def test_dark_cover_box_uses_yellow_highlight_when_brand_color_is_missing():
    base, highlight, box = _resolve_cover_colors(
        {"title_bg_color": "#101010", "title_text_color": "#FFFFFF"}
    )

    assert base == "#FFFFFF"
    assert highlight == "#FFD400"
    assert highlight != base
    assert _contrast_ratio(highlight, box) >= 4.5


def test_cover_copy_fits_two_or_three_lines_at_thumbnail_safe_size():
    text = "Beware unregulated peptide vials"
    configured_path = str(TITLE_FONTS[DEFAULT_EDITOR_TITLE_FONT_KEY]["path"])
    font_path = _title_font_path_for_text(text, configured_path)

    rendered, font, size, lines = _fit_cover_layout(
        text, font_path=font_path, preferred_size=150,
    )

    assert rendered == text
    assert size >= MIN_COVER_FONT_SIZE
    assert 2 <= len(lines) <= 3
    assert max(font.getlength(line) for line in lines) <= COVER_TEXT_MAX_WIDTH


def test_highlight_phrase_matches_only_contiguous_whole_words():
    assert _highlight_word_indexes(
        "Beware unregulated peptide vials", "unregulated peptide"
    ) == {1, 2}
    assert _highlight_word_indexes(
        "Beware unregulated peptide vials", "regulated peptide"
    ) == set()
