import secrets
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from PIL import ImageFont
from flask import current_app

from app.video_shorts.config import (
    DEFAULT_SUB_FONT_SIZE,
    DEFAULT_SUBTITLE_BG_ALPHA,
    DEFAULT_SUBTITLE_BG_COLOR,
    DEFAULT_SUBTITLE_TEXT_ALPHA,
    DEFAULT_TITLE_BG_COLOR,
    DEFAULT_TITLE_BG_ALPHA,
    DEFAULT_TITLE_MARGIN,
    DEFAULT_VIDEO_OVERLAY_OFFSET,
    FFMPEG_RENDER_TIMEOUT,
    FFMPEG_SHORT_TIMEOUT,
    FFPROBE_TIMEOUT,
    STATIC_VISUAL_PRESETS,
    SUB_MARGIN_DEFAULT,
)

from app.video_shorts.services.media_utils import (
    _resolve_ffmpeg,
    run_media_subprocess,
    scale_media_timeout,
)

SUBSCRIBE_OVERLAY_WIDTH = 260
SUBSCRIBE_OVERLAY_PADDING = 40
SUBSCRIBE_OVERLAY_BOTTOM_OFFSET = 30
SUBSCRIBE_OVERLAY_PATH = Path(__file__).resolve().parents[1] / "static" / "subscribe.gif"
SUBTITLE_FONTS_DIR = Path(__file__).resolve().parents[1] / "static" / "fonts"

CROP_TARGET_SIZE = 1080  # reference crop resolution before resizing for display
VIDEO_TARGET_WIDTH = 720
_target_height = int(round(VIDEO_TARGET_WIDTH * 16 / 9))
VIDEO_TARGET_HEIGHT = _target_height + (_target_height % 2)
VIDEO_OVERLAY_WIDTH = 900
VIDEO_OVERLAY_TOP_OFFSET = DEFAULT_VIDEO_OVERLAY_OFFSET
PODCAST_LANDSCAPE_WIDTH = 1280
PODCAST_LANDSCAPE_HEIGHT = 720
VIDEO_DATE_BOTTOM_MARGIN = 250
DEFAULT_VIDEO_DATE_TOP = VIDEO_TARGET_HEIGHT - VIDEO_DATE_BOTTOM_MARGIN - 24
TITLE_WRAP_LENGTH = 35
TITLE_WRAP_SIDE_MARGIN = 48
TITLE_WRAP_MAX_LINES = 3
TITLE_WRAP_MIN_FONT_SIZE = 30
TITLE_BOX_BORDER_WIDTH = 22
STATIC_VISUAL_MAP = {entry["key"]: entry for entry in STATIC_VISUAL_PRESETS}


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
        return ImageFont.truetype(str(candidate), int(font_size))
    except Exception:
        current_app.logger.warning("Could not load title font for wrap measurement: %s", candidate)
        return None


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
    adjusted_y = visual_y - max(0, block_height - safe_line_height)
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
        if layout["draw_y"] < TITLE_BOX_BORDER_WIDTH:
            continue
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

    return {
        "text": "\n".join(chosen_lines),
        "font_size": chosen_font_size,
        "draw_y": chosen_layout["draw_y"],
        "line_count": len(chosen_lines),
        "side_margin": side_margin,
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


def _title_drawtext_style(
    *,
    subtitle_preset: Optional[str],
    title_bg_color: Optional[str],
    title_bg_alpha: Optional[int],
) -> str:
    box_color = f"{_hex_to_drawtext_color(title_bg_color)}@{_normalize_alpha_percent(title_bg_alpha, DEFAULT_TITLE_BG_ALPHA) / 100:.2f}"
    parts = [
        "box=1",
        f"boxcolor={box_color}",
        f"boxborderw={TITLE_BOX_BORDER_WIDTH}",
    ]
    if str(subtitle_preset or "").strip() == "green_pop":
        parts.extend([
            "shadowx=0",
            "shadowy=8",
            "shadowcolor=black@0.55",
        ])
    return ":".join(parts)


def _title_visual_y(base_y: int, font_size: int) -> int:
    # FFmpeg drawtext places glyphs slightly low inside boxed titles.
    # Nudge upward a bit so the text feels centered within the background pill.
    offset = max(3, int(round((font_size or 0) * 0.12)))
    return max(0, int(base_y) - offset)


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
    subtitle_font: str,
    subtitle_text_color: Optional[str],
    subtitle_text_alpha: Optional[int],
    subtitle_bg_color: Optional[str],
    subtitle_bg_alpha: Optional[int],
    subtitle_style: Optional[str] = None,
) -> str:
    clean_font = (subtitle_font or "DejaVu Sans").replace("'", "")
    normalized_subtitle_style = str(subtitle_style or "plain").strip().lower()
    base = (
        f"PlayResX={int(target_width)},"
        f"PlayResY={int(target_height)},"
        f"Fontsize={subtitle_font_size},"
    )
    if normalized_subtitle_style == "karaoke":
        return (
            base
            + f"MarginV={subtitle_margin},"
            + "Alignment=2,"
        )
    return (
        base
        + f"PrimaryColour={_hex_to_ass_color_with_alpha(subtitle_text_color, subtitle_text_alpha, '#FFFFFF', DEFAULT_SUBTITLE_TEXT_ALPHA)},"
        + f"BackColour={_hex_to_ass_color_with_alpha(subtitle_bg_color, subtitle_bg_alpha, DEFAULT_SUBTITLE_BG_COLOR, DEFAULT_SUBTITLE_BG_ALPHA)},"
        + "BorderStyle=4,"
        + "Outline=1,"
        + "Shadow=0,"
        + f"MarginV={subtitle_margin},"
        + "Alignment=2,"
        + f"FontName={clean_font}"
    )


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
        return int(width), int(height)
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
    title_bg_color: Optional[str] = None,
    title_bg_alpha: Optional[int] = DEFAULT_TITLE_BG_ALPHA,
    title_text_color: Optional[str] = None,
    subtitle_font_size: int = DEFAULT_SUB_FONT_SIZE,
    subtitle_margin: int = SUB_MARGIN_DEFAULT,
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
    title_txt = _sanitize_text_for_overlay(title or "", 140)
    title_layout = _fit_title_text(
        title_txt,
        font_path=font_path or "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        font_size=title_font_size,
        target_width=VIDEO_TARGET_WIDTH,
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

    if title_txt:
        # Font dosyası
        test_font_file = font_path or "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

        # Çok satırlı metni textfile ile kullan
        debug_textfile = _write_debug_textfile(title_txt.replace("\n", "\n"))

        # UI'dan gelen title_margin'i mantıklı aralığa sıkıştır
        # Çok yukarı veya çok aşağı kaçmasın
        title_style = _title_drawtext_style(
            subtitle_preset=subtitle_preset,
            title_bg_color=title_bg_color,
            title_bg_alpha=title_bg_alpha,
        )

        debug_drawtext = (
            f"{final_label}drawtext="
            f"fontfile='{test_font_file}':"
            f"textfile='{_escape_ass_path(debug_textfile)}':"
            "x=(w-text_w)/2:"
            f"y={title_layout['draw_y']}:"
            f"fontsize={title_layout['font_size']}:"
            f"fontcolor={_hex_to_drawtext_color(title_text_color, '#000000')}:"
            "line_spacing=5:"
            f"{title_style}:"
            "[ov_title_debug]"
        )
        filter_parts.append(debug_drawtext)
        final_label = "[ov_title_debug]"

    if subtitle_path:
        style = _subtitle_force_style(
            target_width=VIDEO_TARGET_WIDTH,
            target_height=VIDEO_TARGET_HEIGHT,
            subtitle_font_size=subtitle_font_size,
            subtitle_margin=subtitle_margin,
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
    ]
    try:
        run_media_subprocess(
            cmd,
            operation="compose_with_background",
            context=f"output={out_path.name}",
            output_paths=[out_path],
            check=True,
            timeout=FFMPEG_RENDER_TIMEOUT,
        )
    except subprocess.CalledProcessError:
        if title_txt:
            # Drawtext patlarsa başlıksız fallback
            fallback_filter = ";".join(filter_parts[:2])
            cmd[9] = fallback_filter        # filter_complex arg
            cmd[11] = "[ov]"                # map video label
            current_app.logger.info(
                "Drawtext failed; retrying without title overlay for %s",
                out_path.name,
            )
            run_media_subprocess(
                cmd,
                operation="compose_with_background_fallback",
                context=f"output={out_path.name}",
                output_paths=[out_path],
                check=True,
                timeout=FFMPEG_RENDER_TIMEOUT,
            )




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
    subtitle_font: str = "DejaVu Sans",
    title_font_size: int = 30,
    title_margin: int = DEFAULT_TITLE_MARGIN,
    title_line_spacing: int = -4,
    title_bg_color: Optional[str] = None,
    title_bg_alpha: Optional[int] = DEFAULT_TITLE_BG_ALPHA,
    title_text_color: Optional[str] = None,
    subtitle_font_size: int = DEFAULT_SUB_FONT_SIZE,
    subtitle_margin: int = SUB_MARGIN_DEFAULT,
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
    show_subtitle: bool = True,
    crop_settings: Optional[Dict[str, float]] = None,
    video_override_source: Optional[Path] = None,
    audio_override_source: Optional[Path] = None,
    podcast_background_image_source: Optional[Path] = None,
    podcast_overlay_video_sources: Optional[list[Path]] = None,
    video_overlay_offset: Optional[int] = None,
    crop_aspect: Optional[str] = None,
    music_only: bool = False,
):
    """Single-pass trim + compose to reduce processing time."""
    if not bg_path.exists():
        raise FileNotFoundError(f"Background image not found: {bg_path}")
    if not src_path.exists():
        raise FileNotFoundError(f"Source video not found: {src_path}")
    resolved_ffmpeg = _resolve_ffmpeg()
    podcast_mode = bool(audio_override_source and Path(audio_override_source).exists())
    duration = max(end - start, 1.0)
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
        if overlay_aspect == "portrait":
            target_width = VIDEO_TARGET_WIDTH
            target_height = VIDEO_TARGET_HEIGHT
        else:
            target_width = PODCAST_LANDSCAPE_WIDTH
            target_height = PODCAST_LANDSCAPE_HEIGHT
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
        if show_title and title_txt:
            test_font_file = font_path or "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
            title_layout = _fit_title_text(
                title_txt,
                font_path=test_font_file,
                font_size=safe_title_font_size,
                target_width=target_width,
                base_y=safe_title_margin,
                line_spacing=safe_title_line_spacing,
            )
            debug_textfile = _write_debug_textfile(title_layout["text"].replace("\n", "\n"))
            title_style = _title_drawtext_style(
                subtitle_preset=subtitle_preset,
                title_bg_color=title_bg_color,
                title_bg_alpha=title_bg_alpha,
            )
            filter_parts.append(
                f"{final_label}drawtext="
                f"fontfile='{test_font_file}':"
                f"textfile='{_escape_ass_path(debug_textfile)}':"
                "x=(w-text_w)/2:"
                f"y={title_layout['draw_y']}:"
                f"fontsize={title_layout['font_size']}:"
                f"fontcolor={_hex_to_drawtext_color(title_text_color, '#000000')}:"
                f"line_spacing={safe_title_line_spacing}:"
                f"{title_style}:"
                "[ov_title]"
            )
            final_label = "[ov_title]"
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
        next_video_input_index = 2 + len(overlay_sources)
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
                date_top = max(0, min(target_height - 80, date_top))
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
            filter_parts.append(f"[{subscribe_input_index}:v]format=rgba[ov_sub_src]")
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
        return


    # Step 1: trim source with pts reset
    trimmed = out_path.parent / f"{out_path.stem}_trimmed.mp4"
    merged_override = None
    merged_audio_override = None
    direct_audio_source: Optional[Path] = None
    direct_audio_ss = 0.0
    direct_audio_duration = 0.0
    has_audio = _has_audio_stream(src_path)
    has_video = _has_video_stream(src_path)
    clip_video_source: Optional[Path] = None

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
            "-i",
            str(src_path),
            "-ss",
            str(start),
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
                        "[a_main][a_bg]amix=inputs=2:duration=first:dropout_transition=0[a_mix]",
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

    _log_video_dimensions("clip_scaled source", clip_video_source)
    overlay_aspect = (crop_aspect or "landscape").lower()
    # Shorts layout behavior:
    # - non-podcast: always render on 9:16 canvas and place overlay inside it.
    # - podcast: allow true horizontal canvas when landscape is selected.
    if podcast_mode and overlay_aspect != "portrait":
        target_width = PODCAST_LANDSCAPE_WIDTH
        target_height = PODCAST_LANDSCAPE_HEIGHT
    else:
        target_width = VIDEO_TARGET_WIDTH
        target_height = VIDEO_TARGET_HEIGHT
    overlay_width = min(VIDEO_OVERLAY_WIDTH, target_width)
    if overlay_aspect == "portrait":
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
    has_crop2 = all(
        crop_settings.get(key) is not None
        for key in ("crop2_x_ratio", "crop2_y_ratio", "crop2_w_ratio", "crop2_h_ratio")
    )

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
    )
    if is_default_crop:
        scale_stage = (
            f"scale={overlay_width}:{overlay_height}:force_original_aspect_ratio=increase,"
            f"crop={overlay_width}:{overlay_height},"
        )
    else:
        scale_stage = f"scale={overlay_width}:{overlay_height},"

    bg_filter = (
        f"[0:v]scale={target_width}:{target_height},setsar=1[bg]"
    )
    filter_parts = [bg_filter]
    if split_stack_enabled:
        crop2_x, crop2_y, crop2_w, crop2_h = _normalize_crop_box("crop2_")
        split_tile_width = target_width
        split_tile_height = max(2, int(target_height / 2))
        split_tile_height -= split_tile_height % 2
        split_seam_y = int(target_height / 2)
        split_divider_thickness = 6
        split_divider_outline_thickness = 1
        filter_parts.extend(
            [
                "[1:v]split=2[split_top_src][split_bottom_src]",
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
        crop_filter = (
            f"[1:v]crop=iw*{_fmt(crop_w)}:ih*{_fmt(crop_h)}:iw*{_fmt(crop_x)}:ih*{_fmt(crop_y)},"
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
    if show_title and title_txt:
        test_font_file = font_path or "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        title_layout = _fit_title_text(
            title_txt,
            font_path=test_font_file,
            font_size=title_font_size,
            target_width=target_width,
            base_y=title_margin,
            line_spacing=safe_title_line_spacing_main,
        )
        debug_textfile = _write_debug_textfile(title_layout["text"].replace("\n", "\n"))
        title_style = _title_drawtext_style(
            subtitle_preset=subtitle_preset,
            title_bg_color=title_bg_color,
            title_bg_alpha=title_bg_alpha,
        )

        debug_drawtext = (
            f"{final_label}drawtext="
            f"fontfile='{test_font_file}':"
            f"textfile='{_escape_ass_path(debug_textfile)}':"
            "x=(w-text_w)/2:"
            f"y={title_layout['draw_y']}:"
            f"fontsize={title_layout['font_size']}:"
            f"fontcolor={_hex_to_drawtext_color(title_text_color, '#000000')}:"
            f"line_spacing={safe_title_line_spacing_main}:"
            f"{title_style}:"
            "[ov_title_debug]"
        )
        filter_parts.append(debug_drawtext)
        final_label = "[ov_title_debug]"


    if effective_subtitle_path:
        style = _subtitle_force_style(
            target_width=target_width,
            target_height=target_height,
            subtitle_font_size=subtitle_font_size,
            subtitle_margin=subtitle_margin,
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
    next_video_input_index = 3 if direct_audio_source else 2
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
        filter_parts.append(
            f"{final_label}[caption_src]overlay=0:0:shortest=1[caption_out]"
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
            date_top = max(0, min(target_height - 80, date_top))
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
        filter_parts.append(
            f"[{subscribe_input_index}:v]format=rgba{overlay_src_label}"
        )
        filter_parts.append(
            f"{final_label}{overlay_src_label}overlay=(W-w)/2:H-h-{SUBSCRIBE_OVERLAY_BOTTOM_OFFSET}:shortest=1{overlay_out_label}"
        )
        final_label = overlay_out_label
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
    ]
    if direct_audio_source is not None:
        # In audio-only fast path, selected visual can be shorter than target clip.
        # Loop it so output duration is driven by requested audio segment.
        cmd.extend(["-stream_loop", "-1", "-i", str(clip_video_source)])
    else:
        cmd.extend(["-i", str(clip_video_source)])
    audio_map = "1:a?"
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
        audio_map = "2:a"
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
            "-loop",
            "1",
            "-i",
            str(bg_path),
        ]
        if direct_audio_source is not None:
            cmd.extend(["-stream_loop", "-1", "-i", str(clip_video_source)])
        else:
            cmd.extend(["-i", str(clip_video_source)])
        audio_map_music_only = "1:a?"
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
            audio_map_music_only = "2:a"
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
    current_app.logger.info("Compose ffmpeg command: %s", " ".join(cmd))
    current_app.logger.debug("Compose ffmpeg command (debug): %s", " ".join(cmd))
    try:
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
        for temp_path in (trimmed, merged_override, merged_audio_override, temp_out_path):
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
):
    duration = max(end - start, 1.0)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_ffmpeg = _resolve_ffmpeg()
    cmd = [
        resolved_ffmpeg,
        "-y",
        "-i",
        str(src),
        "-ss",
        str(start),
        "-t",
        str(duration),
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
        cmd.extend(["-vf", f"subtitles='{_escape_ass_path(subtitle_path)}':fontsdir='{_escape_ass_path(SUBTITLE_FONTS_DIR)}':force_style='{style}'"])
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
