import json
import math
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from flask import current_app

from app.video_shorts.config import (
    DEFAULT_SUBTITLE_PRESET,
    DEFAULT_SUBTITLE_BG_ALPHA,
    DEFAULT_SUBTITLE_BG_COLOR,
    DEFAULT_SUBTITLE_TEXT_ALPHA,
    FFMPEG_RENDER_TIMEOUT,
    FFPROBE_TIMEOUT,
    OPENAI_MODEL,
    SUBTITLE_PILL_FONT_MAP,
    SUBTITLE_PRESETS,
    SUBTITLE_HIGHLIGHT_COLOR,
    WHISPER_MODEL,
    _openai_client,
)
from app.video_shorts.services.db import _ensure_transcript_schema
from app.video_shorts.services.media_utils import _extract_audio_segment, _resolve_ffmpeg, run_media_subprocess
from app.video_shorts.services.transcript_lang_tagging import infer_lang_from_text, tag_segments_with_language
import re


_EN_STOPWORDS = {
    "the", "and", "to", "of", "in", "is", "it", "that", "for", "on", "with",
    "as", "are", "was", "be", "this", "you", "we", "they", "have", "from",
    "at", "or", "not", "but", "your", "our", "their", "can", "will",
}
_TR_STOPWORDS = {
    "ve", "bir", "bu", "ile", "için", "ama", "gibi", "çok", "daha", "de",
    "da", "mi", "mı", "mu", "mü", "sen", "ben", "biz", "siz", "onlar",
}
_MIN_BOUNDARY_CUE_SECONDS = 0.7
KARAOKE_MAX_WORDS = 4
_ASS_RENDER_WIDTH = 720
_ASS_RENDER_HEIGHT = 1280
_ASS_MARGIN_L = 40
_ASS_MARGIN_R = 40
_PILL_CURVE_FACTOR = 0.55228475
_PILLOW_CAPTION_WIDTH = 720
_PILLOW_CAPTION_HEIGHT = 1280
_WORD_HIGHLIGHT_OVERLAY_FPS = 30


def _normalize_whisper_language(raw: Any) -> str:
    val = str(raw or "").strip().lower()
    if not val:
        return ""
    if val.startswith("en") or val in {"english"}:
        return "en"
    if val.startswith("tr") or val in {"turkish", "turkce"}:
        return "tr"
    if val.startswith("ar") or val in {"arabic"}:
        return "ar"
    return val


def _extract_transcription_language(resp: Any) -> str:
    # OpenAI client objects may expose fields via attributes, dicts, or model_dump.
    raw = getattr(resp, "language", None)
    if raw:
        return _normalize_whisper_language(raw)
    if isinstance(resp, dict):
        return _normalize_whisper_language(resp.get("language"))
    try:
        dumped = resp.model_dump()  # pydantic-style response object
    except Exception:
        dumped = None
    if isinstance(dumped, dict):
        return _normalize_whisper_language(dumped.get("language"))
    return ""


def _looks_english_text(text: str) -> bool:
    if not text:
        return False
    tokens = [t.lower() for t in re.findall(r"[a-zA-Z']+", text)]
    if len(tokens) < 6:
        return False
    en_hits = sum(1 for t in tokens if t in _EN_STOPWORDS)
    tr_hits = sum(1 for t in tokens if t in _TR_STOPWORDS)
    tr_chars = sum(1 for ch in text if ch in "çğıöşüÇĞİÖŞÜ")
    return tr_chars == 0 and en_hits >= 2 and en_hits >= (tr_hits + 1)


def _detect_primary_audio_language(video_path: Path) -> str:
    """
    Fast language gate before heavy transcript logic.
    Returns 'en', 'tr', or '' (unknown/auto).
    """
    if not _openai_client or not video_path.exists():
        return ""
    try:
        def _log_probe_result(result: str, path_label: str, votes: List[str], duration_seconds: Optional[float]) -> None:
            try:
                current_app.logger.info(
                    "lang_probe result=%s path=%s votes=%s duration=%.1f",
                    result,
                    path_label,
                    votes,
                    float(duration_seconds) if duration_seconds is not None else -1.0,
                )
            except Exception:
                pass

        def _probe_window_language(start_seconds: float, duration_seconds: float) -> str:
            probe_path: Optional[Path] = None
            try:
                end_seconds = start_seconds + duration_seconds
                probe_path = _extract_audio_segment(video_path, start_seconds, end_seconds)
                with probe_path.open("rb") as f:
                    resp = _openai_client.audio.transcriptions.create(
                        model=WHISPER_MODEL,
                        file=f,
                        response_format="verbose_json",
                    )
                lang = _extract_transcription_language(resp)
                probe_text, _ = _whisper_response_to_segments(resp)
                if lang in {"en", "tr"}:
                    return lang
                if _looks_english_text(probe_text):
                    return "en"
                inferred = infer_lang_from_text(probe_text or "")
                if inferred == "tr":
                    return "tr"
            except Exception as exc:
                try:
                    current_app.logger.warning(
                        "Language probe window failed at %.1fs for %.1fs; ignoring window: %s",
                        start_seconds,
                        duration_seconds,
                        exc,
                    )
                except Exception:
                    pass
            finally:
                if probe_path and probe_path.exists():
                    try:
                        probe_path.unlink()
                    except Exception:
                        pass
            return ""

        duration_seconds: Optional[float] = None
        try:
            ffmpeg_bin = Path(_resolve_ffmpeg())
            ffprobe_bin = ffmpeg_bin.with_name("ffprobe")
            try:
                current_app.logger.info(
                    "lang_probe ffmpeg_resolved=%r ffprobe_bin=%r exists=%s",
                    str(ffmpeg_bin),
                    str(ffprobe_bin),
                    ffprobe_bin.exists(),
                )
            except Exception:
                pass
            ffprobe_cmd = [
                str(ffprobe_bin if ffprobe_bin.exists() else "ffprobe"),
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(video_path),
            ]
            try:
                current_app.logger.info("lang_probe ffprobe_cmd=%s", ffprobe_cmd)
            except Exception:
                pass
            proc = run_media_subprocess(
                ffprobe_cmd,
                operation="language_probe_duration",
                context=f"path={video_path.name}",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=FFPROBE_TIMEOUT,
            )
            try:
                current_app.logger.info(
                    "lang_probe ffprobe rc=%s stdout=%r stderr=%r",
                    proc.returncode,
                    (proc.stdout or "").strip(),
                    (proc.stderr or "").strip()[:500],
                )
            except Exception:
                pass
            if proc.returncode == 0:
                raw_duration = (proc.stdout or "").strip()
                if raw_duration:
                    duration_seconds = float(raw_duration)
        except Exception as e:
            try:
                current_app.logger.warning("lang_probe ffprobe exception: %r", e)
            except Exception:
                pass
            duration_seconds = None
        try:
            current_app.logger.info("lang_probe parsed_duration=%r", duration_seconds)
        except Exception:
            pass

        if not duration_seconds or duration_seconds < 90.0:
            # Short clips keep the original single-window behavior.
            result = _probe_window_language(0.0, 45.0)
            _log_probe_result(result, "single_window", [result], duration_seconds)
            return result

        window_duration = 30.0
        max_start = max(duration_seconds - window_duration, 0.0)
        votes: List[str] = []
        for fraction in (0.20, 0.50, 0.80):
            start_seconds = min(max(duration_seconds * fraction, 0.0), max_start)
            lang = _probe_window_language(start_seconds, window_duration)
            if lang in {"en", "tr"}:
                votes.append(lang)

        en_votes = sum(1 for lang in votes if lang == "en")
        tr_votes = sum(1 for lang in votes if lang == "tr")
        if en_votes > tr_votes:
            _log_probe_result("en", "multi_window", votes, duration_seconds)
            return "en"
        if tr_votes > en_votes:
            _log_probe_result("tr", "multi_window", votes, duration_seconds)
            return "tr"
    except Exception as exc:
        try:
            current_app.logger.warning("Language probe failed; fallback to auto mode: %s", exc)
        except Exception:
            pass
    try:
        current_app.logger.info(
            "lang_probe result=%s path=%s votes=%s duration=%.1f",
            "",
            "multi_window" if 'votes' in locals() else "single_window",
            votes if 'votes' in locals() else [],
            float(duration_seconds) if 'duration_seconds' in locals() and duration_seconds is not None else -1.0,
        )
    except Exception:
        pass
    return ""


def _split_text_into_chunks(text: str, max_words: int = 10) -> List[str]:
    if not text:
        return []
    tokens = re.findall(r"\S+", text)
    chunks: List[str] = []
    for start in range(0, len(tokens), max_words):
        chunk = " ".join(tokens[start : start + max_words])
        chunks.append(chunk)
    return chunks


def _chunked_subtitle_intervals(rel_start: float, rel_end: float, chunk_count: int) -> List[Tuple[float, float]]:
    if chunk_count <= 0:
        return []
    span = max(rel_end - rel_start, 0.05)
    per_chunk = span / chunk_count
    per_chunk = max(per_chunk, 0.05)
    intervals: List[Tuple[float, float]] = []
    current_start = rel_start
    for idx in range(chunk_count):
        if idx == chunk_count - 1:
            current_end = rel_end
        else:
            current_end = min(rel_end, current_start + per_chunk)
        if current_end <= current_start:
            current_end = current_start + 0.01
        intervals.append((current_start, current_end))
        current_start = current_end
    return intervals


def _seg_val(seg: Any, key: str, default=None):
    if isinstance(seg, dict):
        return seg.get(key, default)
    try:
        val = getattr(seg, key)
    except Exception:
        return default
    return val if val is not None else default


def _chunk_text_entries(text: str, rel_start: float, rel_end: float, max_words: int = 10) -> List[Tuple[float, float, str]]:
    chunks = _split_text_into_chunks(text, max_words=max_words)
    if not chunks:
        return []
    intervals = _chunked_subtitle_intervals(rel_start, rel_end, len(chunks))
    entries: List[Tuple[float, float, str]] = []
    for (start, end), chunk in zip(intervals, chunks):
        entries.append((start, end, chunk))
    return entries


def _trim_text_to_segment_overlap(
    text: str,
    *,
    segment_start: float,
    segment_end: float,
    overlap_start: float,
    overlap_end: float,
) -> str:
    text = str(text or "").strip()
    if not text:
        return ""
    tokens = re.findall(r"\S+", text)
    if not tokens:
        return ""
    full_duration = max(float(segment_end) - float(segment_start), 0.0)
    overlap_duration = max(float(overlap_end) - float(overlap_start), 0.0)
    if full_duration <= 0.0 or overlap_duration <= 0.0:
        return ""
    if overlap_start <= segment_start and overlap_end >= segment_end:
        return text

    token_count = len(tokens)
    start_ratio = min(max((float(overlap_start) - float(segment_start)) / full_duration, 0.0), 1.0)
    end_ratio = min(max((float(overlap_end) - float(segment_start)) / full_duration, 0.0), 1.0)
    start_index = min(token_count - 1, max(0, int(start_ratio * token_count)))
    end_index = min(token_count, max(start_index + 1, int(round(end_ratio * token_count))))
    trimmed = tokens[start_index:end_index]
    if not trimmed:
        keep_words = max(1, min(token_count, int(round(token_count * (overlap_duration / full_duration)))))
        trimmed = tokens[-keep_words:] if overlap_start > segment_start else tokens[:keep_words]
    return " ".join(trimmed).strip()


def _build_srt_for_clip(segments: List[Dict[str, Any]], clip_start: float, clip_end: float) -> Path | None:
    """
    Build a temp SRT file for segments within [clip_start, clip_end].
    Returns path or None if no lines.
    """
    lines = []

    def fmt(ts: float) -> str:
        hrs = int(ts // 3600)
        ts -= hrs * 3600
        mins = int(ts // 60)
        ts -= mins * 60
        secs = int(ts)
        ms = int(round((ts - secs) * 1000))
        return f"{hrs:02d}:{mins:02d}:{secs:02d},{ms:03d}"

    idx = 1
    for seg in segments:
        try:
            s = float(seg.get("start", 0.0) or 0.0)
        except Exception:
            continue
        try:
            d = seg.get("duration")
            d = float(d) if d is not None else None
        except Exception:
            d = None
        try:
            e_val = seg.get("end")
            e = float(e_val) if e_val is not None else None
        except Exception:
            e = None
        if e is None:
            e = s + max(d or 0.0, 0.0)
        if d is None:
            d = max(e - s, 0.0)
        if e <= clip_start or s >= clip_end:
            continue
        overlap_start = max(s, clip_start)
        overlap_end = min(e, clip_end)
        overlap_duration = max(overlap_end - overlap_start, 0.0)
        rel_start = max(0.0, overlap_start - clip_start)
        rel_end = max(rel_start + 0.1, overlap_end - clip_start)
        text = (seg.get("tr_text") or seg.get("text") or seg.get("ar_text") or "").strip()
        if not text:
            continue
        is_boundary_segment = s < clip_start or e > clip_end
        if is_boundary_segment:
            text = _trim_text_to_segment_overlap(
                text,
                segment_start=s,
                segment_end=e,
                overlap_start=overlap_start,
                overlap_end=overlap_end,
            )
            if not text or overlap_duration < _MIN_BOUNDARY_CUE_SECONDS:
                continue
        entries = _chunk_text_entries(text, rel_start, rel_end, max_words=10)
        for start, end, chunk_text in entries:
            if is_boundary_segment and (end - start) < _MIN_BOUNDARY_CUE_SECONDS:
                continue
            lines.append(f"{idx}")
            lines.append(f"{fmt(start)} --> {fmt(end)}")
            lines.append(chunk_text)
            lines.append("")
            idx += 1

    if not lines:
        return None

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".srt")
    tmp_path = Path(tmp.name)
    tmp.write("\n".join(lines).encode("utf-8"))
    tmp.close()
    return tmp_path


def _format_ass_time(ts: float) -> str:
    total_cs = max(0, int(round(float(ts or 0.0) * 100)))
    hrs = total_cs // 360000
    total_cs -= hrs * 360000
    mins = total_cs // 6000
    total_cs -= mins * 6000
    secs = total_cs // 100
    cs = total_cs - secs * 100
    return f"{hrs}:{mins:02d}:{secs:02d}.{cs:02d}"


def _hex_to_ass_color_with_alpha(
    color: Optional[str],
    alpha_percent: Optional[int],
    fallback: str,
    fallback_alpha: int,
) -> str:
    value = str(color or fallback or "").strip()
    if not re.fullmatch(r"#[0-9A-Fa-f]{6}", value):
        value = fallback
    try:
        alpha = int(alpha_percent if alpha_percent is not None else fallback_alpha)
    except Exception:
        alpha = fallback_alpha
    alpha = max(0, min(100, alpha))
    rr = int(value[1:3], 16)
    gg = int(value[3:5], 16)
    bb = int(value[5:7], 16)
    aa = int(round((100 - alpha) * 255 / 100))
    return f"&H{aa:02X}{bb:02X}{gg:02X}{rr:02X}"


def _resolve_subtitle_preset(subtitle_preset: Optional[str]) -> Dict[str, Any]:
    preset_key = str(subtitle_preset or DEFAULT_SUBTITLE_PRESET).strip() or DEFAULT_SUBTITLE_PRESET
    return SUBTITLE_PRESETS.get(preset_key, SUBTITLE_PRESETS[DEFAULT_SUBTITLE_PRESET])


def _pill_font_metrics(
    preset_key: str,
    resolved_font: str,
    subtitle_font_size: int,
) -> tuple[ImageFont.FreeTypeFont, str] | None:
    entry = SUBTITLE_PILL_FONT_MAP.get(preset_key)
    if not entry:
        return None
    expected_font_name = str(entry.get("ass_font_name") or "").strip()
    font_path = Path(entry.get("font_path") or "")
    if not expected_font_name or not font_path.exists():
        current_app.logger.warning(
            "pill_font_missing preset=%s expected=%r path=%s",
            preset_key,
            expected_font_name,
            font_path,
        )
        return None
    if resolved_font != expected_font_name:
        current_app.logger.warning(
            "pill_font_mismatch preset=%s resolved=%r expected=%r; falling back to box highlight",
            preset_key,
            resolved_font,
            expected_font_name,
        )
        return None
    try:
        font = ImageFont.truetype(str(font_path), int(subtitle_font_size))
    except Exception as exc:
        current_app.logger.warning(
            "pill_font_load_failed preset=%s path=%s size=%s error=%s",
            preset_key,
            font_path,
            subtitle_font_size,
            exc,
        )
        return None
    return font, expected_font_name


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


def _build_ass_pill_shape(width: float, height: float) -> str:
    safe_width = max(1.0, float(width))
    safe_height = max(1.0, float(height))
    radius = max(1.0, min(safe_height / 2.0, safe_width / 2.0))
    curve = radius * _PILL_CURVE_FACTOR
    right = safe_width
    bottom = safe_height
    return (
        f"m {radius:.2f} 0 "
        f"l {right - radius:.2f} 0 "
        f"b {right - radius + curve:.2f} 0 {right:.2f} {radius - curve:.2f} {right:.2f} {radius:.2f} "
        f"l {right:.2f} {bottom - radius:.2f} "
        f"b {right:.2f} {bottom - radius + curve:.2f} {right - radius + curve:.2f} {bottom:.2f} {right - radius:.2f} {bottom:.2f} "
        f"l {radius:.2f} {bottom:.2f} "
        f"b {radius - curve:.2f} {bottom:.2f} 0 {bottom - radius + curve:.2f} 0 {bottom - radius:.2f} "
        f"l 0 {radius:.2f} "
        f"b 0 {radius - curve:.2f} {radius - curve:.2f} 0 {radius:.2f} 0"
    )


def _rendered_active_token(word: str, padding_spaces: int) -> str:
    hard_padding = " " * max(0, int(padding_spaces))
    return f"{hard_padding}{word}{hard_padding}" if hard_padding else word


def _rendered_line_parts(words: List[str], active_index: int, padding_spaces: int) -> tuple[str, str, str]:
    rendered_words: List[str] = []
    for index, word in enumerate(words):
        if index == active_index:
            rendered_words.append(_rendered_active_token(word, padding_spaces))
        else:
            rendered_words.append(word)
    full_line = " ".join(rendered_words)
    before_parts = rendered_words[:active_index]
    before_text = " ".join(before_parts)
    if before_text:
        before_text += " "
    active_token = rendered_words[active_index]
    return full_line, before_text, active_token


def _compute_single_line_pill_layout(
    font: ImageFont.FreeTypeFont,
    words: List[str],
    *,
    active_index: int,
    pad_x: int,
    subtitle_margin: int,
    pad_y: int,
    effective_line_width: int,
) -> Dict[str, int] | None:
    full_line = " ".join(words)
    before_text = " ".join(words[:active_index])
    if before_text:
        before_text += " "
    line_width = _measure_text_width(font, full_line)
    if line_width > effective_line_width:
        return None
    line_height = _measure_line_height(font)
    text_top = max(0, _ASS_RENDER_HEIGHT - int(subtitle_margin) - line_height)
    x0 = max(_ASS_MARGIN_L, int(round((_ASS_RENDER_WIDTH - line_width) / 2)))
    before_width = _measure_text_width(font, before_text)
    active_word_width = _measure_text_width(font, words[active_index])
    pill_height = line_height + (pad_y * 2)
    pill_width = max(active_word_width + (pad_x * 2), pill_height + 2)
    word_left = x0 + before_width
    pill_left = int(round(word_left - pad_x))
    pill_top = text_top - pad_y
    return {
        "x0": x0,
        "text_top": text_top,
        "line_width": line_width,
        "line_height": line_height,
        "before_width": before_width,
        "active_word_width": active_word_width,
        "word_left": int(round(word_left)),
        "pill_left": pill_left,
        "pill_top": pill_top,
        "pill_width": int(round(pill_width)),
        "pill_height": pill_height,
    }


def _build_active_word_text(
    words: List[str],
    active_index: int,
    *,
    active_word_tags: str,
    active_style_name: str = "ActiveWord",
    reset_style_name: str = "Default",
    padding_spaces: int = 0,
) -> str:
    parts: List[str] = []
    active_word_padding = "\\h" * max(0, int(padding_spaces))
    for index, word in enumerate(words):
        if index == active_index:
            padded_word = f"{active_word_padding}{word}{active_word_padding}" if active_word_padding else word
            parts.append(f"{{\\r{active_style_name}{active_word_tags}}}{padded_word}{{\\r{reset_style_name}}}")
        else:
            parts.append(word)
    return " ".join(parts)


def _build_ass_karaoke_for_clip(
    segments: List[Dict[str, Any]],
    clip_start: float,
    clip_end: float,
    *,
    subtitle_font: str,
    subtitle_font_size: int,
    subtitle_margin: int,
    subtitle_text_color: Optional[str],
    subtitle_text_alpha: Optional[int],
    subtitle_bg_color: Optional[str],
    subtitle_bg_alpha: Optional[int],
    subtitle_preset: Optional[str] = None,
) -> Path | None:
    preset = _resolve_subtitle_preset(subtitle_preset)
    preset_key = str(subtitle_preset or DEFAULT_SUBTITLE_PRESET).strip() or DEFAULT_SUBTITLE_PRESET
    active_color = str(preset.get("active_color") or SUBTITLE_HIGHLIGHT_COLOR).strip() or SUBTITLE_HIGHLIGHT_COLOR
    inactive_color = str(subtitle_text_color or preset.get("inactive_color") or "#FFFFFF").strip() or "#FFFFFF"
    outline_color = str(preset.get("outline_color") or "#000000").strip() or "#000000"
    active_box_color = str(preset.get("active_box_color") or "").strip()
    active_box_outline_color = str(preset.get("active_box_outline_color") or active_box_color or outline_color).strip() or outline_color
    try:
        outline_width = max(0, int(preset.get("outline_width", 1) or 1))
    except Exception:
        outline_width = 1
    try:
        active_box_outline_width = max(0, int(preset.get("active_box_outline_width", outline_width) or outline_width))
    except Exception:
        active_box_outline_width = outline_width
    try:
        border_style = int(preset.get("border_style", 4) or 4)
    except Exception:
        border_style = 4
    try:
        active_box_border_style = int(preset.get("active_box_border_style", 4) or 4)
    except Exception:
        active_box_border_style = 4
    try:
        active_box_padding_spaces = max(0, int(preset.get("active_box_padding_spaces", 0) or 0))
    except Exception:
        active_box_padding_spaces = 0
    pill_enabled = bool(preset.get("active_pill"))
    show_box = bool(preset.get("box", True))
    use_active_word_style = bool(active_box_color)
    try:
        active_scale = max(1, int(preset.get("active_scale", 100) or 100))
    except Exception:
        active_scale = 100
    bold = -1 if bool(preset.get("bold")) else 0
    preset_font = str(preset.get("font") or "").strip()
    resolved_font = subtitle_font
    if preset_font and str(subtitle_preset or DEFAULT_SUBTITLE_PRESET).strip() != DEFAULT_SUBTITLE_PRESET:
        resolved_font = preset_font
    pill_metrics = _pill_font_metrics(preset_key, resolved_font, subtitle_font_size) if pill_enabled else None
    pill_font = pill_metrics[0] if pill_metrics else None
    pill_padding_x = max(10, int(round(float(subtitle_font_size) * 0.48)))
    pill_padding_y = max(4, int(round(float(subtitle_font_size) * 0.14)))
    effective_line_width = _ASS_RENDER_WIDTH - _ASS_MARGIN_L - _ASS_MARGIN_R
    pill_single_line_count = 0
    pill_wrap_fallback_count = 0
    pill_font_fallback_count = 0
    active_word_tags = ""
    if active_scale != 100:
        active_word_tags = f"\\fscx{active_scale}\\fscy{active_scale}"

    events: List[Dict[str, Any]] = []

    for seg in segments:
        try:
            s = float(seg.get("start", 0.0) or 0.0)
        except Exception:
            continue
        try:
            d = seg.get("duration")
            d = float(d) if d is not None else None
        except Exception:
            d = None
        try:
            e_val = seg.get("end")
            e = float(e_val) if e_val is not None else None
        except Exception:
            e = None
        if e is None:
            e = s + max(d or 0.0, 0.0)
        if d is None:
            d = max(e - s, 0.0)
        if e <= clip_start or s >= clip_end:
            continue

        overlap_start = max(s, clip_start)
        overlap_end = min(e, clip_end)
        overlap_duration = max(overlap_end - overlap_start, 0.0)
        rel_start = max(0.0, overlap_start - clip_start)
        rel_end = max(rel_start + 0.1, overlap_end - clip_start)

        words_raw = seg.get("words") or []
        karaoke_words: List[Dict[str, Any]] = []
        if isinstance(words_raw, list):
            for word in words_raw:
                if not isinstance(word, dict):
                    continue
                word_text = str(word.get("word") or "").strip()
                if not word_text:
                    continue
                try:
                    word_start = float(word.get("start"))
                except Exception:
                    word_start = None
                try:
                    word_end = float(word.get("end"))
                except Exception:
                    word_end = None
                if word_start is None or word_end is None:
                    continue
                if word_end <= clip_start or word_start >= clip_end:
                    continue
                karaoke_words.append(
                    {
                        "word": word_text,
                        "start": max(word_start, clip_start),
                        "end": min(word_end, clip_end),
                    }
                )

        if karaoke_words:
            chunk_start = 0
            while chunk_start < len(karaoke_words):
                chunk_words = karaoke_words[chunk_start:chunk_start + KARAOKE_MAX_WORDS]
                chunk_start += KARAOKE_MAX_WORDS
                if not chunk_words:
                    continue
                chunk_rel_start = max(0.0, float(chunk_words[0]["start"]) - clip_start)
                chunk_rel_end = max(chunk_rel_start + 0.1, float(chunk_words[-1]["end"]) - clip_start)
                if (s < clip_start or e > clip_end) and (chunk_rel_end - chunk_rel_start) < _MIN_BOUNDARY_CUE_SECONDS:
                    continue
                if use_active_word_style:
                    word_tokens = [str(item["word"]).strip() for item in chunk_words if str(item.get("word") or "").strip()]
                    if pill_enabled and pill_font and word_tokens:
                        layout_probe = _compute_single_line_pill_layout(
                            pill_font,
                            word_tokens,
                            active_index=0,
                            pad_x=pill_padding_x,
                            subtitle_margin=subtitle_margin,
                            pad_y=pill_padding_y,
                            effective_line_width=effective_line_width,
                        )
                        if layout_probe is not None:
                            for index, item in enumerate(chunk_words):
                                item_start = max(0.0, float(item["start"]) - clip_start)
                                next_start = (
                                    max(0.0, float(chunk_words[index + 1]["start"]) - clip_start)
                                    if index + 1 < len(chunk_words)
                                    else chunk_rel_end
                                )
                                layout = _compute_single_line_pill_layout(
                                    pill_font,
                                    word_tokens,
                                    active_index=index,
                                    pad_x=pill_padding_x,
                                    subtitle_margin=subtitle_margin,
                                    pad_y=pill_padding_y,
                                    effective_line_width=effective_line_width,
                                )
                                if layout is None:
                                    pill_wrap_fallback_count += (len(chunk_words) - index)
                                    break
                                events.append(
                                    {
                                        "layer": 0,
                                        "start": item_start,
                                        "end": max(item_start + 0.01, next_start),
                                        "style": "PillShape",
                                        "text": f"{{\\an7\\pos({layout['pill_left']},{layout['pill_top']})\\bord0\\shad0\\fscx100\\fscy100\\p1}}{_build_ass_pill_shape(layout['pill_width'], layout['pill_height'])}{{\\p0}}",
                                    }
                                )
                                events.append(
                                    {
                                        "layer": 1,
                                        "start": item_start,
                                        "end": max(item_start + 0.01, next_start),
                                        "style": "Default",
                                        "text": (
                                            f"{{\\an7\\pos({layout['x0']},{layout['text_top']})}}"
                                            + _build_active_word_text(
                                                word_tokens,
                                                index,
                                                active_word_tags="",
                                                active_style_name="GhostWord",
                                                reset_style_name="Default",
                                                padding_spaces=0,
                                            )
                                        ),
                                    }
                                )
                                events.append(
                                    {
                                        "layer": 2,
                                        "start": item_start,
                                        "end": max(item_start + 0.01, next_start),
                                        "style": "ActiveWordText",
                                        "text": f"{{\\an7\\pos({layout['word_left']},{layout['text_top']}){active_word_tags}}}{word_tokens[index]}",
                                    }
                                )
                                pill_single_line_count += 1
                            else:
                                continue
                        pill_wrap_fallback_count += len(chunk_words)
                    elif pill_enabled and not pill_font:
                        pill_font_fallback_count += len(chunk_words)
                    for index, item in enumerate(chunk_words):
                        item_start = max(0.0, float(item["start"]) - clip_start)
                        next_start = (
                            max(0.0, float(chunk_words[index + 1]["start"]) - clip_start)
                            if index + 1 < len(chunk_words)
                            else chunk_rel_end
                        )
                        events.append(
                            {
                                "layer": 0,
                                "start": item_start,
                                "end": max(item_start + 0.01, next_start),
                                "style": "Default",
                                "text": _build_active_word_text(
                                    word_tokens,
                                    index,
                                    active_word_tags=active_word_tags,
                                    active_style_name="ActiveWord",
                                    reset_style_name="Default",
                                    padding_spaces=active_box_padding_spaces,
                                ),
                            }
                        )
                    continue
                parts: List[str] = []
                for item in chunk_words:
                    duration_cs = max(1, int(round(max(float(item["end"]) - float(item["start"]), 0.01) * 100)))
                    parts.append(f"{{\\k{duration_cs}}}{item['word']}")
                events.append(
                    {
                        "layer": 0,
                        "start": chunk_rel_start,
                        "end": chunk_rel_end,
                        "style": "Default",
                        "text": "{\\k0}" + " ".join(parts),
                    }
                )
            continue

        text = (seg.get("tr_text") or seg.get("text") or seg.get("ar_text") or "").strip()
        if not text:
            continue
        is_boundary_segment = s < clip_start or e > clip_end
        if is_boundary_segment:
            text = _trim_text_to_segment_overlap(
                text,
                segment_start=s,
                segment_end=e,
                overlap_start=overlap_start,
                overlap_end=overlap_end,
            )
            if not text or overlap_duration < _MIN_BOUNDARY_CUE_SECONDS:
                continue
        entries = _chunk_text_entries(text, rel_start, rel_end, max_words=KARAOKE_MAX_WORDS)
        for start, end, chunk_text in entries:
            if is_boundary_segment and (end - start) < _MIN_BOUNDARY_CUE_SECONDS:
                continue
            words = [token for token in re.findall(r"\S+", chunk_text) if token]
            if not words:
                continue
            if use_active_word_style:
                per_word = max((end - start) / len(words), 0.01)
                if pill_enabled and pill_font:
                    layout_probe = _compute_single_line_pill_layout(
                        pill_font,
                        words,
                        active_index=0,
                        pad_x=pill_padding_x,
                        subtitle_margin=subtitle_margin,
                        pad_y=pill_padding_y,
                        effective_line_width=effective_line_width,
                    )
                    if layout_probe is not None:
                        for index in range(len(words)):
                            word_start = start + (per_word * index)
                            word_end = end if index == len(words) - 1 else start + (per_word * (index + 1))
                            layout = _compute_single_line_pill_layout(
                                pill_font,
                                words,
                                active_index=index,
                                pad_x=pill_padding_x,
                                subtitle_margin=subtitle_margin,
                                pad_y=pill_padding_y,
                                effective_line_width=effective_line_width,
                            )
                            if layout is None:
                                pill_wrap_fallback_count += (len(words) - index)
                                break
                            events.append(
                                {
                                    "layer": 0,
                                    "start": word_start,
                                    "end": max(word_start + 0.01, word_end),
                                    "style": "PillShape",
                                    "text": f"{{\\an7\\pos({layout['pill_left']},{layout['pill_top']})\\bord0\\shad0\\fscx100\\fscy100\\p1}}{_build_ass_pill_shape(layout['pill_width'], layout['pill_height'])}{{\\p0}}",
                                }
                            )
                            events.append(
                                {
                                    "layer": 1,
                                    "start": word_start,
                                    "end": max(word_start + 0.01, word_end),
                                    "style": "Default",
                                    "text": (
                                        f"{{\\an7\\pos({layout['x0']},{layout['text_top']})}}"
                                        + _build_active_word_text(
                                            words,
                                            index,
                                            active_word_tags="",
                                            active_style_name="GhostWord",
                                            reset_style_name="Default",
                                            padding_spaces=0,
                                        )
                                    ),
                                }
                            )
                            events.append(
                                {
                                    "layer": 2,
                                    "start": word_start,
                                    "end": max(word_start + 0.01, word_end),
                                    "style": "ActiveWordText",
                                    "text": f"{{\\an7\\pos({layout['word_left']},{layout['text_top']}){active_word_tags}}}{words[index]}",
                                }
                            )
                            pill_single_line_count += 1
                        else:
                            continue
                    pill_wrap_fallback_count += len(words)
                elif pill_enabled and not pill_font:
                    pill_font_fallback_count += len(words)
                for index in range(len(words)):
                    word_start = start + (per_word * index)
                    word_end = end if index == len(words) - 1 else start + (per_word * (index + 1))
                    events.append(
                        {
                            "layer": 0,
                            "start": word_start,
                            "end": max(word_start + 0.01, word_end),
                            "style": "Default",
                            "text": _build_active_word_text(
                                words,
                                index,
                                active_word_tags=active_word_tags,
                                active_style_name="ActiveWord",
                                reset_style_name="Default",
                                padding_spaces=active_box_padding_spaces,
                            ),
                        }
                    )
                continue
            total_cs = max(1, int(round(max(end - start, 0.01) * 100)))
            base_cs = max(1, total_cs // len(words))
            remaining = total_cs - (base_cs * len(words))
            parts = []
            for idx, token in enumerate(words):
                duration_cs = max(1, base_cs + (1 if idx < remaining else 0))
                parts.append(f"{{\\k{duration_cs}}}{token}")
            events.append({"layer": 0, "start": start, "end": end, "style": "Default", "text": "{\\k0}" + " ".join(parts)})

    if not events:
        return None
    if show_box:
        back_color = _hex_to_ass_color_with_alpha(
            subtitle_bg_color,
            subtitle_bg_alpha,
            DEFAULT_SUBTITLE_BG_COLOR,
            DEFAULT_SUBTITLE_BG_ALPHA,
        )
    else:
        back_color = _hex_to_ass_color_with_alpha("#000000", 0, "#000000", 0)

    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "PlayResX: 1080",
        "PlayResY: 1920",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        "Style: Default,"
        f"{resolved_font},"
        f"{int(subtitle_font_size)},"
        f"{_hex_to_ass_color_with_alpha(inactive_color if use_active_word_style else active_color, 100, SUBTITLE_HIGHLIGHT_COLOR, 100)},"
        f"{_hex_to_ass_color_with_alpha(inactive_color, subtitle_text_alpha, '#FFFFFF', DEFAULT_SUBTITLE_TEXT_ALPHA)},"
        f"{_hex_to_ass_color_with_alpha(outline_color, 100, '#000000', 100)},"
        f"{back_color},"
        f"{bold},0,0,0,100,100,0,0,{border_style},{outline_width},0,2,40,40,"
        f"{int(subtitle_margin)},1",
    ]
    if use_active_word_style:
        lines.append(
            "Style: ActiveWord,"
            f"{resolved_font},"
            f"{int(subtitle_font_size)},"
            f"{_hex_to_ass_color_with_alpha(active_color, 100, '#111827', 100)},"
            f"{_hex_to_ass_color_with_alpha(active_color, 100, '#111827', 100)},"
            f"{_hex_to_ass_color_with_alpha(active_box_outline_color, 100, '#FFD84D', 100)},"
            f"{_hex_to_ass_color_with_alpha(active_box_color, 100, '#FFD84D', 100)},"
            f"{bold},0,0,0,100,100,0,0,{active_box_border_style},{active_box_outline_width},0,2,40,40,"
            f"{int(subtitle_margin)},1"
        )
        lines.append(
            "Style: ActiveWordText,"
            f"{resolved_font},"
            f"{int(subtitle_font_size)},"
            f"{_hex_to_ass_color_with_alpha(active_color, 100, '#111827', 100)},"
            f"{_hex_to_ass_color_with_alpha(active_color, 100, '#111827', 100)},"
            f"{_hex_to_ass_color_with_alpha(outline_color, 0, '#000000', 0)},"
            f"{_hex_to_ass_color_with_alpha('#000000', 0, '#000000', 0)},"
            f"{bold},0,0,0,100,100,0,0,1,0,0,7,0,0,0,1"
        )
        lines.append(
            "Style: GhostWord,"
            f"{resolved_font},"
            f"{int(subtitle_font_size)},"
            "&HFF000000,"
            "&HFF000000,"
            "&HFF000000,"
            "&HFF000000,"
            f"{bold},0,0,0,100,100,0,0,{border_style},{outline_width},0,2,40,40,{int(subtitle_margin)},1"
        )
        lines.append(
            "Style: PillShape,"
            f"{resolved_font},"
            f"{int(subtitle_font_size)},"
            f"{_hex_to_ass_color_with_alpha(active_box_color, 100, '#FFD84D', 100)},"
            f"{_hex_to_ass_color_with_alpha(active_box_color, 100, '#FFD84D', 100)},"
            f"{_hex_to_ass_color_with_alpha(active_box_color, 100, '#FFD84D', 100)},"
            f"{_hex_to_ass_color_with_alpha(active_box_color, 100, '#FFD84D', 100)},"
            "0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1"
        )
    lines.extend([
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ])
    for event in events:
        text = str(event["text"])
        if active_word_tags and not use_active_word_style:
            text = re.sub(
                r"\{\\k(\d+)\}([^\s]+)",
                lambda match: f"{{\\k{match.group(1)}{active_word_tags}}}{match.group(2)}{{\\rDefault}}",
                text,
            )
        lines.append(
            f"Dialogue: {int(event.get('layer', 0))},{_format_ass_time(float(event['start']))},{_format_ass_time(float(event['end']))},{event.get('style', 'Default')},,0,0,0,,{text}"
        )
    if pill_enabled:
        current_app.logger.info(
            "subtitle_pill_stats preset=%s single_line=%s wrap_fallback=%s font_fallback=%s",
            preset_key,
            pill_single_line_count,
            pill_wrap_fallback_count,
            pill_font_fallback_count,
        )

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".ass")
    tmp_path = Path(tmp.name)
    tmp.write("\n".join(lines).encode("utf-8"))
    tmp.close()
    return tmp_path


def _hex_to_rgba(color: str, alpha: int = 255) -> tuple[int, int, int, int]:
    value = str(color or "").strip()
    if not re.fullmatch(r"#[0-9A-Fa-f]{6}", value):
        value = "#FFFFFF"
    return (
        int(value[1:3], 16),
        int(value[3:5], 16),
        int(value[5:7], 16),
        max(0, min(255, int(alpha))),
    )


def _build_blurred_shadow_region(
    layout: Dict[str, Any],
    *,
    font: ImageFont.FreeTypeFont,
    shadow_color: str,
    shadow_opacity: float,
    shadow_offset_x: int,
    shadow_offset_y: int,
    shadow_blur_radius: int,
) -> Dict[str, Any] | None:
    words = list(layout.get("words") or [])
    if not words:
        return None
    if not str(shadow_color or "").strip():
        return None
    opacity = max(0.0, min(1.0, float(shadow_opacity or 0.0)))
    if opacity <= 0.0:
        return None

    probe_image = Image.new("RGBA", (8, 8), (0, 0, 0, 0))
    probe_draw = ImageDraw.Draw(probe_image)
    word_bboxes: List[Tuple[int, int, int, int]] = []
    for word in words:
        bbox = probe_draw.textbbox(
            (int(word["x"]), int(word["y"])),
            str(word["word"]),
            font=font,
            anchor="la",
            stroke_width=0,
        )
        word_bboxes.append((int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3])))
    min_x = min(bbox[0] for bbox in word_bboxes)
    min_y = min(bbox[1] for bbox in word_bboxes)
    max_x = max(bbox[2] for bbox in word_bboxes)
    max_y = max(bbox[3] for bbox in word_bboxes)
    blur_pad = max(2, int(round(max(0, shadow_blur_radius) * 3)))
    region_left = max(0, min_x + min(0, int(shadow_offset_x)) - blur_pad)
    region_top = max(0, min_y + min(0, int(shadow_offset_y)) - blur_pad)
    region_right = min(_PILLOW_CAPTION_WIDTH, max_x + max(0, int(shadow_offset_x)) + blur_pad)
    region_bottom = min(_PILLOW_CAPTION_HEIGHT, max_y + max(0, int(shadow_offset_y)) + blur_pad)
    region_width = max(1, int(region_right - region_left))
    region_height = max(1, int(region_bottom - region_top))

    shadow_region = Image.new("RGBA", (region_width, region_height), (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow_region)
    shadow_fill = _hex_to_rgba(shadow_color, int(round(opacity * 255)))
    for word in words:
        shadow_draw.text(
            (
                int(word["x"]) - region_left + int(shadow_offset_x),
                int(word["y"]) - region_top + int(shadow_offset_y),
            ),
            str(word["word"]),
            font=font,
            fill=shadow_fill,
            anchor="la",
        )
    if shadow_blur_radius > 0:
        shadow_region = shadow_region.filter(ImageFilter.GaussianBlur(int(shadow_blur_radius)))
    return {
        "image": shadow_region,
        "x": region_left,
        "y": region_top,
    }


def _wrap_words_to_lines(
    words: List[str],
    font: ImageFont.FreeTypeFont,
    max_width: int,
    slot_widths: Optional[List[int]] = None,
) -> List[List[Dict[str, Any]]]:
    lines: List[List[Dict[str, Any]]] = []
    current_line: List[Dict[str, Any]] = []
    current_width = 0
    space_width = _measure_text_width(font, " ")

    for index, word in enumerate(words):
        word_width = _measure_text_width(font, word)
        slot_width = max(word_width, int((slot_widths or [])[index])) if slot_widths and index < len(slot_widths) else word_width
        candidate_width = slot_width if not current_line else current_width + space_width + slot_width
        if current_line and candidate_width > max_width:
            lines.append(current_line)
            current_line = [{"word": word, "width": word_width, "slot_width": slot_width}]
            current_width = slot_width
            continue
        current_line.append({"word": word, "width": word_width, "slot_width": slot_width})
        current_width = candidate_width

    if current_line:
        lines.append(current_line)
    return lines


def _layout_wrapped_caption(
    words: List[str],
    *,
    font: ImageFont.FreeTypeFont,
    subtitle_margin: int,
    max_width: int,
    canvas_width: int,
    canvas_height: int,
    slot_widths: Optional[List[int]] = None,
) -> Dict[str, Any]:
    lines = _wrap_words_to_lines(words, font, max_width, slot_widths=slot_widths)
    space_width = _measure_text_width(font, " ")
    line_height = _measure_line_height(font)
    line_gap = max(4, int(round(line_height * 0.18)))
    total_height = (line_height * len(lines)) + (line_gap * max(0, len(lines) - 1))
    block_top = max(0, canvas_height - int(subtitle_margin) - total_height)

    positioned_lines: List[Dict[str, Any]] = []
    flat_words: List[Dict[str, Any]] = []
    word_index = 0
    for line_number, line_words in enumerate(lines):
        line_width = sum(int(item.get("slot_width") or item["width"]) for item in line_words)
        if len(line_words) > 1:
            line_width += space_width * (len(line_words) - 1)
        line_x = int(round((canvas_width - line_width) / 2))
        line_y = block_top + (line_number * (line_height + line_gap))
        slot_x = line_x
        positioned_words: List[Dict[str, Any]] = []
        for pos, item in enumerate(line_words):
            slot_width = int(item.get("slot_width") or item["width"])
            word_x = int(round(slot_x + max(0, (slot_width - item["width"]) / 2.0)))
            entry = {
                "word": item["word"],
                "width": item["width"],
                "slot_width": slot_width,
                "slot_x": slot_x,
                "x": word_x,
                "y": line_y,
                "line_index": line_number,
                "line_width": line_width,
                "global_index": word_index,
            }
            positioned_words.append(entry)
            flat_words.append(entry)
            word_index += 1
            slot_x += slot_width
            if pos < len(line_words) - 1:
                slot_x += space_width
        positioned_lines.append(
            {
                "x": line_x,
                "y": line_y,
                "width": line_width,
                "words": positioned_words,
            }
        )

    return {
        "lines": positioned_lines,
        "words": flat_words,
        "line_height": line_height,
        "line_gap": line_gap,
        "total_height": total_height,
    }


def _draw_outlined_text(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    text: str,
    *,
    font: ImageFont.FreeTypeFont,
    fill: tuple[int, int, int, int],
    stroke_fill: tuple[int, int, int, int],
    stroke_width: int,
) -> None:
    draw.text(
        (x, y),
        text,
        font=font,
        fill=fill,
        stroke_fill=stroke_fill,
        stroke_width=stroke_width,
        anchor="la",
    )


def _render_word_highlight_caption_frame(
    words: List[str],
    *,
    active_index: int,
    font: ImageFont.FreeTypeFont,
    subtitle_margin: int,
    font_size: int,
    inactive_color: str,
    active_color: str,
    outline_color: str,
    outline_width: int,
    pill_color: str,
    draw_pill: bool = True,
    precomputed_layout: Optional[Dict[str, Any]] = None,
    shadow_region: Optional[Dict[str, Any]] = None,
    out_path: Path,
) -> Dict[str, Any]:
    base_pad_x = max(10, int(round(float(font_size) * 0.48)))
    pad_y = max(4, int(round(float(font_size) * 0.14)))
    if precomputed_layout is None:
        probe_image = Image.new("RGBA", (8, 8), (0, 0, 0, 0))
        probe_draw = ImageDraw.Draw(probe_image)
        word_metrics: List[Dict[str, int]] = []
        for word in words:
            bbox = probe_draw.textbbox(
                (0, 0),
                str(word),
                font=font,
                anchor="la",
                stroke_width=outline_width,
            )
            text_w = max(1, int(bbox[2] - bbox[0]))
            text_h = max(1, int(bbox[3] - bbox[1]))
            pill_h = text_h + (pad_y * 2)
            desired_pill_w = max(text_w + (base_pad_x * 2), int(round(pill_h * 2.0)))
            word_metrics.append(
                {
                    "text_w": text_w,
                    "text_h": text_h,
                    "pill_h": pill_h,
                    "desired_pill_w": desired_pill_w,
                }
            )

        slot_widths = [metric["text_w"] for metric in word_metrics]
        slot_widths[active_index] = max(slot_widths[active_index], word_metrics[active_index]["desired_pill_w"])
        layout = _layout_wrapped_caption(
            words,
            font=font,
            subtitle_margin=subtitle_margin,
            max_width=_PILLOW_CAPTION_WIDTH - _ASS_MARGIN_L - _ASS_MARGIN_R,
            canvas_width=_PILLOW_CAPTION_WIDTH,
            canvas_height=_PILLOW_CAPTION_HEIGHT,
            slot_widths=slot_widths,
        )
    else:
        layout = precomputed_layout
    image = Image.new("RGBA", (_PILLOW_CAPTION_WIDTH, _PILLOW_CAPTION_HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    if shadow_region:
        image.alpha_composite(
            shadow_region["image"],
            (int(shadow_region["x"]), int(shadow_region["y"])),
        )

    active_word = layout["words"][active_index]
    active_bbox = draw.textbbox(
        (int(active_word["x"]), int(active_word["y"])),
        str(active_word["word"]),
        font=font,
        anchor="la",
        stroke_width=outline_width,
    )
    active_text_w = max(1, int(active_bbox[2] - active_bbox[0]))
    active_text_h = max(1, int(active_bbox[3] - active_bbox[1]))
    pill_h = active_text_h + (pad_y * 2)
    left_gap = None
    right_gap = None
    pill_w = 0
    pill_left = 0
    pill_top = 0
    pad_left = 0
    pad_right = 0
    if draw_pill:
        radius = pill_h // 2
        min_pill_w = int(round(pill_h * 2.0))
        slot_width = max(int(active_word.get("slot_width") or active_text_w), min_pill_w)

        line_words = layout["lines"][int(active_word["line_index"])]["words"]
        line_index = next(
            (
                idx
                for idx, candidate in enumerate(line_words)
                if int(candidate["global_index"]) == int(active_word["global_index"])
            ),
            0,
        )

        if line_index > 0:
            prev_word = line_words[line_index - 1]
            prev_bbox = draw.textbbox(
                (int(prev_word["x"]), int(prev_word["y"])),
                str(prev_word["word"]),
                font=font,
                anchor="la",
                stroke_width=outline_width,
            )
            left_gap = max(0, int(active_bbox[0] - prev_bbox[2]))
        if line_index + 1 < len(line_words):
            next_word = line_words[line_index + 1]
            next_bbox = draw.textbbox(
                (int(next_word["x"]), int(next_word["y"])),
                str(next_word["word"]),
                font=font,
                anchor="la",
                stroke_width=outline_width,
            )
            right_gap = max(0, int(next_bbox[0] - active_bbox[2]))

        pill_w = slot_width
        pill_left = int(round((int(active_word.get("slot_x") or active_bbox[0]) + (slot_width / 2.0)) - (pill_w / 2.0)))
        pill_top = int(active_bbox[1] - pad_y)
        pad_left = int(active_bbox[0] - pill_left)
        pad_right = int((pill_left + pill_w) - active_bbox[2])
        draw.rounded_rectangle(
            [pill_left, pill_top, pill_left + pill_w, pill_top + pill_h],
            radius=radius,
            fill=_hex_to_rgba(pill_color),
        )

    inactive_rgba = _hex_to_rgba(inactive_color)
    active_rgba = _hex_to_rgba(active_color)
    outline_rgba = _hex_to_rgba(outline_color)

    for word in layout["words"]:
        is_active_word = int(word["global_index"]) == int(active_index)
        fill = active_rgba if is_active_word else inactive_rgba
        _draw_outlined_text(
            draw,
            int(word["x"]),
            int(word["y"]),
            str(word["word"]),
            font=font,
            fill=fill,
            stroke_fill=outline_rgba,
            stroke_width=0 if is_active_word else outline_width,
        )

    image.save(out_path)
    return {
        "layout": layout,
        "active_word": active_word,
        "active_text_bbox": [int(active_bbox[0]), int(active_bbox[1]), int(active_bbox[2]), int(active_bbox[3])],
        "active_text_w": active_text_w,
        "active_text_h": active_text_h,
        "pad_left": pad_left,
        "pad_right": pad_right,
        "pad_y": pad_y,
        "left_gap": left_gap,
        "right_gap": right_gap,
        "pill_left": pill_left,
        "pill_top": pill_top,
        "pill_w": pill_w,
        "pill_h": pill_h,
        "delta_px": round(abs((active_word["x"] + (active_word["width"] / 2.0)) - (pill_left + (pill_w / 2.0))), 2),
    }


def _ffconcat_quote(path: Path) -> str:
    return str(path).replace("\\", "\\\\").replace("'", r"'\''")


def _build_word_highlight_overlay_video(
    *,
    temp_dir: Path,
    overlay_specs: List[Dict[str, Any]],
    clip_duration: float,
) -> Path:
    if not overlay_specs:
        raise ValueError("overlay_specs required")

    blank_frame = temp_dir / "caption_blank.png"
    Image.new("RGBA", (_PILLOW_CAPTION_WIDTH, _PILLOW_CAPTION_HEIGHT), (0, 0, 0, 0)).save(blank_frame)

    timeline: List[Tuple[Path, float]] = []
    cursor = 0.0
    clip_duration = max(float(clip_duration or 0.0), 0.01)
    for spec in overlay_specs:
        start = max(0.0, min(float(spec["start"]), clip_duration))
        end = max(start + 0.01, min(float(spec["end"]), clip_duration))
        if start > cursor:
            timeline.append((blank_frame, start - cursor))
        timeline.append((Path(spec["path"]), end - start))
        cursor = end
    if cursor < clip_duration:
        timeline.append((blank_frame, clip_duration - cursor))
    if not timeline:
        timeline.append((blank_frame, clip_duration))

    concat_file = temp_dir / "overlay.ffconcat"
    concat_lines = ["ffconcat version 1.0"]
    for frame_path, duration in timeline:
        concat_lines.append(f"file '{_ffconcat_quote(frame_path)}'")
        concat_lines.append(f"duration {max(float(duration), 0.01):.6f}")
    concat_lines.append(f"file '{_ffconcat_quote(timeline[-1][0])}'")
    concat_file.write_text("\n".join(concat_lines) + "\n", encoding="utf-8")

    overlay_path = temp_dir / "word_highlight_overlay.mov"
    ffmpeg = _resolve_ffmpeg()
    cmd = [
        ffmpeg,
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_file),
        "-vf",
        f"fps={_WORD_HIGHLIGHT_OVERLAY_FPS},format=rgba",
        "-an",
        "-c:v",
        "qtrle",
        "-pix_fmt",
        "argb",
        str(overlay_path),
    ]
    run_media_subprocess(
        cmd,
        operation="build_word_highlight_overlay_video",
        context=f"frames={len(overlay_specs)} duration={clip_duration:.3f}",
        output_paths=[overlay_path],
        check=True,
        timeout=FFMPEG_RENDER_TIMEOUT,
        capture_output=True,
        text=True,
    )
    if not overlay_path.exists() or overlay_path.stat().st_size <= 0:
        raise RuntimeError("word_highlight overlay video missing or empty")
    return overlay_path


def _build_word_highlight_caption_overlay(
    segments: List[Dict[str, Any]],
    clip_start: float,
    clip_end: float,
    *,
    subtitle_font_size: int,
    subtitle_margin: int,
    subtitle_preset: Optional[str],
) -> Tuple[Path | None, List[Path], Dict[str, Any]]:
    preset = _resolve_subtitle_preset(subtitle_preset)
    preset_key = str(subtitle_preset or DEFAULT_SUBTITLE_PRESET).strip() or DEFAULT_SUBTITLE_PRESET
    font_info = SUBTITLE_PILL_FONT_MAP.get(preset_key)
    if not font_info:
        return None, [], {"reason": "missing_font_map"}
    font_path = Path(font_info.get("font_path") or "")
    if not font_path.exists():
        return None, [], {"reason": "missing_font_file", "font_path": str(font_path)}

    temp_dir = Path(tempfile.mkdtemp(prefix="word_highlight_overlay_"))
    cleanup_paths: List[Path] = [temp_dir]
    font = ImageFont.truetype(str(font_path), int(subtitle_font_size))
    inactive_color = str(preset.get("inactive_color") or "#FFFFFF").strip() or "#FFFFFF"
    active_color = str(preset.get("active_color") or "#111827").strip() or "#111827"
    outline_color = str(preset.get("outline_color") or "#000000").strip() or "#000000"
    pill_color = str(preset.get("active_box_color") or "#FFD84D").strip() or "#FFD84D"
    draw_pill = bool(preset.get("draw_pill", True))
    shadow_color = str(preset.get("shadow_color") or "").strip()
    try:
        outline_width = max(0, int(preset.get("outline_width", 3) or 3))
    except Exception:
        outline_width = 3
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

    overlay_specs: List[Dict[str, Any]] = []
    clip_duration = max(float(clip_end) - float(clip_start), 0.01)
    layout_cache: Dict[Tuple[str, ...], Dict[str, Any]] = {}
    shadow_cache: Dict[Tuple[str, ...], Dict[str, Any] | None] = {}

    for seg in segments:
        try:
            s = float(seg.get("start", 0.0) or 0.0)
        except Exception:
            continue
        try:
            e_val = seg.get("end")
            e = float(e_val) if e_val is not None else None
        except Exception:
            e = None
        if e is None:
            try:
                d = float(seg.get("duration") or 0.0)
            except Exception:
                d = 0.0
            e = s + max(d, 0.0)
        if e <= clip_start or s >= clip_end:
            continue

        overlap_start = max(s, clip_start)
        overlap_end = min(e, clip_end)
        overlap_duration = max(overlap_end - overlap_start, 0.0)
        if overlap_duration <= 0.0:
            continue

        words_raw = seg.get("words") or []
        karaoke_words: List[Dict[str, Any]] = []
        if isinstance(words_raw, list):
            for word in words_raw:
                if not isinstance(word, dict):
                    continue
                word_text = str(word.get("word") or "").strip()
                if not word_text:
                    continue
                try:
                    word_start = float(word.get("start"))
                    word_end = float(word.get("end"))
                except Exception:
                    continue
                if word_end <= clip_start or word_start >= clip_end:
                    continue
                karaoke_words.append(
                    {
                        "word": word_text,
                        "start": max(word_start, clip_start),
                        "end": min(word_end, clip_end),
                    }
                )
        if not karaoke_words:
            display_text = (seg.get("tr_text") or seg.get("text") or seg.get("ar_text") or "").strip()
            if display_text:
                if s < clip_start or e > clip_end:
                    display_text = _trim_text_to_segment_overlap(
                        display_text,
                        segment_start=s,
                        segment_end=e,
                        overlap_start=overlap_start,
                        overlap_end=overlap_end,
                    )
                tokens = [token for token in re.findall(r"\S+", display_text) if token]
                if tokens:
                    step = overlap_duration / len(tokens)
                    cursor = overlap_start
                    for index, token in enumerate(tokens):
                        word_start = cursor
                        word_end = overlap_end if index == len(tokens) - 1 else min(overlap_end, cursor + step)
                        karaoke_words.append(
                            {
                                "word": token,
                                "start": word_start,
                                "end": max(word_start + 0.01, word_end),
                            }
                        )
                        cursor = word_end
        if not karaoke_words:
            continue

        chunk_start = 0
        while chunk_start < len(karaoke_words):
            chunk_words = karaoke_words[chunk_start:chunk_start + KARAOKE_MAX_WORDS]
            chunk_start += KARAOKE_MAX_WORDS
            word_tokens = [str(item["word"]).strip() for item in chunk_words if str(item.get("word") or "").strip()]
            if not word_tokens:
                continue
            chunk_key = tuple(word_tokens)
            precomputed_layout = None
            if not draw_pill:
                precomputed_layout = layout_cache.get(chunk_key)
                if precomputed_layout is None:
                    precomputed_layout = _layout_wrapped_caption(
                        word_tokens,
                        font=font,
                        subtitle_margin=subtitle_margin,
                        max_width=_PILLOW_CAPTION_WIDTH - _ASS_MARGIN_L - _ASS_MARGIN_R,
                        canvas_width=_PILLOW_CAPTION_WIDTH,
                        canvas_height=_PILLOW_CAPTION_HEIGHT,
                    )
                    layout_cache[chunk_key] = precomputed_layout
            shadow_region = None
            if shadow_color and shadow_opacity > 0.0:
                shadow_region = shadow_cache.get(chunk_key)
                if chunk_key not in shadow_cache:
                    layout_for_shadow = precomputed_layout or _layout_wrapped_caption(
                        word_tokens,
                        font=font,
                        subtitle_margin=subtitle_margin,
                        max_width=_PILLOW_CAPTION_WIDTH - _ASS_MARGIN_L - _ASS_MARGIN_R,
                        canvas_width=_PILLOW_CAPTION_WIDTH,
                        canvas_height=_PILLOW_CAPTION_HEIGHT,
                    )
                    shadow_region = _build_blurred_shadow_region(
                        layout_for_shadow,
                        font=font,
                        shadow_color=shadow_color,
                        shadow_opacity=shadow_opacity,
                        shadow_offset_x=shadow_offset_x,
                        shadow_offset_y=shadow_offset_y,
                        shadow_blur_radius=shadow_blur_radius,
                    )
                    shadow_cache[chunk_key] = shadow_region
                else:
                    shadow_region = shadow_cache[chunk_key]
            for index, item in enumerate(chunk_words):
                item_start = max(0.0, float(item["start"]) - clip_start)
                next_start = (
                    max(0.0, float(chunk_words[index + 1]["start"]) - clip_start)
                    if index + 1 < len(chunk_words)
                    else max(item_start + 0.01, float(chunk_words[-1]["end"]) - clip_start)
                )
                frame_path = temp_dir / f"caption_{len(overlay_specs):04d}.png"
                metrics = _render_word_highlight_caption_frame(
                    word_tokens,
                    active_index=index,
                    font=font,
                    subtitle_margin=subtitle_margin,
                    font_size=subtitle_font_size,
                    inactive_color=inactive_color,
                    active_color=active_color,
                    outline_color=outline_color,
                    outline_width=outline_width,
                    pill_color=pill_color,
                    draw_pill=draw_pill,
                    precomputed_layout=precomputed_layout,
                    shadow_region=shadow_region,
                    out_path=frame_path,
                )
                overlay_specs.append(
                    {
                        "path": frame_path,
                        "start": item_start,
                        "end": max(item_start + 0.01, next_start),
                        "metrics": metrics,
                    }
                )

    if not overlay_specs:
        shutil.rmtree(temp_dir, ignore_errors=True)
        return None, [], {"reason": "no_specs"}

    overlay_path = _build_word_highlight_overlay_video(
        temp_dir=temp_dir,
        overlay_specs=overlay_specs,
        clip_duration=clip_duration,
    )
    cleanup_paths.append(overlay_path)
    return overlay_path, cleanup_paths, {
        "specs": [],
        "frame_count": len(overlay_specs),
        "timeline_segments": len(overlay_specs),
        "timing_events": [
            {
                "start": float(spec["start"]),
                "end": float(spec["end"]),
                "active_word": (((spec.get("metrics") or {}).get("active_word")) or {}).get("word"),
                "line_count": len((((spec.get("metrics") or {}).get("layout")) or {}).get("lines") or []),
                "metrics": spec.get("metrics") or {},
            }
            for spec in overlay_specs
        ],
        "temp_dir": str(temp_dir),
        "overlay_path": str(overlay_path),
    }


def _build_srt_from_text(text: str, clip_start: float, clip_end: float) -> Path | None:
    """
    Build a temp SRT file using the provided text for the clip duration.
    """
    if not text:
        return None

    def fmt(ts: float) -> str:
        hrs = int(ts // 3600)
        ts -= hrs * 3600
        mins = int(ts // 60)
        ts -= mins * 60
        secs = int(ts)
        ms = int(round((ts - secs) * 1000))
        return f"{hrs:02d}:{mins:02d}:{secs:02d},{ms:03d}"

    duration = max((clip_end - clip_start), 0.1)
    lines = [
        "1",
        f"{fmt(0.0)} --> {fmt(duration)}",
        text.strip(),
        "",
    ]
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".srt")
    tmp_path = Path(tmp.name)
    tmp.write("\n".join(lines).encode("utf-8"))
    tmp.close()
    return tmp_path


def build_transcript_for_range(segments: List[Dict[str, Any]], start: float, end: float, prefer_tr: bool = True) -> str:
    """
    Join segment texts that overlap [start, end]. Text preference:
    if prefer_tr: tr_text or text or ar_text; else: text (fallback ar_text).
    """
    if not segments:
        return ""
    try:
        start = float(start)
    except Exception:
        start = 0.0
    try:
        end = float(end)
    except Exception:
        end = start
    texts = []
    for seg in segments:
        try:
            seg_start = float(seg.get("start", 0.0) or 0.0)
        except Exception:
            continue
        seg_dur_val = seg.get("duration")
        seg_end_val = seg.get("end")
        try:
            seg_dur = float(seg_dur_val) if seg_dur_val is not None else None
        except Exception:
            seg_dur = None
        try:
            seg_end = float(seg_end_val) if seg_end_val is not None else None
        except Exception:
            seg_end = None
        if seg_end is None:
            seg_end = seg_start + max(seg_dur or 0.0, 0.0)
        if seg_end <= start or seg_start >= end:
            continue
        if prefer_tr:
            txt = (seg.get("tr_text") or seg.get("text") or seg.get("ar_text") or "").strip()
        else:
            txt = (seg.get("text") or seg.get("ar_text") or "").strip()
        if txt:
            texts.append(txt)
    return " ".join(texts).strip()


def _prepare_audio_for_whisper(src: Path, size_limit_mb: int = 23) -> Path:
    """
    Whisper API has a 25MB file limit and only accepts certain audio/video containers.
    Convert to a small mono MP3 to avoid container/format rejections (e.g., TS mislabeled as MP4).
    """
    if not src.exists():
        raise FileNotFoundError(f"Source not found for whisper: {src}")
    resolved_ffmpeg = _resolve_ffmpeg()
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
    tmp_path = Path(tmp.name)
    tmp.close()
    cmd = [
        resolved_ffmpeg,
        "-y",
        "-i",
        str(src),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-b:a",
        "48k",
        str(tmp_path),
    ]
    run_media_subprocess(
        cmd,
        operation="prepare_audio_for_whisper",
        context=f"src={src.name}",
        output_paths=[tmp_path],
        check=True,
        timeout=FFMPEG_RENDER_TIMEOUT,
    )
    new_size_mb = tmp_path.stat().st_size / (1024 * 1024)
    if new_size_mb > size_limit_mb:
        tmp_path.unlink(missing_ok=True)
        raise RuntimeError(f"Converted audio still too large for whisper ({new_size_mb:.1f}MB)")
    return tmp_path


def _prepare_audio_chunks_for_whisper(
    src: Path, chunk_seconds: int = 1500, size_limit_mb: int = 23
) -> List[Tuple[Path, float]]:
    """
    Convert and split audio into Whisper-sized chunks.
    Returns list of (chunk_path, offset_seconds).
    """
    if not src.exists():
        raise FileNotFoundError(f"Source not found for whisper: {src}")
    resolved_ffmpeg = _resolve_ffmpeg()
    tmp_dir = Path(tempfile.mkdtemp(prefix="whisper_chunks_"))
    pattern = str(tmp_dir / "chunk_%03d.mp3")
    cmd = [
        resolved_ffmpeg,
        "-y",
        "-i",
        str(src),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-b:a",
        "48k",
        "-f",
        "segment",
        "-segment_time",
        str(chunk_seconds),
        "-reset_timestamps",
        "1",
        pattern,
    ]
    run_media_subprocess(
        cmd,
        operation="prepare_audio_chunks_for_whisper",
        context=f"src={src.name}",
        output_paths=[tmp_dir],
        check=True,
        timeout=FFMPEG_RENDER_TIMEOUT,
    )
    chunks = sorted(tmp_dir.glob("chunk_*.mp3"))
    if not chunks:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise RuntimeError("Failed to create audio chunks for whisper.")
    max_size_mb = max((p.stat().st_size / (1024 * 1024) for p in chunks), default=0.0)
    if max_size_mb > size_limit_mb:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise RuntimeError(f"Audio chunk too large for whisper ({max_size_mb:.1f}MB)")
    return [(chunk, idx * float(chunk_seconds)) for idx, chunk in enumerate(chunks)]


def _split_segment_by_word_tags(seg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Given a Whisper segment with word timings, split into logical segments
    where each chunk has a homogeneous tag from classify_words_with_llm.
    If tagging fails or no words, return [seg] unchanged.
    """
    def _lang_from_tag(tag: str) -> str:
        if tag == "arabic_prayer_or_quran":
            return "ar"
        if tag == "mixed":
            return "mixed"
        return "tr"

    words = seg.get("words") or []
    if not isinstance(words, list) or not words:
        return [seg]
    word_texts = []
    word_spans = []
    for w in words:
        w_text = w.get("word") if isinstance(w, dict) else None
        if not w_text:
            continue
        try:
            w_start = float(w.get("start", seg.get("start", 0.0)))
            w_end = float(w.get("end", w_start))
        except Exception:
            continue
        word_texts.append(str(w_text).strip())
        word_spans.append((w_start, w_end))
    # Fallback: if Whisper didn't return word timings, synthesize from text.
    if (not word_texts or len(word_texts) != len(word_spans)) and seg.get("text"):
        word_texts = []
        word_spans = []
        tokens = str(seg.get("text") or "").strip().split()
        try:
            s0 = float(seg.get("start", 0.0) or 0.0)
            e0 = float(seg.get("end", s0) or s0)
            dur = max(e0 - s0, 0.0)
        except Exception:
            s0, e0, dur = 0.0, 0.0, 0.0
        step = dur / len(tokens) if tokens else 0.0
        for idx, tok in enumerate(tokens):
            w_start = s0 + idx * step
            w_end = w_start + step if step > 0 else w_start
            word_texts.append(tok)
            word_spans.append((w_start, w_end))
    if not word_texts or len(word_texts) != len(word_spans):
        return [seg]

    segment_text = seg.get("text") or seg.get("tr_text") or ""
    tags = classify_words_with_llm(word_texts, segment_text)
    if not isinstance(tags, list) or len(tags) != len(word_texts):
        seg_copy = dict(seg)
        seg_copy["word_tags"] = ["turkish_speech"] * len(word_texts)
        if not seg_copy.get("words"):
            seg_copy["words"] = [
                {"word": wt, "start": ws, "end": we}
                for wt, (ws, we) in zip(word_texts, word_spans)
            ]
        return [seg_copy]

    # --- Heuristic smoothing: no dictionary, just patterns & context ---
    def has_arabic_chars(w: str) -> bool:
        return any("\u0600" <= ch <= "\u06FF" for ch in w)

    def has_turkish_chars(w: str) -> bool:
        return any(ch in "çğıöşüÇĞİÖŞÜ" for ch in w)

    # 0) Script rule: if a token has Arabic script, force Arabic
    for i, w in enumerate(word_texts):
        if has_arabic_chars(w):
            tags[i] = "arabic_prayer_or_quran"

    # 1) Split into sentence-like groups by punctuation
    sentence_groups: List[List[int]] = []
    current_group: List[int] = []
    punct_chars = [".", ",", "?", "!", ";", ":", "…"]

    for idx, w in enumerate(word_texts):
        current_group.append(idx)
        if any(ch in w for ch in punct_chars):
            sentence_groups.append(current_group)
            current_group = []
    if current_group:
        sentence_groups.append(current_group)

    smoothed = tags[:]

    for group in sentence_groups:
        if not group:
            continue
        group_tags = [tags[i] for i in group]
        total = len(group_tags)
        ar_indices = [i for i in group if tags[i] == "arabic_prayer_or_quran"]
        ar_count = len(ar_indices)
        if total == 0:
            continue

        ar_ratio = ar_count / total

        # 2a) Single Arabic in an otherwise Turkish sentence (no Arabic script) -> demote
        if ar_count == 1:
            gi = ar_indices[0]
            w = word_texts[gi]
            if not has_arabic_chars(w):
                smoothed[gi] = "turkish_speech"
            continue

        # 2b) Small phrase with high Arabic ratio -> promote all to Arabic
        if 2 <= ar_count <= total and total <= 7 and ar_ratio >= 0.4:
            for gi in group:
                smoothed[gi] = "arabic_prayer_or_quran"
            continue

        # 2c) No Arabic tags but the phrase looks phonetic-Arabic (short block, no Turkish diacritics)
        if ar_count == 0 and total <= 6:
            has_turkish = any(has_turkish_chars(word_texts[i]) for i in group)
            if not has_turkish:
                for gi in group:
                    smoothed[gi] = "arabic_prayer_or_quran"

    # 3) Collapse runs of the same tag to produce contiguous segments
    segments: List[Dict[str, Any]] = []
    if not smoothed:
        return [seg]
    curr_tag = smoothed[0]
    curr_words = [word_texts[0]]
    curr_tags = [smoothed[0]]
    curr_start, curr_end = word_spans[0]
    for i in range(1, len(smoothed)):
        tag = smoothed[i]
        wtxt = word_texts[i]
        wstart, wend = word_spans[i]
        if tag == curr_tag:
            curr_words.append(wtxt)
            curr_tags.append(tag)
            curr_end = wend
        else:
            segments.append(
                {
                    "start": curr_start,
                    "end": curr_end,
                    "duration": max(curr_end - curr_start, 0.0),
                    "text": " ".join(curr_words).strip(),
                    "label": curr_tag,
                    "word_tags": curr_tags[:],
                    "words": [
                        {"word": wt, "start": ws, "end": we}
                        for wt, (ws, we) in zip(curr_words, [word_spans[j] for j in range(i - len(curr_words), i)])
                    ],
                }
            )
            curr_tag = tag
            curr_words = [wtxt]
            curr_tags = [tag]
            curr_start, curr_end = wstart, wend
    segments.append(
        {
            "start": curr_start,
            "end": curr_end,
            "duration": max(curr_end - curr_start, 0.0),
            "text": " ".join(curr_words).strip(),
            "label": curr_tag,
            "word_tags": curr_tags[:],
            "words": [
                {"word": wt, "start": ws, "end": we}
                for wt, (ws, we) in zip(curr_words, word_spans[-len(curr_words):])
            ],
        }
    )
    # Also attach full-length word_tags if no split happened
    if len(segments) == 1 and len(curr_words) == len(word_texts):
        segments[0]["word_tags"] = smoothed
    return segments


def classify_segments_with_llm(segments: List[Dict[str, Any]]) -> List[str]:
    """
    Classify segments as Turkish speech vs Arabic prayer/Quran.
    """
    if not segments:
        return []
    if not _openai_client:
        return ["turkish_speech"] * len(segments)

    prompt = {
        "role": "system",
        "content": (
            "You will classify Turkish lecture segments as either Turkish speech or Arabic/Quran recitation.\n"
            "Return a list of labels, same length as input, using only these labels:\n"
            "- turkish_speech\n"
            "- arabic_prayer_or_quran\n"
            "- other\n\n"
            "Be conservative: only use arabic_prayer_or_quran when the text clearly indicates Quran recitation or Arabic prayer words."
        ),
    }

    user_segments = []
    for seg in segments:
        txt = seg.get("text") or seg.get("tr_text") or seg.get("ar_text") or ""
        user_segments.append({"start": seg.get("start"), "text": txt})
    resp = _openai_client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[prompt, {"role": "user", "content": json.dumps(user_segments)}],
        response_format={"type": "json_object"},
    )
    try:
        raw = resp.choices[0].message.content if resp.choices else ""
        data = json.loads(raw or "{}")
        labels = data.get("labels") or data.get("tags") or data.get("result") or []
        out = []
        for lbl in labels:
            if lbl in {"turkish_speech", "arabic_prayer_or_quran", "other"}:
                out.append(lbl)
            else:
                out.append("turkish_speech")
        if len(out) != len(segments):
            out = ["turkish_speech"] * len(segments)
        return out
    except Exception:
        return ["turkish_speech"] * len(segments)


def classify_words_with_llm(words: List[str], segment_text: str = "", batch_size: int = 80) -> List[str]:
    """
    Tag each word as Turkish vs Arabic prayer/Quran using an LLM classifier.
    """
    if not words:
        return []
    if not _openai_client:
        return ["turkish_speech"] * len(words)

    default_tags = ["turkish_speech"] * len(words)

    def looks_turkish_charwise(word: str) -> bool:
        turkish_chars = "çğıöşüÇĞİÖŞÜ"
        if any(ch in turkish_chars for ch in word):
            return True
        lower = word.lower().strip(".,!?;:\"'()[]")
        short_turkish = {"ve", "mi", "de", "da", "ki", "bu", "şu", "o", "ben", "sen", "biz", "siz"}
        if lower in short_turkish:
            return True
        return False

    def postprocess_labels(words_list: List[str], labels_list: List[str]) -> List[str]:
        """
        If LLM marked everything as turkish_speech but there is a run of 3+ long,
        non-Turkish-looking tokens, mark that block as arabic_prayer_or_quran.
        """
        n = len(words_list)
        if len(labels_list) != n:
            return labels_list
        labels_mut = list(labels_list)
        i = 0
        while i < n:
            if labels_mut[i] != "turkish_speech":
                i += 1
                continue
            j = i
            block_indices = []
            while j < n and labels_mut[j] == "turkish_speech":
                w = str(words_list[j] or "")
                if (not looks_turkish_charwise(w)) and len(w) >= 6:
                    block_indices.append(j)
                    j += 1
                else:
                    break
            if len(block_indices) >= 3:
                for k in block_indices:
                    labels_mut[k] = "arabic_prayer_or_quran"
                i = block_indices[-1] + 1
            else:
                i += 1
        return labels_mut

    def batch(iterable, size):
        for i in range(0, len(iterable), size):
            yield i, iterable[i:i + size]

    batch_default = ["turkish_speech"] * batch_size
    all_tags: List[str] = []
    for i, batch_words in batch(words, batch_size):
        prompt = {
            "role": "system",
            "content": (
                "You are a word-level language tagger for subtitles of Turkish religious lectures.\n"
                "\n"
                "Input:\n"
                "- JSON with two fields:\n"
                "  - 'segment_text': full transcript text of the segment (usually Turkish, sometimes contains Arabic Quran or duaa).\n"
                "  - 'words': array of token strings in the same order as in 'segment_text'.\n"
                "\n"
                "Your task:\n"
                "- For EACH word in 'words', output ONE label indicating what kind of content it is.\n"
                "- The output MUST be a JSON object with a single key 'labels', whose value is an array of strings.\n"
                "- The length of 'labels' MUST be exactly the same as the length of 'words'.\n"
                "\n"
                "Allowed labels:\n"
                "- 'turkish_speech'           : normal spoken Turkish words or Turkish sentences.\n"
                "- 'arabic_prayer_or_quran'   : Quran recitation or Arabic duaa words, even if written in Latin letters.\n"
                "- 'mixed'                    : a single token that clearly mixes Arabic and Turkish in one word.\n"
                "- 'other'                    : non speech tokens like sound effects, music markers, emojis, noise, etc.\n"
                "\n"
                "GENERAL PRINCIPLES:\n"
                "- Use 'turkish_speech' for normal Turkish sentences and common religious vocabulary used as Turkish speech.\n"
                "- Examples that are usually 'turkish_speech' when inside a Turkish sentence:\n"
                "  Allah, Rabb, peygamber, cennet, cehennem, namaz, dua, sabır, rahmet, ihlas, takva, imtihan, vb.\n"
                "- Never mark an entire clearly Turkish sentence as 'arabic_prayer_or_quran' just because it contains 'Allah'.\n"
                "\n"
                "EVERYDAY TURKISH EXPRESSIONS WITH ALLAH:\n"
                "- Short everyday phrases like:\n"
                "  'Allah esirgesin', 'Allah razı olsun', 'Allah yardımcımız olsun'\n"
                "  should be labeled word by word as 'turkish_speech' when used as normal Turkish speech.\n"
                "\n"
                "INVOCATIONS COMMONLY USED IN TURKISH:\n"
                "- Some Arabic-origin invocations are common in Turkish and may be spoken as part of normal Turkish speech.\n"
                "- When such an invocation appears once and the speaker immediately continues in Turkish,\n"
                "  you may still label that short phrase as 'turkish_speech' word by word so it stays in Turkish Latin script.\n"
                "- Only when a phrase is clearly part of a longer continuous Quran recitation or fully Arabic duaa block\n"
                "  together with several other Arabic-looking words, you should label those words as 'arabic_prayer_or_quran'.\n"
                "\n"
                "WHEN TO USE 'arabic_prayer_or_quran':\n"
                "- Use 'arabic_prayer_or_quran' when a sequence of several consecutive words:\n"
                "  * does not look like normal Turkish morphology,\n"
                "  * would not be grammatical Turkish if read as Turkish,\n"
                "  * sounds like flowing Arabic recitation or duaa when read together.\n"
                "- In such clearly Arabic-looking blocks, each token in that block should be 'arabic_prayer_or_quran'.\n"
                "- Turkish sentences before or after that block MUST stay 'turkish_speech'.\n"
                "\n"
                "MIXED CASES:\n"
                "- A Turkish sentence that quotes a short Arabic phrase should be split logically:\n"
                "  Arabic block inside the quote => 'arabic_prayer_or_quran'\n"
                "  Turkish words before or after => 'turkish_speech'.\n"
                "\n"
                "WHEN TO USE 'mixed':\n"
                "- Use 'mixed' only if a single token clearly combines Arabic and Turkish in one word.\n"
                "- If you are not sure, prefer 'turkish_speech' over 'mixed'.\n"
                "\n"
                "WHEN TO USE 'other':\n"
                "- Use 'other' for tokens that are not normal words in Turkish or Arabic speech\n"
                "  (for example: [music], [applause], pure emojis, raw URLs, editor notes).\n"
                "\n"
                "IMPORTANT CONSTRAINTS:\n"
                "- Do NOT translate, rewrite, or invent new words or sentences.\n"
                "- You only assign labels to the given 'words' array.\n"
                "- Use the surrounding 'segment_text' as context to decide if a token is part of Turkish speech\n"
                "  or part of an Arabic Quran or duaa phrase.\n"
                "- If three or more consecutive tokens are not normal Turkish words and together they form\n"
                "  a phrase that clearly does not follow Turkish grammar, you should strongly prefer\n"
                "  labeling that whole block as 'arabic_prayer_or_quran' instead of 'turkish_speech'.\n"
                "\n"
                "OUTPUT FORMAT:\n"
                "- Return ONLY a JSON object like:\n"
                "  {\"labels\": [\"turkish_speech\", \"turkish_speech\", \"arabic_prayer_or_quran\", ...]}\n"
                "  with no extra commentary.\n"
            ),
        }
        user_payload = {
            "segment_text": segment_text,
            "words": batch_words,
        }
        try:
            resp = _openai_client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[
                    prompt,
                    {"role": "user", "content": json.dumps(user_payload)},
                ],
                response_format={"type": "json_object"},
            )
            raw = resp.choices[0].message.content if resp.choices else ""
            data = json.loads(raw or "{}")
            tags = data.get("labels") or data.get("tags") or data.get("result") or []
            cleaned = []
            for t in tags:
                t_str = str(t or "").strip()
                if t_str in {"turkish_speech", "arabic_prayer_or_quran", "mixed", "other"}:
                    cleaned.append(t_str)
                else:
                    cleaned.append("turkish_speech")
            all_tags.extend(cleaned)
        except Exception as e:
            try:
                current_app.logger.warning("LLM word classify batch failed (idx %s-%s): %s", i, i + len(batch_words), e)
            except Exception:
                pass
            all_tags.extend(batch_default)

    if len(all_tags) != len(words):
        try:
            current_app.logger.warning("[WORD_TAG] output length mismatch %s vs %s, using defaults", len(all_tags), len(words))
        except Exception:
            pass
        return default_tags

    all_tags = postprocess_labels(words, all_tags)
    return all_tags


def _normalize_segments_for_use(raw_segments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    for seg in raw_segments or []:
        if not isinstance(seg, dict):
            continue
        text = seg.get("text") or ""
        tr_text = seg.get("tr_text") or text
        ar_text = seg.get("ar_text")
        label = seg.get("label")
        try:
            start = float(seg.get("start", 0.0))
        except Exception:
            start = 0.0
        end_val = seg.get("end")
        try:
            end = float(end_val) if end_val is not None else None
        except Exception:
            end = None
        dur_val = seg.get("duration")
        try:
            duration = float(dur_val) if dur_val is not None else None
        except Exception:
            duration = None
        if end is None and duration is not None:
            end = start + max(duration, 0.0)
        if duration is None and end is not None:
            duration = max(end - start, 0.0)
        lang = seg.get("lang") or seg.get("language")
        if label == "arabic_prayer_or_quran":
            lang = "ar"
        if not lang:
            lang = infer_lang_from_text(ar_text or tr_text or text)
        # If we already have an Arabic script text, trust that to mark Arabic.
        if ar_text:
            lang = "ar"
        normalized.append(
            {
                "start": start,
                "end": end if end is not None else start + max(duration or 0.0, 0.0),
                "duration": duration if duration is not None else 0.0,
                "text": tr_text or text or ar_text or "",
                "tr_text": tr_text or text or ar_text or "",
                "ar_text": ar_text,
                "label": label,
                "lang": lang,
                "words": seg.get("words"),
                "word_tags": seg.get("word_tags"),
            }
        )
    return normalized


def _refine_arabic_segments_with_whisper(segments: List[Dict[str, Any]], video_path: Path) -> List[Dict[str, Any]]:
    """
    For segments tagged as Arabic, re-run Whisper with language='ar' on that time window to get Arabic script.
    """
    if not _openai_client:
        return segments
    updated = []
    for seg in segments:
        seg_copy = dict(seg)
        tr_txt = seg_copy.get("tr_text") or seg_copy.get("text") or ""
        lang_val = (seg_copy.get("lang") or "").lower()
        needs_ar_refine = lang_val == "ar" or bool(seg_copy.get("force_ar"))
        if not needs_ar_refine:
            updated.append(seg_copy)
            continue
        try:
            start = float(seg_copy.get("start", 0.0))
            end = seg_copy.get("end")
            dur = seg_copy.get("duration")
            if end is None:
                end = start + max(float(dur or 0.0), 0.0)
            else:
                end = float(end)
            if end <= start:
                raise ValueError("invalid segment timing")
        except Exception:
            updated.append(seg_copy)
            continue

        snippet_path = None
        try:
            snippet_path = _extract_audio_segment(video_path, start, end)
            with snippet_path.open("rb") as f:
                resp = _openai_client.audio.transcriptions.create(
                    model=WHISPER_MODEL,
                    file=f,
                    response_format="text",
                    language="ar",
                )
            refined_txt = ""
            if hasattr(resp, "text"):
                refined_txt = resp.text or ""
            else:
                refined_txt = str(resp or "")
            refined_txt = str(refined_txt or "").strip()
            if refined_txt:
                seg_copy["ar_text"] = refined_txt
                if not seg_copy.get("text"):
                    seg_copy["text"] = refined_txt
                seg_copy["lang"] = "ar"
        except Exception as e:
            try:
                current_app.logger.warning("Arabic refinement failed for segment %.2f-%.2f: %s", start, end, e)
            except Exception:
                pass
        finally:
            if snippet_path and snippet_path.exists():
                try:
                    snippet_path.unlink()
                except Exception:
                    pass
        updated.append(seg_copy)
    return updated


def _fetch_transcript(conn, video_id: str) -> Tuple[str, List[Dict[str, Any]]]:
    cols = _ensure_transcript_schema(conn)
    has_whisper_col = "whisper_segments_json" in cols
    select_sql = (
        "SELECT full_text, segments_json, whisper_segments_json FROM youtube_transcripts WHERE video_id = ?"
        if has_whisper_col
        else "SELECT full_text, segments_json, NULL as whisper_segments_json FROM youtube_transcripts WHERE video_id = ?"
    )
    row = conn.execute(select_sql, [video_id]).fetchone()
    if not row:
        return "", []
    full_text = row[0] or ""
    segments_raw_json = row[2] if has_whisper_col and row[2] else row[1]
    segments: List[Dict[str, Any]] = []
    if segments_raw_json:
        try:
            parsed = json.loads(segments_raw_json)
            if isinstance(parsed, list):
                segments = parsed
        except Exception:
            segments = []
    return full_text, _normalize_segments_for_use(segments)


def _whisper_response_to_segments(resp: Any) -> Tuple[str, List[Dict[str, Any]]]:
    # Preserve Whisper's full text as-is
    try:
        full_text_raw = getattr(resp, "text", None)
    except Exception:
        full_text_raw = None
    full_text = str(full_text_raw or "").strip()

    def _word_to_dict(w):
        if isinstance(w, dict):
            return {
                "word": w.get("word"),
                "start": w.get("start"),
                "end": w.get("end"),
            }
        try:
            return {
                "word": getattr(w, "word", None),
                "start": getattr(w, "start", None),
                "end": getattr(w, "end", None),
            }
        except Exception:
            return {"word": None, "start": None, "end": None}

    def _segment_to_dict(seg: Any) -> Dict[str, Any]:
        words_raw = _seg_val(seg, "words") or []
        words_list = []
        if isinstance(words_raw, list):
            for w in words_raw:
                wd = _word_to_dict(w)
                if wd:
                    words_list.append(wd)
        start_val = _seg_val(seg, "start", 0.0) or 0.0
        end_val = _seg_val(seg, "end")
        dur_val = _seg_val(seg, "duration")
        text_val = _seg_val(seg, "text") or ""
        # Synthesize word timings if missing but text exists
        if (not words_list) and text_val:
            tokens = [t for t in text_val.strip().split() if t]
            try:
                s0 = float(start_val)
            except Exception:
                s0 = 0.0
            try:
                e0 = float(end_val) if end_val is not None else None
            except Exception:
                e0 = None
            try:
                d0 = float(dur_val) if dur_val is not None else None
            except Exception:
                d0 = None
            if e0 is None and d0 is not None:
                e0 = s0 + max(d0, 0.0)
            if d0 is None and e0 is not None:
                d0 = max(e0 - s0, 0.0)
            if d0 is None:
                d0 = 0.0
            step = (d0 / len(tokens)) if tokens else 0.0
            cur = s0
            for tok in tokens:
                w_start = cur
                w_end = cur + step if step > 0 else cur
                words_list.append({"word": tok, "start": w_start, "end": w_end})
                cur = w_end
        return {
            "start": start_val,
            "end": end_val,
            "duration": dur_val,
            "text": text_val,
            "words": words_list,
        }

    segments = []
    try:
        raw_segments = getattr(resp, "segments", None) or []
    except Exception:
        raw_segments = []
    for s in raw_segments:
        try:
            segments.append(_segment_to_dict(s))
        except Exception:
            continue
    return full_text, segments


def _offset_segments(segments: List[Dict[str, Any]], offset: float) -> None:
    if not offset:
        return
    for seg in segments:
        for key in ("start", "end"):
            if seg.get(key) is not None:
                try:
                    seg[key] = float(seg[key]) + offset
                except Exception:
                    pass
        words = seg.get("words")
        if isinstance(words, list):
            for w in words:
                if not isinstance(w, dict):
                    continue
                for key in ("start", "end"):
                    if w.get(key) is not None:
                        try:
                            w[key] = float(w[key]) + offset
                        except Exception:
                            pass


def _transcribe_with_whisper(
    video_path: Path,
    progress_cb: Optional[Callable[[str, str, Dict[str, Any]], None]] = None,
) -> Tuple[str, List[Dict[str, Any]]]:
    def _emit(stage: str, message: str, **extra: Any) -> None:
        if not progress_cb:
            return
        try:
            progress_cb(stage, message, extra)
        except Exception:
            pass

    _emit("start", "Transcription job started.")
    if not _openai_client:
        raise RuntimeError("OPENAI_API_KEY is missing; cannot run Whisper.")
    if not video_path.exists():
        raise FileNotFoundError(f"Video not found at {video_path}")

    _emit("language_probe", "Detecting primary audio language.")
    transcription_mode = _detect_primary_audio_language(video_path)
    if transcription_mode == "en":
        _emit("language_mode", "Language detected: English. Running English flow.")
    elif transcription_mode == "tr":
        _emit("language_mode", "Language detected: Turkish. Running Turkish flow.")
    else:
        _emit("language_mode", "Language uncertain. Running auto mode.")

    audio_jobs: List[Tuple[Path, float]] = []
    cleanup_paths: List[Path] = []
    cleanup_dirs = set()
    audio_ref_path: Path = video_path

    try:
        _emit("prepare_audio", "Preparing audio for Whisper.")
        audio_path = _prepare_audio_for_whisper(video_path)
        audio_jobs = [(audio_path, 0.0)]
        cleanup_paths.append(audio_path)
        audio_ref_path = audio_path
        _emit("prepare_audio_done", "Audio prepared.")
    except RuntimeError as exc:
        if "too large" not in str(exc).lower():
            raise
        _emit("prepare_chunks", "Source is large; splitting into chunks.")
        audio_jobs = _prepare_audio_chunks_for_whisper(video_path)
        cleanup_paths.extend([p for p, _ in audio_jobs])
        for p in cleanup_paths:
            if p.parent.name.startswith("whisper_chunks_"):
                cleanup_dirs.add(p.parent)
        audio_ref_path = video_path
        _emit("prepare_chunks_done", "Chunking complete.", chunk_count=len(audio_jobs))

    full_text_parts: List[str] = []
    segments: List[Dict[str, Any]] = []
    chunk_languages: List[str] = []
    total_chunks = len(audio_jobs)
    _emit("whisper_start", "Sending audio to Whisper.", chunk_count=total_chunks)
    for index, (audio_path, offset) in enumerate(audio_jobs, start=1):
        _emit(
            "whisper_chunk_start",
            f"Transcribing chunk {index}/{total_chunks}.",
            chunk_index=index,
            chunk_count=total_chunks,
        )
        with audio_path.open("rb") as f:
            request_payload: Dict[str, Any] = {
                "model": WHISPER_MODEL,
                "file": f,
                "response_format": "verbose_json",
                "timestamp_granularities": ["segment", "word"],  # ensure segment + word timings
            }
            if transcription_mode in {"en", "tr"}:
                request_payload["language"] = transcription_mode
            resp = _openai_client.audio.transcriptions.create(
                **request_payload,
            )
        chunk_lang = _extract_transcription_language(resp)
        if chunk_lang:
            chunk_languages.append(chunk_lang)
        chunk_text, chunk_segments = _whisper_response_to_segments(resp)
        _offset_segments(chunk_segments, offset)
        if chunk_text:
            full_text_parts.append(chunk_text)
        segments.extend(chunk_segments)
        _emit(
            "whisper_chunk_done",
            f"Chunk {index}/{total_chunks} transcribed.",
            chunk_index=index,
            chunk_count=total_chunks,
            segment_count=len(chunk_segments),
        )

    full_text = " ".join(full_text_parts).strip()

    def _overlap(a, b):
        return a[0] < b[1] and b[0] < a[1]

    def _seg_bounds(seg):
        try:
            s = float(_seg_val(seg, "start", 0.0) or 0.0)
        except Exception:
            s = 0.0
        e_val = _seg_val(seg, "end")
        try:
            e = float(e_val) if e_val is not None else None
        except Exception:
            e = None
        d_val = _seg_val(seg, "duration")
        try:
            d = float(d_val) if d_val is not None else None
        except Exception:
            d = None
        if e is None:
            e = s + max(d or 0.0, 0.0)
        if d is None:
            d = max(e - s, 0.0)
        return s, e, d

    # Convert whisper segments to normal form (defensive against object or dict)
    normalized = []
    for seg in segments:
        try:
            start = float(_seg_val(seg, "start", 0.0) or 0.0)
        except Exception:
            start = 0.0
        dur_val = _seg_val(seg, "duration")
        try:
            duration = float(dur_val) if dur_val is not None else None
        except Exception:
            duration = None
        end_val = _seg_val(seg, "end")
        try:
            end = float(end_val) if end_val is not None else None
        except Exception:
            end = None
        if end is None and duration is not None:
            end = start + max(duration, 0.0)
        if duration is None and end is not None:
            duration = max(end - start, 0.0)
        normalized.append(
            {
                "start": start,
                "end": end,
                "duration": duration,
                "text": _seg_val(seg, "text") or "",
                "words": _seg_val(seg, "words") or [],
            }
        )

    # Keep Whisper transcripts exactly as produced for every language.
    # Downstream subtitle and clip flows expect normalized segments, but they do
    # not require the slower language-classification or Arabic refinement passes.
    en_chunks = sum(1 for lang in chunk_languages if lang == "en")
    looks_english = _looks_english_text(full_text)
    english_mode = (
        transcription_mode == "en"
        or (
            transcription_mode != "tr"
            and ((en_chunks and en_chunks >= max(1, len(chunk_languages) // 2)) or looks_english)
        )
    )
    detected_lang = "en" if english_mode else (transcription_mode if transcription_mode in {"en", "tr"} else "")
    if not detected_lang:
        inferred_lang = infer_lang_from_text(full_text or "")
        detected_lang = "en" if inferred_lang == "en" else "tr"

    _emit("language_passthrough", "Preserving Whisper output.")
    segments: List[Dict[str, Any]] = []
    for seg in normalized:
        seg_text = (seg.get("text") or "").strip()
        segments.append(
            {
                "start": seg.get("start", 0.0),
                "end": seg.get("end"),
                "duration": seg.get("duration"),
                "lang": detected_lang,
                "label": "speech",
                "tr_text": seg_text,
                "ar_text": None,
                "text": seg_text,
                "words": seg.get("words"),
                "word_tags": None,
            }
        )

    _emit("cleanup", "Cleaning temporary files.")
    # Cleanup temp audio if created
    if cleanup_paths:
        for path in cleanup_paths:
            try:
                path.unlink()
            except Exception:
                pass
        for tmp_dir in cleanup_dirs:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    _emit("done", "Transcription finished.", segment_count=len(segments))
    return str(full_text), segments
