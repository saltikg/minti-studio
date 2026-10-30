from __future__ import annotations

import ctypes
import gc
import json
import math
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageSequence, features
from flask import current_app

from app.video_shorts.config import (
    ARABIC_FALLBACK_FONT_PATH,
    DESIGN_GEOMETRY,
    DEFAULT_SUB_FONT_SIZE,
    DEFAULT_SUBTITLE_BG_ALPHA,
    DEFAULT_SUBTITLE_BG_COLOR,
    DEFAULT_SUBTITLE_TEXT_ALPHA,
    DEFAULT_TITLE_BG_COLOR,
    DEFAULT_TITLE_BG_ALPHA,
    DEFAULT_TITLE_MARGIN,
    DEFAULT_VIDEO_OVERLAY_OFFSET,
    DESIGN_POSITION_LIMITS,
    clamp_design_position,
    normalize_caption_anchor,
    FFMPEG_RENDER_TIMEOUT,
    FFMPEG_SHORT_TIMEOUT,
    FFPROBE_TIMEOUT,
    FACE_AWARE_MAX_ZOOM,
    FACE_AWARE_TARGET_FACE_RATIO,
    STATIC_VISUAL_PRESETS,
    SUBTITLE_PRESETS,
    SUB_MARGIN_DEFAULT,
)
from app.video_shorts.services.arabic_text import contains_arabic_script

from app.video_shorts.services.media_utils import (
    _normalize_source_cache_container,
    _resolve_ffmpeg,
    run_media_subprocess,
    scale_media_timeout,
)
from app.video_shorts.services.music import music_volume_expression, normalize_music_level
from app.video_shorts.services import render_timing
from app.video_shorts.services.clip_ranges import resolve_clip_ranges

SUBSCRIBE_OVERLAY_SCALE = DESIGN_GEOMETRY["subscribe_scale"]
SUBSCRIBE_OVERLAY_BOTTOM_OFFSET = DESIGN_GEOMETRY["subscribe_bottom"]
SUBSCRIBE_OVERLAY_PATH = Path(__file__).resolve().parents[1] / "static" / "subscribe.gif"
SUBTITLE_FONTS_DIR = Path(__file__).resolve().parents[1] / "static" / "fonts"
FACE_AWARE_FIT_FRAME_TOP = DESIGN_GEOMETRY["fit_top"]
FACE_AWARE_FILL_FALLBACK_SUBTITLE_MARGIN = 405
FACE_AWARE_FILL_CAPTION_GAP = 36.0
FACE_AWARE_FILL_CAPTION_MIN_TOP = 640.0
FACE_AWARE_FILL_SUBSCRIBE_ROOM = 300.0
FACE_AWARE_LEGACY_DEFAULT_SUBTITLE_MARGINS = {270, 320, 405, 520}
FACE_AWARE_TOP_CAPTION_MAX_LINES = 2
FACE_AWARE_TOP_CAPTION_LINE_HEIGHT_MULTIPLIER = 1.15
FACE_AWARE_TOP_CAPTION_SUBSCRIBE_GAP_RATIO = 50.0 / 1920.0
FACE_AWARE_SHORTS_UI_TOP_RATIO = 0.8
FACE_AWARE_SUBSCRIBE_TARGET_WIDTH_RATIO = 380.0 / 1080.0
OUTREACH_SUBSCRIBE_BOTTOM_RATIO = 1500.0 / 1920.0
OUTREACH_BLUR_WIDTH = 270
OUTREACH_BLUR_HEIGHT = 480
OUTREACH_BLUR_SIGMA = 20
OUTREACH_BACKGROUND_DARKEN_OPACITY = 0.32
CLIP_EDGE_SNAP_WINDOW_SECONDS = 0.250
CLIP_EDGE_WORD_GUARD_SECONDS = 0.030
CLIP_AUDIO_FADE_IN_SECONDS = 0.050
CLIP_AUDIO_FADE_OUT_SECONDS = 0.150

CROP_TARGET_SIZE = 1080  # reference crop resolution before resizing for display
VIDEO_TARGET_WIDTH = DESIGN_GEOMETRY["width"]
_target_height = int(round(VIDEO_TARGET_WIDTH * 16 / 9))
VIDEO_TARGET_HEIGHT = _target_height + (_target_height % 2)
VIDEO_OVERLAY_WIDTH = 900
VIDEO_OVERLAY_TOP_OFFSET = DEFAULT_VIDEO_OVERLAY_OFFSET
PODCAST_LANDSCAPE_WIDTH = 1280
PODCAST_LANDSCAPE_HEIGHT = 720
ORIGINAL_LAYOUT_MAX_WIDTH = DESIGN_GEOMETRY["original_width"]
ORIGINAL_LAYOUT_MAX_HEIGHT = DESIGN_GEOMETRY["original_height"]
ORIGINAL_LAYOUT_HD_MAX_WIDTH = DESIGN_GEOMETRY["original_hd_width"]
ORIGINAL_LAYOUT_HD_MAX_HEIGHT = DESIGN_GEOMETRY["original_hd_height"]
VIDEO_DATE_BOTTOM_MARGIN = 250
DEFAULT_VIDEO_DATE_TOP = VIDEO_TARGET_HEIGHT - VIDEO_DATE_BOTTOM_MARGIN - 24
TITLE_WRAP_LENGTH = 35
TITLE_WRAP_SIDE_MARGIN = DESIGN_GEOMETRY["title_side_margin"]
TITLE_WRAP_MAX_LINES = DESIGN_GEOMETRY["title_max_lines"]
TITLE_WRAP_MIN_FONT_SIZE = DESIGN_GEOMETRY["title_min_font_size"]
TITLE_BOX_BORDER_WIDTH = DESIGN_GEOMETRY["title_box_border"]
STATIC_VISUAL_MAP = {entry["key"]: entry for entry in STATIC_VISUAL_PRESETS}
PILLOW_RAQM_AVAILABLE = bool(features.check("raqm"))


def _render_overlay_shapes(
    shapes: Optional[list[Dict[str, Any]]],
    *,
    target_width: int,
    target_height: int,
    owner_user_id: Optional[str] = None,
    brand_id: Optional[str] = None,
) -> Optional[Path]:
    if not shapes:
        return None
    canvas = Image.new("RGBA", (target_width, target_height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas, "RGBA")
    scale_x = float(target_width) / 1080.0
    scale_y = float(target_height) / 1920.0
    drawn = 0
    for shape in list(shapes)[:5]:
        if not isinstance(shape, dict):
            continue
        try:
            coordinate_min = float(DESIGN_GEOMETRY["shape_coordinate_min"])
            coordinate_max = float(DESIGN_GEOMETRY["shape_coordinate_max"])
            size_max = float(DESIGN_GEOMETRY["shape_size_max"])
            x = max(coordinate_min, min(coordinate_max, float(shape.get("x", 0))))
            y = max(coordinate_min, min(coordinate_max, float(shape.get("y", 0))))
            w = max(1.0, min(size_max, float(shape.get("w", 1))))
            h = max(1.0, min(size_max, float(shape.get("h", 1))))
            alpha = max(0, min(100, int(round(float(shape.get("alpha", 100))))))
            radius = max(0.0, float(shape.get("radius", 0)))
        except Exception:
            continue
        left = int(round(x * scale_x))
        top = int(round(y * scale_y))
        right = int(round((x + w) * scale_x))
        bottom = int(round((y + h) * scale_y))
        if str(shape.get("type") or "shape").strip().lower() == "image":
            managed_path = None
            try:
                local_override = str(shape.get("_image_path") or "").strip()
                if local_override:
                    image_path = Path(local_override)
                else:
                    if not owner_user_id:
                        raise ValueError("Overlay image owner is missing")
                    from app.video_shorts.services.overlay_assets import resolve_overlay_image_asset

                    image_path, managed_path = resolve_overlay_image_asset(
                        str(shape.get("asset_id") or ""),
                        owner_user_id=str(owner_user_id),
                        brand_id=brand_id,
                    )
                with Image.open(image_path) as source_image:
                    overlay_image = source_image.convert("RGBA").resize(
                        (max(1, right - left), max(1, bottom - top)),
                        Image.Resampling.LANCZOS,
                    )
                if alpha < 100:
                    alpha_channel = overlay_image.getchannel("A").point(
                        lambda value: int(round(value * alpha / 100.0))
                    )
                    overlay_image.putalpha(alpha_channel)
                canvas.alpha_composite(overlay_image, (left, top))
                drawn += 1
            except Exception as exc:
                current_app.logger.warning(
                    "Overlay image skipped asset_id=%s owner_user_id=%s brand_id=%s error=%s",
                    shape.get("asset_id"),
                    owner_user_id,
                    brand_id,
                    exc,
                )
            finally:
                if managed_path is not None:
                    managed_path.cleanup()
            continue
        color = str(shape.get("color") or "#000000").strip().lstrip("#")
        if not re.fullmatch(r"[0-9A-Fa-f]{6}", color):
            color = "000000"
        rgba = tuple(int(color[offset:offset + 2], 16) for offset in (0, 2, 4)) + (round(alpha * 255 / 100),)
        scaled_radius = int(round(radius * min(scale_x, scale_y)))
        draw.rounded_rectangle(
            [left, top, max(left + 1, right), max(top + 1, bottom)],
            radius=max(0, scaled_radius),
            fill=rgba,
        )
        drawn += 1
    if not drawn:
        return None
    handle = tempfile.NamedTemporaryFile(prefix="minti-shapes-", suffix=".png", delete=False)
    handle.close()
    output = Path(handle.name)
    canvas.save(output, format="PNG")
    return output


def _even_dimension(value: float) -> int:
    safe = max(2, int(round(float(value or 0))))
    return safe if safe % 2 == 0 else safe - 1


def _original_layout_target_dimensions(source_width: Any, source_height: Any) -> tuple[int, int]:
    """Return an even source-aspect canvas for Original layout, capped at 720p/1080p."""
    try:
        width = float(source_width or 0)
        height = float(source_height or 0)
    except (TypeError, ValueError):
        width = 0
        height = 0
    if width <= 0 or height <= 0:
        return PODCAST_LANDSCAPE_WIDTH, PODCAST_LANDSCAPE_HEIGHT
    max_width = ORIGINAL_LAYOUT_HD_MAX_WIDTH if width >= ORIGINAL_LAYOUT_HD_MAX_WIDTH or height >= ORIGINAL_LAYOUT_HD_MAX_HEIGHT else ORIGINAL_LAYOUT_MAX_WIDTH
    max_height = ORIGINAL_LAYOUT_HD_MAX_HEIGHT if width >= ORIGINAL_LAYOUT_HD_MAX_WIDTH or height >= ORIGINAL_LAYOUT_HD_MAX_HEIGHT else ORIGINAL_LAYOUT_MAX_HEIGHT
    scale = min(float(max_width) / width, float(max_height) / height, 1.0)
    target_width = _even_dimension(width * scale)
    target_height = _even_dimension(height * scale)
    return max(2, target_width), max(2, target_height)


def _normalize_original_zoom_rect(raw_value: Any) -> Optional[Dict[str, float]]:
    if raw_value is None:
        return None
    if isinstance(raw_value, str):
        raw_text = raw_value.strip()
        if not raw_text:
            return None
        try:
            parsed = json.loads(raw_text)
        except Exception:
            return None
    elif isinstance(raw_value, dict):
        parsed = raw_value
    else:
        return None
    try:
        x = float(parsed.get("x"))
        y = float(parsed.get("y"))
        w = float(parsed.get("w"))
        h = float(parsed.get("h"))
    except Exception:
        return None
    if not all(math.isfinite(value) for value in (x, y, w, h)):
        return None
    w = max(0.4, min(1.0, w))
    h = max(0.4, min(1.0, h))
    x = max(0.0, min(1.0 - w, x))
    y = max(0.0, min(1.0 - h, y))
    if w >= 0.999 and h >= 0.999:
        return None
    return {"x": x, "y": y, "w": w, "h": h}


def _ffmpeg_escape(text: str) -> str:
    if not text:
        return ""
    return (
        text.replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("%", "\\%")
        .replace("[", "\\[")
        .replace("]", "\\]")
        .replace("'", "\\'")
        .replace("\"", "\\\"")
    )


def _ffmpeg_final_speed(stderr: str | None) -> str:
    matches = re.findall(r"speed=\s*([^\s]+)", stderr or "")
    return matches[-1] if matches else "unknown"


def _format_loadavg(loadavg: tuple[float, float, float] | None) -> str:
    if not loadavg:
        return "unknown"
    return ",".join(f"{value:.2f}" for value in loadavg)


def _normalize_keep_ranges_for_trim(keep_ranges: Optional[list[Dict[str, Any]]]) -> list[dict[str, Any]]:
    ranges: list[dict[str, Any]] = []
    for item in keep_ranges or []:
        if not isinstance(item, dict):
            continue
        try:
            start = float(item.get("start"))
            end = float(item.get("end"))
        except Exception:
            continue
        if start < 0 or end <= start:
            continue
        normalized: dict[str, Any] = {"start": round(start, 6), "end": round(end, 6)}
        try:
            first_index = int(item.get("start_word_index"))
            last_index = int(item.get("end_word_index"))
        except (TypeError, ValueError):
            first_index = last_index = -1
        if first_index >= 0 and last_index >= first_index:
            normalized["start_word_index"] = first_index
            normalized["end_word_index"] = last_index
        ranges.append(normalized)
    ranges.sort(key=lambda item: (item["start"], item["end"]))
    return ranges


def _log_multi_range_boundary_warning(message: str, *args: Any) -> None:
    try:
        current_app.logger.warning(message, *args)
    except Exception:
        pass


def _extract_mono_pcm_window(
    src_path: Path,
    *,
    window_start: float,
    duration: float,
    sample_rate: int = 16000,
) -> np.ndarray:
    """Read one mono PCM audio window for all multi-range boundary analysis."""
    if duration <= 0:
        return np.array([], dtype=np.float32)
    resolved_ffmpeg = _resolve_ffmpeg()
    cmd = [
        resolved_ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{max(0.0, window_start):.6f}",
        "-t",
        f"{duration:.6f}",
        "-i",
        str(src_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(sample_rate),
        "-f",
        "s16le",
        "pipe:1",
    ]
    result = run_media_subprocess(
        cmd,
        operation="multi_range_boundary_audio",
        context=f"src={src_path.name} start={window_start:.3f} duration={duration:.3f}",
        check=True,
        timeout=scale_media_timeout(FFMPEG_SHORT_TIMEOUT, duration_seconds=duration, multiplier=1.0, extra_seconds=30),
        capture_output=True,
        text=False,
    )
    return np.frombuffer(result.stdout or b"", dtype=np.int16).astype(np.float32) / 32768.0


def _lowest_energy_time_in_pcm_window(
    pcm: np.ndarray,
    *,
    window_start: float,
    search_start: float,
    search_end: float,
    sample_rate: int = 16000,
    frame_seconds: float = 0.01,
) -> Optional[float]:
    """Return the center time of the quietest 10ms frame inside a source-time window."""
    if pcm.size == 0 or search_end <= search_start:
        return None
    frame_size = max(1, int(round(sample_rate * frame_seconds)))
    first_sample = max(0, int(round((search_start - window_start) * sample_rate)))
    last_sample = min(int(pcm.size), int(round((search_end - window_start) * sample_rate)))
    if last_sample <= first_sample:
        return None
    best_rms: Optional[float] = None
    best_offset = first_sample
    for offset in range(first_sample, max(first_sample + 1, last_sample - frame_size + 1), frame_size):
        frame = pcm[offset : min(offset + frame_size, last_sample)]
        if frame.size == 0:
            continue
        rms = float(np.sqrt(np.mean(np.square(frame))))
        if best_rms is None or rms < best_rms:
            best_rms = rms
            best_offset = offset
    center_sample = best_offset + (frame_size / 2.0)
    return window_start + (center_sample / float(sample_rate))


def _snap_keep_range_boundaries_to_audio(
    src_path: Path,
    ranges: list[dict[str, float]],
    *,
    sample_rate: int = 16000,
) -> list[dict[str, float]]:
    """Move internal multi-range cut points to nearby low-energy audio frames.

    Whisper word end times can be early. For internal joins, this gives the last
    kept word a little breathing room by choosing the quietest 10ms audio frame
    within the allowed 200ms adjustment window. Failures return the original ranges.
    """
    if len(ranges) <= 1:
        return ranges
    windows: list[Tuple[str, int, float, float]] = []
    for index in range(len(ranges)):
        if index > 0:
            boundary = float(ranges[index]["start"])
            windows.append(("start", index, max(0.0, boundary - 0.20), boundary + 0.05))
        if index < len(ranges) - 1:
            boundary = float(ranges[index]["end"])
            windows.append(("end", index, max(0.0, boundary - 0.05), boundary + 0.20))
    if not windows:
        return ranges
    window_start = min(item[2] for item in windows)
    window_end = max(item[3] for item in windows)
    try:
        pcm = _extract_mono_pcm_window(
            src_path,
            window_start=window_start,
            duration=max(0.001, window_end - window_start),
            sample_rate=sample_rate,
        )
    except Exception as exc:
        _log_multi_range_boundary_warning(
            "Multi-range audio boundary analysis failed src=%s ranges=%s error=%s",
            src_path.name,
            len(ranges),
            exc,
        )
        return ranges
    adjusted = [dict(item) for item in ranges]
    adjustments: list[dict[str, Any]] = []
    for kind, index, search_start, search_end in windows:
        original = float(ranges[index][kind])
        if kind == "end":
            lower = max(float(ranges[index]["start"]) + 0.001, original - 0.20, search_start)
            upper = min(float(ranges[index + 1]["start"]) - 0.001, original + 0.20, search_end)
        else:
            lower = max(float(adjusted[index - 1]["end"]) + 0.001, original - 0.20, search_start)
            upper = min(float(ranges[index]["end"]) - 0.001, original + 0.20, search_end)
        candidate = _lowest_energy_time_in_pcm_window(
            pcm,
            window_start=window_start,
            search_start=lower,
            search_end=upper,
            sample_rate=sample_rate,
        )
        if candidate is None:
            continue
        snapped = round(min(max(candidate, lower), upper), 6)
        if abs(snapped - original) > 0.0005:
            adjusted[index][kind] = snapped
            adjustments.append({"range": index, "boundary": kind, "from": round(original, 6), "to": snapped})
    if adjustments:
        try:
            current_app.logger.info(
                "Multi-range audio boundary snap src=%s adjustments=%s",
                src_path.name,
                json.dumps(adjustments, ensure_ascii=False),
            )
        except Exception:
            pass
    return adjusted


def _normalize_transcript_words_for_boundary_snap(
    transcript_words: Optional[list[Dict[str, Any]]],
) -> list[dict[str, Any]]:
    words: list[dict[str, Any]] = []
    for item in transcript_words or []:
        if not isinstance(item, dict):
            continue
        try:
            start = float(item.get("start"))
            end = float(item.get("end"))
        except (TypeError, ValueError):
            continue
        if start < 0 or end < start:
            continue
        try:
            word_index = int(item.get("index"))
        except (TypeError, ValueError):
            word_index = len(words)
        words.append({"start": start, "end": end, "index": word_index})
    return sorted(words, key=lambda item: (item["start"], item["end"]))


def _outer_word_boundary_context(
    ranges: list[dict[str, float]],
    transcript_words: Optional[list[Dict[str, Any]]],
) -> dict[str, Optional[float]]:
    """Find transcript neighbors around the selected outer words."""
    words = _normalize_transcript_words_for_boundary_snap(transcript_words)
    if not ranges or not words:
        return {}
    current_start = float(ranges[0]["start"])
    current_end = float(ranges[-1]["end"])
    explicit_first = ranges[0].get("start_word_index")
    explicit_last = ranges[-1].get("end_word_index")
    index_positions = {int(word["index"]): position for position, word in enumerate(words)}
    first_index = index_positions.get(int(explicit_first)) if explicit_first is not None else None
    last_index = index_positions.get(int(explicit_last)) if explicit_last is not None else None
    if first_index is None:
        first_index = min(range(len(words)), key=lambda index: abs(words[index]["start"] - current_start))
        if abs(words[first_index]["start"] - current_start) > CLIP_EDGE_SNAP_WINDOW_SECONDS:
            first_index = -1
    if last_index is None:
        last_index = min(range(len(words)), key=lambda index: abs(words[index]["end"] - current_end))
        if abs(words[last_index]["end"] - current_end) > CLIP_EDGE_SNAP_WINDOW_SECONDS:
            last_index = -1
    return {
        "previous_end": words[first_index - 1]["end"] if first_index > 0 else None,
        "first_start": words[first_index]["start"] if first_index >= 0 else None,
        "last_end": words[last_index]["end"] if last_index >= 0 else None,
        "next_start": words[last_index + 1]["start"] if 0 <= last_index < len(words) - 1 else None,
    }


def _snap_outer_keep_range_boundaries_to_audio(
    src_path: Path,
    ranges: list[dict[str, float]],
    transcript_words: Optional[list[Dict[str, Any]]],
    *,
    sample_rate: int = 16000,
) -> tuple[list[dict[str, float]], dict[str, Optional[float]]]:
    """Snap only the clip's outer edges between adjacent transcript words."""
    context = _outer_word_boundary_context(ranges, transcript_words)
    if not ranges or not context:
        return ranges, context
    current_start = float(ranges[0]["start"])
    current_end = float(ranges[-1]["end"])
    previous_end = context.get("previous_end")
    first_start = context.get("first_start")
    last_end = context.get("last_end")
    next_start = context.get("next_start")
    windows: list[tuple[str, float, float, float, float, float]] = []

    if previous_end is not None and first_start is not None:
        gap_start = float(previous_end)
        gap_end = max(gap_start, float(first_start))
        midpoint = (gap_start + gap_end) / 2.0
        if gap_end - gap_start < CLIP_EDGE_WORD_GUARD_SECONDS:
            search_start = search_end = midpoint
        else:
            search_start = max(gap_start + CLIP_EDGE_WORD_GUARD_SECONDS, current_start - CLIP_EDGE_SNAP_WINDOW_SECONDS)
            search_end = min(gap_end, current_start + CLIP_EDGE_SNAP_WINDOW_SECONDS)
        windows.append(("start", search_start, search_end, gap_start, gap_end, midpoint))
    if last_end is not None and next_start is not None:
        gap_start = float(last_end)
        gap_end = max(gap_start, float(next_start))
        midpoint = (gap_start + gap_end) / 2.0
        if gap_end - gap_start < CLIP_EDGE_WORD_GUARD_SECONDS:
            search_start = search_end = midpoint
        else:
            search_start = max(gap_start, current_end - CLIP_EDGE_SNAP_WINDOW_SECONDS)
            search_end = min(gap_end - CLIP_EDGE_WORD_GUARD_SECONDS, current_end + CLIP_EDGE_SNAP_WINDOW_SECONDS)
        windows.append(("end", search_start, search_end, gap_start, gap_end, midpoint))
    if not windows:
        return ranges, context

    pcm_window_start = max(0.0, min(min(item[1], item[2]) for item in windows))
    pcm_window_end = max(max(item[1], item[2]) for item in windows)
    try:
        pcm = _extract_mono_pcm_window(
            src_path,
            window_start=pcm_window_start,
            duration=max(0.001, pcm_window_end - pcm_window_start),
            sample_rate=sample_rate,
        )
    except Exception as exc:
        _log_multi_range_boundary_warning(
            "Clip outer audio boundary analysis failed src=%s error=%s",
            src_path.name,
            exc,
        )
        return ranges, context

    adjusted = [dict(item) for item in ranges]
    adjustments: list[dict[str, Any]] = []
    for kind, search_start, search_end, gap_start, gap_end, midpoint in windows:
        original = current_start if kind == "start" else current_end
        if search_end - search_start < 0.010:
            candidate = midpoint
        else:
            candidate = _lowest_energy_time_in_pcm_window(
                pcm,
                window_start=pcm_window_start,
                search_start=search_start,
                search_end=search_end,
                sample_rate=sample_rate,
            )
            if candidate is None:
                candidate = midpoint
        if kind == "start":
            snapped = min(max(float(candidate), gap_start), gap_end, adjusted[0]["end"] - 0.001)
            adjusted[0]["start"] = round(max(0.0, snapped), 6)
        else:
            snapped = max(min(float(candidate), gap_end), gap_start, adjusted[-1]["start"] + 0.001)
            adjusted[-1]["end"] = round(snapped, 6)
        if abs(snapped - original) > 0.0005:
            adjustments.append({"boundary": kind, "from": round(original, 6), "to": round(snapped, 6)})
    if adjustments:
        try:
            current_app.logger.info(
                "Clip outer audio boundary snap src=%s adjustments=%s context=%s",
                src_path.name,
                json.dumps(adjustments, ensure_ascii=False),
                json.dumps(context, ensure_ascii=False),
            )
        except Exception:
            pass
    return adjusted, context


def _audio_edge_fade_filter(duration: float) -> str:
    safe_duration = max(0.001, float(duration))
    fade_in = min(CLIP_AUDIO_FADE_IN_SECONDS, safe_duration / 2.0)
    fade_out = min(CLIP_AUDIO_FADE_OUT_SECONDS, safe_duration / 2.0)
    fade_out_start = max(fade_in, safe_duration - fade_out)
    return f"afade=t=in:st=0:d={fade_in:.3f},afade=t=out:st={fade_out_start:.6f}:d={fade_out:.3f}"


def _trim_source_to_keep_ranges(
    src_path: Path,
    keep_ranges: list[Dict[str, Any]],
    out_path: Path,
    *,
    media_duration_seconds: Optional[float] = None,
    fallback_duration_seconds: Optional[float] = None,
    transcript_word_selection: bool = False,
) -> float:
    """Create one clip from multiple source ranges using a single ffmpeg process.

    The returned duration is the sum of kept range durations. Callers use the output
    file as a new zero-based timeline for captions, face-aware layout and compose.
    """
    ranges = _normalize_keep_ranges_for_trim(keep_ranges)
    if not ranges:
        return 0.0
    if not src_path.exists():
        raise FileNotFoundError(f"Source video not found: {src_path}")
    src_path, ranges, has_audio = resolve_keep_ranges_for_trim(
        src_path,
        ranges,
        media_duration_seconds=media_duration_seconds,
        fallback_duration_seconds=fallback_duration_seconds,
        transcript_word_selection=transcript_word_selection,
    )
    if len(ranges) <= 1:
        return max(0.0, ranges[0]["end"] - ranges[0]["start"])
    resolved_ffmpeg = _resolve_ffmpeg()
    source_fps = _probe_video_frame_rate(src_path) or 30.0
    if has_audio:
        for index, item in enumerate(ranges):
            if index >= len(keep_ranges) or not isinstance(keep_ranges[index], dict):
                continue
            keep_ranges[index]["start"] = item["start"]
            keep_ranges[index]["end"] = item["end"]
    first_start = float(ranges[0].get("start") or 0.0)
    seek_offset = max(0.0, first_start - 2.0)
    cmd = [resolved_ffmpeg, "-y", "-stats_period", "1"]
    if seek_offset > 0:
        cmd.extend(["-ss", f"{seek_offset:.6f}", "-copyts"])
    cmd.extend(["-i", str(src_path)])
    select_terms: list[str] = []
    gap_terms: list[str] = []
    total_duration = 0.0
    previous_end: Optional[float] = None
    for item in ranges:
        start = float(item["start"])
        end = float(item["end"])
        if previous_end is not None:
            gap = max(0.0, start - previous_end)
            if gap > 0:
                gap_terms.append(f"gte(T\\,{start:.6f})*{gap:.6f}")
        duration = max(0.001, end - start)
        total_duration += duration
        select_terms.append(f"(gte(t\\,{start:.6f})*lt(t\\,{end:.6f}))")
        previous_end = end
    select_expr = "+".join(select_terms)
    gap_expr = "+".join(gap_terms) if gap_terms else "0"
    origin_expr = f"{first_start:.6f}+({gap_expr})"
    filter_parts = [
        f"[0:v]settb=AVTB,select='{select_expr}',setpts='PTS-({origin_expr})/TB',fps=fps={source_fps:.6f}:start_time=0,settb=AVTB[v]",
    ]
    if has_audio:
        filter_parts.append(
            f"[0:a]asettb=AVTB,asetnsamples=n=480,aselect='{select_expr}',asetpts='PTS-({origin_expr})/TB',"
            f"asettb=AVTB,aresample=async=1:first_pts=0,{_audio_edge_fade_filter(total_duration)}[a]"
        )
    cmd.extend(["-filter_complex", ";".join(filter_parts), "-map", "[v]"])
    if has_audio:
        cmd.extend(["-map", "[a]"])
    cmd.extend(["-t", f"{total_duration:.6f}"])
    cmd.extend(["-r", f"{source_fps:.6f}", "-vsync", "cfr"])
    cmd.extend(["-c:v", "libx264", "-preset", "ultrafast", "-crf", "16"])
    if has_audio:
        cmd.extend(["-c:a", "aac"])
    cmd.extend(["-avoid_negative_ts", "make_zero", "-movflags", "+faststart", str(out_path)])
    current_app.logger.info("Multi-range trim ffmpeg command: %s", " ".join(cmd))
    load_start = os.getloadavg() if hasattr(os, "getloadavg") else None
    started = time.monotonic()
    result = run_media_subprocess(
        cmd,
        operation="multi_range_trim",
        context=f"src={src_path.name} output={out_path.name} ranges={len(ranges)}",
        output_paths=[out_path],
        check=True,
        timeout=scale_media_timeout(
            FFMPEG_RENDER_TIMEOUT,
            duration_seconds=total_duration,
            multiplier=2.0,
            extra_seconds=120,
        ),
        capture_output=True,
        text=True,
    )
    load_end = os.getloadavg() if hasattr(os, "getloadavg") else None
    current_app.logger.info(
        "Multi-range trim completed output=%s ranges=%s duration=%.3f elapsed_ms=%s ffmpeg_speed=%s load_start=%s load_end=%s",
        out_path.name,
        len(ranges),
        total_duration,
        int((time.monotonic() - started) * 1000),
        _ffmpeg_final_speed(result.stderr),
        _format_loadavg(load_start),
        _format_loadavg(load_end),
    )
    return total_duration


def resolve_keep_ranges_for_trim(
    src_path: Path,
    keep_ranges: list[Dict[str, Any]],
    *,
    media_duration_seconds: Optional[float] = None,
    fallback_duration_seconds: Optional[float] = None,
    transcript_word_selection: bool = False,
    transcript_words: Optional[list[Dict[str, Any]]] = None,
) -> tuple[Path, list[dict[str, float]], bool]:
    """Resolve the exact source ranges used by the multi-range ffmpeg trim."""
    ranges = _normalize_keep_ranges_for_trim(keep_ranges)
    if not src_path.exists():
        raise FileNotFoundError(f"Source video not found: {src_path}")
    normalized_src_path = _normalize_source_cache_container(src_path)
    has_audio = _has_audio_stream(normalized_src_path)
    if len(ranges) > 1 and has_audio:
        ranges = _snap_keep_range_boundaries_to_audio(normalized_src_path, ranges)
    if ranges and has_audio and transcript_word_selection and transcript_words:
        ranges, _ = _snap_outer_keep_range_boundaries_to_audio(
            normalized_src_path,
            ranges,
            transcript_words,
        )
    if ranges:
        resolution = resolve_clip_ranges(
            start=ranges[0]["start"],
            end=ranges[-1]["end"],
            keep_ranges=ranges,
            media_duration_seconds=media_duration_seconds,
            fallback_duration_seconds=fallback_duration_seconds,
            transcript_word_selection=transcript_word_selection,
        )
        if not resolution.ok:
            raise ValueError(resolution.error)
        ranges = resolution.ranges
    return normalized_src_path, ranges, has_audio


def _probe_video_frame_rate(source: Path) -> Optional[float]:
    ffprobe_cmd = [
        _resolve_ffprobe(),
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=avg_frame_rate,r_frame_rate",
        "-of",
        "json",
        str(source),
    ]
    try:
        result = run_media_subprocess(
            ffprobe_cmd,
            operation="probe_video_frame_rate",
            context=f"source={source.name}",
            check=True,
            capture_output=True,
            text=True,
            timeout=FFPROBE_TIMEOUT,
        )
        data = json.loads(result.stdout or "{}")
        streams = data.get("streams") or []
        if not streams:
            return None
        stream = streams[0] or {}
        for key in ("avg_frame_rate", "r_frame_rate"):
            raw = str(stream.get(key) or "").strip()
            if not raw or raw == "0/0":
                continue
            if "/" in raw:
                numerator, denominator = raw.split("/", 1)
                value = float(numerator) / max(1.0, float(denominator))
            else:
                value = float(raw)
            if value > 0:
                return value
    except Exception as exc:
        current_app.logger.warning("FFprobe frame-rate check failed for %s: %s", source, exc)
    return None


def _ffmpeg_filter_expr(expr: str) -> str:
    return str(expr or "").replace("\\", "\\\\").replace(",", "\\,")


def _load_face_track_points(
    face_track_smooth_path: Optional[Path],
    *,
    clip_start: float,
    crop_w: float,
    crop_h: float,
) -> list[dict[str, float]]:
    if not face_track_smooth_path:
        return []
    path = Path(face_track_smooth_path)
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        current_app.logger.warning("Could not load face track smooth path=%s", path)
        return []
    if not isinstance(payload, list):
        return []
    max_x = max(0.0, 1.0 - float(crop_w))
    max_y = max(0.0, 1.0 - float(crop_h))
    points: list[dict[str, float]] = []
    for row in payload:
        if not isinstance(row, dict):
            continue
        try:
            t_abs = float(row.get("t"))
            cx = float(row.get("cx_ratio"))
            cy = float(row.get("cy_ratio"))
            h_ratio = float(row.get("h_ratio") or 0.0)
        except Exception:
            continue
        if not (0.0 <= cx <= 1.0 and 0.0 <= cy <= 1.0):
            continue
        points.append(
            {
                "t": max(0.0, t_abs - float(clip_start)),
                "cx": cx,
                "cy": cy,
                "h": max(0.0, min(1.0, h_ratio)),
                "x": max(0.0, min(max_x, cx - (float(crop_w) / 2.0))),
                "y": max(0.0, min(max_y, cy - (float(crop_h) / 2.0))),
            }
        )
    deduped: list[dict[str, float]] = []
    for point in sorted(points, key=lambda item: item["t"]):
        if deduped and abs(deduped[-1]["t"] - point["t"]) < 1e-3:
            deduped[-1] = point
        else:
            deduped.append(point)
    return deduped


def _face_aware_fill_zoom_for_segment(
    face_track_points: list[dict[str, float]],
    *,
    segment_start: float,
    segment_end: float,
    crop_h: float,
    target_face_ratio: float = FACE_AWARE_TARGET_FACE_RATIO,
    max_zoom: float = FACE_AWARE_MAX_ZOOM,
) -> float:
    """Return a static fill-segment zoom that makes the median face reach the target output-height ratio."""
    try:
        safe_crop_h = max(0.01, min(1.0, float(crop_h)))
        safe_target = max(0.01, float(target_face_ratio or FACE_AWARE_TARGET_FACE_RATIO))
        safe_max_zoom = max(1.0, float(max_zoom or FACE_AWARE_MAX_ZOOM))
    except Exception:
        return 1.0
    heights: list[float] = []
    for point in face_track_points or []:
        try:
            t_val = float(point.get("t") or 0.0)
            h_val = float(point.get("h") or 0.0)
        except Exception:
            continue
        if float(segment_start) <= t_val <= float(segment_end) and h_val > 0:
            heights.append(h_val)
    if not heights:
        return 1.0
    heights.sort()
    mid = len(heights) // 2
    median_h = heights[mid] if len(heights) % 2 else (heights[mid - 1] + heights[mid]) / 2.0
    h_portrait_equiv = median_h / safe_crop_h
    if h_portrait_equiv <= 0:
        return 1.0
    zoom = safe_target / h_portrait_equiv
    return round(max(1.0, min(safe_max_zoom, zoom)), 6)


def _piecewise_linear_ratio_expr(
    points: list[dict[str, float]],
    key: str,
    *,
    time_expr: str = "t",
) -> Optional[str]:
    cleaned: list[dict[str, float]] = []
    for point in sorted(points or [], key=lambda item: float(item.get("t") or 0.0)):
        try:
            t_val = float(point["t"])
            value = float(point[key])
        except Exception:
            continue
        normalized = {"t": t_val, key: value}
        if cleaned and abs(float(cleaned[-1]["t"]) - t_val) < 1e-6:
            cleaned[-1] = normalized
        else:
            cleaned.append(normalized)
    points = cleaned
    if not points:
        return None
    if len(points) == 1:
        return f"{points[0][key]:.6f}"
    t_var = str(time_expr or "t")
    parts: list[str] = []
    first = points[0]
    if float(first["t"]) > 0:
        parts.append(f"(lt({t_var},{float(first['t']):.6f})*{float(first[key]):.6f})")
    for index in range(0, len(points) - 1):
        current = points[index]
        nxt = points[index + 1]
        t0 = float(current["t"])
        t1 = float(nxt["t"])
        v0 = float(current[key])
        v1 = float(nxt[key])
        if t1 <= t0:
            segment_expr = f"{v0:.6f}"
        else:
            segment_expr = f"({v0:.6f}+({v1 - v0:.6f})*({t_var}-{t0:.6f})/{t1 - t0:.6f})"
        parts.append(f"(gte({t_var},{t0:.6f})*lt({t_var},{t1:.6f})*{segment_expr})")
    last = points[-1]
    parts.append(f"(gte({t_var},{float(last['t']):.6f})*{float(last[key]):.6f})")
    return "+".join(parts)


def _sanitize_text_for_overlay(text: str, max_len: int = 160) -> str:
    if not text:
        return ""
    t = (text or "").replace("\n", " ").replace("\r", " ").strip()
    t = t[:max_len]
    # remove only non-printable control chars, keep Unicode accents
    t = "".join(ch for ch in t if ord(ch) >= 32)
    return t


def _measure_text_width(font: ImageFont.FreeTypeFont, text: str) -> int:
    if not text:
        return 0
    try:
        return int(round(float(font.getlength(text))))
    except Exception:
        bbox = font.getbbox(text)
        return int(max(0, bbox[2] - bbox[0]))


def _measure_line_height(font: ImageFont.FreeTypeFont) -> int:
    try:
        ascent, descent = font.getmetrics()
        return int(max(1, ascent + descent))
    except Exception:
        bbox = font.getbbox("Ag")
        return int(max(1, bbox[3] - bbox[1]))


def _load_title_font(font_path: Optional[str], font_size: int) -> Optional[ImageFont.FreeTypeFont]:
    candidate = Path(str(font_path or "")).expanduser() if font_path else None
    if not candidate or not candidate.exists():
        return None
    try:
        kwargs: Dict[str, Any] = {}
        if PILLOW_RAQM_AVAILABLE and candidate == ARABIC_FALLBACK_FONT_PATH:
            kwargs["layout_engine"] = ImageFont.Layout.RAQM
        font = ImageFont.truetype(str(candidate), int(font_size), **kwargs)
        if candidate == ARABIC_FALLBACK_FONT_PATH:
            try:
                font.set_variation_by_name("Bold")
            except Exception:
                pass
        return font
    except Exception:
        current_app.logger.warning("Could not load title font for wrap measurement: %s", candidate)
        return None


def _title_font_path_for_text(text: Any, font_path: Optional[str]) -> str:
    if contains_arabic_script(text) and ARABIC_FALLBACK_FONT_PATH.exists():
        return str(ARABIC_FALLBACK_FONT_PATH)
    return str(font_path or "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")


def _wrap_words_to_width(words: list[str], font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    if not words:
        return []
    lines: list[str] = []
    current_words: list[str] = []
    current_width = 0
    space_width = _measure_text_width(font, " ")
    for word in words:
        word_width = _measure_text_width(font, word)
        candidate_width = word_width if not current_words else current_width + space_width + word_width
        if current_words and candidate_width > max_width:
            lines.append(" ".join(current_words))
            current_words = [word]
            current_width = word_width
            continue
        current_words.append(word)
        current_width = candidate_width
    if current_words:
        lines.append(" ".join(current_words))
    return lines


def _balance_two_title_lines(
    words: list[str],
    lines: list[str],
    font: ImageFont.FreeTypeFont,
    max_width: int,
) -> list[str]:
    """Choose the most even valid two-line split instead of leaving an orphan."""
    if len(lines) != 2 or len(words) < 3:
        return lines
    candidates: list[tuple[int, int, list[str]]] = []
    for split_at in range(1, len(words)):
        candidate = [" ".join(words[:split_at]), " ".join(words[split_at:])]
        widths = [_measure_text_width(font, line) for line in candidate]
        if max(widths) > max_width:
            continue
        word_imbalance = abs(split_at - (len(words) - split_at))
        width_imbalance = abs(widths[0] - widths[1])
        candidates.append((word_imbalance, width_imbalance, candidate))
    if not candidates:
        return lines
    return min(candidates, key=lambda item: (item[0], item[1]))[2]


def _title_side_margin(target_width: int) -> int:
    scale = max(1.0, float(target_width) / float(VIDEO_TARGET_WIDTH))
    return max(TITLE_WRAP_SIDE_MARGIN, int(round(TITLE_WRAP_SIDE_MARGIN * scale)))


def _title_layout_metrics(
    *,
    base_y: int,
    font_size: int,
    line_count: int,
    line_height: int,
    line_spacing: int,
) -> dict[str, int]:
    safe_lines = max(1, int(line_count))
    safe_line_height = max(1, int(line_height))
    safe_spacing = int(line_spacing)
    block_height = safe_line_height * safe_lines + safe_spacing * max(0, safe_lines - 1)
    block_height = max(safe_line_height, block_height)
    visual_y = _title_visual_y(base_y, font_size)
    # Title position is its top edge, independent of the number of lines.
    adjusted_y = visual_y
    return {
        "line_height": safe_line_height,
        "block_height": block_height,
        "draw_y": max(0, adjusted_y),
    }


def _fit_title_text(
    text: str,
    *,
    font_path: Optional[str],
    font_size: int,
    target_width: int,
    target_height: int,
    base_y: int,
    line_spacing: int,
    max_lines: int = TITLE_WRAP_MAX_LINES,
    min_font_size: int = TITLE_WRAP_MIN_FONT_SIZE,
) -> dict[str, Any]:
    if not text:
        return {
            "text": "",
            "font_size": int(font_size),
            "draw_y": _title_visual_y(base_y, font_size),
            "line_count": 0,
            "side_margin": _title_side_margin(target_width),
        }
    font_path = _title_font_path_for_text(text, font_path)
    words = text.split()
    if not words:
        return {
            "text": "",
            "font_size": int(font_size),
            "draw_y": _title_visual_y(base_y, font_size),
            "line_count": 0,
            "side_margin": _title_side_margin(target_width),
        }

    side_margin = _title_side_margin(target_width)
    max_text_width = max(
        120,
        int(target_width) - (2 * side_margin) - (2 * TITLE_BOX_BORDER_WIDTH),
    )
    chosen_lines = [" ".join(words)]
    chosen_font_size = max(int(font_size), int(min_font_size))
    chosen_layout = _title_layout_metrics(
        base_y=base_y,
        font_size=chosen_font_size,
        line_count=1,
        line_height=max(chosen_font_size, 1),
        line_spacing=line_spacing,
    )
    measured = False

    for candidate_font_size in range(int(font_size), int(min_font_size) - 1, -1):
        font = _load_title_font(font_path, candidate_font_size)
        if font is None:
            break
        measured = True
        lines = _wrap_words_to_width(words, font, max_text_width)
        lines = _balance_two_title_lines(words, lines, font, max_text_width)
        if len(lines) > max_lines:
            continue
        line_height = _measure_line_height(font)
        layout = _title_layout_metrics(
            base_y=base_y,
            font_size=candidate_font_size,
            line_count=len(lines),
            line_height=line_height,
            line_spacing=line_spacing,
        )
        chosen_lines = lines
        chosen_font_size = candidate_font_size
        chosen_layout = layout
        break

    if not measured:
        current_words: list[str] = []
        fallback_lines: list[str] = []
        for word in words:
            candidate_words = current_words + [word]
            candidate_line = " ".join(candidate_words)
            if len(candidate_line) > TITLE_WRAP_LENGTH and current_words:
                fallback_lines.append(" ".join(current_words))
                current_words = [word]
            else:
                current_words = candidate_words
        if current_words:
            fallback_lines.append(" ".join(current_words))
        chosen_lines = fallback_lines or chosen_lines
        chosen_font_size = int(font_size)
        chosen_layout = _title_layout_metrics(
            base_y=base_y,
            font_size=chosen_font_size,
            line_count=len(chosen_lines),
            line_height=max(chosen_font_size, 1),
            line_spacing=line_spacing,
        )

    block_height = int(chosen_layout.get("block_height") or max(1, chosen_font_size))
    draw_y = clamp_design_position(
        "title_margin",
        chosen_layout["draw_y"],
        target_height=target_height,
        element_height=block_height,
    )
    return {
        "text": "\n".join(chosen_lines),
        "font_size": chosen_font_size,
        "draw_y": draw_y,
        "block_height": block_height,
        "line_count": len(chosen_lines),
        "side_margin": side_margin,
    }


_TR_UPPER_MAP = str.maketrans({
    "i": "İ",
    "ı": "I",
})


def _turkish_upper(text: str) -> str:
    return str(text or "").translate(_TR_UPPER_MAP).upper()


def _normalize_title_language(language: Any) -> str:
    value = str(language or "").strip().lower()
    if not value:
        return ""
    if value.startswith("tr") or value in {"tur", "turkish", "turkce"}:
        return "tr"
    return value[:2]


def _apply_title_case(text: str, title_language: Optional[str], uppercase: bool) -> str:
    """Apply the shared, language-aware title casing used by every title engine."""
    value = str(text or "")
    if not uppercase:
        return value
    return _turkish_upper(value) if _normalize_title_language(title_language) == "tr" else value.upper()


def _hex_to_rgba(color: Optional[str], alpha: int = 255) -> tuple[int, int, int, int]:
    value = str(color or "").strip()
    if not value.startswith("#") or len(value) != 7:
        value = "#FFFFFF"
    try:
        red = int(value[1:3], 16)
        green = int(value[3:5], 16)
        blue = int(value[5:7], 16)
    except ValueError:
        red, green, blue = 255, 255, 255
    return (
        red,
        green,
        blue,
        max(0, min(255, int(alpha))),
    )


def _build_blurred_text_shadow(
    entries: List[Dict[str, Any]],
    *,
    canvas_width: int,
    canvas_height: int,
    font: ImageFont.FreeTypeFont,
    shadow_color: str,
    shadow_opacity: float,
    shadow_offset_x: int,
    shadow_offset_y: int,
    shadow_blur_radius: int,
) -> Optional[Image.Image]:
    if not entries:
        return None
    opacity = max(0.0, min(1.0, float(shadow_opacity or 0.0)))
    if opacity <= 0.0:
        return None
    if not str(shadow_color or "").strip():
        return None
    shadow_layer = Image.new("RGBA", (canvas_width, canvas_height), (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow_layer)
    shadow_fill = _hex_to_rgba(shadow_color, int(round(opacity * 255)))
    for entry in entries:
        shadow_draw.text(
            (
                int(entry["x"]) + int(shadow_offset_x),
                int(entry["y"]) + int(shadow_offset_y),
            ),
            str(entry["text"]),
            font=font,
            fill=shadow_fill,
            anchor="la",
        )
    if shadow_blur_radius > 0:
        shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(int(shadow_blur_radius)))
    return shadow_layer


def _resolve_title_shadow_style(subtitle_preset: Optional[str]) -> dict[str, Any]:
    preset = SUBTITLE_PRESETS.get(str(subtitle_preset or "").strip()) or {}
    try:
        shadow_opacity = max(0.0, min(1.0, float(preset.get("shadow_opacity", 0.0) or 0.0)))
    except Exception:
        shadow_opacity = 0.0
    try:
        shadow_offset_x = int(preset.get("shadow_offset_x", 0) or 0)
    except Exception:
        shadow_offset_x = 0
    try:
        shadow_offset_y = int(preset.get("shadow_offset_y", 0) or 0)
    except Exception:
        shadow_offset_y = 0
    try:
        shadow_blur_radius = max(0, int(preset.get("shadow_blur_radius", 0) or 0))
    except Exception:
        shadow_blur_radius = 0
    return {
        "shadow_color": str(preset.get("shadow_color") or "").strip(),
        "shadow_opacity": shadow_opacity,
        "shadow_offset_x": shadow_offset_x,
        "shadow_offset_y": shadow_offset_y,
        "shadow_blur_radius": shadow_blur_radius,
    }


def _relative_luminance(hex_color: Optional[str]) -> float:
    value = str(hex_color or "#FFFFFF").strip().lstrip("#")
    if len(value) != 6 or any(char not in "0123456789abcdefABCDEF" for char in value):
        value = "FFFFFF"
    channels = [int(value[index:index + 2], 16) / 255.0 for index in (0, 2, 4)]
    linear = [channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4 for channel in channels]
    return (0.2126 * linear[0]) + (0.7152 * linear[1]) + (0.0722 * linear[2])


def _resolve_title_text_effects(
    *,
    subtitle_preset: Optional[str],
    title_text_color: Optional[str],
    title_bg_alpha: Optional[int],
    title_outline_color: Optional[str],
    title_outline_width: int,
) -> dict[str, Any]:
    """Resolve title effects once for both browser measurement and Pillow output."""
    has_box = _normalize_alpha_percent(title_bg_alpha, DEFAULT_TITLE_BG_ALPHA) > 0
    light_text = _relative_luminance(title_text_color) > 0.5
    if has_box or not light_text:
        return {
            "outline_color": str(title_outline_color or "#000000"),
            "outline_width": 0,
            "shadow": {
                "shadow_color": "",
                "shadow_opacity": 0.0,
                "shadow_offset_x": 0,
                "shadow_offset_y": 0,
                "shadow_blur_radius": 0,
            },
        }
    return {
        "outline_color": str(title_outline_color or "#000000"),
        "outline_width": max(0, int(title_outline_width or 0)),
        "shadow": _resolve_title_shadow_style(subtitle_preset),
    }


def _title_overlay_layout(
    text: str,
    *,
    font_path: str,
    font_size: int,
    target_width: int,
    target_height: int,
    base_y: int,
    line_spacing: int,
    title_text_color: Optional[str],
    subtitle_preset: Optional[str],
    title_bg_color: Optional[str] = DEFAULT_TITLE_BG_COLOR,
    title_bg_alpha: Optional[int] = DEFAULT_TITLE_BG_ALPHA,
    uppercase: bool,
    video_top_limit: Optional[int],
    title_language: Optional[str] = None,
    title_outline_color: Optional[str] = "#000000",
    title_outline_width: int = 0,
    max_lines: int = TITLE_WRAP_MAX_LINES,
    min_font_size: int = TITLE_WRAP_MIN_FONT_SIZE,
) -> dict[str, Any]:
    """Measure title geometry without generating an image."""
    font_size = max(1, int(round(font_size * DESIGN_GEOMETRY["font_scale"])))
    min_font_size = min(font_size, min_font_size)
    display_text = _apply_title_case(text, title_language, uppercase)
    effective_font_path = str(ARABIC_FALLBACK_FONT_PATH) if contains_arabic_script(display_text) else font_path
    font = _load_title_font(effective_font_path, font_size)
    if font is None:
        raise RuntimeError(f"Pillow title font could not be resolved: {effective_font_path}")

    words = display_text.split()
    if not words:
        raise RuntimeError("Pillow title render requested with empty text")

    side_margin = _title_side_margin(target_width)
    effects = _resolve_title_text_effects(
        subtitle_preset=subtitle_preset,
        title_text_color=title_text_color,
        title_bg_alpha=title_bg_alpha,
        title_outline_color=title_outline_color,
        title_outline_width=title_outline_width,
    )
    safe_outline_width = effects["outline_width"]
    # Reserve the outline width in the wrapping calculation so strokes stay in bounds.
    max_text_width = max(120, int(target_width) - (2 * (side_margin + safe_outline_width)))
    safe_line_spacing = int(line_spacing)
    chosen_lines: list[str] = [" ".join(words)]
    chosen_font_size = max(int(font_size), int(min_font_size))
    chosen_font = font
    chosen_line_height = _measure_line_height(font)

    for candidate_font_size in range(int(font_size), int(min_font_size) - 1, -1):
        candidate_font = _load_title_font(effective_font_path, candidate_font_size)
        if candidate_font is None:
            continue
        candidate_lines = _wrap_words_to_width(words, candidate_font, max_text_width)
        candidate_lines = _balance_two_title_lines(
            words, candidate_lines, candidate_font, max_text_width
        )
        if len(candidate_lines) > max_lines:
            if candidate_font_size == int(min_font_size):
                chosen_lines = candidate_lines
                chosen_font_size = candidate_font_size
                chosen_font = candidate_font
                chosen_line_height = _measure_line_height(candidate_font)
            continue
        chosen_lines = candidate_lines
        chosen_font_size = candidate_font_size
        chosen_font = candidate_font
        chosen_line_height = _measure_line_height(candidate_font)
        break

    block_height = (
        chosen_line_height * len(chosen_lines)
        + safe_line_spacing * max(0, len(chosen_lines) - 1)
    )
    draw_y = clamp_design_position(
        "title_margin",
        base_y,
        target_height=target_height,
        element_height=block_height,
    )
    if video_top_limit is not None and block_height > 0 and draw_y + block_height > int(video_top_limit):
        draw_y = max(0, int(video_top_limit) - block_height)

    line_entries: List[Dict[str, Any]] = []
    for line_index, line in enumerate(chosen_lines):
        line_width = _measure_text_width(chosen_font, line)
        line_x = int(round((target_width - line_width) / 2.0))
        line_y = draw_y + line_index * (chosen_line_height + safe_line_spacing)
        line_entries.append(
            {
                "text": line,
                "x": line_x,
                "y": line_y,
            }
        )

    return dict(font=chosen_font, font_size=chosen_font_size, lines=chosen_lines,
                line_height=chosen_line_height, entries=line_entries, draw_y=draw_y,
                block_height=block_height, side_margin=side_margin,
                outline_width=safe_outline_width, max_text_width=max_text_width,
                outline_color=effects["outline_color"], shadow=effects["shadow"],
                text=display_text)


def _render_title_overlay(
    text: str,
    *,
    font_path: str,
    font_size: int,
    target_width: int,
    target_height: int,
    base_y: int,
    line_spacing: int,
    title_text_color: Optional[str],
    subtitle_preset: Optional[str],
    title_bg_color: Optional[str] = DEFAULT_TITLE_BG_COLOR,
    title_bg_alpha: Optional[int] = DEFAULT_TITLE_BG_ALPHA,
    uppercase: bool,
    video_top_limit: Optional[int],
    title_language: Optional[str] = None,
    title_outline_color: Optional[str] = "#000000",
    title_outline_width: int = 0,
    max_lines: int = TITLE_WRAP_MAX_LINES,
    min_font_size: int = TITLE_WRAP_MIN_FONT_SIZE,
) -> tuple[Path, dict[str, Any]]:
    layout = _title_overlay_layout(
        text, font_path=font_path, font_size=font_size, target_width=target_width,
        target_height=target_height, base_y=base_y, line_spacing=line_spacing,
        title_text_color=title_text_color, subtitle_preset=subtitle_preset,
        title_bg_color=title_bg_color, title_bg_alpha=title_bg_alpha,
        uppercase=uppercase, video_top_limit=video_top_limit,
        title_language=title_language, title_outline_color=title_outline_color,
        title_outline_width=title_outline_width, max_lines=max_lines, min_font_size=min_font_size,
    )
    chosen_font = layout["font"]
    chosen_font_size = layout["font_size"]
    chosen_lines = layout["lines"]
    line_entries = layout["entries"]
    draw_y = layout["draw_y"]
    block_height = layout["block_height"]
    side_margin = layout["side_margin"]
    safe_outline_width = layout["outline_width"]
    max_text_width = layout["max_text_width"]
    display_text = layout["text"]
    overlay = Image.new("RGBA", (int(target_width), int(target_height)), (0, 0, 0, 0))
    shadow_style = layout["shadow"]
    shadow_layer = _build_blurred_text_shadow(
        line_entries,
        canvas_width=int(target_width),
        canvas_height=int(target_height),
        font=chosen_font,
        shadow_color=shadow_style["shadow_color"],
        shadow_opacity=shadow_style["shadow_opacity"],
        shadow_offset_x=shadow_style["shadow_offset_x"],
        shadow_offset_y=shadow_style["shadow_offset_y"],
        shadow_blur_radius=shadow_style["shadow_blur_radius"],
    )
    if shadow_layer is not None:
        overlay = Image.alpha_composite(overlay, shadow_layer)

    draw = ImageDraw.Draw(overlay)
    bg_alpha = _normalize_alpha_percent(title_bg_alpha, DEFAULT_TITLE_BG_ALPHA)
    if bg_alpha > 0 and line_entries:
        bg_pad_x = max(DESIGN_GEOMETRY["title_padding_min_x"], int(round(float(chosen_font_size) * DESIGN_GEOMETRY["title_padding_x"])))
        bg_pad_y = max(DESIGN_GEOMETRY["title_padding_min_y"], int(round(float(chosen_font_size) * DESIGN_GEOMETRY["title_padding_y"])))
        bg_left = int(target_width)
        bg_top = int(target_height)
        bg_right = 0
        bg_bottom = 0
        for entry in line_entries:
            bbox = draw.textbbox(
                (int(entry["x"]), int(entry["y"])),
                str(entry["text"]),
                font=chosen_font,
                anchor="la",
                stroke_width=safe_outline_width,
            )
            bg_left = min(bg_left, int(bbox[0]))
            bg_top = min(bg_top, int(bbox[1]))
            bg_right = max(bg_right, int(bbox[2]))
            bg_bottom = max(bg_bottom, int(bbox[3]))
        if bg_right > bg_left and bg_bottom > bg_top:
            draw.rounded_rectangle(
                [
                    max(0, bg_left - bg_pad_x),
                    max(0, bg_top - bg_pad_y),
                    min(int(target_width), bg_right + bg_pad_x),
                    min(int(target_height), bg_bottom + bg_pad_y),
                ],
                radius=max(DESIGN_GEOMETRY["box_radius_min"], int(round(float(chosen_font_size) * DESIGN_GEOMETRY["title_box_radius"]))),
                fill=_hex_to_rgba(title_bg_color or DEFAULT_TITLE_BG_COLOR, int(round(bg_alpha * 255 / 100.0))),
            )
    text_fill = _hex_to_rgba(title_text_color or "#FFFFFF", 255)
    outline_fill = _hex_to_rgba(layout["outline_color"], 255)
    for entry in line_entries:
        draw_args = {
            "font": chosen_font,
            "fill": text_fill,
            "anchor": "la",
        }
        if safe_outline_width > 0:
            draw_args.update(stroke_fill=outline_fill, stroke_width=safe_outline_width)
        draw.text((int(entry["x"]), int(entry["y"])), str(entry["text"]), **draw_args)

    handle = tempfile.NamedTemporaryFile(delete=False, suffix=".png", prefix="title_overlay_")
    handle.close()
    output_path = Path(handle.name)
    overlay.save(output_path)
    return output_path, {
        "text": display_text,
        "font_size": chosen_font_size,
        "line_count": len(chosen_lines),
        "side_margin": side_margin + safe_outline_width,
        "max_text_width": max_text_width,
        "draw_y": draw_y,
        "block_height": block_height,
        "shadow": shadow_style,
    }


def _escape_ass_path(path: Path) -> str:
    return str(path).replace("\\", "\\\\").replace("'", "\\'")


def _write_debug_textfile(text: str) -> Path:
    tmp_dir = Path(tempfile.gettempdir())
    txt_path = tmp_dir / f"title_debug_{secrets.token_hex(8)}.txt"
    txt_path.write_text(text, encoding="utf-8")
    return txt_path


def _hex_to_drawtext_color(hex_code: Optional[str], default: str = "#FFFF00") -> str:
    value = (hex_code or default).lstrip("#").upper()
    if len(value) != 6 or any(ch not in "0123456789ABCDEF" for ch in value):
        value = default.lstrip("#").upper()
    return f"0x{value}"


def _hex_to_ass_color(hex_code: Optional[str], default: str = "#FFFFFF") -> str:
    value = (hex_code or default).lstrip("#").upper()
    if len(value) != 6 or any(ch not in "0123456789ABCDEF" for ch in value):
        value = default.lstrip("#").upper()
    rr = value[0:2]
    gg = value[2:4]
    bb = value[4:6]
    return f"&H00{bb}{gg}{rr}"


def _hex_to_ass_color_with_alpha(
    hex_code: Optional[str],
    alpha_percent: Optional[int],
    default: str = "#FFFFFF",
    default_alpha: int = 100,
) -> str:
    value = (hex_code or default).lstrip("#").upper()
    if len(value) != 6 or any(ch not in "0123456789ABCDEF" for ch in value):
        value = default.lstrip("#").upper()
    opacity = _normalize_alpha_percent(alpha_percent, default_alpha)
    ass_alpha = max(0, min(255, round(255 * (1 - (opacity / 100)))))
    rr = value[0:2]
    gg = value[2:4]
    bb = value[4:6]
    return f"&H{ass_alpha:02X}{bb}{gg}{rr}"


def _normalize_alpha_percent(value: Optional[int], default: int = DEFAULT_TITLE_BG_ALPHA) -> int:
    try:
        alpha = int(float(value if value is not None else default))
    except Exception:
        alpha = int(default)
    return max(0, min(100, alpha))


def _overlay_y_expr(
    top_offset: int,
    subtitle_path: Optional[Path],
    subtitle_margin: Optional[int],
    subtitle_font_size: Optional[int],
) -> str:
    safe_bottom = 220
    if subtitle_path:
        try:
            font_size = int(subtitle_font_size or 0)
        except (TypeError, ValueError):
            font_size = 0
        try:
            margin = int(subtitle_margin or 0)
        except (TypeError, ValueError):
            margin = 0
        safe_bottom = max(safe_bottom, margin + font_size * 2)
    expr = f"max(0,min({top_offset},H-h-{safe_bottom}))"
    return expr.replace(",", r"\,")


def _subtitle_force_style(
    *,
    target_width: int,
    target_height: int,
    subtitle_font_size: int,
    subtitle_margin: int,
    caption_anchor: str = "bottom",
    subtitle_font: str,
    subtitle_text_color: Optional[str],
    subtitle_text_alpha: Optional[int],
    subtitle_bg_color: Optional[str],
    subtitle_bg_alpha: Optional[int],
    subtitle_style: Optional[str] = None,
) -> str:
    clean_font = (subtitle_font or "DejaVu Sans").replace("'", "")
    normalized_subtitle_style = str(subtitle_style or "plain").strip().lower()
    alignment = {"top": 8, "center": 5, "bottom": 2}[normalize_caption_anchor(caption_anchor)]
    base = (
        f"PlayResX={int(target_width)},"
        f"PlayResY={int(target_height)},"
        f"Fontsize={subtitle_font_size},"
    )
    if normalized_subtitle_style == "karaoke":
        return base + f"MarginV={subtitle_margin}," + f"Alignment={alignment},"
    return (
        base
        + f"PrimaryColour={_hex_to_ass_color_with_alpha(subtitle_text_color, subtitle_text_alpha, '#FFFFFF', DEFAULT_SUBTITLE_TEXT_ALPHA)},"
        + f"BackColour={_hex_to_ass_color_with_alpha(subtitle_bg_color, subtitle_bg_alpha, DEFAULT_SUBTITLE_BG_COLOR, DEFAULT_SUBTITLE_BG_ALPHA)},"
        + "BorderStyle=4,"
        + "Outline=1,"
        + "Shadow=0,"
        + f"MarginV={subtitle_margin},"
        + f"Alignment={alignment},"
        + f"FontName={clean_font}"
    )


def _anchored_ass_from_srt(
    subtitle_path: Path,
    *,
    target_width: int,
    target_height: int,
    caption_position: int,
    caption_anchor: str,
) -> Optional[Path]:
    """Convert SRT cues to ASS so an arbitrary top/center anchor can use \pos."""
    if Path(subtitle_path).suffix.lower() == ".ass":
        return None
    try:
        raw = Path(subtitle_path).read_text(encoding="utf-8-sig")
    except Exception:
        return None
    anchor = normalize_caption_anchor(caption_anchor)
    alignment = {"top": 8, "center": 5, "bottom": 2}[anchor]
    y = int(caption_position) if anchor in {"top", "center"} else int(target_height) - int(caption_position)
    events = []
    for block in re.split(r"\r?\n\s*\r?\n", raw.strip()):
        lines = [line.strip("\ufeff") for line in block.splitlines() if line.strip()]
        timing_index = next((idx for idx, line in enumerate(lines) if "-->" in line), None)
        if timing_index is None:
            continue
        start_raw, end_raw = [part.strip().replace(",", ".") for part in lines[timing_index].split("-->", 1)]
        def ass_time(value: str) -> str:
            hours, minutes, seconds = value.split(":")
            return f"{int(hours)}:{int(minutes):02d}:{float(seconds):05.2f}"
        try:
            start_value, end_value = ass_time(start_raw), ass_time(end_raw)
        except Exception:
            continue
        text_value = r"\N".join(lines[timing_index + 1:]).replace("{", r"\{").replace("}", r"\}")
        events.append(f"Dialogue: 0,{start_value},{end_value},Default,,0,0,0,,{{\\an{alignment}\\pos({int(target_width)//2},{y})}}{text_value}")
    if not events:
        return None
    content = [
        "[Script Info]", "ScriptType: v4.00+", "WrapStyle: 2", "ScaledBorderAndShadow: yes",
        f"PlayResX: {int(target_width)}", f"PlayResY: {int(target_height)}", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Default,DejaVu Sans,45,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,4,1,0,{alignment},40,40,0,1",
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text", *events,
    ]
    handle = tempfile.NamedTemporaryFile(delete=False, suffix=".ass")
    path = Path(handle.name)
    handle.write("\n".join(content).encode("utf-8"))
    handle.close()
    return path


def _face_aware_fit_position_metrics(
    *,
    target_width: int = VIDEO_TARGET_WIDTH,
    target_height: int = VIDEO_TARGET_HEIGHT,
    subtitle_font_size: int = DEFAULT_SUB_FONT_SIZE,
) -> Dict[str, float]:
    frame_height = float(target_width) * 9.0 / 16.0
    frame_top = min(
        max(0.0, FACE_AWARE_FIT_FRAME_TOP),
        max(0.0, float(target_height) - frame_height),
    )
    frame_bottom = frame_top + frame_height
    caption_block_height = max(24.0, float(subtitle_font_size or DEFAULT_SUB_FONT_SIZE) * 2.0)
    caption_top = min(float(target_height) - caption_block_height - 30.0, frame_bottom + DESIGN_GEOMETRY["fit_caption_gap"])
    caption_margin_v = max(0.0, float(target_height) - caption_top - caption_block_height)
    subscribe_y = min(float(target_height) - 30.0, caption_top + caption_block_height + DESIGN_GEOMETRY["fit_caption_gap"])
    return {
        "frame_top": frame_top,
        "frame_bottom": frame_bottom,
        "title_bottom": max(0.0, frame_top - DESIGN_GEOMETRY["fit_caption_gap"]),
        "caption_top": caption_top,
        "caption_margin_v": caption_margin_v,
        "subscribe_y": subscribe_y,
    }


def _face_aware_top_caption_subscribe_metrics(
    *,
    target_height: int,
    subtitle_font_size: int,
    subtitle_top: int,
) -> Dict[str, float]:
    """Reserve two caption lines, then fit subscribe above the Shorts UI zone."""
    design_scale = max(0.01, float(target_height) / float(DESIGN_GEOMETRY["height"]))
    caption_top = float(subtitle_top) * design_scale
    max_caption_height = (
        float(subtitle_font_size)
        * design_scale
        * FACE_AWARE_TOP_CAPTION_MAX_LINES
        * FACE_AWARE_TOP_CAPTION_LINE_HEIGHT_MULTIPLIER
    )
    gap = float(target_height) * FACE_AWARE_TOP_CAPTION_SUBSCRIBE_GAP_RATIO
    subscribe_top = caption_top + max_caption_height + gap
    ui_top = float(target_height) * FACE_AWARE_SHORTS_UI_TOP_RATIO
    return {
        "caption_top": caption_top,
        "max_caption_height": max_caption_height,
        "subscribe_gap": gap,
        "subscribe_top": min(subscribe_top, ui_top - 1.0),
        "subscribe_max_height": max(1.0, ui_top - subscribe_top),
        "ui_top": ui_top,
    }


def _overlay_visible_alpha_geometry(path: Path) -> Optional[Dict[str, int]]:
    """Return the union of visible pixels across an image overlay's frames."""
    try:
        with Image.open(path) as image:
            canvas_width, canvas_height = image.size
            union_bbox = None
            primary_bbox = None
            for frame in ImageSequence.Iterator(image):
                bbox = frame.convert("RGBA").getchannel("A").getbbox()
                if not bbox:
                    continue
                if primary_bbox is None:
                    primary_bbox = bbox
                if union_bbox is None:
                    union_bbox = bbox
                else:
                    union_bbox = (
                        min(union_bbox[0], bbox[0]),
                        min(union_bbox[1], bbox[1]),
                        max(union_bbox[2], bbox[2]),
                        max(union_bbox[3], bbox[3]),
                    )
            if not union_bbox or not primary_bbox:
                return None
            return {
                "canvas_width": int(canvas_width),
                "canvas_height": int(canvas_height),
                "top": int(union_bbox[1]),
                "width": max(1, int(union_bbox[2] - union_bbox[0])),
                "height": max(1, int(union_bbox[3] - union_bbox[1])),
                "primary_top": int(primary_bbox[1]),
                "primary_width": max(1, int(primary_bbox[2] - primary_bbox[0])),
                "primary_height": max(1, int(primary_bbox[3] - primary_bbox[1])),
            }
    except Exception:
        return None


def _overlay_canvas_dimensions(path: Path) -> Optional[Tuple[int, int]]:
    try:
        with Image.open(path) as image:
            return int(image.width), int(image.height)
    except Exception:
        pass
    try:
        cmd = [
            _resolve_ffprobe(),
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=p=0:s=x",
            str(path),
        ]
        result = run_media_subprocess(
            cmd,
            operation="probe_overlay_dimensions",
            context=f"path={path.name}",
            check=True,
            capture_output=True,
            text=True,
            timeout=FFPROBE_TIMEOUT,
        )
        width, height = result.stdout.strip().split("x", 1)
        return int(width), int(height)
    except Exception:
        return None


def _face_aware_split_position_metrics(
    *,
    target_height: int = VIDEO_TARGET_HEIGHT,
    subtitle_font_size: int = DEFAULT_SUB_FONT_SIZE,
) -> Dict[str, float]:
    caption_block_height = _face_aware_caption_block_height(subtitle_font_size)
    caption_top = max(0.0, min(float(target_height) - caption_block_height, (float(target_height) * DESIGN_GEOMETRY["split_ratio"]) - (caption_block_height / 2.0)))
    return {
        "caption_top": caption_top,
        "caption_margin_v": max(0.0, float(target_height) - caption_top - caption_block_height),
        "caption_block_height": caption_block_height,
    }


def _face_aware_caption_block_height(subtitle_font_size: Optional[int]) -> float:
    return max(24.0, float(subtitle_font_size or DEFAULT_SUB_FONT_SIZE) * 2.0)


def _face_aware_event_caption_block(metrics: Optional[Dict[str, Any]], *, fallback_top: float, fallback_height: float) -> Dict[str, float]:
    metrics = metrics if isinstance(metrics, dict) else {}
    bbox = metrics.get("block_bbox")
    try:
        if isinstance(bbox, (list, tuple)) and len(bbox) >= 4:
            top = float(bbox[1])
            bottom = float(bbox[3])
            if bottom > top:
                return {
                    "top": top,
                    "height": bottom - top,
                    "center_y": (top + bottom) / 2.0,
                }
    except Exception:
        pass
    try:
        center_y = float(metrics.get("block_center_y"))
        height = max(1.0, float(metrics.get("block_height") or fallback_height))
        return {"top": center_y - (height / 2.0), "height": height, "center_y": center_y}
    except Exception:
        height = max(1.0, float(fallback_height or _face_aware_caption_block_height(None)))
        top = float(fallback_top or 0.0)
        return {"top": top, "height": height, "center_y": top + (height / 2.0)}


def _face_aware_caption_top_for_block(
    segment: Dict[str, Any],
    *,
    block_height: float,
    target_width: int,
    target_height: int,
    subtitle_margin: Optional[int],
    subtitle_margin_custom: Optional[bool] = None,
) -> float:
    mode = str(segment.get("mode") or "fill").strip().lower()
    safe_block_height = max(1.0, float(block_height or _face_aware_caption_block_height(None)))
    if not _is_face_aware_default_subtitle_margin(
        subtitle_margin,
        subtitle_margin_custom=subtitle_margin_custom,
    ):
        margin = clamp_design_position(
            "sub_margin",
            subtitle_margin,
            target_height=target_height,
            element_height=safe_block_height,
        )
        return max(0.0, float(target_height) - float(margin) - safe_block_height)
    if mode == "split":
        return max(0.0, min(float(target_height) - safe_block_height, (float(target_height) * DESIGN_GEOMETRY["split_ratio"]) - (safe_block_height / 2.0)))
    if mode == "fit":
        metrics = _face_aware_fit_position_metrics(
            target_width=target_width,
            target_height=target_height,
            subtitle_font_size=int(max(1, round(safe_block_height / 2.0))),
        )
        return max(0.0, min(float(target_height) - safe_block_height, float(metrics["frame_bottom"]) + DESIGN_GEOMETRY["fit_caption_gap"]))
    try:
        face_bottom = segment.get("caption_face_bottom")
        if face_bottom is not None:
            min_top = FACE_AWARE_FILL_CAPTION_MIN_TOP
            max_top = max(min_top, float(target_height) - FACE_AWARE_FILL_SUBSCRIBE_ROOM - safe_block_height)
            return max(min_top, min(max_top, float(face_bottom) + FACE_AWARE_FILL_CAPTION_GAP))
    except Exception:
        pass
    margin = FACE_AWARE_FILL_FALLBACK_SUBTITLE_MARGIN
    return max(0.0, float(target_height) - float(margin) - safe_block_height)


def _is_face_aware_default_subtitle_margin(
    subtitle_margin: Optional[int],
    *,
    subtitle_margin_custom: Optional[bool] = None,
) -> bool:
    if subtitle_margin_custom is not None:
        if isinstance(subtitle_margin_custom, str):
            is_custom = subtitle_margin_custom.strip().lower() in {"1", "true", "yes", "on", "t"}
        else:
            is_custom = bool(subtitle_margin_custom)
        return not is_custom
    try:
        margin = int(round(float(subtitle_margin)))
    except Exception:
        return True
    return margin in FACE_AWARE_LEGACY_DEFAULT_SUBTITLE_MARGINS


def _face_aware_fill_subtitle_margin(
    subtitle_margin: Optional[int],
    *,
    subtitle_margin_custom: Optional[bool] = None,
) -> int:
    if _is_face_aware_default_subtitle_margin(subtitle_margin, subtitle_margin_custom=subtitle_margin_custom):
        return FACE_AWARE_FILL_FALLBACK_SUBTITLE_MARGIN
    try:
        return int(round(float(subtitle_margin)))
    except Exception:
        return FACE_AWARE_FILL_FALLBACK_SUBTITLE_MARGIN


def _face_aware_fill_caption_metrics_for_segment(
    segment: Dict[str, Any],
    *,
    face_track_points: List[Dict[str, float]],
    crop_h: float,
    target_height: int,
    subtitle_font_size: int,
    subtitle_margin: Optional[int],
    subtitle_margin_custom: Optional[bool] = None,
) -> Dict[str, Optional[float]]:
    caption_block_height = _face_aware_caption_block_height(subtitle_font_size)
    if not _is_face_aware_default_subtitle_margin(subtitle_margin, subtitle_margin_custom=subtitle_margin_custom):
        margin = _face_aware_fill_subtitle_margin(subtitle_margin, subtitle_margin_custom=subtitle_margin_custom)
        caption_top = max(0.0, float(target_height) - float(margin) - caption_block_height)
        return {
            "median_face_bottom": None,
            "caption_top": caption_top,
            "caption_margin_v": float(margin),
            "caption_block_height": caption_block_height,
            "subscribe_y": caption_top + caption_block_height + DESIGN_GEOMETRY["fit_caption_gap"],
        }
    try:
        seg_start = float(segment.get("start") or 0.0)
        seg_end = float(segment.get("end") or 0.0)
    except Exception:
        seg_start = 0.0
        seg_end = 0.0
    try:
        segment_zoom = max(1.0, min(float(FACE_AWARE_MAX_ZOOM), float(segment.get("zoom") or 1.0)))
    except Exception:
        segment_zoom = 1.0
    segment_crop_h = max(0.01, min(1.0, float(crop_h or 1.0) / segment_zoom))
    face_bottom_values: List[float] = []
    for point in face_track_points or []:
        try:
            point_t = float(point.get("t") or 0.0)
            cy = float(point.get("cy") or 0.0)
            face_h = float(point.get("h") or 0.0)
        except Exception:
            continue
        if not (seg_start <= point_t <= seg_end) or face_h <= 0:
            continue
        segment_crop_y = max(0.0, min(max(0.0, 1.0 - segment_crop_h), cy - (segment_crop_h / 2.0)))
        face_bottom = ((cy + (face_h / 2.0) - segment_crop_y) / segment_crop_h) * float(target_height)
        face_bottom_values.append(face_bottom)
    if not face_bottom_values:
        margin = FACE_AWARE_FILL_FALLBACK_SUBTITLE_MARGIN
        caption_top = max(0.0, float(target_height) - float(margin) - caption_block_height)
        return {
            "median_face_bottom": None,
            "caption_top": caption_top,
            "caption_margin_v": float(margin),
            "caption_block_height": caption_block_height,
            "subscribe_y": caption_top + caption_block_height + DESIGN_GEOMETRY["fit_caption_gap"],
        }
    face_bottom_values.sort()
    mid = len(face_bottom_values) // 2
    median_face_bottom = (
        face_bottom_values[mid]
        if len(face_bottom_values) % 2
        else (face_bottom_values[mid - 1] + face_bottom_values[mid]) / 2.0
    )
    min_top = FACE_AWARE_FILL_CAPTION_MIN_TOP
    max_top = max(min_top, float(target_height) - FACE_AWARE_FILL_SUBSCRIBE_ROOM - caption_block_height)
    caption_top = max(min_top, min(max_top, median_face_bottom + FACE_AWARE_FILL_CAPTION_GAP))
    caption_margin_v = max(0.0, float(target_height) - caption_top - caption_block_height)
    return {
        "median_face_bottom": median_face_bottom,
        "caption_top": caption_top,
        "caption_margin_v": caption_margin_v,
        "caption_block_height": caption_block_height,
        "subscribe_y": caption_top + caption_block_height + DESIGN_GEOMETRY["fit_caption_gap"],
    }


def _face_aware_segment_for_time(layout_segments: List[Dict[str, Any]], event_start: float) -> Dict[str, Any]:
    for index, segment in enumerate(layout_segments or []):
        try:
            start = float(segment.get("start") or 0.0)
            end = float(segment.get("end") or 0.0)
        except Exception:
            continue
        if start <= float(event_start) < end:
            return {**segment, "_index": index}
    return {"mode": "fill", "_index": 0}


def _face_aware_style_suffix_for_time(layout_segments: List[Dict[str, Any]], event_start: float) -> str:
    segment = _face_aware_segment_for_time(layout_segments, event_start)
    mode = str(segment.get("mode") or "fill").strip().lower()
    if mode == "fit":
        return "Fit"
    if mode == "split":
        return "Split"
    return str(segment.get("caption_style_suffix") or f"Fill{int(segment.get('_index') or 0)}")


def _ass_time_to_seconds(value: str) -> float:
    match = re.match(r"\s*(\d+):(\d{2}):(\d{2})(?:[.,](\d+))?", str(value or ""))
    if not match:
        return 0.0
    hrs, mins, secs = [int(match.group(idx)) for idx in range(1, 4)]
    frac = match.group(4) or "0"
    return hrs * 3600 + mins * 60 + secs + (float(f"0.{frac}") if frac else 0.0)


def _format_face_aware_ass_time(seconds: float) -> str:
    total_cs = max(0, int(round(float(seconds or 0.0) * 100)))
    hrs = total_cs // 360000
    total_cs -= hrs * 360000
    mins = total_cs // 6000
    total_cs -= mins * 6000
    secs = total_cs // 100
    cs = total_cs - secs * 100
    return f"{hrs}:{mins:02d}:{secs:02d}.{cs:02d}"


def _face_aware_ass_style_line(
    style_name: str,
    *,
    subtitle_font_size: int,
    subtitle_margin: int,
    subtitle_font: str,
    subtitle_text_color: Optional[str],
    subtitle_text_alpha: Optional[int],
    subtitle_bg_color: Optional[str],
    subtitle_bg_alpha: Optional[int],
    subtitle_style: Optional[str],
) -> str:
    clean_font = (subtitle_font or "DejaVu Sans").replace("'", "")
    normalized_subtitle_style = str(subtitle_style or "plain").strip().lower()
    primary = _hex_to_ass_color_with_alpha(subtitle_text_color, subtitle_text_alpha, "#FFFFFF", DEFAULT_SUBTITLE_TEXT_ALPHA)
    if normalized_subtitle_style == "karaoke":
        bg_alpha = _normalize_alpha_percent(subtitle_bg_alpha, 0)
        if bg_alpha > 0:
            back = _hex_to_ass_color_with_alpha(subtitle_bg_color, bg_alpha, DEFAULT_SUBTITLE_BG_COLOR, DEFAULT_SUBTITLE_BG_ALPHA)
            border_style = "4"
        else:
            back = _hex_to_ass_color_with_alpha("#000000", 0, "#000000", 0)
            border_style = "1"
        outline = "0"
    else:
        back = _hex_to_ass_color_with_alpha(subtitle_bg_color, subtitle_bg_alpha, DEFAULT_SUBTITLE_BG_COLOR, DEFAULT_SUBTITLE_BG_ALPHA)
        border_style = "4"
        outline = "1"
    return (
        f"Style: {style_name},"
        f"{clean_font},"
        f"{int(subtitle_font_size)},"
        f"{primary},"
        f"{primary},"
        "&H00000000,"
        f"{back},"
        f"0,0,0,0,100,100,0,0,{border_style},{outline},0,2,40,40,{int(round(subtitle_margin))},1"
    )


def _parse_srt_timestamp(value: str) -> float:
    value = str(value or "").strip().replace(",", ".")
    parts = value.split(":")
    if len(parts) != 3:
        return 0.0
    return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])


def _build_face_aware_subtitle_path(
    subtitle_path: Path,
    *,
    layout_segments: List[Dict[str, Any]],
    target_width: int,
    target_height: int,
    subtitle_font_size: int,
    fit_margin: int,
    subtitle_font: str,
    subtitle_text_color: Optional[str],
    subtitle_text_alpha: Optional[int],
    subtitle_bg_color: Optional[str],
    subtitle_bg_alpha: Optional[int],
    subtitle_style: Optional[str],
) -> Optional[Path]:
    if not layout_segments or not subtitle_path or not Path(subtitle_path).exists():
        return None
    source = Path(subtitle_path)
    text = source.read_text(encoding="utf-8", errors="ignore")
    if source.suffix.lower() == ".srt":
        fill_styles = []
        for idx, segment in enumerate(layout_segments or []):
            if str(segment.get("mode") or "fill").strip().lower() == "fit":
                continue
            suffix = str(segment.get("caption_style_suffix") or f"Fill{idx}")
            margin = int(round(float(segment.get("caption_margin_v") or FACE_AWARE_FILL_FALLBACK_SUBTITLE_MARGIN)))
            fill_styles.append((suffix, margin))
        if not fill_styles:
            fill_styles = [("Fill0", FACE_AWARE_FILL_FALLBACK_SUBTITLE_MARGIN)]
        split_margin = int(round(_face_aware_split_position_metrics(
            target_height=target_height,
            subtitle_font_size=subtitle_font_size,
        )["caption_margin_v"]))
        events: List[Tuple[float, float, str]] = []
        for block in re.split(r"\n\s*\n", text.strip()):
            lines = [line.strip("\ufeff") for line in block.splitlines() if line.strip()]
            if len(lines) >= 2 and "-->" in lines[0]:
                timing_index = 0
            elif len(lines) >= 3 and "-->" in lines[1]:
                timing_index = 1
            else:
                continue
            start_raw, end_raw = [part.strip() for part in lines[timing_index].split("-->", 1)]
            body = "\\N".join(lines[timing_index + 1:]).replace("{", "\\{").replace("}", "\\}")
            if body:
                events.append((_parse_srt_timestamp(start_raw), _parse_srt_timestamp(end_raw), body))
        if not events:
            return None
        ass_lines = [
            "[Script Info]",
            "ScriptType: v4.00+",
            "WrapStyle: 2",
            "ScaledBorderAndShadow: yes",
            f"PlayResX: {int(target_width)}",
            f"PlayResY: {int(target_height)}",
            "",
            "[V4+ Styles]",
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
            "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
            "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
            *[
                _face_aware_ass_style_line(
                    f"FillStyle{suffix}",
                    subtitle_font_size=subtitle_font_size,
                    subtitle_margin=margin,
                    subtitle_font=subtitle_font,
                    subtitle_text_color=subtitle_text_color,
                    subtitle_text_alpha=subtitle_text_alpha,
                    subtitle_bg_color=subtitle_bg_color,
                    subtitle_bg_alpha=subtitle_bg_alpha,
                    subtitle_style=subtitle_style,
                )
                for suffix, margin in fill_styles
            ],
            _face_aware_ass_style_line(
                "FitStyle",
                subtitle_font_size=subtitle_font_size,
                subtitle_margin=fit_margin,
                subtitle_font=subtitle_font,
                subtitle_text_color=subtitle_text_color,
                subtitle_text_alpha=subtitle_text_alpha,
                subtitle_bg_color=subtitle_bg_color,
                subtitle_bg_alpha=subtitle_bg_alpha,
                subtitle_style=subtitle_style,
            ),
            _face_aware_ass_style_line(
                "SplitStyle",
                subtitle_font_size=subtitle_font_size,
                subtitle_margin=split_margin,
                subtitle_font=subtitle_font,
                subtitle_text_color=subtitle_text_color,
                subtitle_text_alpha=subtitle_text_alpha,
                subtitle_bg_color=subtitle_bg_color,
                subtitle_bg_alpha=subtitle_bg_alpha,
                subtitle_style=subtitle_style,
            ),
            "",
            "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        ]
        for start, end, body in events:
            suffix = _face_aware_style_suffix_for_time(layout_segments, start)
            style = f"{suffix}Style" if suffix in {"Fit", "Split"} else f"FillStyle{suffix}"
            if suffix == "Split":
                body = r"{\an5\pos(360,640)}" + body
            ass_lines.append(
                f"Dialogue: 0,{_format_face_aware_ass_time(start)},{_format_face_aware_ass_time(end)},{style},,0,0,0,,{body}"
            )
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".face-aware.ass")
        tmp_path = Path(tmp.name)
        tmp.write("\n".join(ass_lines).encode("utf-8"))
        tmp.close()
        return tmp_path

    if source.suffix.lower() != ".ass":
        return None
    lines = text.splitlines()
    output: List[str] = []
    in_styles = False
    style_lines: List[str] = []
    style_name_index = 0
    style_margin_index: Optional[int] = None
    for line in lines:
        stripped = line.strip()
        if stripped == "[V4+ Styles]":
            in_styles = True
            output.append(line)
            continue
        if in_styles and stripped.startswith("[") and stripped != "[V4+ Styles]":
            for style_line in style_lines:
                values = style_line.split(":", 1)[1].split(",")
                if not values:
                    continue
                base_name = values[style_name_index].strip() or "Default"
                suffix_margins = [
                    (
                        str(segment.get("caption_style_suffix") or f"Fill{idx}"),
                        int(round(float(segment.get("caption_margin_v") or FACE_AWARE_FILL_FALLBACK_SUBTITLE_MARGIN))),
                    )
                    for idx, segment in enumerate(layout_segments or [])
                    if str(segment.get("mode") or "fill").strip().lower() == "fill"
                ] or [("Fill0", FACE_AWARE_FILL_FALLBACK_SUBTITLE_MARGIN)]
                suffix_margins.append(("Fit", fit_margin))
                suffix_margins.append(("Split", int(round(_face_aware_split_position_metrics(
                    target_height=target_height,
                    subtitle_font_size=subtitle_font_size,
                )["caption_margin_v"]))))
                for suffix, margin in suffix_margins:
                    new_values = list(values)
                    new_values[style_name_index] = f"{base_name}{suffix}"
                    if style_margin_index is not None and style_margin_index < len(new_values):
                        new_values[style_margin_index] = str(int(round(margin)))
                    output.append("Style: " + ",".join(new_values))
            in_styles = False
            output.append(line)
            continue
        if in_styles and line.startswith("Format:"):
            style_format = [part.strip() for part in line.split(":", 1)[1].split(",")]
            style_name_index = style_format.index("Name") if "Name" in style_format else 0
            style_margin_index = style_format.index("MarginV") if "MarginV" in style_format else None
            output.append(line)
            continue
        if in_styles and line.startswith("Style:"):
            style_lines.append(line)
            continue
        if line.startswith("Dialogue:"):
            parts = line.split(":", 1)[1].split(",", 9)
            if len(parts) >= 10:
                suffix = _face_aware_style_suffix_for_time(layout_segments, _ass_time_to_seconds(parts[1]))
                parts[3] = f"{(parts[3].strip() or 'Default')}{suffix}"
                if suffix == "Split":
                    parts[9] = r"{\an5\pos(360,640)}" + parts[9]
                output.append("Dialogue: " + ",".join(parts))
                continue
        output.append(line)
    if in_styles:
        for style_line in style_lines:
            values = style_line.split(":", 1)[1].split(",")
            if not values:
                continue
            base_name = values[style_name_index].strip() or "Default"
            suffix_margins = [
                (
                    str(segment.get("caption_style_suffix") or f"Fill{idx}"),
                    int(round(float(segment.get("caption_margin_v") or FACE_AWARE_FILL_FALLBACK_SUBTITLE_MARGIN))),
                )
                for idx, segment in enumerate(layout_segments or [])
                if str(segment.get("mode") or "fill").strip().lower() == "fill"
            ] or [("Fill0", FACE_AWARE_FILL_FALLBACK_SUBTITLE_MARGIN)]
            suffix_margins.append(("Fit", fit_margin))
            suffix_margins.append(("Split", int(round(_face_aware_split_position_metrics(
                target_height=target_height,
                subtitle_font_size=subtitle_font_size,
            )["caption_margin_v"]))))
            for suffix, margin in suffix_margins:
                new_values = list(values)
                new_values[style_name_index] = f"{base_name}{suffix}"
                if style_margin_index is not None and style_margin_index < len(new_values):
                    new_values[style_margin_index] = str(int(round(margin)))
                output.append("Style: " + ",".join(new_values))
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".face-aware.ass")
    tmp_path = Path(tmp.name)
    tmp.write("\n".join(output).encode("utf-8"))
    tmp.close()
    return tmp_path


def _normalize_subtitle_overlay_specs(
    subtitle_overlay_specs: Optional[list[Dict[str, Any]]],
) -> list[Dict[str, Any]]:
    normalized: list[Dict[str, Any]] = []
    for spec in subtitle_overlay_specs or []:
        if not isinstance(spec, dict):
            continue
        raw_path = spec.get("path")
        if not raw_path:
            continue
        path = Path(raw_path)
        if not path.exists():
            continue
        try:
            start = max(0.0, float(spec.get("start") or 0.0))
            end = max(start + 0.01, float(spec.get("end") or 0.0))
        except Exception:
            continue
        normalized.append({"path": path, "start": start, "end": end})
    return normalized


def _normalize_subtitle_overlay_events(
    subtitle_overlay_events: Optional[list[Dict[str, Any]]],
) -> list[Dict[str, Any]]:
    normalized: list[Dict[str, Any]] = []
    for event in subtitle_overlay_events or []:
        if not isinstance(event, dict):
            continue
        try:
            start = max(0.0, float(event.get("start") or 0.0))
            end = max(start + 0.01, float(event.get("end") or 0.0))
        except Exception:
            continue
        normalized.append({"start": start, "end": end, "metrics": event.get("metrics") or {}})
    normalized.sort(key=lambda item: item["start"])
    return normalized


def _piecewise_constant_expr(
    intervals: list[tuple[float, float, Any]],
    *,
    duration: float,
    default_expr: Any = 0.0,
) -> str:
    """Build a flat ffmpeg expression for per-time constant values.

    A deeply nested if(between(...), y, next) chain hits ffmpeg's expression
    parser limit for long karaoke overlays. A sum of non-overlapping
    between(t,start,end)*(value-default) terms preserves the same piecewise
    constants without parser recursion.
    """
    def _expr(value: Any) -> str:
        try:
            return f"{round(float(value), 2):.2f}"
        except Exception:
            return str(value or "0").strip() or "0"

    default_value = _expr(default_expr)
    cleaned: list[tuple[float, float, str]] = []
    for raw_start, raw_end, raw_value in sorted(intervals, key=lambda item: (item[0], item[1])):
        try:
            start = max(0.0, float(raw_start))
            end = min(float(duration), float(raw_end))
            value_expr = _expr(raw_value)
        except Exception:
            continue
        if end <= start:
            continue
        if cleaned and cleaned[-1][2] == value_expr and start <= cleaned[-1][1] + 0.05:
            prev_start, _prev_end, prev_value = cleaned[-1]
            cleaned[-1] = (prev_start, end, prev_value)
        else:
            cleaned.append((start, end, value_expr))
    if not cleaned:
        return default_value
    if len(cleaned) == 1 and cleaned[0][0] <= 0.01 and cleaned[0][1] >= float(duration) - 0.01:
        return cleaned[0][2]
    parts = [default_value]
    for start, end, value_expr in cleaned:
        parts.append(f"(between(t,{start:.6f},{end:.6f})*(({value_expr})-({default_value})))")
    return "+".join(parts)


def _piecewise_constant_overlay_y_expr(
    intervals: list[tuple[float, float, float]],
    *,
    duration: float,
    default_y: float = 0.0,
) -> str:
    return _piecewise_constant_expr(intervals, duration=duration, default_expr=default_y)


def _append_timed_subtitle_overlay_filters(
    filter_parts: list[str],
    final_label: str,
    *,
    start_input_index: int,
    overlay_specs: list[Dict[str, Any]],
    label_prefix: str,
) -> tuple[str, int]:
    next_input_index = start_input_index
    current_label = final_label
    for spec_index, spec in enumerate(overlay_specs):
        src_label = f"[{label_prefix}_src_{spec_index}]"
        out_label = f"[{label_prefix}_out_{spec_index}]"
        start = float(spec["start"])
        end = float(spec["end"])
        filter_parts.append(f"[{next_input_index}:v]format=rgba{src_label}")
        filter_parts.append(
            f"{current_label}{src_label}overlay=0:0:enable='between(t,{start:.6f},{end:.6f})'{out_label}"
        )
        current_label = out_label
        next_input_index += 1
    return current_label, next_input_index


def _pick_font():
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for c in candidates:
        if Path(c).exists():
            return c
    return None


def _build_static_visual_clip(
    key: str,
    duration: float,
    font_path: str = None,
    image_path: Optional[Path] = None,
) -> Path:
    if image_path is None:
        visual = STATIC_VISUAL_MAP.get(key)
        if not visual:
            raise ValueError(f"Unknown static visual key: {key}")
        image_path = visual.get("image_path")
    if not image_path or not Path(image_path).exists():
        raise FileNotFoundError(f"Static visual image not found for {key}")
    resolved_ffmpeg = _resolve_ffmpeg()
    target_duration = max(1.0, float(duration or 0.0))
    tmp_dir = Path(tempfile.gettempdir())
    safe_key = (key or "static").replace(":", "_")
    out_path = tmp_dir / f"static_visual_{safe_key}_{secrets.token_hex(6)}.mp4"
    # Sadece kaynak oranını bozmadan çift piksele yuvarla; asıl kırpma user crop'unda yapılacak
    vf = "scale=trunc(iw/2)*2:trunc(ih/2)*2,setsar=1"
    cmd = [
        resolved_ffmpeg,
        "-y",
        "-loop",
        "1",
        "-i",
        str(image_path),
        "-t",
        str(target_duration),
        "-vf",
        vf,
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-preset",
        "ultrafast",
        "-crf",
        "23",
        "-movflags",
        "+faststart",
        str(out_path),
    ]
    current_app.logger.info("Building static visual clip %s -> %s", key, out_path)
    current_app.logger.debug("Static ffmpeg command: %s", " ".join(cmd))
    run_media_subprocess(
        cmd,
        operation="build_static_visual_clip",
        context=f"key={key} output={out_path.name}",
        output_paths=[out_path],
        check=True,
        timeout=FFMPEG_SHORT_TIMEOUT,
        capture_output=True,
        text=True,
    )
    if not out_path.exists():
        raise FileNotFoundError(f"Static clip not produced for {key}")
    size = out_path.stat().st_size
    current_app.logger.info("Static clip %s ready (%d bytes)", out_path, size)
    _log_video_dimensions("static visual output", out_path)
    return out_path


def _resolve_ffprobe() -> str:
    ffmpeg_path = Path(_resolve_ffmpeg())
    candidate = ffmpeg_path.with_name("ffprobe")
    if candidate.exists():
        return str(candidate)
    return "ffprobe"


def _has_audio_stream(source: Path) -> bool:
    ffprobe_cmd = [
        _resolve_ffprobe(),
        "-v",
        "error",
        "-select_streams",
        "a",
        "-show_entries",
        "stream=index",
        "-of",
        "csv=p=0",
        str(source),
    ]
    try:
        result = run_media_subprocess(
            ffprobe_cmd,
            operation="has_audio_stream",
            context=f"source={source.name}",
            check=False,
            capture_output=True,
            text=True,
            timeout=FFPROBE_TIMEOUT,
        )
        return bool(result.stdout.strip())
    except Exception as exc:
        current_app.logger.warning("FFprobe audio check failed for %s: %s", source, exc)
        return False


def _has_video_stream(source: Path) -> bool:
    ffprobe_cmd = [
        _resolve_ffprobe(),
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=index",
        "-of",
        "csv=p=0",
        str(source),
    ]
    try:
        result = run_media_subprocess(
            ffprobe_cmd,
            operation="has_video_stream",
            context=f"source={source.name}",
            check=False,
            capture_output=True,
            text=True,
            timeout=FFPROBE_TIMEOUT,
        )
        return bool(result.stdout.strip())
    except Exception as exc:
        current_app.logger.warning("FFprobe video check failed for %s: %s", source, exc)
        return False


def _probe_video_dimensions(path: Path) -> Tuple[int, int]:
    cmd = [
        _resolve_ffprobe(),
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height",
        "-of",
        "csv=p=0:s=x",
        str(path),
    ]
    result = run_media_subprocess(
        cmd,
        operation="probe_video_dimensions",
        context=f"path={path.name}",
        check=True,
        capture_output=True,
        text=True,
        timeout=FFPROBE_TIMEOUT,
    )
    dims = result.stdout.strip()
    if "x" in dims:
        width, height = dims.split("x")
        clean_width, clean_height = int(width), int(height)
        render_timing.record_source_resolution(clean_width, clean_height)
        return clean_width, clean_height
    raise RuntimeError(f"ffprobe returned unexpected dimensions: {dims}")


def _probe_media_duration_seconds(path: Path) -> Optional[float]:
    cmd = [
        _resolve_ffprobe(),
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    try:
        result = run_media_subprocess(
            cmd,
            operation="probe_media_duration",
            context=f"path={path.name}",
            check=True,
            capture_output=True,
            text=True,
            timeout=FFPROBE_TIMEOUT,
        )
        raw = (result.stdout or "").strip()
        if not raw:
            return None
        value = float(raw)
        if value > 0:
            return value
    except Exception as exc:
        current_app.logger.debug("Duration probe failed for %s: %s", path, exc)
    return None


def _log_video_dimensions(label: str, path: Path) -> None:
    try:
        width, height = _probe_video_dimensions(path)
        current_app.logger.debug("%s dimensions %dx%d", label, width, height)
    except Exception as exc:
        current_app.logger.debug("Failed to probe %s dimensions (%s): %s", label, path, exc)

def _compose_with_background(
    bg_path: Path,
    clip_path: Path,
    title: str,
    subtitle: str,
    out_path: Path,
    font_path: str = None,
    title_font_name: str = None,
    subtitle_path: Path = None,
    subtitle_font: str = "DejaVu Sans",
    title_font_size: int = 30,
    title_margin: int = DEFAULT_TITLE_MARGIN,
    title_line_spacing: int = -4,
    title_engine: str = "pillow",
    title_uppercase: bool = False,
    title_language: Optional[str] = None,
    title_bg_color: Optional[str] = None,
    title_bg_alpha: Optional[int] = DEFAULT_TITLE_BG_ALPHA,
    title_text_color: Optional[str] = None,
    subtitle_font_size: int = DEFAULT_SUB_FONT_SIZE,
    subtitle_margin: int = SUB_MARGIN_DEFAULT,
    caption_anchor: str = "bottom",
    subtitle_margin_custom: Optional[bool] = None,
    subtitle_text_color: Optional[str] = None,
    subtitle_bg_color: Optional[str] = None,
    subtitle_bg_alpha: Optional[int] = DEFAULT_SUBTITLE_BG_ALPHA,
    subtitle_text_alpha: Optional[int] = DEFAULT_SUBTITLE_TEXT_ALPHA,
    subtitle_style: Optional[str] = "plain",
    subtitle_preset: Optional[str] = None,
):
    if not bg_path.exists():
        raise FileNotFoundError(f"Background image not found: {bg_path}")
    resolved_ffmpeg = _resolve_ffmpeg()

    # Başlık metni, kısalt ve satırları biraz daha kısa tut
    title_txt = _apply_title_case(
        _sanitize_text_for_overlay(title or "", 140),
        title_language,
        bool(title_uppercase),
    )
    title_layout = _fit_title_text(
        title_txt,
        font_path=font_path or "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        font_size=title_font_size,
        target_width=VIDEO_TARGET_WIDTH,
        target_height=VIDEO_TARGET_HEIGHT,
        base_y=max(80, min(title_margin, 250)),
        line_spacing=5,
    )
    title_txt = title_layout["text"]

    overlay_y_expr = _overlay_y_expr(
        VIDEO_OVERLAY_TOP_OFFSET,
        subtitle_path,
        subtitle_margin,
        subtitle_font_size,
    )

    filter_parts = [
        # Soldan 100, sağdan 320, üstten ve alttan 80 piksel kes, sonra 710 px genişliğe ölçekle
        "[1:v]crop=in_w-420:in_h-160:100:80,scale=710:-2,setpts=PTS-STARTPTS[vid]",
        f"[0:v][vid]overlay=(main_w-overlay_w)/2:{overlay_y_expr}:shortest=1[ov]",
    ]
    final_label = "[ov]"

    title_overlay_path = None
    if title_txt:
        test_font_file = _title_font_path_for_text(title_txt, font_path)
        title_overlay_path, _ = _render_title_overlay(
            title_txt,
            font_path=test_font_file,
            font_size=title_font_size,
            target_width=VIDEO_TARGET_WIDTH,
            target_height=VIDEO_TARGET_HEIGHT,
            base_y=title_margin,
            line_spacing=title_line_spacing,
            title_text_color=title_text_color,
            subtitle_preset=subtitle_preset,
            title_bg_color=title_bg_color,
            title_bg_alpha=title_bg_alpha,
            uppercase=bool(title_uppercase),
            title_language=title_language,
            video_top_limit=None,
        )
        filter_parts.append("[2:v]format=rgba[title_src]")
        filter_parts.append(f"{final_label}[title_src]overlay=0:0:shortest=1[ov_title]")
        final_label = "[ov_title]"

    if subtitle_path:
        style = _subtitle_force_style(
            target_width=VIDEO_TARGET_WIDTH,
            target_height=VIDEO_TARGET_HEIGHT,
            subtitle_font_size=subtitle_font_size,
            subtitle_margin=subtitle_margin,
            caption_anchor=caption_anchor,
            subtitle_font=subtitle_font,
            subtitle_text_color=subtitle_text_color,
            subtitle_text_alpha=subtitle_text_alpha,
            subtitle_bg_color=subtitle_bg_color,
            subtitle_bg_alpha=subtitle_bg_alpha,
            subtitle_style=subtitle_style,
        )
        filter_parts.append(
            f"{final_label}subtitles='{_escape_ass_path(subtitle_path)}':fontsdir='{_escape_ass_path(SUBTITLE_FONTS_DIR)}':force_style='{style}'[subout]"
        )
        final_label = "[subout]"

    filter_complex = ";".join(filter_parts)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    temp_out_path = out_path.with_name(f"{out_path.stem}.compose-{secrets.token_hex(4)}{out_path.suffix}")
    cmd = [
        resolved_ffmpeg,
        "-y",
        "-loop",
        "1",
        "-i",
        str(bg_path),
        "-i",
        str(clip_path),
    ]
    if title_overlay_path:
        cmd.extend(["-loop", "1", "-i", str(title_overlay_path)])
    cmd.extend([
        "-filter_complex",
        filter_complex,
        "-map",
        final_label,
        "-map",
        "1:a?",
        "-shortest",
        "-preset",
        "ultrafast",
        "-crf",
        "23",
        "-c:v",
        "libx264",
        "-c:a",
        "aac",
        "-movflags",
        "+faststart",
        str(out_path),
    ])
    try:
        run_media_subprocess(
            cmd,
            operation="compose_with_background",
            context=f"output={out_path.name}",
            output_paths=[out_path],
            check=True,
            timeout=FFMPEG_RENDER_TIMEOUT,
        )
    finally:
        if title_overlay_path:
            title_overlay_path.unlink(missing_ok=True)




def _compose_trimmed_with_background(
    bg_path: Path,
    src_path: Path,
    start: float,
    end: float,
    title: str,
    subtitle: str,
    out_path: Path,
    font_path: str = None,
    title_font_name: str = None,
    subtitle_path: Path = None,
    subtitle_overlay_video_path: Optional[Path] = None,
    subtitle_overlay_specs: Optional[list[Dict[str, Any]]] = None,
    subtitle_overlay_events: Optional[list[Dict[str, Any]]] = None,
    subtitle_font: str = "DejaVu Sans",
    title_font_size: int = 30,
    title_margin: int = DEFAULT_TITLE_MARGIN,
    title_line_spacing: int = -4,
    title_engine: str = "pillow",
    title_uppercase: bool = False,
    title_language: Optional[str] = None,
    title_bg_color: Optional[str] = None,
    title_bg_alpha: Optional[int] = DEFAULT_TITLE_BG_ALPHA,
    title_text_color: Optional[str] = None,
    title_outline_color: Optional[str] = "#000000",
    title_outline_width: int = 0,
    subtitle_font_size: int = DEFAULT_SUB_FONT_SIZE,
    subtitle_margin: int = SUB_MARGIN_DEFAULT,
    caption_anchor: str = "bottom",
    subtitle_margin_custom: Optional[bool] = None,
    subtitle_text_color: Optional[str] = None,
    subtitle_bg_color: Optional[str] = None,
    subtitle_bg_alpha: Optional[int] = DEFAULT_SUBTITLE_BG_ALPHA,
    subtitle_text_alpha: Optional[int] = DEFAULT_SUBTITLE_TEXT_ALPHA,
    subtitle_style: Optional[str] = "plain",
    subtitle_preset: Optional[str] = None,
    video_date_text: Optional[str] = None,
    video_date_top: Optional[int] = None,
    subscribe_overlay_enabled: bool = False,
    subscribe_overlay_path: Optional[Path] = None,
    show_title: bool = True,
    title_display_seconds: Optional[float] = None,
    show_subtitle: bool = True,
    crop_settings: Optional[Dict[str, float]] = None,
    video_override_source: Optional[Path] = None,
    audio_override_source: Optional[Path] = None,
    podcast_background_image_source: Optional[Path] = None,
    podcast_overlay_video_sources: Optional[list[Path]] = None,
    video_overlay_offset: Optional[int] = None,
    crop_aspect: Optional[str] = None,
    music_only: bool = False,
    face_track_smooth_path: Optional[Path] = None,
    overlay_shapes: Optional[list[Dict[str, Any]]] = None,
    overlay_owner_user_id: Optional[str] = None,
    overlay_brand_id: Optional[str] = None,
    music_path: Optional[Path] = None,
    music_level: int = 50,
    music_start_seconds: float = 0.0,
    music_duck: bool = True,
    music_fade_out: bool = True,
    music_speech_ranges: Optional[list[Dict[str, float]]] = None,
    outreach_layout: bool = False,
    source_keep_ranges: Optional[list[Dict[str, Any]]] = None,
    low_memory_demo: bool = False,
):
    """Single-pass trim + compose to reduce processing time."""
    if not bg_path.exists():
        raise FileNotFoundError(f"Background image not found: {bg_path}")
    if not src_path.exists():
        raise FileNotFoundError(f"Source video not found: {src_path}")
    resolved_ffmpeg = _resolve_ffmpeg()
    podcast_mode = bool(audio_override_source and Path(audio_override_source).exists())
    normalized_title_engine = "pillow"
    direct_keep_ranges = _normalize_keep_ranges_for_trim(source_keep_ranges)
    if len(direct_keep_ranges) > 1:
        duration = max(
            sum(max(0.0, item["end"] - item["start"]) for item in direct_keep_ranges),
            1.0,
        )
    else:
        direct_keep_ranges = []
        duration = max(end - start, 1.0)
    try:
        normalized_title_display_seconds = max(1.0, min(30.0, float(title_display_seconds)))
    except (TypeError, ValueError):
        normalized_title_display_seconds = None
    if normalized_title_display_seconds is not None and duration <= normalized_title_display_seconds:
        normalized_title_display_seconds = None

    def _title_source_filter(input_label: str, output_label: str) -> str:
        filters = "format=rgba"
        if normalized_title_display_seconds is not None:
            filters += (
                f",fade=t=out:st={normalized_title_display_seconds:.3f}:"
                "d=0.300:alpha=1"
            )
        return f"{input_label}{filters}{output_label}"
    if music_only and video_override_source:
        override_duration = _probe_media_duration_seconds(video_override_source)
        if override_duration:
            duration = max(1.0, override_duration)
            current_app.logger.info(
                "Music-only duration override applied: %.3fs (source=%s)",
                duration,
                video_override_source,
            )
    if podcast_mode and audio_override_source:
        audio_duration = _probe_media_duration_seconds(audio_override_source)
        if audio_duration:
            duration = max(1.0, float(audio_duration))

    # Fast path for long-form podcast exports: render directly from selected image + audio.
    if podcast_mode and podcast_background_image_source and Path(podcast_background_image_source).exists():
        overlay_aspect = (crop_aspect or "landscape").lower()
        try:
            safe_title_margin = int(title_margin if title_margin is not None else DEFAULT_TITLE_MARGIN)
        except (TypeError, ValueError):
            safe_title_margin = 40
        try:
            safe_title_font_size = int(title_font_size if title_font_size is not None else 30)
        except (TypeError, ValueError):
            safe_title_font_size = 30
        try:
            safe_title_line_spacing = int(title_line_spacing if title_line_spacing is not None else -4)
        except (TypeError, ValueError):
            safe_title_line_spacing = -4
        try:
            safe_subtitle_font_size = int(subtitle_font_size if subtitle_font_size is not None else DEFAULT_SUB_FONT_SIZE)
        except (TypeError, ValueError):
            safe_subtitle_font_size = DEFAULT_SUB_FONT_SIZE
        try:
            safe_subtitle_margin = int(subtitle_margin if subtitle_margin is not None else SUB_MARGIN_DEFAULT)
        except (TypeError, ValueError):
            safe_subtitle_margin = 60
        normalized_title_engine = "pillow"
        if overlay_aspect == "portrait":
            target_width = VIDEO_TARGET_WIDTH
            target_height = VIDEO_TARGET_HEIGHT
        else:
            target_width = PODCAST_LANDSCAPE_WIDTH
            target_height = PODCAST_LANDSCAPE_HEIGHT
        render_timing.record_output_resolution(target_width, target_height)
        title_txt = _sanitize_text_for_overlay(title or "", 140)
        title_overlay_path = None
        effective_subtitle_path = subtitle_path if show_subtitle else None
        effective_subtitle_overlay_path = (
            Path(subtitle_overlay_video_path)
            if show_subtitle and subtitle_overlay_video_path and Path(subtitle_overlay_video_path).exists()
            else None
        )
        effective_subtitle_overlay_specs = (
            _normalize_subtitle_overlay_specs(subtitle_overlay_specs)
            if show_subtitle
            else []
        )
        final_label = "[base]"
        filter_parts = [
            f"[0:v]scale={target_width}:{target_height}:force_original_aspect_ratio=increase,"
            f"crop={target_width}:{target_height},setsar=1[base]"
        ]
        overlay_sources = [Path(p) for p in (podcast_overlay_video_sources or []) if p and Path(p).exists()][:2]
        if overlay_sources:
            if len(overlay_sources) == 1:
                ov_w = int(target_width * 0.62)
                ov_h = int(round(ov_w * 9 / 16))
                max_h = int(target_height * 0.62)
                if ov_h > max_h and max_h > 0:
                    ov_h = max_h
                    ov_w = int(round(ov_h * 16 / 9))
                ov_w += ov_w % 2
                ov_h += ov_h % 2
                filter_parts.append(
                    f"[2:v]scale={ov_w}:{ov_h}:force_original_aspect_ratio=decrease,"
                    f"pad={ov_w}:{ov_h}:(ow-iw)/2:(oh-ih)/2:color=black@0,setsar=1[pod_ov0]"
                )
                filter_parts.append(
                    f"{final_label}[pod_ov0]overlay=(W-w)/2:(H-h)/2:shortest=1[pod_ov_out]"
                )
                final_label = "[pod_ov_out]"
            else:
                ov_w = 520
                ov_h = 520
                gap = max(20, int((target_width - (ov_w * 2)) / 3))
                filter_parts.append(
                    f"[2:v]scale={ov_w}:{ov_h}:force_original_aspect_ratio=decrease,"
                    f"pad={ov_w}:{ov_h}:(ow-iw)/2:(oh-ih)/2:color=black@0,setsar=1[pod_ov0]"
                )
                filter_parts.append(
                    f"[3:v]scale={ov_w}:{ov_h}:force_original_aspect_ratio=decrease,"
                    f"pad={ov_w}:{ov_h}:(ow-iw)/2:(oh-ih)/2:color=black@0,setsar=1[pod_ov1]"
                )
                left_x = int((target_width - (ov_w * 2 + gap)) / 2)
                right_x = left_x + ov_w + gap
                filter_parts.append(
                    f"{final_label}[pod_ov0]overlay={left_x}:(H-h)/2:shortest=1[pod_ov_tmp]"
                )
                filter_parts.append(
                    f"[pod_ov_tmp][pod_ov1]overlay={right_x}:(H-h)/2:shortest=1[pod_ov_out]"
                )
                final_label = "[pod_ov_out]"
        next_video_input_index = 2 + len(overlay_sources)
        if show_title and title_txt:
            test_font_file = _title_font_path_for_text(title_txt, font_path)
            if normalized_title_engine == "pillow":
                title_overlay_path, title_overlay_meta = _render_title_overlay(
                    title_txt,
                    font_path=test_font_file,
                    font_size=safe_title_font_size,
                    target_width=target_width,
                    target_height=target_height,
                    base_y=safe_title_margin,
                    line_spacing=safe_title_line_spacing,
                    title_text_color=title_text_color,
                    subtitle_preset=subtitle_preset,
                    title_bg_color=title_bg_color,
                    title_bg_alpha=title_bg_alpha,
                    uppercase=bool(title_uppercase),
                    title_language=title_language,
                    title_outline_color=title_outline_color,
                    title_outline_width=title_outline_width,
                    video_top_limit=None,
                )
                current_app.logger.info(
                    "Pillow title overlay (podcast fast-path) text_width=%s margin=%s font_size=%s lines=%s draw_y=%s",
                    title_overlay_meta["max_text_width"],
                    title_overlay_meta["side_margin"],
                    title_overlay_meta["font_size"],
                    title_overlay_meta["line_count"],
                    title_overlay_meta["draw_y"],
                )
                title_input_index = next_video_input_index
                filter_parts.append(f"[{title_input_index}:v]format=rgba[pod_title_src]")
                filter_parts.append(
                    f"{final_label}[pod_title_src]overlay=0:0:shortest=1[pod_title_out]"
                )
                final_label = "[pod_title_out]"
                next_video_input_index += 1
        if effective_subtitle_path:
            style = _subtitle_force_style(
                target_width=target_width,
                target_height=target_height,
                subtitle_font_size=safe_subtitle_font_size,
                subtitle_margin=safe_subtitle_margin,
                subtitle_font=subtitle_font,
                subtitle_text_color=subtitle_text_color,
                subtitle_text_alpha=subtitle_text_alpha,
                subtitle_bg_color=subtitle_bg_color,
                subtitle_bg_alpha=subtitle_bg_alpha,
                subtitle_style=subtitle_style,
            )
            filter_parts.append(
                f"{final_label}subtitles='{_escape_ass_path(effective_subtitle_path)}':fontsdir='{_escape_ass_path(SUBTITLE_FONTS_DIR)}':force_style='{style}'[subout]"
            )
            final_label = "[subout]"
        if effective_subtitle_overlay_specs:
            final_label, next_video_input_index = _append_timed_subtitle_overlay_filters(
                filter_parts,
                final_label,
                start_input_index=next_video_input_index,
                overlay_specs=effective_subtitle_overlay_specs,
                label_prefix="pod_caption",
            )
        elif effective_subtitle_overlay_path:
            filter_parts.append(f"[{next_video_input_index}:v]format=rgba[pod_caption_src]")
            filter_parts.append(
                f"{final_label}[pod_caption_src]overlay=0:0:shortest=1[pod_caption_out]"
            )
            final_label = "[pod_caption_out]"
            next_video_input_index += 1
        if video_date_text:
            date_txt = _sanitize_text_for_overlay(video_date_text, 160)
            if date_txt:
                date_font_file = font_path or "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
                date_textfile = _write_debug_textfile(date_txt)
                try:
                    date_top = int(video_date_top if video_date_top is not None else DEFAULT_VIDEO_DATE_TOP)
                except (TypeError, ValueError):
                    date_top = DEFAULT_VIDEO_DATE_TOP
                date_top = clamp_design_position(
                    "video_date_top",
                    date_top,
                    target_height=target_height,
                    element_height=DESIGN_POSITION_LIMITS["video_date_top"]["fallback_element_height"],
                )
                filter_parts.append(
                    f"{final_label}drawtext="
                    f"fontfile='{_escape_ass_path(Path(date_font_file))}':"
                    f"textfile='{_escape_ass_path(date_textfile)}':"
                    "x=(w-text_w)/2:"
                    f"y={date_top}:"
                    "fontsize=24:"
                    "fontcolor=white:"
                    "box=1:"
                    "boxcolor=black@0.6:"
                    "boxborderw=18:"
                    "[ov_date]"
                )
                final_label = "[ov_date]"
        overlay_asset_path = subscribe_overlay_path if subscribe_overlay_path and Path(subscribe_overlay_path).exists() else None
        overlay_enabled = subscribe_overlay_enabled and bool(overlay_asset_path)
        if overlay_enabled:
            subscribe_input_index = next_video_input_index
            filter_parts.append(f"[{subscribe_input_index}:v]scale=iw*{SUBSCRIBE_OVERLAY_SCALE}:ih*{SUBSCRIBE_OVERLAY_SCALE},format=rgba[ov_sub_src]")
            filter_parts.append(
                f"{final_label}[ov_sub_src]overlay=(W-w)/2:H-h-{SUBSCRIBE_OVERLAY_BOTTOM_OFFSET}:shortest=1[ov_sub]"
            )
            final_label = "[ov_sub]"
        filter_complex = ";".join(filter_parts)
        cmd = [
            resolved_ffmpeg,
            "-y",
            "-loop",
            "1",
            "-i",
            str(podcast_background_image_source),
            "-stream_loop",
            "-1",
            "-i",
            str(audio_override_source),
        ]
        for source in overlay_sources:
            cmd.extend(["-stream_loop", "-1", "-i", str(source)])
        if title_overlay_path:
            cmd.extend(["-loop", "1", "-i", str(title_overlay_path)])
        if effective_subtitle_overlay_specs:
            for spec in effective_subtitle_overlay_specs:
                cmd.extend(["-loop", "1", "-i", str(spec["path"])])
        elif effective_subtitle_overlay_path:
            cmd.extend(["-stream_loop", "-1", "-i", str(effective_subtitle_overlay_path)])
        if overlay_enabled:
            cmd.extend(["-stream_loop", "-1", "-i", str(overlay_asset_path)])
        cmd.extend(
            [
                "-t",
                f"{max(1.0, float(duration)):.3f}",
                "-filter_complex",
                filter_complex,
                "-map",
                final_label,
                "-map",
                "1:a",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-crf",
                "23",
                "-c:a",
                "aac",
                "-shortest",
                "-movflags",
                "+faststart",
                str(out_path),
            ]
        )
        base_ffmpeg_timeout = int(FFMPEG_RENDER_TIMEOUT)
        fast_path_timeout = scale_media_timeout(
            base_ffmpeg_timeout,
            duration_seconds=duration,
            multiplier=2.0,
            extra_seconds=120,
        )
        current_app.logger.info(
            "Podcast fast-path ffmpeg command (timeout=%ss): %s",
            fast_path_timeout,
            " ".join(cmd),
        )
        try:
            run_media_subprocess(
                cmd,
                operation="podcast_fast_path_render",
                context=f"output={out_path.name}",
                output_paths=[out_path],
                check=True,
                timeout=fast_path_timeout,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as err:
            current_app.logger.error("Podcast fast-path failed stdout=%s stderr=%s", err.stdout, err.stderr)
            raise RuntimeError(
                f"FFmpeg podcast fast-path failed: {err.stderr.strip() or err.stdout.strip()}"
            ) from err
        finally:
            if title_overlay_path and Path(title_overlay_path).exists():
                try:
                    Path(title_overlay_path).unlink()
                except Exception:
                    pass
        return


    # Step 1: trim source with pts reset
    trimmed = out_path.parent / f"{out_path.stem}_trimmed_{secrets.token_hex(4)}.mp4"
    merged_override = None
    merged_audio_override = None
    direct_audio_source: Optional[Path] = None
    direct_audio_ss = 0.0
    direct_audio_duration = 0.0
    has_audio = _has_audio_stream(src_path)
    has_video = _has_video_stream(src_path)
    clip_video_source: Optional[Path] = None
    direct_visual_override = (
        Path(video_override_source)
        if video_override_source
        and Path(video_override_source).exists()
        and _has_video_stream(Path(video_override_source))
        and has_video
        and not audio_override_source
        and not podcast_mode
        else None
    )
    direct_source_timeline = bool(
        has_video
        and (not video_override_source or direct_visual_override)
        and not audio_override_source
        and not podcast_mode
    )
    direct_source_ranges = direct_keep_ranges or [
        {"start": max(0.0, float(start)), "end": max(float(start), float(end))}
    ]
    direct_source_seek = float(direct_source_ranges[0]["start"])
    direct_source_input_duration = max(
        0.001,
        float(direct_source_ranges[-1]["end"]) - direct_source_seek,
    )

    if direct_source_timeline:
        clip_video_source = src_path
        current_app.logger.info(
            "Direct single-pass source timeline enabled source=%s ranges=%s seek=%.3f duration=%.3f",
            src_path.name,
            len(direct_source_ranges),
            direct_source_seek,
            duration,
        )

    # Audio-only fast path:
    # If source is audio-only and we already have a visual override video, skip
    # intermediate trim/merge mp4 generation and compose directly in one pass.
    if (not has_video) and has_audio and video_override_source and Path(video_override_source).exists() and _has_video_stream(Path(video_override_source)):
        clip_video_source = Path(video_override_source)
        direct_audio_source = src_path
        direct_audio_ss = max(0.0, float(start))
        direct_audio_duration = max(1.0, float(duration))
        current_app.logger.info(
            "Audio-only fast compose enabled: visual=%s audio=%s start=%.3f duration=%.3f",
            clip_video_source,
            src_path,
            direct_audio_ss,
            direct_audio_duration,
        )

    if clip_video_source is None and has_video:
        trim_filter_parts = ["[0:v]setpts=PTS-STARTPTS[v]"]
        if has_audio:
            trim_filter_parts.append("[0:a]asetpts=PTS-STARTPTS,aresample=async=1:first_pts=0[a]")
        trim_filter = ";".join(trim_filter_parts)
        trim_cmd = [
            resolved_ffmpeg,
            "-y",
            "-stats_period",
            "1",
            "-ss",
            str(start),
            "-i",
            str(src_path),
            "-t",
            str(duration),
            "-avoid_negative_ts",
            "make_zero",
            "-fflags",
            "+genpts",
            "-reset_timestamps",
            "1",
            "-filter_complex",
            trim_filter,
            "-map",
            "[v]",
        ]
        if has_audio:
            trim_cmd.extend(["-map", "[a]"])
    elif clip_video_source is None:
        # Audio-only sources (mp3/m4a/...) need a synthetic video stream.
        # Build a timed clip from background image + trimmed audio.
        trim_cmd = [
            resolved_ffmpeg,
            "-y",
            "-stats_period",
            "1",
            "-loop",
            "1",
            "-i",
            str(bg_path),
            "-ss",
            str(start),
            "-t",
            str(duration),
        ]
        if has_audio:
            trim_cmd.extend(["-i", str(src_path)])
            audio_filter = "[1:a]asetpts=PTS-STARTPTS,aresample=async=1:first_pts=0[a]"
        else:
            trim_cmd.extend(["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"])
            audio_filter = "[1:a]asetpts=PTS-STARTPTS[a]"
        trim_cmd.extend(
            [
                "-filter_complex",
                "[0:v]scale=1280:720:force_original_aspect_ratio=increase,crop=1280:720,setsar=1,setpts=PTS-STARTPTS[v];"
                + audio_filter,
                "-map",
                "[v]",
                "-map",
                "[a]",
                "-shortest",
            ]
        )
    if clip_video_source is None:
        trim_cmd.extend(
            [
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-crf",
                "23",
            ]
        )
        if has_audio:
            trim_cmd.extend(["-c:a", "aac"])
        trim_cmd.extend(
            [
                str(trimmed),
            ]
        )
        current_app.logger.debug("Trim ffmpeg command: %s", " ".join(trim_cmd))
        try:
            trim_load_start = os.getloadavg() if hasattr(os, "getloadavg") else None
            trim_started = time.monotonic()
            trim_result = run_media_subprocess(
                trim_cmd,
                operation="trim_clip",
                context=f"src={src_path.name} output={trimmed.name}",
                output_paths=[trimmed],
                check=True,
                timeout=scale_media_timeout(
                    FFMPEG_RENDER_TIMEOUT,
                    duration_seconds=duration,
                    multiplier=2.0,
                    extra_seconds=120,
                ),
                capture_output=True,
                text=True,
            )
            trim_elapsed_ms = int((time.monotonic() - trim_started) * 1000)
            trim_load_end = os.getloadavg() if hasattr(os, "getloadavg") else None
            current_app.logger.info(
                "Trim pass completed output=%s duration=%.3f elapsed_ms=%s ffmpeg_speed=%s load_start=%s load_end=%s",
                trimmed.name,
                duration,
                trim_elapsed_ms,
                _ffmpeg_final_speed(trim_result.stderr),
                _format_loadavg(trim_load_start),
                _format_loadavg(trim_load_end),
            )
            current_app.logger.debug("Trim stdout: %s", trim_result.stdout)
            current_app.logger.debug("Trim stderr: %s", trim_result.stderr)
            _log_video_dimensions("trimmed clip", trimmed)
        except subprocess.CalledProcessError as err:
            current_app.logger.error(
                "Trim failed stdout=%s stderr=%s",
                err.stdout,
                err.stderr,
            )
            if trimmed.exists():
                try:
                    trimmed.unlink()
                except Exception:
                    pass
            raise RuntimeError(
                f"FFmpeg trim failed: {err.stderr.strip() or err.stdout.strip()}"
            ) from err
        current_app.logger.info("Trimmed clip written: %s (exists=%s)", trimmed, trimmed.exists())

        clip_video_source = trimmed
    if clip_video_source is not None and video_override_source and clip_video_source == trimmed:
        merged_override = trimmed.parent / f"{video_override_source.stem}_with_audio_{secrets.token_hex(6)}.mp4"
        target_duration = max(1.0, float(duration))
        override_has_audio = _has_audio_stream(video_override_source)
        trimmed_has_audio = _has_audio_stream(trimmed)
        merge_cmd = [
            resolved_ffmpeg,
            "-y",
            "-stream_loop",
            "-1",
            "-i",
            str(video_override_source),
            "-i",
            str(trimmed),
            "-t",
            f"{target_duration:.3f}",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "23",
            "-movflags",
            "+faststart",
        ]
        if trimmed_has_audio and override_has_audio:
            if music_only:
                merge_cmd.extend(
                    [
                        "-map",
                        "0:v",
                        "-map",
                        "0:a",
                        "-c:a",
                        "aac",
                    ]
                )
                current_app.logger.info(
                    "Using music-only audio from override source (speech muted)"
                )
            else:
                merge_cmd.extend(
                    [
                        "-filter_complex",
                        "[1:a]aresample=async=1:first_pts=0[a_main];"
                        "[0:a]aresample=async=1:first_pts=0[a_bg];"
                        "[a_main][a_bg]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a_mix]",
                        "-map",
                        "0:v",
                        "-map",
                        "[a_mix]",
                        "-c:a",
                        "aac",
                    ]
                )
                current_app.logger.info(
                    "Merging override audio with source audio (job visual + original clip)"
                )
        elif trimmed_has_audio:
            merge_cmd.extend(
                [
                    "-map",
                    "0:v",
                    "-map",
                    "1:a",
                    "-c:a",
                    "aac",
                ]
            )
        elif override_has_audio:
            merge_cmd.extend(
                [
                    "-map",
                    "0:v",
                    "-map",
                    "0:a",
                    "-c:a",
                    "aac",
                ]
            )
        else:
            merge_cmd.extend(
                [
                    "-map",
                    "0:v",
                ]
            )
        merge_cmd.append(str(merged_override))
        current_app.logger.debug("Merge visual override command: %s", " ".join(merge_cmd))
        try:
            merge_result = run_media_subprocess(
                merge_cmd,
                operation="merge_visual_override",
                context=f"output={merged_override.name}",
                output_paths=[merged_override],
                check=True,
                timeout=scale_media_timeout(
                    FFMPEG_RENDER_TIMEOUT,
                    duration_seconds=target_duration,
                    multiplier=2.0,
                    extra_seconds=120,
                ),
                capture_output=True,
                text=True,
            )
            current_app.logger.debug("Merge stdout: %s", merge_result.stdout)
            current_app.logger.debug("Merge stderr: %s", merge_result.stderr)
        except subprocess.CalledProcessError as err:
            current_app.logger.error(
                "Merge static visual failed stdout=%s stderr=%s",
                err.stdout,
                err.stderr,
            )
            if merged_override and merged_override.exists():
                try:
                    merged_override.unlink()
                except Exception:
                    pass
            raise RuntimeError(
                f"FFmpeg merge failed: {err.stderr.strip() or err.stdout.strip()}"
            ) from err
        if not merged_override.exists():
            raise FileNotFoundError(f"Merged static file was not created: {merged_override}")
        clip_video_source = merged_override
        _log_video_dimensions("static override source", merged_override)

    if clip_video_source is None:
        raise RuntimeError("Clip video source could not be prepared.")

    if audio_override_source and audio_override_source.exists() and direct_audio_source is None:
        if _has_audio_stream(audio_override_source):
            merged_audio_override = trimmed.parent / f"{out_path.stem}_audio_override_{secrets.token_hex(6)}.mp4"
            audio_merge_cmd = [
                resolved_ffmpeg,
                "-y",
                "-i",
                str(clip_video_source),
                "-stream_loop",
                "-1",
                "-i",
                str(audio_override_source),
                "-t",
                f"{max(1.0, float(duration)):.3f}",
                "-map",
                "0:v",
                "-map",
                "1:a",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-crf",
                "23",
                "-c:a",
                "aac",
                "-movflags",
                "+faststart",
                str(merged_audio_override),
            ]
            current_app.logger.info(
                "Applying audio override source to composed clip video source: %s",
                audio_override_source,
            )
            try:
                run_media_subprocess(
                    audio_merge_cmd,
                    operation="merge_audio_override",
                    context=f"output={merged_audio_override.name}",
                    output_paths=[merged_audio_override],
                    check=True,
                    timeout=scale_media_timeout(
                        FFMPEG_RENDER_TIMEOUT,
                        duration_seconds=duration,
                        multiplier=2.0,
                        extra_seconds=120,
                    ),
                    capture_output=True,
                    text=True,
                )
            except subprocess.CalledProcessError as err:
                current_app.logger.error(
                    "Audio override merge failed stdout=%s stderr=%s",
                    err.stdout,
                    err.stderr,
                )
                if merged_audio_override and merged_audio_override.exists():
                    try:
                        merged_audio_override.unlink()
                    except Exception:
                        pass
                raise RuntimeError(
                    f"FFmpeg audio override merge failed: {err.stderr.strip() or err.stdout.strip()}"
                ) from err
            if merged_audio_override.exists():
                clip_video_source = merged_audio_override

    layout_video_source = direct_visual_override or clip_video_source
    _log_video_dimensions("clip_scaled source", layout_video_source)
    overlay_aspect = (crop_aspect or "landscape").lower()
    original_layout_mode = overlay_aspect == "original" and not podcast_mode
    # Shorts layout behavior:
    # - non-podcast: always render on 9:16 canvas and place overlay inside it.
    # - podcast: allow true horizontal canvas when landscape is selected.
    if original_layout_mode:
        try:
            source_width, source_height = _probe_video_dimensions(layout_video_source)
        except Exception:
            source_width, source_height = None, None
        target_width, target_height = _original_layout_target_dimensions(source_width, source_height)
        current_app.logger.info(
            "Original layout target source=%sx%s output=%sx%s",
            source_width,
            source_height,
            target_width,
            target_height,
        )
    elif podcast_mode and overlay_aspect != "portrait":
        target_width = PODCAST_LANDSCAPE_WIDTH
        target_height = PODCAST_LANDSCAPE_HEIGHT
    else:
        target_width = VIDEO_TARGET_WIDTH
        target_height = VIDEO_TARGET_HEIGHT
    render_timing.record_output_resolution(target_width, target_height)
    overlay_width = min(VIDEO_OVERLAY_WIDTH, target_width)
    if original_layout_mode:
        overlay_width = target_width
        overlay_height = target_height
    elif overlay_aspect == "portrait":
        overlay_width = target_width
        overlay_height = target_height
    else:
        overlay_height = int(round(overlay_width * 9 / 16))
        overlay_height += overlay_height % 2
    if podcast_mode:
        # In podcast mode, selected visual should fill the full 16:9 canvas instead of sitting on the shorts background.
        overlay_width = target_width
        overlay_height = target_height
    overlay_top_offset = VIDEO_OVERLAY_TOP_OFFSET if video_overlay_offset is None else video_overlay_offset
    try:
        overlay_top_offset = int(overlay_top_offset)
    except Exception:
        overlay_top_offset = VIDEO_OVERLAY_TOP_OFFSET
    overlay_top_offset = max(0, min(1200, overlay_top_offset))

    # Step 2: overlay trimmed onto background
    title_txt = _sanitize_text_for_overlay(title or "", 140)
    effective_subtitle_path = subtitle_path if show_subtitle else None
    effective_subtitle_overlay_path = (
        Path(subtitle_overlay_video_path)
        if show_subtitle and subtitle_overlay_video_path and Path(subtitle_overlay_video_path).exists()
        else None
    )
    effective_subtitle_overlay_specs = (
        _normalize_subtitle_overlay_specs(subtitle_overlay_specs)
        if show_subtitle
        else []
    )
    effective_subtitle_overlay_events = (
        _normalize_subtitle_overlay_events(subtitle_overlay_events)
        if show_subtitle
        else []
    )
    try:
        safe_title_line_spacing_main = int(title_line_spacing if title_line_spacing is not None else -4)
    except (TypeError, ValueError):
        safe_title_line_spacing_main = -4

    # Normalize crop ratios (x/y default to 0, width/height defaults to 1)
    crop_settings = crop_settings or {}
    def _normalize(value: Any, default: float) -> float:
        try:
            val = float(value)
        except Exception:
            val = default
        return max(0.0, min(1.0, val))

    def _normalize_crop_box(prefix: str) -> tuple[float, float, float, float]:
        box_x = _normalize(crop_settings.get(f"{prefix}x_ratio"), 0.0)
        box_y = _normalize(crop_settings.get(f"{prefix}y_ratio"), 0.0)
        box_w = max(0.01, min(1.0 - box_x, _normalize(crop_settings.get(f"{prefix}w_ratio"), 1.0)))
        box_h = max(0.01, min(1.0 - box_y, _normalize(crop_settings.get(f"{prefix}h_ratio"), 1.0)))
        return box_x, box_y, box_w, box_h

    crop_x = _normalize(crop_settings.get("crop_x_ratio"), 0.0)
    crop_y = _normalize(crop_settings.get("crop_y_ratio"), 0.0)
    crop_w = max(0.01, min(1.0 - crop_x, _normalize(crop_settings.get("crop_w_ratio"), 1.0)))
    crop_h = max(0.01, min(1.0 - crop_y, _normalize(crop_settings.get("crop_h_ratio"), 1.0)))
    split_enabled = bool(crop_settings.get("split_enabled"))
    crop_locked = bool(crop_settings.get("crop_locked"))
    has_crop2 = all(
        crop_settings.get(key) is not None
        for key in ("crop2_x_ratio", "crop2_y_ratio", "crop2_w_ratio", "crop2_h_ratio")
    )
    raw_layout_segments = crop_settings.get("layout_segments")

    def _fmt(v: float) -> str:
        return f"{v:.6f}"

    def _is_default_crop() -> bool:
        eps = 1e-6
        return (
            abs(crop_x) < eps
            and abs(crop_y) < eps
            and abs(crop_w - 1.0) < eps
            and abs(crop_h - 1.0) < eps
        )

    is_default_crop = _is_default_crop()
    split_stack_enabled = bool(
        split_enabled
        and overlay_aspect == "portrait"
        and has_crop2
        and not podcast_mode
        and not original_layout_mode
    )
    layout_segments: List[Dict[str, Any]] = []
    if isinstance(raw_layout_segments, list) and not original_layout_mode and not split_stack_enabled and not podcast_mode and not video_override_source:
        for segment in raw_layout_segments:
            if not isinstance(segment, dict):
                continue
            try:
                seg_start = max(0.0, float(segment.get("start") or 0.0))
                seg_end = min(float(duration), max(seg_start, float(segment.get("end") or 0.0)))
            except Exception:
                continue
            mode = str(segment.get("mode") or "").strip().lower()
            if mode not in {"fill", "fit", "split"} or seg_end - seg_start <= 1e-3:
                continue
            normalized_segment = {"start": seg_start, "end": seg_end, "mode": mode}
            if mode == "fill":
                try:
                    normalized_segment["zoom"] = max(1.0, min(float(FACE_AWARE_MAX_ZOOM), float(segment.get("zoom") or 1.0)))
                except Exception:
                    normalized_segment["zoom"] = 1.0
            elif mode == "split":
                normalized_segment["zoom"] = 1.0
                split_crops = segment.get("split_crops") if isinstance(segment.get("split_crops"), dict) else {}
                clean_crops: Dict[str, Dict[str, float]] = {}
                for key in ("top", "bottom"):
                    rect = split_crops.get(key) if isinstance(split_crops, dict) else None
                    if not isinstance(rect, dict):
                        continue
                    try:
                        rect_x = max(0.0, min(1.0, float(rect.get("x") or 0.0)))
                        rect_y = max(0.0, min(1.0, float(rect.get("y") or 0.0)))
                        rect_w = max(0.01, min(1.0 - rect_x, float(rect.get("w") or 1.0)))
                        rect_h = max(0.01, min(1.0 - rect_y, float(rect.get("h") or 1.0)))
                    except Exception:
                        continue
                    clean_crops[key] = {"x": rect_x, "y": rect_y, "w": rect_w, "h": rect_h}
                if set(clean_crops) == {"top", "bottom"}:
                    normalized_segment["split_crops"] = clean_crops
                else:
                    normalized_segment["mode"] = "fit"
                    normalized_segment["zoom"] = 1.0
            else:
                normalized_segment["zoom"] = 1.0
            layout_segments.append(normalized_segment)
        layout_segments.sort(key=lambda item: item["start"])
        layout_segments = [
            segment for segment in layout_segments
            if segment["end"] - segment["start"] > 1e-3
        ]
        if len(layout_segments) > 1:
            layout_segments[0]["start"] = 0.0
            layout_segments[-1]["end"] = float(duration)
        elif len(layout_segments) == 1:
            layout_segments[0]["start"] = 0.0
            layout_segments[0]["end"] = float(duration)
    face_aware_layout_active = len(layout_segments) >= 1
    face_aware_auto_caption_placement = bool(
        face_aware_layout_active
        and _is_face_aware_default_subtitle_margin(
            subtitle_margin,
            subtitle_margin_custom=subtitle_margin_custom,
        )
    )
    face_aware_fit_metrics = _face_aware_fit_position_metrics(
        target_width=target_width,
        target_height=target_height,
        subtitle_font_size=subtitle_font_size,
    )
    face_aware_fit_frame_height = max(
        2,
        int(round(face_aware_fit_metrics["frame_bottom"] - face_aware_fit_metrics["frame_top"])),
    )
    face_aware_fit_frame_height -= face_aware_fit_frame_height % 2
    face_aware_split_metrics = _face_aware_split_position_metrics(
        target_height=target_height,
        subtitle_font_size=subtitle_font_size,
    )
    if is_default_crop:
        scale_stage = (
            f"scale={overlay_width}:{overlay_height}:force_original_aspect_ratio=increase,"
            f"crop={overlay_width}:{overlay_height},"
        )
    else:
        scale_stage = f"scale={overlay_width}:{overlay_height},"

    face_aware_has_fit_segment = any(
        str(segment.get("mode") or "").strip().lower() == "fit"
        for segment in layout_segments
    )
    needs_background_stream = (not original_layout_mode) and (split_stack_enabled or (not face_aware_layout_active) or face_aware_has_fit_segment)
    clip_input_label = "[1:v]"
    direct_timeline_audio_label: Optional[str] = None
    filter_parts: list[str] = []
    if direct_source_timeline:
        timeline_source_has_audio = bool(
            has_audio and not (direct_visual_override and music_only)
        )
        relative_ranges = [
            {
                "start": max(0.0, float(item["start"]) - direct_source_seek),
                "end": max(0.0, float(item["end"]) - direct_source_seek),
            }
            for item in direct_source_ranges
        ]
        if len(relative_ranges) == 1:
            item = relative_ranges[0]
            if not direct_visual_override:
                filter_parts.append(
                    f"[1:v]trim=start={item['start']:.6f}:end={item['end']:.6f},"
                    "setpts=PTS-STARTPTS,settb=AVTB[clip_direct_v]"
                )
            if timeline_source_has_audio:
                filter_parts.append(
                    f"[1:a]atrim=start={item['start']:.6f}:end={item['end']:.6f},"
                    "asetpts=PTS-STARTPTS,aresample=async=1:first_pts=0[clip_source_a]"
                )
                direct_timeline_audio_label = "[clip_source_a]"
        else:
            if not direct_visual_override:
                video_split_labels = "".join(
                    f"[clip_range_v_src_{index}]" for index in range(len(relative_ranges))
                )
                filter_parts.append(f"[1:v]split={len(relative_ranges)}{video_split_labels}")
            if timeline_source_has_audio:
                audio_split_labels = "".join(
                    f"[clip_range_a_src_{index}]" for index in range(len(relative_ranges))
                )
                filter_parts.append(f"[1:a]asplit={len(relative_ranges)}{audio_split_labels}")
            video_concat_inputs: list[str] = []
            audio_concat_inputs: list[str] = []
            for index, item in enumerate(relative_ranges):
                if not direct_visual_override:
                    filter_parts.append(
                        f"[clip_range_v_src_{index}]trim=start={item['start']:.6f}:end={item['end']:.6f},"
                        f"setpts=PTS-STARTPTS,settb=AVTB[clip_range_v_{index}]"
                    )
                    video_concat_inputs.append(f"[clip_range_v_{index}]")
                if timeline_source_has_audio:
                    filter_parts.append(
                        f"[clip_range_a_src_{index}]atrim=start={item['start']:.6f}:end={item['end']:.6f},"
                        f"asetpts=PTS-STARTPTS,aresample=async=1:first_pts=0[clip_range_a_{index}]"
                    )
                    audio_concat_inputs.append(f"[clip_range_a_{index}]")
            if not direct_visual_override and timeline_source_has_audio:
                concat_inputs = "".join(
                    video_label + audio_label
                    for video_label, audio_label in zip(video_concat_inputs, audio_concat_inputs)
                )
                filter_parts.append(
                    concat_inputs
                    + f"concat=n={len(relative_ranges)}:v=1:a=1[clip_direct_v][clip_source_a]"
                )
                direct_timeline_audio_label = "[clip_source_a]"
            elif not direct_visual_override:
                filter_parts.append(
                    "".join(video_concat_inputs)
                    + f"concat=n={len(relative_ranges)}:v=1:a=0[clip_direct_v]"
                )
            elif timeline_source_has_audio:
                filter_parts.append(
                    "".join(audio_concat_inputs)
                    + f"concat=n={len(relative_ranges)}:v=0:a=1[clip_source_a]"
                )
                direct_timeline_audio_label = "[clip_source_a]"
        if direct_visual_override:
            filter_parts.append(
                f"[2:v]trim=duration={duration:.6f},setpts=PTS-STARTPTS,settb=AVTB[clip_direct_v]"
            )
            override_has_audio = _has_audio_stream(direct_visual_override)
            if override_has_audio:
                filter_parts.append(
                    f"[2:a]atrim=duration={duration:.6f},asetpts=PTS-STARTPTS,"
                    "aresample=async=1:first_pts=0[clip_override_a]"
                )
                if music_only or not direct_timeline_audio_label:
                    direct_timeline_audio_label = "[clip_override_a]"
                else:
                    filter_parts.append(
                        f"{direct_timeline_audio_label}[clip_override_a]"
                        "amix=inputs=2:duration=first:dropout_transition=0:normalize=0[clip_base_a]"
                    )
                    direct_timeline_audio_label = "[clip_base_a]"
        clip_input_label = "[clip_direct_v]"
    if needs_background_stream and outreach_layout:
        outreach_source_label = clip_input_label
        clip_input_label = "[outreach_clip_src]"
        filter_parts.extend(
            [
                f"{outreach_source_label}split=2[outreach_bg_src][outreach_clip_src]",
                (
                    f"[outreach_bg_src]scale={OUTREACH_BLUR_WIDTH}:{OUTREACH_BLUR_HEIGHT}:"
                    "force_original_aspect_ratio=increase,"
                    f"crop={OUTREACH_BLUR_WIDTH}:{OUTREACH_BLUR_HEIGHT},"
                    f"gblur=sigma={OUTREACH_BLUR_SIGMA}:steps=2,"
                    f"scale={target_width}:{target_height}:flags=bilinear,"
                    f"drawbox=x=0:y=0:w=iw:h=ih:color=black@{OUTREACH_BACKGROUND_DARKEN_OPACITY}:t=fill,"
                    "setsar=1[bg]"
                ),
            ]
        )
    elif needs_background_stream:
        filter_parts.append(f"[0:v]scale={target_width}:{target_height},setsar=1[bg]")
    if split_stack_enabled:
        crop2_x, crop2_y, crop2_w, crop2_h = _normalize_crop_box("crop2_")
        split_tile_width = target_width
        split_tile_height = max(2, int(target_height * DESIGN_GEOMETRY["split_ratio"]))
        split_tile_height -= split_tile_height % 2
        split_seam_y = int(target_height * DESIGN_GEOMETRY["split_ratio"])
        split_divider_thickness = DESIGN_GEOMETRY["split_divider"]
        split_divider_outline_thickness = DESIGN_GEOMETRY["split_outline"]
        filter_parts.extend(
            [
                f"{clip_input_label}split=2[split_top_src][split_bottom_src]",
                (
                    f"[split_top_src]crop=iw*{_fmt(crop_w)}:ih*{_fmt(crop_h)}:iw*{_fmt(crop_x)}:ih*{_fmt(crop_y)},"
                    f"scale={split_tile_width}:{split_tile_height},"
                    "setsar=1,"
                    "setpts=PTS-STARTPTS[top]"
                ),
                (
                    f"[split_bottom_src]crop=iw*{_fmt(crop2_w)}:ih*{_fmt(crop2_h)}:iw*{_fmt(crop2_x)}:ih*{_fmt(crop2_y)},"
                    f"scale={split_tile_width}:{split_tile_height},"
                    "setsar=1,"
                    "setpts=PTS-STARTPTS[bottom]"
                ),
                "[top][bottom]vstack=inputs=2[clip_stack]",
                (
                    f"[clip_stack]drawbox=x=0:y={split_seam_y}-{split_divider_thickness}/2:"
                    f"w={target_width}:h={split_divider_thickness}:color=white@1.0:t=fill[clip_stack_div_base]"
                ),
                (
                    f"[clip_stack_div_base]drawbox=x=0:y={split_seam_y}-{split_divider_thickness}/2-{split_divider_outline_thickness}:"
                    f"w={target_width}:h={split_divider_outline_thickness}:color=black@1.0:t=fill[clip_stack_div_top]"
                ),
                (
                    f"[clip_stack_div_top]drawbox=x=0:y={split_seam_y}+{split_divider_thickness}/2:"
                    f"w={target_width}:h={split_divider_outline_thickness}:color=black@1.0:t=fill[clip_stack_div]"
                ),
                "[bg][clip_stack_div]overlay=(W-w)/2:0:shortest=1[ov]",
            ]
        )
    else:
        face_track_points = []
        if (
            not is_default_crop
            and not crop_locked
            and face_track_smooth_path
            and not podcast_mode
            and not video_override_source
        ):
            face_track_points = _load_face_track_points(
                face_track_smooth_path,
                clip_start=start,
                crop_w=crop_w,
                crop_h=crop_h,
            )
        elif crop_locked and face_track_smooth_path and not split_stack_enabled:
            current_app.logger.info(
                "Face-track skipped: crop_locked crop_x=%.6f crop_y=%.6f crop_w=%.6f crop_h=%.6f",
                crop_x,
                crop_y,
                crop_w,
                crop_h,
            )
        dynamic_crop_enabled = len(face_track_points) >= 2
        if original_layout_mode:
            original_zoom_rect = _normalize_original_zoom_rect(crop_settings.get("original_zoom_rect"))
            if original_zoom_rect:
                crop_expr = (
                    f"crop=iw*{_fmt(original_zoom_rect['w'])}:ih*{_fmt(original_zoom_rect['h'])}:"
                    f"iw*{_fmt(original_zoom_rect['x'])}:ih*{_fmt(original_zoom_rect['y'])},"
                )
                current_app.logger.info("Original layout zoom crop rect=%s", original_zoom_rect)
            else:
                crop_expr = ""
            filter_parts.append(
                f"{clip_input_label}{crop_expr}scale={target_width}:{target_height},setsar=1,setpts=PTS-STARTPTS[ov]"
            )
        elif face_aware_layout_active:
            for idx, segment in enumerate(layout_segments):
                if str(segment.get("mode") or "fill").strip().lower() != "fill":
                    continue
                metrics = _face_aware_fill_caption_metrics_for_segment(
                    segment,
                    face_track_points=face_track_points,
                    crop_h=crop_h,
                    target_height=target_height,
                    subtitle_font_size=subtitle_font_size,
                    subtitle_margin=subtitle_margin,
                    subtitle_margin_custom=subtitle_margin_custom,
                )
                segment["caption_style_suffix"] = f"Fill{idx}"
                segment["caption_margin_v"] = float(metrics["caption_margin_v"] or FACE_AWARE_FILL_FALLBACK_SUBTITLE_MARGIN)
                segment["caption_top"] = float(metrics["caption_top"] or 0.0)
                segment["caption_block_height"] = float(metrics["caption_block_height"] or _face_aware_caption_block_height(subtitle_font_size))
                segment["caption_face_bottom"] = metrics["median_face_bottom"]
                segment["subscribe_y"] = float(metrics["subscribe_y"] or 0.0)
            caption_block_height = _face_aware_caption_block_height(subtitle_font_size)
            for segment in layout_segments:
                caption_top = _face_aware_caption_top_for_block(
                    segment,
                    block_height=caption_block_height,
                    target_width=target_width,
                    target_height=target_height,
                    subtitle_margin=subtitle_margin,
                    subtitle_margin_custom=subtitle_margin_custom,
                )
                segment["caption_top"] = float(caption_top)
                segment["caption_block_height"] = float(caption_block_height)
                segment["caption_margin_v"] = max(
                    0.0,
                    float(target_height) - float(caption_top) - float(caption_block_height),
                )
                if str(segment.get("mode") or "fill").strip().lower() == "fit":
                    segment["subscribe_y"] = float(face_aware_fit_metrics["subscribe_y"])
                else:
                    segment["subscribe_y"] = float(
                        caption_top + caption_block_height + DESIGN_GEOMETRY["fit_caption_gap"]
                    )
        if dynamic_crop_enabled:
            x_ratio_expr = _piecewise_linear_ratio_expr(face_track_points, "x") or _fmt(crop_x)
            y_ratio_expr = _piecewise_linear_ratio_expr(face_track_points, "y") or _fmt(crop_y)
            crop_x_expr = f"iw*({_ffmpeg_filter_expr(x_ratio_expr)})"
            crop_y_expr = f"ih*({_ffmpeg_filter_expr(y_ratio_expr)})"
            current_app.logger.info(
                "Dynamic face-track crop enabled path=%s points=%s crop_w=%.6f crop_h=%.6f x_expr=%s y_expr=%s",
                face_track_smooth_path,
                len(face_track_points),
                crop_w,
                crop_h,
                crop_x_expr,
                crop_y_expr,
            )
        else:
            crop_x_expr = f"iw*{_fmt(crop_x)}"
            crop_y_expr = f"ih*{_fmt(crop_y)}"
            if face_track_smooth_path and not split_stack_enabled:
                current_app.logger.info(
                    "Dynamic face-track crop fallback static path=%s points=%s default_crop=%s podcast=%s",
                    face_track_smooth_path,
                    len(face_track_points),
                    is_default_crop,
                    podcast_mode,
                )
        if (not original_layout_mode) and face_aware_layout_active:
            if len(layout_segments) > 1:
                source_labels = "".join(f"[layout_src_{idx}]" for idx in range(len(layout_segments)))
                filter_parts.append(f"{clip_input_label}split={len(layout_segments)}{source_labels}")
            fit_indices = [idx for idx, segment in enumerate(layout_segments) if segment["mode"] == "fit"]
            fit_bg_labels = {}
            if fit_indices:
                if len(fit_indices) == 1:
                    fit_bg_labels[fit_indices[0]] = "[layout_bg_0]"
                    filter_parts.append("[bg]trim=duration=999999,setpts=PTS-STARTPTS,settb=AVTB[layout_bg_0]")
                else:
                    split_labels = "".join(f"[layout_bg_src_{idx}]" for idx in fit_indices)
                    filter_parts.append(f"[bg]split={len(fit_indices)}{split_labels}")
                    for idx in fit_indices:
                        fit_bg_labels[idx] = f"[layout_bg_{idx}]"
                        filter_parts.append(
                            f"[layout_bg_src_{idx}]trim=duration=999999,setpts=PTS-STARTPTS,settb=AVTB{fit_bg_labels[idx]}"
                        )
            segment_outputs: List[str] = []
            for idx, segment in enumerate(layout_segments):
                seg_start = float(segment["start"])
                seg_end = float(segment["end"])
                seg_duration = max(0.001, seg_end - seg_start)
                seg_trim = f"[layout_trim_{idx}]"
                seg_out = f"[layout_out_{idx}]"
                layout_src = f"[layout_src_{idx}]" if len(layout_segments) > 1 else clip_input_label
                filter_parts.append(
                    f"{layout_src}trim=start={seg_start:.6f}:end={seg_end:.6f},"
                    f"setpts=PTS-STARTPTS,settb=AVTB{seg_trim}"
                )
                if segment["mode"] == "fit":
                    fit_scaled = f"[layout_fit_{idx}]"
                    fit_bg = f"[layout_bg_trim_{idx}]"
                    filter_parts.append(
                        f"{seg_trim}scale={target_width}:-2,"
                        f"setsar=1,settb=AVTB{fit_scaled}"
                    )
                    filter_parts.append(
                        f"{fit_bg_labels[idx]}trim=duration={seg_duration:.6f},"
                        f"setpts=PTS-STARTPTS,settb=AVTB{fit_bg}"
                    )
                    fit_overlay_y = (
                        f"{_fmt(face_aware_fit_metrics['frame_top'])}"
                        f"+({face_aware_fit_frame_height}-h)/2"
                    )
                    filter_parts.append(
                        f"{fit_bg}{fit_scaled}overlay=(W-w)/2:{fit_overlay_y}:shortest=1,"
                        f"format=yuv420p,settb=AVTB{seg_out}"
                    )
                elif segment["mode"] == "split":
                    split_crops = segment.get("split_crops") if isinstance(segment.get("split_crops"), dict) else {}
                    top_rect = split_crops.get("top") if isinstance(split_crops, dict) else {}
                    bottom_rect = split_crops.get("bottom") if isinstance(split_crops, dict) else {}
                    split_tile_width = target_width
                    split_tile_height = max(2, int(target_height * DESIGN_GEOMETRY["split_ratio"]))
                    split_tile_height -= split_tile_height % 2
                    split_seam_y = int(target_height * DESIGN_GEOMETRY["split_ratio"])
                    split_divider_thickness = DESIGN_GEOMETRY["split_divider"]
                    split_divider_outline_thickness = DESIGN_GEOMETRY["split_outline"]
                    split_top_src = f"[layout_split_top_src_{idx}]"
                    split_bottom_src = f"[layout_split_bottom_src_{idx}]"
                    split_top = f"[layout_split_top_{idx}]"
                    split_bottom = f"[layout_split_bottom_{idx}]"
                    split_stack = f"[layout_split_stack_{idx}]"
                    split_div_base = f"[layout_split_div_base_{idx}]"
                    split_div_top = f"[layout_split_div_top_{idx}]"
                    filter_parts.append(f"{seg_trim}split=2{split_top_src}{split_bottom_src}")
                    filter_parts.append(
                        f"{split_top_src}crop=iw*{_fmt(float(top_rect.get('w') or 1.0))}:"
                        f"ih*{_fmt(float(top_rect.get('h') or 1.0))}:"
                        f"iw*{_fmt(float(top_rect.get('x') or 0.0))}:"
                        f"ih*{_fmt(float(top_rect.get('y') or 0.0))},"
                        f"scale={split_tile_width}:{split_tile_height},setsar=1,settb=AVTB{split_top}"
                    )
                    filter_parts.append(
                        f"{split_bottom_src}crop=iw*{_fmt(float(bottom_rect.get('w') or 1.0))}:"
                        f"ih*{_fmt(float(bottom_rect.get('h') or 1.0))}:"
                        f"iw*{_fmt(float(bottom_rect.get('x') or 0.0))}:"
                        f"ih*{_fmt(float(bottom_rect.get('y') or 0.0))},"
                        f"scale={split_tile_width}:{split_tile_height},setsar=1,settb=AVTB{split_bottom}"
                    )
                    filter_parts.append(f"{split_top}{split_bottom}vstack=inputs=2{split_stack}")
                    filter_parts.append(
                        f"{split_stack}drawbox=x=0:y={split_seam_y}-{split_divider_thickness}/2:"
                        f"w={target_width}:h={split_divider_thickness}:color=white@1.0:t=fill{split_div_base}"
                    )
                    filter_parts.append(
                        f"{split_div_base}drawbox=x=0:y={split_seam_y}-{split_divider_thickness}/2-{split_divider_outline_thickness}:"
                        f"w={target_width}:h={split_divider_outline_thickness}:color=black@1.0:t=fill{split_div_top}"
                    )
                    filter_parts.append(
                        f"{split_div_top}drawbox=x=0:y={split_seam_y}+{split_divider_thickness}/2:"
                        f"w={target_width}:h={split_divider_outline_thickness}:color=black@1.0:t=fill,"
                        f"format=yuv420p,settb=AVTB{seg_out}"
                    )
                else:
                    try:
                        segment_zoom = max(1.0, min(float(FACE_AWARE_MAX_ZOOM), float(segment.get("zoom") or 1.0)))
                    except Exception:
                        segment_zoom = 1.0
                    if segment_zoom > 1.0001:
                        seg_crop_w = max(0.01, min(1.0, crop_w / segment_zoom))
                        seg_crop_h = max(0.01, min(1.0, crop_h / segment_zoom))
                        if dynamic_crop_enabled:
                            segment_time_expr = f"(t+{seg_start:.6f})"
                            seg_cx_expr = _piecewise_linear_ratio_expr(
                                face_track_points,
                                "cx",
                                time_expr=segment_time_expr,
                            ) or _fmt(crop_x + crop_w / 2.0)
                            seg_crop_x_expr_raw = f"min(max(({seg_cx_expr})-{_fmt(seg_crop_w / 2.0)},0),{_fmt(max(0.0, 1.0 - seg_crop_w))})"
                            seg_cy_expr = _piecewise_linear_ratio_expr(
                                face_track_points,
                                "cy",
                                time_expr=segment_time_expr,
                            ) or _fmt(crop_y + crop_h / 2.0)
                            seg_crop_y_expr_raw = f"min(max(({seg_cy_expr})-{_fmt(seg_crop_h / 2.0)},0),{_fmt(max(0.0, 1.0 - seg_crop_h))})"
                        else:
                            seg_crop_x_expr_raw = f"{_fmt(crop_x + (crop_w - seg_crop_w) / 2.0)}"
                            seg_crop_y_expr_raw = f"{_fmt(crop_y + (crop_h - seg_crop_h) / 2.0)}"
                        seg_crop_x_expr = f"iw*({_ffmpeg_filter_expr(seg_crop_x_expr_raw)})"
                        seg_crop_y_expr = f"ih*({_ffmpeg_filter_expr(seg_crop_y_expr_raw)})"
                        seg_crop_w_for_filter = seg_crop_w
                        seg_crop_h_for_filter = seg_crop_h
                    elif dynamic_crop_enabled:
                        segment_time_expr = f"(t+{seg_start:.6f})"
                        seg_x_expr = _piecewise_linear_ratio_expr(
                            face_track_points,
                            "x",
                            time_expr=segment_time_expr,
                        ) or _fmt(crop_x)
                        seg_y_expr = _piecewise_linear_ratio_expr(
                            face_track_points,
                            "y",
                            time_expr=segment_time_expr,
                        ) or _fmt(crop_y)
                        seg_crop_x_expr = f"iw*({_ffmpeg_filter_expr(seg_x_expr)})"
                        seg_crop_y_expr = f"ih*({_ffmpeg_filter_expr(seg_y_expr)})"
                        seg_crop_w_for_filter = crop_w
                        seg_crop_h_for_filter = crop_h
                    else:
                        seg_crop_x_expr = f"iw*{_fmt(crop_x)}"
                        seg_crop_y_expr = f"ih*{_fmt(crop_y)}"
                        seg_crop_w_for_filter = crop_w
                        seg_crop_h_for_filter = crop_h
                    filter_parts.append(
                        f"{seg_trim}crop=iw*{_fmt(seg_crop_w_for_filter)}:ih*{_fmt(seg_crop_h_for_filter)}:{seg_crop_x_expr}:{seg_crop_y_expr},"
                        f"{scale_stage}setsar=1,format=yuv420p,settb=AVTB{seg_out}"
                    )
                segment_outputs.append(seg_out)
            if len(segment_outputs) == 1:
                filter_parts.append(f"{segment_outputs[0]}null[ov]")
            else:
                filter_parts.append(
                    "".join(segment_outputs) + f"concat=n={len(segment_outputs)}:v=1:a=0[ov]"
                )
            current_app.logger.info(
                "face_aware_layout compositor_segments=%s fill_caption_metrics=%s split_crops=%s duration=%.3f",
                [
                    (
                        round(float(seg["start"]), 3),
                        round(float(seg["end"]), 3),
                        seg["mode"],
                        round(float(seg.get("zoom") or 1.0), 3),
                    )
                    for seg in layout_segments
                ],
                [
                    {
                        "start": round(float(seg.get("start") or 0.0), 3),
                        "end": round(float(seg.get("end") or 0.0), 3),
                        "zoom": round(float(seg.get("zoom") or 1.0), 3),
                        "face_bottom": (
                            round(float(seg["caption_face_bottom"]), 2)
                            if seg.get("caption_face_bottom") is not None
                            else None
                        ),
                        "caption_top": round(float(seg.get("caption_top") or 0.0), 2),
                        "caption_margin_v": round(float(seg.get("caption_margin_v") or 0.0), 2),
                        "subscribe_y": round(float(seg.get("subscribe_y") or 0.0), 2),
                    }
                    for seg in layout_segments
                    if str(seg.get("mode") or "fill").strip().lower() == "fill"
                ],
                [
                    {
                        "start": round(float(seg.get("start") or 0.0), 3),
                        "end": round(float(seg.get("end") or 0.0), 3),
                        "face_count": 2,
                        "crops": seg.get("split_crops"),
                    }
                    for seg in layout_segments
                    if str(seg.get("mode") or "").strip().lower() == "split"
                ],
                duration,
            )
        elif not original_layout_mode:
            crop_filter = (
                f"{clip_input_label}crop=iw*{_fmt(crop_w)}:ih*{_fmt(crop_h)}:{crop_x_expr}:{crop_y_expr},"
                f"{scale_stage}"
                "setsar=1,"
                "setpts=PTS-STARTPTS[clip_scaled]"
            )
            current_app.logger.debug(
                "clip_scaled target dims=%dx%d default_crop=%s",
                overlay_width,
                overlay_height,
                is_default_crop,
            )
            overlay_y_expr = "0" if podcast_mode else _overlay_y_expr(
                overlay_top_offset,
                effective_subtitle_path,
                subtitle_margin,
                subtitle_font_size,
            )
            filter_parts.extend(
                [
                    crop_filter,
                    f"[bg][clip_scaled]overlay=(W-w)/2:{overlay_y_expr}:shortest=1[ov]",
                ]
            )
    final_label = "[ov]"
    overlay_asset_path = subscribe_overlay_path if subscribe_overlay_path and Path(subscribe_overlay_path).exists() else None
    overlay_enabled = subscribe_overlay_enabled and bool(overlay_asset_path)
    base_filter_len = len(filter_parts)
    base_final_label = final_label
    title_overlay_path: Optional[Path] = None
    title_overlay_paths: List[Path] = []
    next_video_input_index = 3 if (direct_audio_source or direct_visual_override) else 2
    shape_overlay_path = _render_overlay_shapes(
        overlay_shapes,
        target_width=target_width,
        target_height=target_height,
        owner_user_id=overlay_owner_user_id,
        brand_id=overlay_brand_id,
    )
    if shape_overlay_path:
        shape_input_index = next_video_input_index
        next_video_input_index += 1
        filter_parts.append(f"[{shape_input_index}:v]format=rgba[shape_overlay_src]")
        filter_parts.append(
            f"{final_label}[shape_overlay_src]overlay=0:0:shortest=1[shape_overlay_out]"
        )
        final_label = "[shape_overlay_out]"
    if show_title and title_txt:
        test_font_file = _title_font_path_for_text(title_txt, font_path)
        if normalized_title_engine == "pillow":
            try:
                requested_video_top = int(video_overlay_offset if video_overlay_offset is not None else VIDEO_OVERLAY_TOP_OFFSET)
            except (TypeError, ValueError):
                requested_video_top = VIDEO_OVERLAY_TOP_OFFSET
            title_margin_custom = int(title_margin or DEFAULT_TITLE_MARGIN) != int(DEFAULT_TITLE_MARGIN)
            video_top_limit = None
            if not title_margin_custom and str(crop_aspect or "landscape").strip().lower() != "portrait":
                video_top_limit = max(0, min(target_height, requested_video_top))
            title_overlay_path, title_overlay_meta = _render_title_overlay(
                title_txt,
                font_path=test_font_file,
                font_size=title_font_size,
                target_width=target_width,
                target_height=target_height,
                base_y=title_margin,
                line_spacing=safe_title_line_spacing_main,
                title_text_color=title_text_color,
                subtitle_preset=subtitle_preset,
                title_bg_color=title_bg_color,
                title_bg_alpha=title_bg_alpha,
                uppercase=bool(title_uppercase),
                title_language=title_language,
                title_outline_color=title_outline_color,
                title_outline_width=title_outline_width,
                video_top_limit=video_top_limit,
            )
            current_app.logger.info(
                "Pillow title overlay text_width=%s margin=%s font_size=%s lines=%s draw_y=%s shadow=%s",
                title_overlay_meta["max_text_width"],
                title_overlay_meta["side_margin"],
                title_overlay_meta["font_size"],
                title_overlay_meta["line_count"],
                title_overlay_meta["draw_y"],
                title_overlay_meta["shadow"],
            )
            if face_aware_layout_active and not title_margin_custom:
                fill_segment_indices = [
                    idx for idx, segment in enumerate(layout_segments)
                    if str(segment.get("mode") or "fill") != "fit"
                ]
                fit_segment_indices = [
                    idx for idx, segment in enumerate(layout_segments)
                    if str(segment.get("mode") or "fill") == "fit"
                ]
                fill_source_by_index: Dict[int, str] = {}
                fit_source_by_index: Dict[int, str] = {}
                if fill_segment_indices:
                    title_overlay_paths.append(title_overlay_path)
                    fill_title_input_index = next_video_input_index
                    next_video_input_index += 1
                    filter_parts.append(
                        _title_source_filter(f"[{fill_title_input_index}:v]", "[title_fill_src]")
                    )
                    if len(fill_segment_indices) > 1:
                        fill_split_labels = "".join(f"[title_fill_src_{idx}]" for idx in fill_segment_indices)
                        filter_parts.append(f"[title_fill_src]split={len(fill_segment_indices)}{fill_split_labels}")
                        fill_source_by_index = {idx: f"[title_fill_src_{idx}]" for idx in fill_segment_indices}
                    else:
                        fill_source_by_index[fill_segment_indices[0]] = "[title_fill_src]"
                if fit_segment_indices:
                    fit_title_overlay_path, fit_title_overlay_meta = _render_title_overlay(
                        title_txt,
                        font_path=test_font_file,
                        font_size=title_font_size,
                        target_width=target_width,
                        target_height=target_height,
                        base_y=int(round(face_aware_fit_metrics["title_bottom"])),
                        line_spacing=safe_title_line_spacing_main,
                        title_text_color=title_text_color,
                        subtitle_preset=subtitle_preset,
                        title_bg_color=title_bg_color,
                        title_bg_alpha=title_bg_alpha,
                        uppercase=bool(title_uppercase),
                        title_language=title_language,
                        title_outline_color=title_outline_color,
                        title_outline_width=title_outline_width,
                        video_top_limit=int(round(face_aware_fit_metrics["title_bottom"])),
                    )
                    title_overlay_paths.append(fit_title_overlay_path)
                    current_app.logger.info(
                        "Pillow title overlay fit text_width=%s margin=%s font_size=%s lines=%s draw_y=%s title_bottom=%.2f shadow=%s",
                        fit_title_overlay_meta["max_text_width"],
                        fit_title_overlay_meta["side_margin"],
                        fit_title_overlay_meta["font_size"],
                        fit_title_overlay_meta["line_count"],
                        fit_title_overlay_meta["draw_y"],
                        face_aware_fit_metrics["title_bottom"],
                        fit_title_overlay_meta["shadow"],
                    )
                    fit_title_input_index = next_video_input_index
                    next_video_input_index += 1
                    filter_parts.append(
                        _title_source_filter(f"[{fit_title_input_index}:v]", "[title_fit_src]")
                    )
                    if len(fit_segment_indices) > 1:
                        fit_split_labels = "".join(f"[title_fit_src_{idx}]" for idx in fit_segment_indices)
                        filter_parts.append(f"[title_fit_src]split={len(fit_segment_indices)}{fit_split_labels}")
                        fit_source_by_index = {idx: f"[title_fit_src_{idx}]" for idx in fit_segment_indices}
                    else:
                        fit_source_by_index[fit_segment_indices[0]] = "[title_fit_src]"
                current_title_label = final_label
                for idx, segment in enumerate(layout_segments):
                    seg_start = float(segment.get("start") or 0.0)
                    seg_end = float(segment.get("end") or duration)
                    source_label = (
                        fit_source_by_index.get(idx)
                        if str(segment.get("mode") or "fill") == "fit"
                        else fill_source_by_index.get(idx)
                    )
                    if not source_label:
                        continue
                    next_title_label = f"[ov_title_debug_{idx}]"
                    enable_expr = f"between(t\\,{seg_start:.6f}\\,{seg_end:.6f})"
                    filter_parts.append(
                        f"{current_title_label}{source_label}overlay=0:0:shortest=1:enable='{enable_expr}'{next_title_label}"
                    )
                    current_title_label = next_title_label
                final_label = current_title_label
            else:
                title_overlay_paths.append(title_overlay_path)
                title_input_index = next_video_input_index
                filter_parts.append(
                    _title_source_filter(f"[{title_input_index}:v]", "[title_src]")
                )
                filter_parts.append(
                    f"{final_label}[title_src]overlay=0:0:shortest=1[ov_title_debug]"
                )
                final_label = "[ov_title_debug]"
                next_video_input_index += 1
    face_aware_subtitle_path: Optional[Path] = None
    anchored_subtitle_path: Optional[Path] = None
    if effective_subtitle_path:
        subtitle_path_for_filter = effective_subtitle_path
        subtitle_margin_for_filter = subtitle_margin
        if normalize_caption_anchor(caption_anchor) != "bottom":
            anchored_subtitle_path = _anchored_ass_from_srt(
                effective_subtitle_path,
                target_width=target_width,
                target_height=target_height,
                caption_position=subtitle_margin,
                caption_anchor=caption_anchor,
            )
            if anchored_subtitle_path:
                subtitle_path_for_filter = anchored_subtitle_path
        if face_aware_auto_caption_placement:
            try:
                face_aware_subtitle_path = _build_face_aware_subtitle_path(
                    effective_subtitle_path,
                    layout_segments=layout_segments,
                    target_width=target_width,
                    target_height=target_height,
                    subtitle_font_size=subtitle_font_size,
                    fit_margin=int(round(face_aware_fit_metrics["caption_margin_v"])),
                    subtitle_font=subtitle_font,
                    subtitle_text_color=subtitle_text_color,
                    subtitle_text_alpha=subtitle_text_alpha,
                    subtitle_bg_color=subtitle_bg_color,
                    subtitle_bg_alpha=subtitle_bg_alpha,
                    subtitle_style=subtitle_style,
                )
            except Exception as exc:
                current_app.logger.warning("Face-aware subtitle style split skipped: %s", exc)
                face_aware_subtitle_path = None
            if face_aware_subtitle_path:
                subtitle_path_for_filter = face_aware_subtitle_path
                subtitle_margin_for_filter = 0
        style = _subtitle_force_style(
            target_width=target_width,
            target_height=target_height,
            subtitle_font_size=subtitle_font_size,
            subtitle_margin=subtitle_margin_for_filter,
            caption_anchor=caption_anchor,
            subtitle_font=subtitle_font,
            subtitle_text_color=subtitle_text_color,
            subtitle_text_alpha=subtitle_text_alpha,
            subtitle_bg_color=subtitle_bg_color,
            subtitle_bg_alpha=subtitle_bg_alpha,
            subtitle_style=subtitle_style,
        )
        if face_aware_subtitle_path:
            filter_parts.append(
                f"{final_label}subtitles='{_escape_ass_path(subtitle_path_for_filter)}':fontsdir='{_escape_ass_path(SUBTITLE_FONTS_DIR)}'[subout]"
            )
        else:
            filter_parts.append(
                f"{final_label}subtitles='{_escape_ass_path(subtitle_path_for_filter)}':fontsdir='{_escape_ass_path(SUBTITLE_FONTS_DIR)}':force_style='{style}'[subout]"
            )
        final_label = "[subout]"
    if effective_subtitle_overlay_specs:
        final_label, next_video_input_index = _append_timed_subtitle_overlay_filters(
            filter_parts,
            final_label,
            start_input_index=next_video_input_index,
            overlay_specs=effective_subtitle_overlay_specs,
            label_prefix="caption",
        )
    elif effective_subtitle_overlay_path:
        filter_parts.append(f"[{next_video_input_index}:v]format=rgba[caption_src]")
        caption_overlay_y_expr = "0"
        if face_aware_auto_caption_placement:
            caption_overlay_y_expr = "0"
            if effective_subtitle_overlay_events:
                fallback_height = _face_aware_caption_block_height(subtitle_font_size)
                fallback_top = float(target_height) - float(_face_aware_fill_subtitle_margin(subtitle_margin, subtitle_margin_custom=subtitle_margin_custom)) - fallback_height
                caption_y_intervals: list[tuple[float, float, float]] = []
                for event in effective_subtitle_overlay_events:
                    event_start = float(event["start"])
                    event_end = min(float(duration), float(event["end"]))
                    if event_end <= event_start:
                        continue
                    event_segment = _face_aware_segment_for_time(layout_segments, event_start)
                    block = _face_aware_event_caption_block(
                        event.get("metrics") or {},
                        fallback_top=fallback_top,
                        fallback_height=fallback_height,
                    )
                    desired_top = _face_aware_caption_top_for_block(
                        event_segment,
                        block_height=block["height"],
                        target_width=target_width,
                        target_height=target_height,
                        subtitle_margin=subtitle_margin,
                        subtitle_margin_custom=subtitle_margin_custom,
                    )
                    segment_caption_y = desired_top - block["top"]
                    caption_y_intervals.append((event_start, event_end, segment_caption_y))
                caption_overlay_y_expr = _piecewise_constant_overlay_y_expr(caption_y_intervals, duration=duration)
            else:
                try:
                    fill_margin = float(_face_aware_fill_subtitle_margin(subtitle_margin, subtitle_margin_custom=subtitle_margin_custom))
                    fit_margin = float(face_aware_fit_metrics["caption_margin_v"])
                    caption_block_height = _face_aware_caption_block_height(subtitle_font_size)
                    split_caption_top = float(face_aware_split_metrics["caption_top"])
                    split_base_top = (
                        float(target_height)
                        - fill_margin
                        - caption_block_height
                    )
                    fit_caption_y = fill_margin - fit_margin
                    split_caption_y = split_caption_top - split_base_top
                except Exception:
                    fill_margin = float(_face_aware_fill_subtitle_margin(subtitle_margin, subtitle_margin_custom=subtitle_margin_custom))
                    fit_caption_y = 0.0
                    split_caption_y = 0.0
                caption_y_intervals: list[tuple[float, float, float]] = []
                for segment in layout_segments:
                    seg_start = float(segment.get("start") or 0.0)
                    seg_end = float(segment.get("end") or duration)
                    segment_mode = str(segment.get("mode") or "fill")
                    if segment_mode == "fit":
                        segment_caption_y = fit_caption_y
                    elif segment_mode == "split":
                        segment_caption_y = split_caption_y
                    else:
                        try:
                            segment_margin = float(segment.get("caption_margin_v") or FACE_AWARE_FILL_FALLBACK_SUBTITLE_MARGIN)
                            segment_caption_y = fill_margin - segment_margin
                        except Exception:
                            segment_caption_y = 0.0
                    caption_y_intervals.append((seg_start, seg_end, segment_caption_y))
                caption_overlay_y_expr = _piecewise_constant_overlay_y_expr(caption_y_intervals, duration=duration)
            caption_overlay_y_expr = caption_overlay_y_expr.replace(",", r"\,")
        filter_parts.append(
            f"{final_label}[caption_src]overlay=0:{caption_overlay_y_expr}:shortest=1[caption_out]"
        )
        final_label = "[caption_out]"
        next_video_input_index += 1
    current_app.logger.info("video_date_text=%r", video_date_text)
    if video_date_text:
        date_txt = _sanitize_text_for_overlay(video_date_text, 160)
        if date_txt:
            date_font_file = font_path or "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
            date_font_size = 24
            try:
                date_top = int(video_date_top if video_date_top is not None else DEFAULT_VIDEO_DATE_TOP)
            except (TypeError, ValueError):
                date_top = DEFAULT_VIDEO_DATE_TOP
            date_top = clamp_design_position(
                "video_date_top",
                date_top,
                target_height=target_height,
                element_height=DESIGN_POSITION_LIMITS["video_date_top"]["fallback_element_height"],
            )
            date_y_expr = str(date_top)
            date_box_color = "black@0.6"
            date_textfile = _write_debug_textfile(date_txt)
            date_drawtext = (
                f"{final_label}drawtext="
                f"fontfile='{_escape_ass_path(Path(date_font_file))}':"
                f"textfile='{_escape_ass_path(date_textfile)}':"
                "x=(w-text_w)/2:"
                f"y={date_y_expr}:"
                f"fontsize={date_font_size}:"
                "fontcolor=white:"
                "box=1:"
                f"boxcolor={date_box_color}:"
                "boxborderw=18:"
                "[ov_date]"
            )
            filter_parts.append(date_drawtext)
            final_label = "[ov_date]"
    if overlay_enabled:
        subscribe_input_index = next_video_input_index
        overlay_src_label = "[ov_sub_src]"
        overlay_out_label = "[ov_sub]"
        subscribe_scale_filter = f"scale=iw*{SUBSCRIBE_OVERLAY_SCALE}:ih*{SUBSCRIBE_OVERLAY_SCALE}"
        top_caption_subscribe_metrics = None
        if face_aware_layout_active and not face_aware_auto_caption_placement and normalize_caption_anchor(caption_anchor) == "top":
            top_caption_subscribe_metrics = _face_aware_top_caption_subscribe_metrics(
                target_height=target_height,
                subtitle_font_size=subtitle_font_size,
                subtitle_top=subtitle_margin,
            )
            visible_geometry = _overlay_visible_alpha_geometry(Path(overlay_asset_path))
            if visible_geometry:
                target_visible_width = float(target_width) * FACE_AWARE_SUBSCRIBE_TARGET_WIDTH_RATIO
                primary_width = float(visible_geometry["primary_width"])
                primary_height = float(visible_geometry["primary_height"])
                effective_scale = min(
                    float(SUBSCRIBE_OVERLAY_SCALE),
                    target_visible_width / primary_width,
                )
                max_visible_height = float(top_caption_subscribe_metrics["subscribe_max_height"])
                effective_scale = min(effective_scale, max_visible_height / primary_height)
                canvas_height = int(visible_geometry["canvas_height"])
                scaled_canvas_height = max(2, int(math.floor(canvas_height * effective_scale)))
                scaled_canvas_height -= scaled_canvas_height % 2
                effective_scale = float(scaled_canvas_height) / float(canvas_height)
                if outreach_layout:
                    min_subscribe_top = (
                        float(top_caption_subscribe_metrics["caption_top"])
                        + float(top_caption_subscribe_metrics["max_caption_height"])
                        + float(top_caption_subscribe_metrics["subscribe_gap"])
                    )
                    primary_scaled_height = primary_height * effective_scale
                    target_subscribe_top = (
                        float(target_height) * OUTREACH_SUBSCRIBE_BOTTOM_RATIO
                        - primary_scaled_height
                    )
                    max_subscribe_top = (
                        float(top_caption_subscribe_metrics["ui_top"])
                        - primary_scaled_height
                    )
                    top_caption_subscribe_metrics["subscribe_top"] = min(
                        max_subscribe_top,
                        max(min_subscribe_top, target_subscribe_top),
                    )
                visible_canvas_height = max(
                    2,
                    int(math.floor(top_caption_subscribe_metrics["ui_top"] - top_caption_subscribe_metrics["subscribe_top"])),
                )
                visible_canvas_height -= visible_canvas_height % 2
                subscribe_scale_filter = (
                    f"scale=-2:{scaled_canvas_height},"
                    f"crop=iw:min(ih\\,{visible_canvas_height}):0:0"
                )
                top_caption_subscribe_metrics["subscribe_top"] -= (
                    float(visible_geometry["primary_top"]) * effective_scale
                )
            else:
                subscribe_scale_filter = (
                    "scale=-2:"
                    f"min(ih*{SUBSCRIBE_OVERLAY_SCALE}\\,{int(top_caption_subscribe_metrics['subscribe_max_height'])})"
                )
        filter_parts.append(
            f"[{subscribe_input_index}:v]{subscribe_scale_filter},format=rgba{overlay_src_label}"
        )
        subscribe_y_expr = f"H-h-{SUBSCRIBE_OVERLAY_BOTTOM_OFFSET}"
        if top_caption_subscribe_metrics:
            subscribe_y_expr = f"{top_caption_subscribe_metrics['subscribe_top']:.2f}"
        elif face_aware_auto_caption_placement:
            default_subscribe_top = float(target_height) - float(SUBSCRIBE_OVERLAY_BOTTOM_OFFSET)
            try:
                overlay_dimensions = _overlay_canvas_dimensions(Path(overlay_asset_path))
                if not overlay_dimensions:
                    raise RuntimeError("overlay dimensions unavailable")
                _, overlay_source_height = overlay_dimensions
                default_subscribe_top -= float(overlay_source_height) * float(SUBSCRIBE_OVERLAY_SCALE)
            except Exception as exc:
                current_app.logger.warning(
                    "Face-aware subscribe placement probe failed path=%s: %s",
                    overlay_asset_path,
                    exc,
                )
            subscribe_y_intervals: list[tuple[float, float, str]] = []
            for segment in layout_segments:
                seg_start = float(segment.get("start") or 0.0)
                seg_end = float(segment.get("end") or duration)
                if str(segment.get("mode") or "fill") == "fit":
                    segment_subscribe_top = min(
                        default_subscribe_top,
                        float(face_aware_fit_metrics["subscribe_y"]),
                    )
                else:
                    segment_subscribe_top = max(
                        default_subscribe_top,
                        float(segment.get("subscribe_y") or 0.0),
                    )
                segment["subscribe_top"] = float(segment_subscribe_top)
                subscribe_y_intervals.append((seg_start, seg_end, f"{segment_subscribe_top:.2f}"))
            subscribe_y_expr = _piecewise_constant_expr(
                subscribe_y_intervals,
                duration=duration,
                default_expr=subscribe_y_expr,
            )
            subscribe_y_expr = subscribe_y_expr.replace(",", r"\,")
        filter_parts.append(
            f"{final_label}{overlay_src_label}overlay=(W-w)/2:{subscribe_y_expr}:shortest=1{overlay_out_label}"
        )
        final_label = overlay_out_label
    if face_aware_layout_active:
        resolved_layout_segments = [
            {
                "start": round(float(segment.get("start") or 0.0), 6),
                "end": round(float(segment.get("end") or duration), 6),
                "mode": str(segment.get("mode") or "fill"),
                "caption_top": round(float(segment.get("caption_top") or 0.0), 3),
                "caption_block_height": round(float(segment.get("caption_block_height") or 0.0), 3),
                "subscribe_top": (
                    round(float(segment["subscribe_top"]), 3)
                    if segment.get("subscribe_top") is not None
                    else None
                ),
            }
            for segment in layout_segments
        ]
        crop_settings["resolved_layout_segments"] = resolved_layout_segments
        current_app.logger.info(
            "face_aware_caption_placement auto=%s subtitle_margin=%s subtitle_margin_custom=%s segments=%s",
            face_aware_auto_caption_placement,
            subtitle_margin,
            subtitle_margin_custom,
            resolved_layout_segments,
        )
    music_enabled = bool(
        music_path
        and Path(music_path).exists()
        and normalize_music_level(music_level) > 0
        and not music_only
        and not podcast_mode
    )
    music_input_index = next_video_input_index + (1 if overlay_enabled else 0)
    music_audio_map = None
    if music_enabled:
        volume_expr = music_volume_expression(
            music_level,
            music_speech_ranges or [],
            duck=bool(music_duck),
        ).replace(",", r"\,")
        music_filters = (
            f"[{music_input_index}:a]atrim=start={max(0.0, float(music_start_seconds or 0.0)):.3f}:"
            f"duration={duration:.3f},asetpts=PTS-STARTPTS,aresample=async=1:first_pts=0,"
            f"volume='{volume_expr}':eval=frame"
        )
        music_filters += "[music_bg]"
        filter_parts.append(music_filters)
        voice_input_index = 2 if direct_audio_source is not None else 1
        voice_source = direct_audio_source is not None or _has_audio_stream(clip_video_source)
        if voice_source:
            if direct_timeline_audio_label:
                filter_parts.append(
                    f"{direct_timeline_audio_label}aresample=async=1:first_pts=0[voice_main]"
                )
            else:
                filter_parts.append(
                    f"[{voice_input_index}:a]aresample=async=1:first_pts=0[voice_main]"
                )
            filter_parts.append(
                "[voice_main][music_bg]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a_music_mix]"
            )
            music_audio_map = "[a_music_mix]"
        else:
            music_audio_map = "[music_bg]"
    edge_audio_map: Optional[str] = None
    if not podcast_mode:
        edge_audio_source = music_audio_map or direct_timeline_audio_label
        if not edge_audio_source and direct_audio_source is not None and _has_audio_stream(direct_audio_source):
            edge_audio_source = "[2:a]"
        if not edge_audio_source and clip_video_source and _has_audio_stream(clip_video_source):
            edge_audio_source = "[1:a]"
        if edge_audio_source:
            filter_parts.append(
                f"{edge_audio_source}{_audio_edge_fade_filter(duration)}[clip_edge_faded_a]"
            )
            edge_audio_map = "[clip_edge_faded_a]"
    filter_complex = ";".join(filter_parts)

    if low_memory_demo:
        # Caption/face-layout preparation creates many large temporary images.
        # Release that transient heap before ffmpeg joins the process tree.
        gc.collect()
        try:
            ctypes.CDLL(None).malloc_trim(0)
        except (AttributeError, OSError):
            pass

    out_path.parent.mkdir(parents=True, exist_ok=True)
    temp_out_path = out_path.with_name(f"{out_path.stem}.compose-{secrets.token_hex(4)}{out_path.suffix}")
    cmd = [
        resolved_ffmpeg,
        "-y",
        "-stats_period",
        "1",
        "-loop",
        "1",
        "-i",
        str(bg_path),
    ]
    if direct_audio_source is not None:
        # In audio-only fast path, selected visual can be shorter than target clip.
        # Loop it so output duration is driven by requested audio segment.
        cmd.extend(["-stream_loop", "-1", "-i", str(clip_video_source)])
    elif direct_source_timeline:
        cmd.extend(
            [
                "-ss",
                f"{direct_source_seek:.6f}",
                "-t",
                f"{direct_source_input_duration + 0.250:.6f}",
                "-i",
                str(clip_video_source),
            ]
        )
        if direct_visual_override:
            cmd.extend(["-stream_loop", "-1", "-i", str(direct_visual_override)])
    else:
        cmd.extend(["-i", str(clip_video_source)])
    audio_map = edge_audio_map or direct_timeline_audio_label or "1:a?"
    if direct_audio_source is not None:
        cmd.extend(
            [
                "-ss",
                f"{direct_audio_ss:.3f}",
                "-t",
                f"{direct_audio_duration:.3f}",
                "-i",
                str(direct_audio_source),
            ]
        )
        audio_map = edge_audio_map or "2:a"
    if shape_overlay_path:
        cmd.extend(["-loop", "1", "-i", str(shape_overlay_path)])
    for overlay_path in title_overlay_paths:
        cmd.extend(["-loop", "1", "-i", str(overlay_path)])
    if effective_subtitle_overlay_specs:
        for spec in effective_subtitle_overlay_specs:
            cmd.extend(["-loop", "1", "-i", str(spec["path"])])
    elif effective_subtitle_overlay_path:
        cmd.extend(["-i", str(effective_subtitle_overlay_path)])
    if overlay_enabled:
        cmd.extend(["-stream_loop", "-1", "-i", str(overlay_asset_path)])
    if music_enabled:
        cmd.extend(["-stream_loop", "-1", "-i", str(music_path)])
        audio_map = edge_audio_map or music_audio_map or audio_map
    cmd.extend(
        [
            "-filter_complex",
            filter_complex,
            "-map",
            final_label,
            "-map",
            audio_map,
            "-c:a",
            "aac",
            "-shortest",
            "-preset",
            "ultrafast",
            "-crf",
            "23",
            "-c:v",
            "libx264",
            "-movflags",
            "+faststart",
            str(temp_out_path),
        ]
    )
    if music_only:
        cmd = [
            resolved_ffmpeg,
            "-y",
            "-stats_period",
            "1",
            "-loop",
            "1",
            "-i",
            str(bg_path),
        ]
        if direct_audio_source is not None:
            cmd.extend(["-stream_loop", "-1", "-i", str(clip_video_source)])
        elif direct_source_timeline:
            cmd.extend(
                [
                    "-ss",
                    f"{direct_source_seek:.6f}",
                    "-t",
                    f"{direct_source_input_duration + 0.250:.6f}",
                    "-i",
                    str(clip_video_source),
                ]
            )
            if direct_visual_override:
                cmd.extend(["-stream_loop", "-1", "-i", str(direct_visual_override)])
        else:
            cmd.extend(["-i", str(clip_video_source)])
        audio_map_music_only = edge_audio_map or direct_timeline_audio_label or "1:a?"
        if direct_audio_source is not None:
            cmd.extend(
                [
                    "-ss",
                    f"{direct_audio_ss:.3f}",
                    "-t",
                    f"{direct_audio_duration:.3f}",
                    "-i",
                    str(direct_audio_source),
                ]
            )
            audio_map_music_only = edge_audio_map or "2:a"
        for overlay_path in title_overlay_paths:
            cmd.extend(["-loop", "1", "-i", str(overlay_path)])
        if effective_subtitle_overlay_specs:
            for spec in effective_subtitle_overlay_specs:
                cmd.extend(["-loop", "1", "-i", str(spec["path"])])
        elif effective_subtitle_overlay_path:
            cmd.extend(["-i", str(effective_subtitle_overlay_path)])
        if overlay_enabled:
            cmd.extend(["-stream_loop", "-1", "-i", str(overlay_asset_path)])
        cmd.extend(
            [
                "-filter_complex",
                filter_complex,
                "-map",
                final_label,
                "-map",
                audio_map_music_only,
                "-c:a",
                "aac",
                "-shortest",
                "-preset",
                "ultrafast",
                "-crf",
                "23",
                "-c:v",
                "libx264",
                "-movflags",
                "+faststart",
                str(temp_out_path),
            ]
        )
    if low_memory_demo:
        cmd[1:1] = ["-filter_threads", "1", "-filter_complex_threads", "1"]
        output_index = len(cmd) - 1
        cmd[output_index:output_index] = ["-threads", "1"]
        current_app.logger.info(
            "Discovery demo low-memory ffmpeg settings enabled threads=1 filter_threads=1"
        )
    current_app.logger.info("Compose ffmpeg command: %s", " ".join(cmd))
    current_app.logger.debug("Compose ffmpeg command (debug): %s", " ".join(cmd))
    try:
        compose_load_start = os.getloadavg() if hasattr(os, "getloadavg") else None
        compose_started = time.monotonic()
        result = run_media_subprocess(
            cmd,
            operation="compose_trimmed_with_background",
            context=f"output={out_path.name} temp={temp_out_path.name}",
            output_paths=[temp_out_path],
            check=True,
            timeout=scale_media_timeout(
                FFMPEG_RENDER_TIMEOUT,
                duration_seconds=duration,
                multiplier=2.0,
                extra_seconds=120,
            ),
            capture_output=True,
            text=True,
        )
        compose_elapsed_ms = int((time.monotonic() - compose_started) * 1000)
        compose_load_end = os.getloadavg() if hasattr(os, "getloadavg") else None
        current_app.logger.info(
            "Compose pass completed output=%s duration=%.3f elapsed_ms=%s ffmpeg_speed=%s load_start=%s load_end=%s",
            temp_out_path.name,
            duration,
            compose_elapsed_ms,
            _ffmpeg_final_speed(result.stderr),
            _format_loadavg(compose_load_start),
            _format_loadavg(compose_load_end),
        )
        current_app.logger.debug("Compose stdout: %s", result.stdout)
        current_app.logger.debug("Compose stderr: %s", result.stderr)
        if not temp_out_path.exists() or temp_out_path.stat().st_size <= 0:
            raise RuntimeError(f"FFmpeg compose output missing: {temp_out_path}")
        shutil.move(str(temp_out_path), str(out_path))
    except subprocess.CalledProcessError as err:
        current_app.logger.error("Compose failed stdout=%s stderr=%s", err.stdout, err.stderr)
        raise RuntimeError(
            f"FFmpeg compose failed (key={bg_path.name}): {err.stderr.strip() or err.stdout.strip()}"
        ) from err
    finally:
        for temp_path in (trimmed, merged_override, merged_audio_override, temp_out_path, face_aware_subtitle_path, anchored_subtitle_path, shape_overlay_path, *title_overlay_paths):
            if temp_path and temp_path.exists():
                try:
                    temp_path.unlink()
                except Exception:
                    pass


def _cut_clip(
    src: Path,
    start: float,
    end: float,
    out_path: Path,
    subtitle_path: Path = None,
    subtitle_font: str = "DejaVu Sans",
    subtitle_font_size: int = DEFAULT_SUB_FONT_SIZE,
    subtitle_margin: int = SUB_MARGIN_DEFAULT,
    subtitle_text_color: Optional[str] = None,
    subtitle_bg_color: Optional[str] = None,
    subtitle_bg_alpha: Optional[int] = DEFAULT_SUBTITLE_BG_ALPHA,
    subtitle_text_alpha: Optional[int] = DEFAULT_SUBTITLE_TEXT_ALPHA,
    subtitle_style: Optional[str] = "plain",
    source_keep_ranges: Optional[list[Dict[str, Any]]] = None,
):
    keep_ranges = _normalize_keep_ranges_for_trim(source_keep_ranges)
    if len(keep_ranges) > 1:
        duration = max(
            sum(max(0.0, item["end"] - item["start"]) for item in keep_ranges),
            1.0,
        )
    else:
        keep_ranges = []
        duration = max(end - start, 1.0)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_ffmpeg = _resolve_ffmpeg()
    has_audio = _has_audio_stream(src)
    seek_start = float(keep_ranges[0]["start"]) if keep_ranges else max(0.0, float(start))
    input_duration = (
        max(0.001, float(keep_ranges[-1]["end"]) - seek_start)
        if keep_ranges
        else duration
    )
    cmd = [
        resolved_ffmpeg,
        "-y",
        "-ss",
        f"{seek_start:.6f}",
        "-t",
        f"{input_duration + 0.250:.6f}",
        "-i",
        str(src),
    ]
    video_map = "0:v"
    audio_map = "0:a?"
    filter_parts: list[str] = []
    if keep_ranges:
        relative_ranges = [
            {
                "start": max(0.0, item["start"] - seek_start),
                "end": max(0.0, item["end"] - seek_start),
            }
            for item in keep_ranges
        ]
        video_split_labels = "".join(
            f"[cut_range_v_src_{index}]" for index in range(len(relative_ranges))
        )
        filter_parts.append(f"[0:v]split={len(relative_ranges)}{video_split_labels}")
        if has_audio:
            audio_split_labels = "".join(
                f"[cut_range_a_src_{index}]" for index in range(len(relative_ranges))
            )
            filter_parts.append(f"[0:a]asplit={len(relative_ranges)}{audio_split_labels}")
        concat_inputs: list[str] = []
        for index, item in enumerate(relative_ranges):
            filter_parts.append(
                f"[cut_range_v_src_{index}]trim=start={item['start']:.6f}:end={item['end']:.6f},"
                f"setpts=PTS-STARTPTS,settb=AVTB[cut_range_v_{index}]"
            )
            concat_inputs.append(f"[cut_range_v_{index}]")
            if has_audio:
                filter_parts.append(
                    f"[cut_range_a_src_{index}]atrim=start={item['start']:.6f}:end={item['end']:.6f},"
                    f"asetpts=PTS-STARTPTS,aresample=async=1:first_pts=0[cut_range_a_{index}]"
                )
                concat_inputs.append(f"[cut_range_a_{index}]")
        if has_audio:
            filter_parts.append(
                "".join(concat_inputs)
                + f"concat=n={len(relative_ranges)}:v=1:a=1[cut_v][cut_a_raw]"
            )
            filter_parts.append(
                f"[cut_a_raw]{_audio_edge_fade_filter(duration)}[cut_a]"
            )
            audio_map = "[cut_a]"
        else:
            filter_parts.append(
                "".join(concat_inputs)
                + f"concat=n={len(relative_ranges)}:v=1:a=0[cut_v]"
            )
        video_map = "[cut_v]"
    elif has_audio:
        filter_parts.append(f"[0:a]{_audio_edge_fade_filter(duration)}[cut_a]")
        audio_map = "[cut_a]"
    if subtitle_path:
        try:
            source_width, source_height = _probe_video_dimensions(src)
        except Exception:
            source_width, source_height = VIDEO_TARGET_WIDTH, VIDEO_TARGET_HEIGHT
        style = _subtitle_force_style(
            target_width=source_width,
            target_height=source_height,
            subtitle_font_size=subtitle_font_size,
            subtitle_margin=subtitle_margin,
            subtitle_font=subtitle_font,
            subtitle_text_color=subtitle_text_color,
            subtitle_text_alpha=subtitle_text_alpha,
            subtitle_bg_color=subtitle_bg_color,
            subtitle_bg_alpha=subtitle_bg_alpha,
            subtitle_style=subtitle_style,
        )
        subtitle_filter = f"subtitles='{_escape_ass_path(subtitle_path)}':fontsdir='{_escape_ass_path(SUBTITLE_FONTS_DIR)}':force_style='{style}'"
        if filter_parts:
            filter_parts.append(f"{video_map}{subtitle_filter}[cut_sub]")
            video_map = "[cut_sub]"
        else:
            cmd.extend(["-vf", subtitle_filter])
    if filter_parts:
        cmd.extend(["-filter_complex", ";".join(filter_parts), "-map", video_map, "-map", audio_map])
    cmd.extend(
        [
            "-t",
            f"{duration:.6f}",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(out_path),
        ]
    )
    run_media_subprocess(
        cmd,
        operation="cut_clip",
        context=f"src={src.name} output={out_path.name}",
        output_paths=[out_path],
        check=True,
        timeout=scale_media_timeout(
            FFMPEG_RENDER_TIMEOUT,
            duration_seconds=duration,
            multiplier=2.0,
            extra_seconds=120,
        ),
    )
