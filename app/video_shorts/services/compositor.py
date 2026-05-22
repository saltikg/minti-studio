import secrets
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from flask import current_app

from app.video_shorts.config import (
    DEFAULT_TITLE_BG_COLOR,
    DEFAULT_TITLE_MARGIN,
    DEFAULT_VIDEO_OVERLAY_OFFSET,
    FFMPEG_TIMEOUT,
    STATIC_VISUAL_PRESETS,
    SUB_MARGIN_DEFAULT,
)

from app.video_shorts.services.media_utils import _resolve_ffmpeg

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
TITLE_WRAP_LENGTH = 35
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


def _wrap_text_for_title(text: str, max_line_len: int = 35) -> str:
    if not text:
        return ""
    words = text.split()
    lines = []
    current = []
    for w in words:
        if sum(len(x) for x in current) + len(current) + len(w) > max_line_len and current:
            lines.append(" ".join(current))
            current = [w]
        else:
            current.append(w)
    if current:
        lines.append(" ".join(current))
    return "\n".join(lines)


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
        safe_bottom = max(safe_bottom, margin + font_size * 5)
    expr = f"max(0,min({top_offset},H-h-{safe_bottom}))"
    return expr.replace(",", r"\,")


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
    subprocess.run(cmd, check=True, timeout=FFMPEG_TIMEOUT, capture_output=True, text=True)
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
        result = subprocess.run(
            ffprobe_cmd,
            check=False,
            capture_output=True,
            text=True,
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
        result = subprocess.run(
            ffprobe_cmd,
            check=False,
            capture_output=True,
            text=True,
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
    result = subprocess.run(
        cmd,
        check=True,
        capture_output=True,
        text=True,
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
        result = subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
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
    subtitle_font_size: int = 10,
    subtitle_margin: int = SUB_MARGIN_DEFAULT,
):
    if not bg_path.exists():
        raise FileNotFoundError(f"Background image not found: {bg_path}")
    resolved_ffmpeg = _resolve_ffmpeg()

    # Başlık metni, kısalt ve satırları biraz daha kısa tut
    title_txt = _sanitize_text_for_overlay(title or "", 140)
    title_txt = _wrap_text_for_title(title_txt, TITLE_WRAP_LENGTH)

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
        title_test_y = max(80, min(title_margin, 250))

        # Sarı arka planı hafif transparan yap
        box_color = f"{_hex_to_drawtext_color(title_bg_color)}@0.92"

        debug_drawtext = (
            f"{final_label}drawtext="
            f"fontfile='{test_font_file}':"
            f"textfile='{_escape_ass_path(debug_textfile)}':"
            "x=(w-text_w)/2:"            # yatayda ortalı
            f"y={title_test_y}:"         # dikey pozisyon
            f"fontsize={title_font_size}:"
            "fontcolor=black:"
            "line_spacing=5:"   # satırlar arası mesafeyi biraz kıs
            "box=1:"
            f"boxcolor={box_color}:"
            "boxborderw=22:"             # daha kalın padding
            "[ov_title_debug]"
        )
        filter_parts.append(debug_drawtext)
        final_label = "[ov_title_debug]"

    if subtitle_path:
        clean_font = (subtitle_font or "DejaVu Sans").replace("'", "")
        style = (
            f"Fontsize={subtitle_font_size},"
            "PrimaryColour=&H00FFFFFF,"
            "BackColour=&H00000000,"
            "BorderStyle=4,"
            "Outline=1,"
            "Shadow=0,"
            f"MarginV={subtitle_margin},"
            f"Alignment=2,"
            f"FontName={clean_font}"
        )
        filter_parts.append(
            f"{final_label}subtitles='{_escape_ass_path(subtitle_path)}':fontsdir='{_escape_ass_path(SUBTITLE_FONTS_DIR)}':force_style='{style}'[subout]"
        )
        final_label = "[subout]"

    filter_complex = ";".join(filter_parts)

    out_path.parent.mkdir(parents=True, exist_ok=True)
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
        subprocess.run(cmd, check=True, timeout=FFMPEG_TIMEOUT)
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
            subprocess.run(cmd, check=True, timeout=FFMPEG_TIMEOUT)




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
    subtitle_font: str = "DejaVu Sans",
    title_font_size: int = 30,
    title_margin: int = DEFAULT_TITLE_MARGIN,
    title_line_spacing: int = -4,
    title_bg_color: Optional[str] = None,
    subtitle_font_size: int = 10,
    subtitle_margin: int = SUB_MARGIN_DEFAULT,
    video_date_text: Optional[str] = None,
    subscribe_overlay_enabled: bool = False,
    subscribe_overlay_path: Optional[Path] = None,
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
            safe_subtitle_font_size = int(subtitle_font_size if subtitle_font_size is not None else 10)
        except (TypeError, ValueError):
            safe_subtitle_font_size = 10
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
        title_txt = _wrap_text_for_title(title_txt, TITLE_WRAP_LENGTH)
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
        if title_txt:
            test_font_file = font_path or "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
            debug_textfile = _write_debug_textfile(title_txt.replace("\n", "\n"))
            box_color = f"{_hex_to_drawtext_color(title_bg_color)}@1"
            filter_parts.append(
                f"{final_label}drawtext="
                f"fontfile='{test_font_file}':"
                f"textfile='{_escape_ass_path(debug_textfile)}':"
                "x=(w-text_w)/2:"
                f"y={safe_title_margin}:"
                f"fontsize={safe_title_font_size}:"
                "fontcolor=black:"
                "box=1:"
                f"line_spacing={safe_title_line_spacing}:"
                f"boxcolor={box_color}:"
                "boxborderw=22:"
                "[ov_title]"
            )
            final_label = "[ov_title]"
        if subtitle_path:
            clean_font = (subtitle_font or "DejaVu Sans").replace("'", "")
            style = (
                f"Fontsize={safe_subtitle_font_size},"
                "PrimaryColour=&H00FFFFFF,"
                "BackColour=&H00000000,"
                "BorderStyle=4,"
                "Outline=1,"
                "Shadow=0,"
                f"MarginV={safe_subtitle_margin},"
                "Alignment=2,"
                f"FontName={clean_font}"
            )
            filter_parts.append(
                f"{final_label}subtitles='{_escape_ass_path(subtitle_path)}':fontsdir='{_escape_ass_path(SUBTITLE_FONTS_DIR)}':force_style='{style}'[subout]"
            )
            final_label = "[subout]"
        if video_date_text:
            date_txt = _sanitize_text_for_overlay(video_date_text, 160)
            if date_txt:
                date_font_file = font_path or "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
                date_textfile = _write_debug_textfile(date_txt)
                filter_parts.append(
                    f"{final_label}drawtext="
                    f"fontfile='{_escape_ass_path(Path(date_font_file))}':"
                    f"textfile='{_escape_ass_path(date_textfile)}':"
                    "x=(w-text_w)/2:"
                    f"y=main_h-text_h-{VIDEO_DATE_BOTTOM_MARGIN}:"
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
            subscribe_input_index = 2 + len(overlay_sources)
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
        try:
            base_ffmpeg_timeout = int(FFMPEG_TIMEOUT) if FFMPEG_TIMEOUT is not None else 0
        except (TypeError, ValueError):
            base_ffmpeg_timeout = 0
        fast_path_timeout = max(base_ffmpeg_timeout, int(max(1.0, float(duration)) * 2.0) + 120)
        current_app.logger.info(
            "Podcast fast-path ffmpeg command (timeout=%ss): %s",
            fast_path_timeout,
            " ".join(cmd),
        )
        try:
            subprocess.run(
                cmd,
                check=True,
                timeout=fast_path_timeout,
                capture_output=True,
                text=True,
            )
        except subprocess.TimeoutExpired as err:
            current_app.logger.error(
                "Podcast fast-path timeout after %ss for %s",
                fast_path_timeout,
                out_path,
            )
            raise RuntimeError(
                f"FFmpeg podcast fast-path timeout after {fast_path_timeout}s"
            ) from err
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
            trim_result = subprocess.run(
                trim_cmd,
                check=True,
                timeout=FFMPEG_TIMEOUT,
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
            merge_result = subprocess.run(
                merge_cmd,
                check=True,
                timeout=FFMPEG_TIMEOUT,
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
                subprocess.run(
                    audio_merge_cmd,
                    check=True,
                    timeout=FFMPEG_TIMEOUT,
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
    title_txt = _wrap_text_for_title(title_txt, TITLE_WRAP_LENGTH)
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

    crop_x = _normalize(crop_settings.get("crop_x_ratio"), 0.0)
    crop_y = _normalize(crop_settings.get("crop_y_ratio"), 0.0)
    crop_w = max(0.01, min(1.0 - crop_x, _normalize(crop_settings.get("crop_w_ratio"), 1.0)))
    crop_h = max(0.01, min(1.0 - crop_y, _normalize(crop_settings.get("crop_h_ratio"), 1.0)))

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
    if is_default_crop:
        scale_stage = (
            f"scale={overlay_width}:{overlay_height}:force_original_aspect_ratio=increase,"
            f"crop={overlay_width}:{overlay_height},"
        )
    else:
        scale_stage = f"scale={overlay_width}:{overlay_height},"

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
        subtitle_path,
        subtitle_margin,
        subtitle_font_size,
    )
    bg_filter = (
        f"[0:v]scale={target_width}:{target_height},setsar=1[bg]"
    )
    filter_parts = [
        bg_filter,
        crop_filter,
        f"[bg][clip_scaled]overlay=(W-w)/2:{overlay_y_expr}:shortest=1[ov]",
    ]
    final_label = "[ov]"
    overlay_asset_path = subscribe_overlay_path if subscribe_overlay_path and Path(subscribe_overlay_path).exists() else None
    overlay_enabled = subscribe_overlay_enabled and bool(overlay_asset_path)
    base_filter_len = len(filter_parts)
    base_final_label = final_label
    if title_txt:
        test_font_file = font_path or "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        debug_textfile = _write_debug_textfile(title_txt.replace("\n", "\n"))
        title_test_y = title_margin
        box_color = f"{_hex_to_drawtext_color(title_bg_color)}@1"

        debug_drawtext = (
            f"{final_label}drawtext="
            f"fontfile='{test_font_file}':"
            f"textfile='{_escape_ass_path(debug_textfile)}':"
            "x=(w-text_w)/2:"            # yatayda ortalı
            f"y={title_test_y}:"          # dikeyde UI + clamp
            f"fontsize={title_font_size}:"
            "fontcolor=black:"
            "box=1:"
            f"line_spacing={safe_title_line_spacing_main}:"

            f"boxcolor={box_color}:"      # hafif transparan sarı
            "boxborderw=22:"              # daha kalın padding
            "[ov_title_debug]"
        )
        filter_parts.append(debug_drawtext)
        final_label = "[ov_title_debug]"


    if subtitle_path:
        clean_font = (subtitle_font or "DejaVu Sans").replace("'", "")
        style = (
            f"Fontsize={subtitle_font_size},"
            "PrimaryColour=&H00FFFFFF,"
            "BackColour=&H00000000,"
            "BorderStyle=4,"
            "Outline=1,"
            "Shadow=0,"
            f"MarginV={subtitle_margin},"
            f"Alignment=2,"
            f"FontName={clean_font}"
        )
        filter_parts.append(
            f"{final_label}subtitles='{_escape_ass_path(subtitle_path)}':fontsdir='{_escape_ass_path(SUBTITLE_FONTS_DIR)}':force_style='{style}'[subout]"
        )
        final_label = "[subout]"
    current_app.logger.info("video_date_text=%r", video_date_text)
    if video_date_text:
        date_txt = _sanitize_text_for_overlay(video_date_text, 160)
        if date_txt:
            date_font_file = font_path or "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
            date_font_size = 24
            date_y_expr = f"main_h-text_h-{VIDEO_DATE_BOTTOM_MARGIN}"
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
    subscribe_input_index = 3 if direct_audio_source else 2
    if overlay_enabled:
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
            str(out_path),
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
                str(out_path),
            ]
        )
    current_app.logger.info("Compose ffmpeg command: %s", " ".join(cmd))
    current_app.logger.debug("Compose ffmpeg command (debug): %s", " ".join(cmd))
    try:
        result = subprocess.run(cmd, check=True, timeout=FFMPEG_TIMEOUT, capture_output=True, text=True)
        current_app.logger.debug("Compose stdout: %s", result.stdout)
        current_app.logger.debug("Compose stderr: %s", result.stderr)
    except subprocess.CalledProcessError as err:
        current_app.logger.error("Compose failed stdout=%s stderr=%s", err.stdout, err.stderr)
        raise RuntimeError(
            f"FFmpeg compose failed (key={bg_path.name}): {err.stderr.strip() or err.stdout.strip()}"
        ) from err
    finally:
        for temp_path in (trimmed, merged_override, merged_audio_override):
            if temp_path and temp_path.exists():
                try:
                    temp_path.unlink()
                except Exception:
                    pass


def _cut_clip(src: Path, start: float, end: float, out_path: Path, subtitle_path: Path = None, subtitle_font: str = "DejaVu Sans", subtitle_font_size: int = 10, subtitle_margin: int = SUB_MARGIN_DEFAULT):
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
        clean_font = (subtitle_font or "DejaVu Sans").replace("'", "")
        style = (
            f"Fontsize={subtitle_font_size},"
            "PrimaryColour=&H00FFFFFF,"
            "BackColour=&H00000000,"
            "BorderStyle=4,"
            "Outline=1,"
            "Shadow=0,"
            f"MarginV={subtitle_margin},"
            f"Alignment=2,"
            f"FontName={clean_font}"
        )
        cmd.extend(["-vf", f"subtitles='{_escape_ass_path(subtitle_path)}':fontsdir='{_escape_ass_path(SUBTITLE_FONTS_DIR)}':force_style='{style}'"])
    try:
        subprocess.run(cmd, check=True, timeout=FFMPEG_TIMEOUT)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError("ffmpeg timed out") from e
