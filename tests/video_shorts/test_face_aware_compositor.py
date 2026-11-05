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
