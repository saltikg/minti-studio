from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from flask import Flask
from PIL import Image

from app.video_shorts.services import compositor
from app.video_shorts.services.caption_geometry import split_caption_line_offsets


FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


@pytest.mark.parametrize(
    ("target_width", "target_height", "custom_position", "expected_split_top", "expected_fill_top"),
    [
        (720, 1280, 857, 503.2, 832.0),
        (1080, 1920, 1286, 816.8, 1248.0),
    ],
)
def test_auto_caption_policy_is_mode_specific_with_custom_position(
    target_width: int,
    target_height: int,
    custom_position: int,
    expected_split_top: float,
    expected_fill_top: float,
) -> None:
    block_height = 124.0
    scale = target_height / 1280.0
    fill_face_bottom = 700.0 * scale

    split_top = compositor._face_aware_caption_top_for_block(
        {"mode": "split"},
        block_height=block_height,
        target_width=target_width,
        target_height=target_height,
        subtitle_margin=custom_position,
        subtitle_margin_custom=True,
        caption_anchor="top",
    )
    fill_top = compositor._face_aware_caption_top_for_block(
        {"mode": "fill", "caption_face_bottom": fill_face_bottom, "auto_layout": True},
        block_height=block_height,
        target_width=target_width,
        target_height=target_height,
        subtitle_margin=custom_position,
        subtitle_margin_custom=True,
        caption_anchor="top",
    )
    fit_top = compositor._face_aware_caption_top_for_block(
        {"mode": "fit", "auto_layout": True},
        block_height=block_height,
        target_width=target_width,
        target_height=target_height,
        subtitle_margin=custom_position,
        subtitle_margin_custom=True,
        caption_anchor="top",
    )

    assert split_top == pytest.approx(expected_split_top)
    assert fill_top == expected_fill_top
    assert fill_top > fill_face_bottom
    fit_metrics = compositor._face_aware_fit_position_metrics(
        target_width=target_width,
        target_height=target_height,
        subtitle_font_size=int(block_height / 2),
    )
    expected_fit_top = fit_metrics["frame_bottom"] + (
        compositor.DESIGN_GEOMETRY["fit_caption_gap"] * target_height / compositor.DESIGN_GEOMETRY["height"]
    )
    assert fit_top == pytest.approx(expected_fit_top)


@pytest.mark.parametrize("block_height", [62.0, 124.0])
def test_split_caption_bottom_edge_is_fixed_above_divider(block_height: float) -> None:
    top = compositor._face_aware_split_caption_top(
        block_height=block_height,
        target_height=1280,
    )

    expected_bottom = 1280 * (
        compositor.DESIGN_GEOMETRY["split_ratio"]
        - compositor.DESIGN_GEOMETRY["split_caption_gap_ratio"]
    )
    assert top + block_height == pytest.approx(expected_bottom)


def test_custom_split_caption_position_is_ignored() -> None:
    automatic = compositor._face_aware_caption_top_for_block(
        {"mode": "split"},
        block_height=124.0,
        target_width=720,
        target_height=1280,
        subtitle_margin=400,
        subtitle_margin_custom=False,
    )
    custom = compositor._face_aware_caption_top_for_block(
        {"mode": "split"},
        block_height=124.0,
        target_width=720,
        target_height=1280,
        subtitle_margin=900,
        subtitle_margin_custom=True,
        caption_anchor="top",
    )
    assert custom == pytest.approx(automatic)


@pytest.mark.parametrize(
    ("boxes", "expected"),
    [
        ([[10, 100, 100, 120]], [507.2]),
        ([[10, 100, 100, 120], [10, 140, 100, 160]], [507.2, 512.8]),
        (
            [[10, 80, 100, 100], [10, 110, 100, 130], [10, 150, 100, 170]],
            [497.2, 497.2, 502.8],
        ),
    ],
)
def test_split_caption_line_offsets_use_measured_glyph_edges(boxes, expected) -> None:
    assert split_caption_line_offsets(boxes, frame_height=1280) == pytest.approx(expected)


def test_split_caption_raster_places_two_lines_around_divider(tmp_path: Path) -> None:
    source_path = tmp_path / "caption.png"
    image = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
    for top, bottom in ((100, 120), (140, 160)):
        for y in range(top, bottom):
            for x in range(20, 180):
                image.putpixel((x, y), (255, 255, 255, 255))
    image.save(source_path)

    output_path = compositor._split_caption_overlay_frame(
        source_path,
        metrics={"line_glyph_boxes": [[20, 100, 180, 120], [20, 140, 180, 160]]},
        target_height=200,
    )

    with Image.open(output_path) as output:
        alpha = output.getchannel("A")
        assert alpha.crop((0, 97, 200, 98)).getbbox() is not None
        assert alpha.crop((0, 98, 200, 102)).getbbox() is None
        assert alpha.crop((0, 102, 200, 103)).getbbox() is not None


def test_split_caption_overlay_specs_use_transformed_frame(tmp_path: Path) -> None:
    source_path = tmp_path / "caption.png"
    Image.new("RGBA", (200, 200), (255, 255, 255, 0)).save(source_path)

    specs = compositor._face_aware_subtitle_overlay_specs(
        [{"path": source_path, "start": 0.0, "end": 1.0}],
        overlay_events=[{
            "start": 0.0,
            "end": 1.0,
            "metrics": {"line_glyph_boxes": [[20, 100, 180, 120]]},
        }],
        layout_segments=[{"start": 0.0, "end": 1.0, "mode": "split"}],
        target_width=200,
        target_height=200,
        subtitle_font_size=40,
        subtitle_margin=60,
        subtitle_margin_custom=True,
        caption_anchor="top",
    )

    assert len(specs) == 1
    assert specs[0]["path"] != source_path
    assert specs[0]["path"].exists()
    assert specs[0]["y_offset"] == 0.0


def test_manual_custom_caption_position_is_unchanged() -> None:
    assert compositor.caption_block_top(857, "top", 124, 1280) == 857
    assert compositor.caption_block_top(857, "bottom", 124, 1280) == 299


def test_default_locked_crop_is_not_a_manual_reframe_and_tracks_face(tmp_path: Path) -> None:
    assert not compositor._has_manual_reframe(
        crop_locked=True,
        crop_x=0.0,
        crop_y=0.0,
        crop_w=1.0,
        crop_h=1.0,
    )
    fill_x, fill_y, fill_w, fill_h = compositor._fill_crop_box_for_canvas(
        crop_x=0.0,
        crop_y=0.0,
        crop_w=1.0,
        crop_h=1.0,
        is_default_crop=True,
        source_width=1280,
        source_height=720,
        target_width=1080,
        target_height=1920,
    )
    assert (fill_x, fill_y, fill_w, fill_h) == pytest.approx(
        (0.341796875, 0.0, 0.31640625, 1.0)
    )

    track_path = tmp_path / "track.json"
    track_path.write_text(
        json.dumps(
            [
                {"t": 0.0, "cx_ratio": 0.22, "cy_ratio": 0.4, "w_ratio": 0.12, "h_ratio": 0.2},
                {"t": 1.0, "cx_ratio": 0.78, "cy_ratio": 0.4, "w_ratio": 0.12, "h_ratio": 0.2},
            ]
        ),
        encoding="utf-8",
    )
    points = compositor._load_face_track_points(
        track_path,
        clip_start=0.0,
        crop_w=fill_w,
        crop_h=fill_h,
    )

    assert len(points) == 2
    for point in points:
        crop_left = point["x"]
        crop_right = crop_left + fill_w
        face_left = point["cx"] - point["w"] / 2.0
        face_right = point["cx"] + point["w"] / 2.0
        assert crop_left <= point["cx"] <= crop_right
        assert crop_left <= face_left and face_right <= crop_right


def test_locked_non_default_crop_is_manual_and_preserved_exactly() -> None:
    manual = (0.11, 0.07, 0.42, 0.81)
    assert compositor._has_manual_reframe(
        crop_locked=True,
        crop_x=manual[0],
        crop_y=manual[1],
        crop_w=manual[2],
        crop_h=manual[3],
    )
    assert compositor._fill_crop_box_for_canvas(
        crop_x=manual[0],
        crop_y=manual[1],
        crop_w=manual[2],
        crop_h=manual[3],
        is_default_crop=False,
        source_width=1280,
        source_height=720,
        target_width=1080,
        target_height=1920,
    ) == manual


def test_no_track_keeps_existing_center_fill_fallback() -> None:
    fill_x, fill_y, fill_w, fill_h = compositor._fill_crop_box_for_canvas(
        crop_x=0.0,
        crop_y=0.0,
        crop_w=1.0,
        crop_h=1.0,
        is_default_crop=True,
        source_width=1280,
        source_height=720,
        target_width=1080,
        target_height=1920,
    )
    assert fill_x == pytest.approx((1.0 - fill_w) / 2.0)
    assert fill_y == pytest.approx((1.0 - fill_h) / 2.0)


@pytest.mark.parametrize("face_height", [0.28, 0.42, 0.46])
def test_fill_zoom_defaults_to_one_for_normal_face_sizes(face_height: float) -> None:
    zoom = compositor._face_aware_fill_zoom_for_segment(
        [{"t": 0.5, "h": face_height, "w": 0.12}],
        segment_start=0.0,
        segment_end=1.0,
        crop_w=0.31640625,
        crop_h=1.0,
    )

    assert zoom == 1.0


def test_fill_zoom_for_small_face_is_bounded_and_keeps_headroom_and_side_margin() -> None:
    point = {"t": 0.5, "cx": 0.52, "cy": 0.08, "w": 0.05, "h": 0.07, "found": True}
    base_crop_w = 0.31640625
    zoom = compositor._face_aware_fill_zoom_for_segment(
        [point],
        segment_start=0.0,
        segment_end=1.0,
        crop_w=base_crop_w,
        crop_h=1.0,
    )
    crop_w = base_crop_w / zoom
    crop_h = 1.0 / zoom
    tracked = compositor._stabilize_face_track_crop([point], crop_w=crop_w, crop_h=crop_h)[0]
    face_left = point["cx"] - point["w"] / 2.0
    face_right = point["cx"] + point["w"] / 2.0
    face_top = point["cy"] - point["h"] / 2.0

    assert 1.0 < zoom <= 1.2
    assert tracked["y"] <= face_top
    assert face_left >= tracked["x"] + crop_w * 0.15
    assert face_right <= tracked["x"] + crop_w * 0.85


def test_face_track_dead_zone_does_not_move_for_small_face_moves() -> None:
    points = [
        {"t": 0.0, "cx": 0.50, "cy": 0.40, "w": 0.08, "h": 0.20, "found": True},
        {"t": 0.5, "cx": 0.51, "cy": 0.40, "w": 0.08, "h": 0.20, "found": True},
        {"t": 1.0, "cx": 0.52, "cy": 0.40, "w": 0.08, "h": 0.20, "found": True},
    ]

    tracked = compositor._stabilize_face_track_crop(points, crop_w=0.31640625, crop_h=1.0)

    assert [point["x"] for point in tracked] == pytest.approx([tracked[0]["x"]] * 3)


def test_face_track_jumps_at_cut_then_eases_only_within_new_shot() -> None:
    points = [
        {"t": 0.0, "cx": 0.20, "cy": 0.40, "w": 0.08, "h": 0.20, "found": True},
        {"t": 0.5, "cx": 0.21, "cy": 0.40, "w": 0.08, "h": 0.20, "found": True},
        {"t": 1.0, "cx": 0.80, "cy": 0.40, "w": 0.08, "h": 0.20, "found": True, "cut": True},
        {"t": 1.5, "cx": 0.82, "cy": 0.40, "w": 0.08, "h": 0.20, "found": True},
        {"t": 2.0, "cx": 0.90, "cy": 0.40, "w": 0.08, "h": 0.20, "found": True},
    ]

    tracked = compositor._stabilize_face_track_crop(points, crop_w=0.31640625, crop_h=1.0)

    assert tracked[1]["x"] == pytest.approx(tracked[0]["x"])
    assert tracked[2]["x"] == pytest.approx(0.80 - (0.31640625 / 2.0))
    assert tracked[2]["x"] != pytest.approx((tracked[0]["x"] + tracked[2]["x"]) / 2.0)
    assert tracked[3]["x"] == pytest.approx(tracked[2]["x"])
    assert tracked[2]["x"] < tracked[4]["x"] < 1.0 - 0.31640625


def test_face_track_cut_looks_ahead_to_first_confirmed_face() -> None:
    points = [
        {"t": 0.0, "cx": 0.20, "cy": 0.40, "w": 0.08, "h": 0.20, "found": True},
        {"t": 1.0, "cx": 0.20, "cy": 0.40, "w": 0.08, "h": 0.20, "found": False, "cut": True},
        {"t": 1.5, "cx": 0.78, "cy": 0.40, "w": 0.08, "h": 0.20, "found": True},
    ]

    tracked = compositor._stabilize_face_track_crop(points, crop_w=0.31640625, crop_h=1.0)

    assert tracked[1]["x"] == pytest.approx(0.78 - (0.31640625 / 2.0))
    assert tracked[2]["x"] == pytest.approx(tracked[1]["x"])

    expression = compositor._piecewise_linear_ratio_expr(tracked, "x")
    assert expression is not None
    assert "if(lt(t,1.000000)" in expression


def test_face_track_holds_position_during_gap_and_on_resume() -> None:
    points = [
        {"t": 0.0, "cx": 0.30, "cy": 0.40, "w": 0.08, "h": 0.20, "found": True},
        {"t": 0.5, "cx": 0.30, "cy": 0.40, "w": 0.08, "h": 0.20, "found": False},
        {"t": 1.0, "cx": 0.30, "cy": 0.40, "w": 0.08, "h": 0.20, "found": False},
        {"t": 1.5, "cx": 0.80, "cy": 0.40, "w": 0.08, "h": 0.20, "found": True},
        {"t": 2.0, "cx": 0.80, "cy": 0.40, "w": 0.08, "h": 0.20, "found": True},
    ]

    tracked = compositor._stabilize_face_track_crop(points, crop_w=0.31640625, crop_h=1.0)

    assert [point["x"] for point in tracked[:4]] == pytest.approx([tracked[0]["x"]] * 4)
    assert tracked[4]["x"] > tracked[3]["x"]


def test_face_track_loader_holds_rows_with_missing_detection_coordinates(tmp_path: Path) -> None:
    track_path = tmp_path / "track_with_gap.json"
    track_path.write_text(
        json.dumps(
            [
                {"t": 0.0, "cx_ratio": 0.30, "cy_ratio": 0.40, "w_ratio": 0.08, "h_ratio": 0.20, "found": True},
                {"t": 0.5, "cx_ratio": None, "cy_ratio": None, "found": False},
                {"t": 1.0, "cx_ratio": None, "cy_ratio": None, "found": False},
                {"t": 1.5, "cx_ratio": 0.80, "cy_ratio": 0.40, "w_ratio": 0.08, "h_ratio": 0.20, "found": True},
                {"t": 2.0, "cx_ratio": 0.80, "cy_ratio": 0.40, "w_ratio": 0.08, "h_ratio": 0.20, "found": True},
            ]
        ),
        encoding="utf-8",
    )

    tracked = compositor._load_face_track_points(
        track_path,
        clip_start=0.0,
        crop_w=0.31640625,
        crop_h=1.0,
    )

    assert len(tracked) == 5
    assert [point["x"] for point in tracked[:4]] == pytest.approx([tracked[0]["x"]] * 4)
    assert tracked[4]["x"] > tracked[3]["x"]


@pytest.mark.parametrize(
    ("target_height", "font_size", "expected_face_bottom", "expected_caption_top"),
    [
        (1280, 41, 640.0, 832.0),
        (1920, 62, 960.0, 1248.0),
    ],
)
def test_auto_fill_uses_detected_face_with_custom_position(
    target_height: int,
    font_size: int,
    expected_face_bottom: float,
    expected_caption_top: float,
) -> None:
    metrics = compositor._face_aware_fill_caption_metrics_for_segment(
        {"start": 0.0, "end": 2.0, "mode": "fill", "zoom": 1.0},
        face_track_points=[{"t": 1.0, "cy": 0.4, "h": 0.2}],
        crop_h=1.0,
        target_height=target_height,
        subtitle_font_size=font_size,
        subtitle_margin=857 if target_height == 1280 else 1286,
        subtitle_margin_custom=True,
    )

    assert metrics["median_face_bottom"] == expected_face_bottom
    assert metrics["caption_top"] == expected_caption_top
    assert metrics["caption_top"] > metrics["median_face_bottom"]
    assert metrics["subscribe_y"] > metrics["caption_top"] + metrics["caption_block_height"]


@pytest.mark.parametrize(
    ("target_height", "block_height"),
    [(1280, 100.0), (1920, 150.0)],
)
def test_fill_caption_lower_third_low_face_and_bottom_overlay(
    target_height: int,
    block_height: float,
) -> None:
    scale = target_height / 1280.0
    lower_third = target_height * 0.65

    assert compositor._face_aware_fill_caption_top(
        face_bottom=target_height * 0.30,
        block_height=block_height,
        target_height=target_height,
    ) == pytest.approx(lower_third)

    low_face_bottom = target_height * 0.75
    assert compositor._face_aware_fill_caption_top(
        face_bottom=low_face_bottom,
        block_height=block_height,
        target_height=target_height,
    ) == pytest.approx(low_face_bottom + (36.0 * scale))

    overlay_top = target_height * 0.80
    caption_top = compositor._face_aware_fill_caption_top(
        face_bottom=low_face_bottom,
        block_height=block_height,
        target_height=target_height,
        bottom_overlay_top=overlay_top,
    )
    assert caption_top + block_height + (36.0 * scale) == pytest.approx(overlay_top)


def test_manual_fill_custom_caption_position_is_unchanged() -> None:
    caption_top = compositor._face_aware_caption_top_for_block(
        {"mode": "fill", "auto_layout": False},
        block_height=124.0,
        target_width=720,
        target_height=1280,
        subtitle_margin=857,
        subtitle_margin_custom=True,
        caption_anchor="top",
    )
    assert caption_top == 857.0


def test_pillow_overlay_specs_receive_fill_caption_offset() -> None:
    specs = compositor._face_aware_subtitle_overlay_specs(
        [{"path": Path("caption.png"), "start": 0.0, "end": 1.0}],
        overlay_events=[
            {"start": 0.0, "end": 1.0, "metrics": {"block_bbox": [0.0, 300.0, 720.0, 400.0]}}
        ],
        layout_segments=[
            {"start": 0.0, "end": 1.0, "mode": "fill", "auto_layout": True, "caption_face_bottom": 400.0}
        ],
        target_width=720,
        target_height=1280,
        subtitle_font_size=41,
        subtitle_margin=405,
        subtitle_margin_custom=False,
        caption_anchor="bottom",
    )
    assert specs[0]["y_offset"] == pytest.approx(532.0)


def test_face_aware_ass_uses_automatic_split_and_fit_positions(tmp_path: Path) -> None:
    subtitle_path = tmp_path / "captions.srt"
    subtitle_path.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nSplit words\n\n"
        "2\n00:00:01,000 --> 00:00:02,000\nFit words\n",
        encoding="utf-8",
    )
    output_path = compositor._build_face_aware_subtitle_path(
        subtitle_path,
        layout_segments=[
            {"start": 0.0, "end": 1.0, "mode": "split"},
            {"start": 1.0, "end": 2.0, "mode": "fit"},
        ],
        target_width=1080,
        target_height=1920,
        subtitle_font_size=62,
        fit_margin=405,
        subtitle_font="DejaVu Sans",
        subtitle_text_color="#FFFFFF",
        subtitle_text_alpha=100,
        subtitle_bg_color="#000000",
        subtitle_bg_alpha=0,
        subtitle_style="plain",
        subtitle_margin=1286,
        subtitle_margin_custom=True,
        caption_anchor="top",
    )

    assert output_path is not None
    content = output_path.read_text(encoding="utf-8")
    assert r"{\an5\pos(540,960)}Split words" in content
    assert "FitStyle,,0,0,0,,Fit words" in content
    assert r"{\an8\pos(540,1286)}Fit words" not in content


def test_caption_event_is_split_at_layout_boundary_for_ass(tmp_path: Path) -> None:
    subtitle_path = tmp_path / "cross-boundary.srt"
    subtitle_path.write_text(
        "1\n00:00:00,500 --> 00:00:01,500\nCrossing words\n",
        encoding="utf-8",
    )

    output_path = compositor._build_face_aware_subtitle_path(
        subtitle_path,
        layout_segments=[
            {"start": 0.0, "end": 1.0, "mode": "fill", "caption_style_suffix": "Fill0"},
            {"start": 1.0, "end": 2.0, "mode": "fit", "auto_layout": True},
        ],
        target_width=1080,
        target_height=1920,
        subtitle_font_size=62,
        fit_margin=694,
        subtitle_font="DejaVu Sans",
        subtitle_text_color="#FFFFFF",
        subtitle_text_alpha=100,
        subtitle_bg_color="#000000",
        subtitle_bg_alpha=0,
        subtitle_style="plain",
        subtitle_margin=1286,
        subtitle_margin_custom=True,
        caption_anchor="top",
    )

    content = output_path.read_text(encoding="utf-8")
    assert "0:00:00.50,0:00:01.00,FillStyleFill0" in content
    assert "0:00:01.00,0:00:01.50,FitStyle" in content


def test_pillow_caption_event_is_split_at_layout_boundary() -> None:
    specs = compositor._face_aware_subtitle_overlay_specs(
        [{"path": Path("caption.png"), "start": 0.5, "end": 1.5}],
        overlay_events=[
            {"start": 0.5, "end": 1.5, "metrics": {"block_bbox": [0.0, 300.0, 720.0, 400.0]}}
        ],
        layout_segments=[
            {"start": 0.0, "end": 1.0, "mode": "fill", "auto_layout": True},
            {"start": 1.0, "end": 2.0, "mode": "fit", "auto_layout": True},
        ],
        target_width=720,
        target_height=1280,
        subtitle_font_size=41,
        subtitle_margin=857,
        subtitle_margin_custom=True,
        caption_anchor="top",
    )

    assert [(spec["start"], spec["end"]) for spec in specs] == [(0.5, 1.0), (1.0, 1.5)]
    assert specs[0]["y_offset"] != specs[1]["y_offset"]


@pytest.mark.skipif(not FFMPEG or not FFPROBE, reason="ffmpeg and ffprobe are required")
@pytest.mark.parametrize("target_size", [(720, 1280), (1080, 1920)])
def test_mixed_face_aware_layout_renders_every_segment_on_target_canvas(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    target_size: tuple[int, int],
) -> None:
    target_width, target_height = target_size
    source_path = tmp_path / "source.mp4"
    background_path = tmp_path / "background.png"
    output_path = tmp_path / f"mixed-{target_width}x{target_height}.mp4"
    Image.new("RGB", target_size, (20, 25, 30)).save(background_path)
    subprocess.run(
        [
            FFMPEG,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=640x360:rate=30:duration=6",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(source_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    observed_compose_commands: list[list[str]] = []
    real_run_media_subprocess = compositor.run_media_subprocess

    def _record_compose_command(command, **kwargs):
        if kwargs.get("operation") == "compose_trimmed_with_background":
            observed_compose_commands.append(list(command))
        return real_run_media_subprocess(command, **kwargs)

    monkeypatch.setattr(compositor, "run_media_subprocess", _record_compose_command)
    crop_settings = {
        "crop_x_ratio": 0.0,
        "crop_y_ratio": 0.0,
        "crop_w_ratio": 1.0,
        "crop_h_ratio": 1.0,
        "layout_segments": [
            {"start": 0.0, "end": 2.0, "mode": "fill", "zoom": 1.0},
            {
                "start": 2.0,
                "end": 4.0,
                "mode": "split",
                "split_crops": {
                    "top": {"x": 0.0, "y": 0.0, "w": 0.5, "h": 1.0},
                    "bottom": {"x": 0.5, "y": 0.0, "w": 0.5, "h": 1.0},
                },
            },
            {"start": 4.0, "end": 6.0, "mode": "fit"},
        ],
    }
    app = Flask(__name__)
    with app.app_context():
        compositor._compose_trimmed_with_background(
            background_path,
            source_path,
            0.0,
            6.0,
            "",
            "",
            output_path,
            show_title=False,
            show_subtitle=False,
            crop_settings=crop_settings,
            crop_aspect="landscape",
            render_target_width=target_width,
            render_target_height=target_height,
        )

    assert output_path.exists() and output_path.stat().st_size > 0
    assert observed_compose_commands
    compose_command = observed_compose_commands[-1]
    filter_graph = compose_command[compose_command.index("-filter_complex") + 1]
    assert (
        f"scale={target_width}:{target_height}:force_original_aspect_ratio=increase,"
        f"crop={target_width}:{target_height},fps=30.000000,setsar=1,"
        "format=yuv420p,settb=AVTB[layout_out_0]"
    ) in filter_graph
    assert "concat=n=3:v=1:a=0[ov]" in filter_graph

    probe = subprocess.run(
        [
            FFPROBE,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height,pix_fmt,r_frame_rate",
            "-of",
            "json",
            str(output_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    stream = json.loads(probe.stdout)["streams"][0]
    assert (stream["width"], stream["height"]) == target_size
    assert stream["pix_fmt"] == "yuv420p"
    assert stream["r_frame_rate"] == "30/1"
