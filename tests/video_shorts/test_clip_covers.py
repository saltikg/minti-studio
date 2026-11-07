from __future__ import annotations

import pytest
import numpy as np

from PIL import Image, ImageDraw

from app.video_shorts.config import DEFAULT_EDITOR_TITLE_FONT_KEY, TITLE_FONTS
from app.video_shorts.services.clip_covers import (
    COVER_TEXT_MAX_WIDTH,
    CoverCopyDoesNotFit,
    MIN_COVER_FONT_SIZE,
    _contrast_ratio,
    _fit_cover_layout,
    _fit_bold_main,
    _fit_subject_below_text,
    _draw_bold_text,
    _eye_aspect_ratio,
    _highlight_word_indexes,
    _mouth_region_is_obscured,
    _locale_upper,
    _resolve_cover_colors,
    _tag_main_speaker,
    _thumbnail_copy_fallback,
    normalize_cover_template,
)
from app.video_shorts.services.clip_title import generate_clip_cover_copy
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


def test_long_copy_requests_a_rewrite_instead_of_silently_dropping_words():
    text = "Kıtlık metabolizmasıyla kilo verilemiyor"
    configured_path = str(TITLE_FONTS["montserrat_black_italic"]["path"])
    font_path = _title_font_path_for_text(text, configured_path)

    with pytest.raises(CoverCopyDoesNotFit):
        _fit_cover_layout(text, font_path=font_path, preferred_size=140)

    fallback = _thumbnail_copy_fallback(text)
    assert fallback == "Kıtlık kilo verilemiyor"


def test_missing_mouth_contrast_marks_face_as_occluded():
    assert _mouth_region_is_obscured("neutral", 0.0087) is True
    assert _mouth_region_is_obscured("neutral", 0.0256) is False
    assert _mouth_region_is_obscured("unknown", 0.0) is False


def test_brand_cover_template_defaults_are_explicit():
    assert normalize_cover_template(None, brand_id="37594f93-7692-49b2-b559-2e0841fe0268") == "bold"
    assert normalize_cover_template(None, brand_id="3dab32b8-a17a-4b96-a027-e49774813247") == "clean"
    assert normalize_cover_template("clean", brand_id="37594f93-7692-49b2-b559-2e0841fe0268") == "clean"


def test_bold_copy_fallback_obeys_both_template_limits(monkeypatch):
    monkeypatch.setattr("app.video_shorts.services.clip_title._openai_client", None)
    copy = generate_clip_cover_copy(
        "Kıtlık metabolizması kilo vermeyi neden durduruyor?",
        clip_title="Kıtlık metabolizması kilo vermeyi durduruyor",
    )

    assert len(copy["cover_text"].split()) <= 4
    assert 1 <= len(copy["main"].split()) <= 2
    assert 1 <= len(copy["highlight"].split()) <= 3
    assert sum(len(copy[key].split()) for key in ("kicker", "main", "highlight")) <= 6
    assert _locale_upper("kıtlık metabolizması", turkish=True) == "KITLIK METABOLİZMASI"


def test_main_speaker_cluster_rejects_a_brief_different_face():
    candidates = [
        {"face": {"x": 1}, "_speaker_feature": np.array([1.0, 0.0]), "eligible": True, "score": 10, "face_sharpness": 100},
        {"face": {"x": 2}, "_speaker_feature": np.array([0.99, 0.01]), "eligible": True, "score": 11, "face_sharpness": 110},
        {"face": {"x": 3}, "_speaker_feature": np.array([0.0, 1.0]), "eligible": True, "score": 12, "face_sharpness": 120},
    ]

    _tag_main_speaker(candidates)

    assert candidates[0]["main_speaker"] is True
    assert candidates[1]["main_speaker"] is True
    assert candidates[2]["main_speaker"] is False
    assert candidates[2]["eligible"] is False
    assert "different_speaker" in candidates[2]["rejection_reasons"]


def test_long_two_word_bold_main_wraps_instead_of_clipping():
    draw = ImageDraw.Draw(Image.new("RGB", (1080, 1920)))
    font_path = str(TITLE_FONTS["oswald_bold"]["path"])

    font, size, lines = _fit_bold_main(draw, "KÜÇÜK ATIŞTIRMALIKLAR", font_path)

    assert size >= 200
    assert lines == ["KÜÇÜK", "ATIŞTIRMALIKLAR"]


def test_eye_aspect_ratio_rejects_a_partly_closed_eye():
    from types import SimpleNamespace

    points = [SimpleNamespace(x=0.0, y=0.0) for _ in range(6)]
    for index, pair in enumerate(((0, 0), (2, -1), (8, -1), (10, 0), (8, 1), (2, 1))):
        points[index] = SimpleNamespace(x=pair[0], y=pair[1])
    open_ratio = _eye_aspect_ratio(points, (0, 1, 2, 3, 4, 5))
    points[1] = SimpleNamespace(x=2, y=-0.4)
    points[2] = SimpleNamespace(x=8, y=-0.4)
    points[4] = SimpleNamespace(x=8, y=0.4)
    points[5] = SimpleNamespace(x=2, y=0.4)
    assert _eye_aspect_ratio(points, (0, 1, 2, 3, 4, 5)) < 0.60 * open_ratio


def test_bold_text_starts_below_chin_after_subject_is_moved():
    subject = Image.new("RGBA", (1080, 1920), (200, 180, 150, 255))
    face = {"x": 300, "y": 600, "w": 500, "h": 900, "chin_y": 1550,
            "landmarks": {"left_eye": {"x": 450, "y": 780}, "right_mouth": {"x": 550, "y": 1400}}}
    moved, fitted = _fit_subject_below_text(subject, face, 650)
    assert moved.size == (1080, 1920)
    assert fitted["y"] >= 60
    assert fitted["chin_y"] + 40 <= 1920 - 650 - 115
    assert fitted["landmarks"]["right_mouth"]["y"] < fitted["chin_y"]


def test_bold_text_refuses_to_clamp_back_over_face():
    image = Image.new("RGB", (1080, 1920), "#0B0B0D")
    with pytest.raises(CoverCopyDoesNotFit):
        _draw_bold_text(
            image, kicker="", main="KİLO", highlight="ENGELİ",
            cover_face={"x": 300, "y": 1050, "w": 500, "h": 600, "chin_y": 1650},
        )
