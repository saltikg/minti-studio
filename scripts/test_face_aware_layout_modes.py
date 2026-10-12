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
from app.video_shorts.config import DEFAULT_SUBTITLE_PRESET, STYLE_TEMPLATES
from app.video_shorts.routes import generation
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

DURATION = 12.0
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


def _case_ok(probe: dict[str, Any], expected_duration: float = DURATION) -> tuple[bool, str]:
    streams = probe.get("streams") or []
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    if not video:
        return False, "missing video"
    if int(video.get("width") or 0) != 720 or int(video.get("height") or 0) != 1280:
        return False, f"bad dims {video.get('width')}x{video.get('height')}"
    duration = float(video.get("duration") or probe.get("format", {}).get("duration") or 0.0)
    frame_tolerance = 1.0 / 25.0
    if abs(duration - expected_duration) > frame_tolerance + 1e-6:
        return False, f"bad duration {duration:.6f}"
    if not audio:
        return False, "missing audio"
    return True, f"720x1280 duration={duration:.6f} audio=yes"


def _append_custom_caption_position_checks(rows: list[tuple[str, str, str]]) -> None:
    segment = {"start": 0.0, "end": 4.0, "mode": "fill", "zoom": 1.25}
    face_points = [
        {"t": 0.5, "cx": 0.52, "cy": 0.32, "h": 0.18},
        {"t": 1.5, "cx": 0.53, "cy": 0.34, "h": 0.19},
        {"t": 2.5, "cx": 0.51, "cy": 0.33, "h": 0.18},
    ]
    custom = compositor._face_aware_fill_caption_metrics_for_segment(
        segment,
        face_track_points=face_points,
        crop_h=1.0,
        target_height=1280,
        subtitle_font_size=52,
        subtitle_margin=520,
        subtitle_margin_custom=True,
    )
    auto = compositor._face_aware_fill_caption_metrics_for_segment(
        segment,
        face_track_points=face_points,
        crop_h=1.0,
        target_height=1280,
        subtitle_font_size=52,
        subtitle_margin=520,
        subtitle_margin_custom=False,
    )
    if round(float(custom["caption_margin_v"] or 0)) == 520 and custom["median_face_bottom"] is None:
        rows.append(("custom_caption_margin_wins", "PASS", "custom subtitle_margin=520 preserved"))
    else:
        rows.append(("custom_caption_margin_wins", "FAIL", f"custom metrics={custom}"))
    if auto["median_face_bottom"] is not None and round(float(auto["caption_margin_v"] or 0)) != 520:
        rows.append(("default_caption_margin_auto_places", "PASS", f"auto margin={auto['caption_margin_v']:.2f}"))
    else:
        rows.append(("default_caption_margin_auto_places", "FAIL", f"auto metrics={auto}"))


def _append_style_template_position_preservation_check(rows: list[tuple[str, str, str]]) -> None:
    base_video = {
        "title_font_key": "inter",
        "title_font_size": 68,
        "subtitle_font_key": "inter",
        "subtitle_font_size": 52,
        "subtitle_margin": 777,
        "subtitle_margin_custom": True,
        "subtitle_style": "karaoke",
        "subtitle_preset": DEFAULT_SUBTITLE_PRESET,
        "title_margin": 123,
        "title_line_spacing": -4,
        "title_bg_color": "#111827",
        "title_bg_alpha": 70,
        "title_text_color": "#FFFFFF",
        "subtitle_text_color": "#FFFFFF",
        "subtitle_bg_color": "#000000",
        "subtitle_bg_alpha": 50,
        "subtitle_text_alpha": 100,
        "video_date_text": "",
        "video_date_top": 40,
        "show_title": True,
        "show_subtitle": True,
        "subscribe_overlay_enabled": True,
        "is_music_only": False,
        "static_visual_key": "",
        "background_visual_key": "",
        "visual_mode": "video",
        "podcast_audio_filename": "",
        "podcast_overlay_short_ids": "[]",
        "video_overlay_offset": 0,
    }
    template = next((item for item in STYLE_TEMPLATES if item.get("key") != "classic"), STYLE_TEMPLATES[0])
    style_only_payload = {
        "subtitle_preset": template.get("subtitle_preset"),
        "subtitle_style": "karaoke",
        "title_font_key": template.get("title_font_key"),
        "title_font_size": template.get("title_font_size"),
        "title_text_color": template.get("title_text_color"),
        "title_bg_color": template.get("title_bg_color"),
        "title_bg_alpha": template.get("title_bg_alpha"),
    }
    saved_video = {**base_video, **{key: value for key, value in style_only_payload.items() if value is not None}}
    options = generation._build_render_job_options(
        plan_index=0,
        title="Demo",
        brand_id="brand",
        crop_ratios={},
        crop_aspect="portrait",
        title_font_key=saved_video["title_font_key"],
        title_font_size=saved_video["title_font_size"],
        subtitle_font_key=saved_video["subtitle_font_key"],
        subtitle_font_size=saved_video["subtitle_font_size"],
        subtitle_margin=saved_video["subtitle_margin"],
        subtitle_margin_custom=saved_video["subtitle_margin_custom"],
        subtitle_style=saved_video["subtitle_style"],
        subtitle_preset=saved_video["subtitle_preset"],
        title_margin=saved_video["title_margin"],
        title_line_spacing=saved_video["title_line_spacing"],
        title_bg_color=saved_video["title_bg_color"],
        title_bg_alpha=saved_video["title_bg_alpha"],
        title_text_color=saved_video["title_text_color"],
        subtitle_text_color=saved_video["subtitle_text_color"],
        subtitle_bg_color=saved_video["subtitle_bg_color"],
        subtitle_bg_alpha=saved_video["subtitle_bg_alpha"],
        subtitle_text_alpha=saved_video["subtitle_text_alpha"],
        date_text=saved_video["video_date_text"],
        date_top=saved_video["video_date_top"],
        show_title=saved_video["show_title"],
        show_subtitle=saved_video["show_subtitle"],
        subscribe_overlay=saved_video["subscribe_overlay_enabled"],
        subscribe_overlay_image=None,
        is_music_only=saved_video["is_music_only"],
        static_visual_key=saved_video["static_visual_key"],
        background_visual_key=saved_video["background_visual_key"],
        visual_mode=saved_video["visual_mode"],
        podcast_audio_filename=saved_video["podcast_audio_filename"],
        podcast_overlay_short_ids=[],
        video_overlay_offset=saved_video["video_overlay_offset"],
        subtitle_text="demo captions",
    )
    if (
        options["subtitle_margin"] == 777
        and options["subtitle_margin_custom"] is True
        and options["title_margin"] == 123
        and options["subtitle_preset"] == template.get("subtitle_preset")
    ):
        rows.append(("style_template_preserves_position", "PASS", "style-only save kept subtitle_margin=777 custom=1 title_margin=123"))
    else:
        rows.append(("style_template_preserves_position", "FAIL", f"options={options}"))


def main() -> int:
    app = create_app()
    rows: list[tuple[str, str, str]] = []
    with app.app_context(), tempfile.TemporaryDirectory(prefix="fa_layout_modes_") as temp_dir:
        _append_custom_caption_position_checks(rows)
        _append_style_template_position_preservation_check(rows)
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
        output = temp / "twelve_segments_long_karaoke_events.mp4"
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
                    "layout_segments": _build_segments(
                        ["fill", "fit", "split", "fill", "fit", "split", "fill", "fit", "split", "fill", "fit", "split"]
                    ),
                },
            )
            ok, detail = _case_ok(_probe(ffprobe, output))
            rows.append(("twelve_segments_long_karaoke_events", "PASS" if ok else "FAIL", detail))
        except Exception as exc:
            rows.append(("twelve_segments_long_karaoke_events", "FAIL", str(exc).splitlines()[-1][:240]))
        output = temp / "multi_range_trim_compose.mp4"
        multi_trimmed = temp / "multi_range_source.mp4"
        ranges = [{"start": 0.0, "end": 2.0}, {"start": 4.0, "end": 6.0}, {"start": 9.0, "end": 12.0}]
        expected_multi_duration = sum(item["end"] - item["start"] for item in ranges)
        try:
            expected_multi_duration = compositor._trim_source_to_keep_ranges(source, ranges, multi_trimmed)
            compositor._compose_trimmed_with_background(
                background,
                multi_trimmed,
                0.0,
                expected_multi_duration,
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
                    "layout_segments": [
                        {"start": 0.0, "end": 3.0, "mode": "fill", "zoom": 1.0},
                        {"start": 3.0, "end": expected_multi_duration, "mode": "fit", "zoom": 1.0},
                    ],
                },
            )
            ok, detail = _case_ok(_probe(ffprobe, output), expected_duration=expected_multi_duration)
            rows.append(("multi_range_trim_compose", "PASS" if ok else "FAIL", detail))
        except Exception as exc:
            rows.append(("multi_range_trim_compose", "FAIL", str(exc).splitlines()[-1][:240]))
    print("case,status,detail")
    for name, status, detail in rows:
        print(f"{name},{status},{detail}")
    return 0 if all(status == "PASS" for _, status, _ in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
