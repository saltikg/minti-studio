from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from flask import Flask
from PIL import Image

from app.video_shorts.services import compositor


FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


@pytest.mark.parametrize(
    ("target_width", "target_height", "custom_position", "expected_split_top", "expected_fill_top"),
    [
        (720, 1280, 857, 578.0, 736.0),
        (1080, 1920, 1286, 898.0, 1104.0),
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
        {"mode": "fill", "caption_face_bottom": fill_face_bottom},
        block_height=block_height,
        target_width=target_width,
        target_height=target_height,
        subtitle_margin=custom_position,
        subtitle_margin_custom=True,
        caption_anchor="top",
    )
    fit_top = compositor._face_aware_caption_top_for_block(
        {"mode": "fit"},
        block_height=block_height,
        target_width=target_width,
        target_height=target_height,
        subtitle_margin=custom_position,
        subtitle_margin_custom=True,
        caption_anchor="top",
    )

    assert split_top == expected_split_top
    assert fill_top == expected_fill_top
    assert fill_top > fill_face_bottom
    assert fit_top == custom_position


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


@pytest.mark.parametrize(
    ("target_height", "font_size", "expected_face_bottom", "expected_caption_top"),
    [
        (1280, 41, 640.0, 676.0),
        (1920, 62, 960.0, 1014.0),
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


def test_face_aware_ass_uses_canvas_split_position_and_custom_fit(tmp_path: Path) -> None:
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
    assert r"{\an8\pos(540,1286)}Fit words" in content


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
