import shutil

from app.video_shorts.services.compositor import _overlay_position_enable_expr
from app.video_shorts.services import compositor, transcript_service
from app.video_shorts.services.transcript_service import _normalize_word_highlight_overlay_specs


def test_caption_overlay_timeline_bridges_small_gaps_and_holds_last_event():
    specs = [
        {"path": "first.png", "start": 1.0, "end": 1.2},
        {"path": "second.png", "start": 1.45, "end": 1.7},
        {"path": "last.png", "start": 2.0, "end": 2.82},
    ]

    normalized = _normalize_word_highlight_overlay_specs(specs, clip_duration=2.89)

    assert [(item["start"], item["end"]) for item in normalized] == [
        (1.0, 1.45),
        (1.45, 1.7),
        (2.0, 2.89),
    ]


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


def test_caption_builder_leaves_a_long_word_gap_blank(monkeypatch):
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
            (0.0, 0.2),
            (0.88, 1.2),
        ]
    finally:
        for path in cleanup:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)


def test_deferred_caption_path_encodes_exactly_once_in_compositor(monkeypatch):
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
        defer_overlay_video=True,
    )
    positioned_temp_dir = None
    try:
        assert overlay is None
        assert encode_calls == []
        monkeypatch.setattr(compositor, "_build_word_highlight_overlay_video", _fake_encode)
        overlay, _origin, positioned_temp_dir, _count = (
            compositor._build_compact_positioned_caption_overlay(
                meta["specs"],
                overlay_events=meta["timing_events"],
                layout_segments=[{"start": 0.0, "end": 1.0, "mode": "split"}],
                target_width=200,
                target_height=200,
                duration=1.0,
                subtitle_font_size=40,
                subtitle_margin=60,
                subtitle_margin_custom=False,
                caption_anchor="bottom",
            )
        )
        caption_inputs = [overlay]
        assert encode_calls == [1]
        assert len(caption_inputs) <= 1
    finally:
        if positioned_temp_dir:
            shutil.rmtree(positioned_temp_dir, ignore_errors=True)
        for path in cleanup:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
