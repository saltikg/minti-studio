from __future__ import annotations

import io
import pytest
import numpy as np

from PIL import Image, ImageDraw

from app.video_shorts.config import DEFAULT_EDITOR_TITLE_FONT_KEY, TITLE_FONTS
from app.video_shorts.services.clip_covers import (
    COVER_TEXT_MAX_WIDTH,
    CoverCopyDoesNotFit,
    CoverValidationError,
    MIN_COVER_FONT_SIZE,
    _cover_status_after_failure,
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
    _resolve_cover_copy,
    _tag_main_speaker,
    _uploaded_cover_image,
    _thumbnail_copy_fallback,
    normalize_cover_template,
    validate_clean_cover_text,
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
    assert normalize_cover_template("ai", brand_id="37594f93-7692-49b2-b559-2e0841fe0268") == "ai"


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


def test_auto_cover_replaces_saved_five_word_copy(monkeypatch):
    generated = {
        "cover_text": "Aileyle zaman çok sınırlı",
        "highlight_phrase": "çok sınırlı",
        "kicker": "AİLEYLE",
        "main": "ZAMAN",
        "highlight": "SINIRLI",
    }
    calls = []
    monkeypatch.setattr(
        "app.video_shorts.services.clip_covers.generate_clip_cover_copy",
        lambda transcript, *, clip_title: calls.append((transcript, clip_title)) or generated,
    )

    resolved = _resolve_cover_copy(
        cover_text="Aileyle kalan görüşme sayısı az",
        highlight="görüşme sayısı",
        kicker="ESKİ",
        main="KAYITLI",
        bold_highlight="METİN",
        transcript="Anne babayla görüşmek için kalan zaman sınırlı.",
        clip_title="Aileyle Kalan Zaman Sınırlı",
        auto_cover_followup=True,
    )

    assert resolved["cover_text"] == generated["cover_text"]
    assert len(resolved["cover_text"].split()) <= 4
    assert resolved["copy_call_used"] is True
    assert calls == [("Anne babayla görüşmek için kalan zaman sınırlı.", "Aileyle Kalan Zaman Sınırlı")]


def test_manual_five_word_cover_copy_has_clear_validation_message():
    with pytest.raises(CoverValidationError, match="^Cover text must be at most 4 words$"):
        validate_clean_cover_text("Aileyle kalan görüşme sayısı az")

    with pytest.raises(CoverValidationError, match="^Cover text must be at most 4 words$"):
        _resolve_cover_copy(
            cover_text="Aileyle kalan görüşme sayısı az",
            highlight="görüşme sayısı",
            kicker="ESKİ",
            main="KAYITLI",
            bold_highlight="METİN",
            transcript="transcript",
            clip_title="title",
            auto_cover_followup=False,
        )

    from app.video_shorts.services.render_jobs import JOB_TYPE_GENERATE_COVER
    from app.video_shorts.worker import _user_facing_terminal_media_message

    message = _user_facing_terminal_media_message(
        {"type": JOB_TYPE_GENERATE_COVER},
        CoverValidationError("Cover text must be at most 4 words"),
    )
    assert message == "Cover text must be at most 4 words"


def test_failed_cover_with_previous_storage_remains_ready(monkeypatch):
    import app.video_shorts.services.clip_covers as covers

    updates = []

    class Result:
        def __init__(self, row=None):
            self.row = row

        def fetchone(self):
            return self.row

    class Conn:
        def execute(self, sql, params):
            if sql.startswith("SELECT storage_key"):
                return Result(("brands/test/private/short-covers/1/cover.jpg",))
            updates.append((sql, params))
            return Result()

        def commit(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(covers, "get_db", Conn)

    covers._record_cover_failure(1, "owner", "brand", "generation failed")

    assert _cover_status_after_failure("brands/test/private/short-covers/1/cover.jpg") == "ready"
    assert _cover_status_after_failure("") == "failed"
    assert updates[0][1] == ["ready", "generation failed", 1, "owner", "brand"]


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


def test_manual_upload_fill_and_fit_strip_metadata():
    image = Image.new("RGB", (800, 600), "#DC241E")
    ImageDraw.Draw(image).rectangle((250, 150, 550, 450), fill="#FFFFFF")
    source = io.BytesIO()
    image.save(source, "PNG")

    filled = _uploaded_cover_image(source.getvalue(), fit_mode="fill", add_text=False,
                                   kicker="", main="", highlight="")
    fitted = _uploaded_cover_image(source.getvalue(), fit_mode="fit", add_text=False,
                                   kicker="", main="", highlight="")

    assert filled.size == fitted.size == (1080, 1920)
    assert filled.getpixel((540, 960)) == (255, 255, 255)
    assert fitted.getpixel((540, 960)) == (255, 255, 255)
    assert filled.getpixel((540, 50)) != fitted.getpixel((540, 50))
    assert not filled.getexif() and not fitted.getexif()


def test_manual_upload_rejects_spoofed_content():
    with pytest.raises(ValueError):
        _uploaded_cover_image(b"not-a-png", fit_mode="fill", add_text=False,
                              kicker="", main="", highlight="")


def test_upload_revalidates_video_tenant_before_storage(monkeypatch):
    import app.video_shorts.services.clip_covers as covers
    from app.video_shorts.services.tenant_media import TenantMediaConflict

    class Conn:
        def close(self):
            pass

    monkeypatch.setattr(covers, "get_db", lambda: Conn())
    monkeypatch.setattr(covers, "_cover_target", lambda *_args, **_kwargs: (_ for _ in ()).throw(TenantMediaConflict("mismatch")))
    monkeypatch.setattr(covers, "put_file_for_owner", lambda *_args, **_kwargs: pytest.fail("storage must not be reached"))
    with pytest.raises(TenantMediaConflict):
        covers.upload_cover(generated_video_id=1, owner_user_id="wrong", brand_id="wrong",
                            video_pk=2, data=b"image", fit_mode="fill", add_text=False)
