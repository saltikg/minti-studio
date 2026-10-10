#!/usr/bin/env python3
"""Exercise face-aware fill/split/fit filter graphs with synthetic media."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PIL import Image

from app import create_app
from app.video_shorts.services import compositor
from app.video_shorts.services.compositor import SUBSCRIBE_OVERLAY_PATH
from app.video_shorts.services.media_utils import _resolve_ffmpeg


CASES: list[tuple[str, list[str]]] = [
    ("fill", ["fill"]),
    ("split", ["split"]),
    ("fit", ["fit"]),
    ("fill_split", ["fill", "split"]),
    ("split_fit", ["split", "fit"]),
    ("fill_fit_fill", ["fill", "fit", "fill"]),
    ("fill_split_fit_fill", ["fill", "split", "fit", "fill"]),
]

DURATION = 6.0
LONG_KARAOKE_EVENT_COUNT = 120


def _run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _build_segments(modes: list[str]) -> list[dict[str, Any]]:
    step = DURATION / max(1, len(modes))
    segments: list[dict[str, Any]] = []
    for idx, mode in enumerate(modes):
        start = round(idx * step, 6)
        end = round(DURATION if idx == len(modes) - 1 else (idx + 1) * step, 6)
        segment: dict[str, Any] = {"start": start, "end": end, "mode": mode, "zoom": 1.0}
        if mode == "split":
            segment["split_crops"] = {
                "top": {"x": 0.10, "y": 0.10, "w": 0.45, "h": 0.80},
                "bottom": {"x": 0.50, "y": 0.10, "w": 0.45, "h": 0.80},
            }
        segments.append(segment)
    return segments


def _build_caption_events(duration: float, count: int) -> list[dict[str, Any]]:
    step = duration / max(1, count)
    events: list[dict[str, Any]] = []
    for idx in range(count):
        start = round(idx * step, 6)
        end = round(min(duration, start + max(0.01, step * 0.72)), 6)
        block_height = 68.0 if idx % 5 else 96.0
        block_top = 858.0 if idx % 7 else 826.0
        events.append(
            {
                "start": start,
                "end": end,
                "metrics": {
                    "block_bbox": [70.0, block_top, 650.0, block_top + block_height],
                },
            }
        )
    return events


def _probe(ffprobe: str, path: Path) -> dict[str, Any]:
    raw = subprocess.check_output(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,width,height,duration:format=duration",
            "-of",
            "json",
            str(path),
        ],
        text=True,
    )
    return json.loads(raw)


def _case_ok(probe: dict[str, Any]) -> tuple[bool, str]:
    streams = probe.get("streams") or []
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    if not video:
        return False, "missing video"
    if int(video.get("width") or 0) != 720 or int(video.get("height") or 0) != 1280:
        return False, f"bad dims {video.get('width')}x{video.get('height')}"
    duration = float(video.get("duration") or probe.get("format", {}).get("duration") or 0.0)
    if abs(duration - DURATION) > (1.0 / 30.0):
        return False, f"bad duration {duration:.6f}"
    if not audio:
        return False, "missing audio"
    return True, f"720x1280 duration={duration:.6f} audio=yes"


def main() -> int:
    app = create_app()
    rows: list[tuple[str, str, str]] = []
    with app.app_context(), tempfile.TemporaryDirectory(prefix="fa_layout_modes_") as temp_dir:
        temp = Path(temp_dir)
        ffmpeg = _resolve_ffmpeg()
        ffprobe = str(Path(ffmpeg).with_name("ffprobe"))
        source = temp / "source.mp4"
        background = temp / "background.jpg"
        caption_overlay = temp / "caption_overlay.mov"
        Image.new("RGB", (720, 1280), (8, 10, 14)).save(background)
        _run(
            [
                ffmpeg,
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"testsrc2=size=1280x720:rate=30:duration={DURATION}",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency=440:sample_rate=48000:duration={DURATION}",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-c:a",
                "aac",
                "-shortest",
                str(source),
            ]
        )
        _run(
            [
                ffmpeg,
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"color=c=black@0.0:s=720x1280:r=30:d={DURATION}",
                "-vf",
                "format=rgba",
                "-c:v",
                "qtrle",
                str(caption_overlay),
            ]
        )
        for name, modes in CASES:
            output = temp / f"{name}.mp4"
            try:
                compositor._compose_trimmed_with_background(
                    background,
                    source,
                    0.0,
                    DURATION,
                    "Demo title",
                    "",
                    output,
                    title_engine="pillow",
                    subtitle_overlay_video_path=caption_overlay,
                    show_title=True,
                    show_subtitle=True,
                    crop_aspect="portrait",
                    subscribe_overlay_enabled=SUBSCRIBE_OVERLAY_PATH.exists(),
                    subscribe_overlay_path=SUBSCRIBE_OVERLAY_PATH,
                    crop_settings={
                        "crop_x_ratio": 0.0,
                        "crop_y_ratio": 0.0,
                        "crop_w_ratio": 1.0,
                        "crop_h_ratio": 1.0,
                        "layout_segments": _build_segments(modes),
                    },
                )
                ok, detail = _case_ok(_probe(ffprobe, output))
                rows.append((name, "PASS" if ok else "FAIL", detail))
            except Exception as exc:
                rows.append((name, "FAIL", str(exc).splitlines()[-1][:240]))
        output = temp / "split_long_karaoke_events.mp4"
        try:
            compositor._compose_trimmed_with_background(
                background,
                source,
                0.0,
                DURATION,
                "Demo title",
                "",
                output,
                title_engine="pillow",
                subtitle_overlay_video_path=caption_overlay,
                subtitle_overlay_events=_build_caption_events(DURATION, LONG_KARAOKE_EVENT_COUNT),
                show_title=True,
                show_subtitle=True,
                crop_aspect="portrait",
                subscribe_overlay_enabled=SUBSCRIBE_OVERLAY_PATH.exists(),
                subscribe_overlay_path=SUBSCRIBE_OVERLAY_PATH,
                crop_settings={
                    "crop_x_ratio": 0.0,
                    "crop_y_ratio": 0.0,
                    "crop_w_ratio": 1.0,
                    "crop_h_ratio": 1.0,
                    "layout_segments": _build_segments(["split"]),
                },
            )
            ok, detail = _case_ok(_probe(ffprobe, output))
            rows.append(("split_long_karaoke_events", "PASS" if ok else "FAIL", detail))
        except Exception as exc:
            rows.append(("split_long_karaoke_events", "FAIL", str(exc).splitlines()[-1][:240]))
    print("case,status,detail")
    for name, status, detail in rows:
        print(f"{name},{status},{detail}")
    return 0 if all(status == "PASS" for _, status, _ in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
