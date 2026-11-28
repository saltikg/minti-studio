import shutil
from pathlib import Path

import pytest
from PIL import Image, ImageFont

from app.video_shorts.routes import generation
from app.video_shorts.config import SUBTITLE_PRESETS
from app.video_shorts.services.compositor import _overlay_position_enable_expr
from app.video_shorts.services import compositor, transcript_service
from app.video_shorts.services.design_preview import measure_design
from app.video_shorts.services.transcript_service import _normalize_word_highlight_overlay_specs


@pytest.mark.parametrize("caption_anchor", ["top", "center", "bottom"])
def test_preview_and_render_visual_boxes_match_for_every_caption_anchor(
    caption_anchor, tmp_path,
):
    font_path = Path("app/video_shorts/static/fonts/Montserrat-BlackItalic.ttf")
    settings = {
        "width": 720,
        "height": 1280,
        "title": "The Peptide Safety Risk Every Woman Should Know",
        "title_size": 45,
        "title_top": 714,
        "title_bg_alpha": 100,
        "caption_size": 44,
        "caption_margin": 884,
        "caption_anchor": caption_anchor,
        "caption_style": "karaoke",
        "caption_source": "contraindication bioidentical peptide safety",
        "caption": "contraindication bioidentical peptide safety",
        "preset": "podcast",
        "subtitle_effect": "none",
        "split_caption": False,
        "active_word": 0,
    }
    behavior = {
        "title_uppercase": False,
        "title_outline_color": "#000000",
        "title_outline_width": 0,
    }
    preview = measure_design(
        settings,
        title_font_path=font_path,
        caption_font_path=font_path,
        title_behavior=behavior,
    )

    title_path, title_meta = compositor._render_title_overlay(
        settings["title"], font_path=str(font_path), font_size=settings["title_size"],
        target_width=720, target_height=1280, base_y=settings["title_top"],
        line_spacing=-4, title_text_color="#FFFFFF", subtitle_preset="podcast",
        title_bg_alpha=100, uppercase=False, video_top_limit=None,
        title_outline_color="#000000", title_outline_width=0, crop_to_bounds=True,
    )
    try:
        with Image.open(title_path) as title_image:
            render_title_box = [
                title_meta["origin_x"], title_meta["origin_y"],
                title_meta["origin_x"] + title_image.width,
                title_meta["origin_y"] + title_image.height,
            ]
        assert render_title_box[1] == pytest.approx(preview["title"]["box"][1], abs=4)
        assert render_title_box[3] == pytest.approx(preview["title"]["box"][3], abs=4)
    finally:
        title_path.unlink(missing_ok=True)

    preset = SUBTITLE_PRESETS["podcast"]
    font = ImageFont.truetype(str(font_path), settings["caption_size"])
    active_font = ImageFont.truetype(
        str(font_path),
        settings["caption_size"] + int(preset.get("active_font_size_delta") or 0),
    )
    caption_path = tmp_path / f"caption-{caption_anchor}.png"
    render_caption = transcript_service._render_word_highlight_caption_frame(
        settings["caption_source"].split()[:4], active_index=0, font=font,
        active_font=active_font, subtitle_margin=settings["caption_margin"],
        caption_anchor=caption_anchor, font_size=settings["caption_size"],
        inactive_color="#FFFFFF", active_color="#FFFF00", outline_color="#000000",
        outline_width=0, pill_color="#FFFF00", caption_bg_color="#000000",
        caption_bg_alpha=100, draw_pill=False, stable_highlight_geometry=True,
        canvas_width=720, canvas_height=1280, out_path=caption_path,
    )
    with Image.open(caption_path) as caption_image:
        render_caption_box = list(caption_image.getbbox())
    assert render_caption_box[1] == pytest.approx(preview["caption"]["box"][1], abs=4)
    assert render_caption_box[3] == pytest.approx(preview["caption"]["box"][3], abs=4)
    assert len(render_caption["layout"]["lines"]) == preview["caption"]["lines"]
    assert render_caption["line_glyph_boxes"] == preview["caption"]["line_glyph_boxes"]


def test_caption_overlay_timeline_holds_short_chunk_gaps_and_last_event():
    specs = [
        {"path": "first.png", "start": 1.0, "end": 1.2},
        {"path": "second.png", "start": 1.45, "end": 1.7},
        {"path": "last.png", "start": 2.0, "end": 2.82},
    ]

    normalized = _normalize_word_highlight_overlay_specs(specs, clip_duration=2.89)

    assert [(item["start"], item["end"]) for item in normalized] == [
        (1.0, 1.45),
        (1.45, 2.0),
        (2.0, 2.89),
    ]


def test_caption_overlay_timeline_holds_every_pause_inside_a_chunk():
    specs = [
        {"path": "first.png", "start": 0.0, "end": 0.2, "chunk_index": 0},
        {"path": "second.png", "start": 2.8, "end": 3.1, "chunk_index": 0},
        {"path": "next.png", "start": 5.0, "end": 5.3, "chunk_index": 1},
    ]

    normalized = _normalize_word_highlight_overlay_specs(specs, clip_duration=6.0)

    assert [value for item in normalized for value in (item["start"], item["end"])] == pytest.approx([
        0.0, 2.8,
        2.8, 3.3,
        5.0, 6.0,
    ])


def test_caption_overlay_timeline_orders_events_and_removes_overlaps():
    specs = [
        {"path": "last.png", "start": 1.7, "end": 2.0},
        {"path": "first.png", "start": 1.0, "end": 1.8},
        {"path": "duplicate.png", "start": 1.7, "end": 1.75},
    ]

    normalized = _normalize_word_highlight_overlay_specs(specs, clip_duration=2.1)

    assert [item["path"] for item in normalized] == ["first.png", "last.png"]
    assert all(
        float(left["end"]) <= float(right["start"])
        for left, right in zip(normalized, normalized[1:])
    )
    assert normalized[-1]["end"] == 2.1


def test_caption_position_enable_timeline_hides_real_gaps_and_covers_layout_slices():
    expression = _overlay_position_enable_expr(
        [
            (0.0, 1.0, 400.0),
            (1.0, 2.0, 600.0),
            (2.68, 3.5, 600.0),
        ],
        duration=3.5,
    )

    assert expression == "between(t,0.000000,2.000000)+between(t,2.680000,3.500000)"
    assert "2.000000,2.680000" not in expression


def test_caption_position_enable_timeline_hides_overlay_without_a_position():
    assert _overlay_position_enable_expr([], duration=2.0) == "0"


def test_caption_placement_uses_fixed_chunk_geometry_not_active_paint_bbox():
    first = compositor._face_aware_event_caption_block(
        {
            "layout": {"visual_top": 1180, "visual_height": 180},
            "block_bbox": [100, 1190, 900, 1280],
        },
        fallback_top=0,
        fallback_height=100,
    )
    second = compositor._face_aware_event_caption_block(
        {
            "layout": {"visual_top": 1180, "visual_height": 180},
            "block_bbox": [100, 1250, 900, 1350],
        },
        fallback_top=0,
        fallback_height=100,
    )

    assert first == second == {"top": 1180.0, "height": 180.0, "center_y": 1270.0}


def test_preview_chunk_geometry_is_stable_across_highlight_states():
    font_path = Path("app/video_shorts/static/fonts/Montserrat-BlackItalic.ttf")
    base = {
        "width": 720,
        "height": 1280,
        "title": "",
        "title_size": 40,
        "title_top": 80,
        "caption_size": 54,
        "caption_margin": 320,
        "caption_anchor": "bottom",
        "caption_style": "karaoke",
        "caption_source": "uygulayamıyorsanız şöyle yapın o",
        "caption": "uygulayamıyorsanız şöyle yapın o",
        "preset": "podcast",
        "split_caption": True,
    }
    behavior = {
        "title_uppercase": False,
        "title_outline_color": "#000000",
        "title_outline_width": 0,
    }

    first = measure_design(
        {**base, "active_word": 0},
        title_font_path=font_path,
        caption_font_path=font_path,
        title_behavior=behavior,
    )["caption"]
    third = measure_design(
        {**base, "active_word": 2},
        title_font_path=font_path,
        caption_font_path=font_path,
        title_behavior=behavior,
    )["caption"]

    assert first["lines"] == third["lines"]
    assert first["line_glyph_boxes"] == third["line_glyph_boxes"]
    assert [(run["x"], run["y"], run["line_index"]) for run in first["runs"]] == [
        (run["x"], run["y"], run["line_index"]) for run in third["runs"]
    ]


def test_caption_builder_keeps_one_chunk_visible_across_a_long_word_gap(monkeypatch):
    monkeypatch.setattr(
        transcript_service,
        "_render_word_highlight_caption_frame",
        lambda *args, **kwargs: {"block_bbox": [0, 0, 100, 50]},
    )
    monkeypatch.setattr(
        transcript_service,
        "_build_word_highlight_overlay_video",
        lambda *, temp_dir, **kwargs: (temp_dir / "overlay.mov", (0, 0)),
    )

    _overlay, cleanup, meta = transcript_service._build_word_highlight_caption_overlay(
        [
            {
                "start": 0.0,
                "end": 1.0,
                "text": "training sleep",
                "words": [
                    {"word": "training", "start": 0.0, "end": 0.2},
                    {"word": "sleep", "start": 0.88, "end": 1.0},
                ],
            }
        ],
        0.0,
        1.2,
        subtitle_font_size=40,
        subtitle_margin=200,
        subtitle_preset="podcast",
        subtitle_font_key="montserrat_black_italic",
        target_width=1080,
        target_height=1920,
    )
    try:
        assert [(event["start"], event["end"]) for event in meta["timing_events"]] == [
            (0.0, 0.88),
            (0.88, 1.2),
        ]
    finally:
        for path in cleanup:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)


def test_caption_path_encodes_exactly_once_before_compositor(monkeypatch):
    encode_calls = []

    def _fake_render(*args, **kwargs):
        from PIL import Image

        Image.new("RGBA", (200, 200), (0, 0, 0, 0)).save(kwargs["out_path"])
        return {
            "block_bbox": [20, 100, 180, 120],
            "line_glyph_boxes": [[20, 100, 180, 120]],
        }

    def _fake_encode(*, temp_dir, overlay_specs, **kwargs):
        encode_calls.append(len(overlay_specs))
        output = temp_dir / "word_highlight_overlay.mov"
        output.touch()
        return output, (0, 0)

    monkeypatch.setattr(transcript_service, "_render_word_highlight_caption_frame", _fake_render)
    monkeypatch.setattr(transcript_service, "_build_word_highlight_overlay_video", _fake_encode)
    overlay, cleanup, meta = transcript_service._build_word_highlight_caption_overlay(
        [{
            "start": 0.0,
            "end": 1.0,
            "words": [{"word": "hello", "start": 0.0, "end": 1.0}],
        }],
        0.0,
        1.0,
        subtitle_font_size=40,
        subtitle_margin=60,
        subtitle_preset="podcast",
        subtitle_font_key="montserrat_black_italic",
        target_width=200,
        target_height=200,
        defer_overlay_video=False,
    )
    try:
        assert overlay is not None
        assert encode_calls == [1]
        caption_inputs = [overlay]
        assert len(caption_inputs) <= 1
        assert meta["overlay_video_deferred"] is False
    finally:
        for path in cleanup:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)


def test_hook_caption_frames_cover_hook_and_natural_occurrence() -> None:
    words = [
        {"word": f"word-{index}", "start": float(index), "end": float(index) + 0.7, "index": index}
        for index in range(12)
    ]
    segments = [{
        "start": 0.0,
        "end": 12.0,
        "text": " ".join(item["word"] for item in words),
        "words": words,
    }]
    ranges = [
        {
            "start": 4.0, "end": 8.0, "start_word_index": 4, "end_word_index": 7,
            "role": "hook", "transition": "flash",
        },
        {"start": 0.0, "end": 12.0, "start_word_index": 0, "end_word_index": 11},
    ]

    remapped = generation._remap_transcript_segments_to_keep_ranges(segments, ranges)
    assert [len(segment["words"]) for segment in remapped] == [4, 12]
    repeated = [f"word-{index}" for index in range(4, 8)]
    assert [word["word"] for word in remapped[0]["words"]] == repeated
    assert [word["word"] for word in remapped[1]["words"]][4:8] == repeated
    assert remapped[0]["words"][0]["start"] == pytest.approx(0.0)
    assert remapped[1]["words"][4]["start"] == pytest.approx(8.0)

    _overlay, cleanup, meta = transcript_service._build_word_highlight_caption_overlay(
        remapped,
        0.0,
        16.0,
        subtitle_font_size=40,
        subtitle_margin=300,
        subtitle_preset="podcast",
        subtitle_font_key="montserrat_black_italic",
        subtitle_effect="outline_shadow",
        target_width=720,
        target_height=1280,
        defer_overlay_video=True,
    )
    try:
        specs = meta.get("specs") or []
        assert any(float(spec["start"]) < 4.0 for spec in specs)
        assert any(4.0 <= float(spec["start"]) < 16.0 for spec in specs)
        assert all(Path(spec["path"]).exists() for spec in specs)
    finally:
        for path in cleanup:
            path = Path(path)
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
