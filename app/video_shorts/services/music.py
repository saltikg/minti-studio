from __future__ import annotations

import math
import tempfile
from array import array
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from app.video_shorts.config import FFMPEG_SHORT_TIMEOUT, FFPROBE_TIMEOUT
from app.video_shorts.services.media_utils import _resolve_ffmpeg, run_media_subprocess


DEFAULT_MUSIC_LEVEL = 50
MUSIC_DUCK_DB = -10.0
MUSIC_DUCK_RAMP_SECONDS = 0.15
MUSIC_WAVEFORM_PEAK_COUNT = 400


def normalize_music_level(value: Any, default: int = DEFAULT_MUSIC_LEVEL) -> int:
    try:
        level = int(round(float(value)))
    except (TypeError, ValueError):
        level = int(default)
    return max(0, min(100, level))


def normalize_music_start_seconds(value: Any) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        seconds = 0.0
    if not math.isfinite(seconds):
        seconds = 0.0
    return max(0.0, seconds)


def music_db_for_level(value: Any) -> float:
    level = normalize_music_level(value)
    if level <= 0:
        return float("-inf")
    return 40.0 * ((float(level) / 100.0) - 1.0)


def music_linear_gain(value: Any) -> float:
    db = music_db_for_level(value)
    if not math.isfinite(db):
        return 0.0
    return math.pow(10.0, db / 20.0)


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _flatten_words(segments: Iterable[Mapping[str, Any]]) -> list[tuple[float, float]]:
    words: list[tuple[float, float]] = []
    for segment in segments or []:
        if not isinstance(segment, Mapping):
            continue
        segment_start = _number(segment.get("start")) or 0.0
        segment_end = _number(segment.get("end"))
        raw_words = segment.get("words") or []
        if isinstance(raw_words, Sequence) and raw_words:
            for word in raw_words:
                if not isinstance(word, Mapping):
                    continue
                start = _number(word.get("start"))
                end = _number(word.get("end"))
                if start is None:
                    continue
                if end is None:
                    end = start + 0.05
                if start < segment_start - 0.25 and segment_start > 0:
                    start += segment_start
                    end += segment_start
                if end > start:
                    words.append((start, end))
        elif segment_end is not None and segment_end > segment_start:
            words.append((segment_start, segment_end))
    return sorted(words)


def _normalize_keep_ranges(raw_ranges: Any) -> list[tuple[float, float]]:
    normalized: list[tuple[float, float]] = []
    if not isinstance(raw_ranges, Sequence) or isinstance(raw_ranges, (str, bytes)):
        return normalized
    for item in raw_ranges:
        if not isinstance(item, Mapping):
            continue
        start = _number(item.get("start"))
        end = _number(item.get("end"))
        if start is None or end is None or end <= start:
            continue
        normalized.append((max(0.0, start), max(0.0, end)))
    normalized.sort()
    return normalized


def build_final_timeline_speech_ranges(
    segments: Iterable[Mapping[str, Any]],
    *,
    clip_start: Any,
    clip_end: Any,
    edit_keep_ranges: Any = None,
    source_duration: Any = None,
    start_padding: float = 0.25,
    end_padding: float = 0.5,
    merge_gap: float = 0.4,
) -> list[dict[str, float]]:
    """Return merged speech ranges on the exact timeline sent to the compositor."""
    start = max(0.0, _number(clip_start) or 0.0)
    end = max(start, _number(clip_end) or start)
    words = _flatten_words(segments)
    keep_ranges = _normalize_keep_ranges(edit_keep_ranges)
    mapped: list[tuple[float, float]] = []
    if len(keep_ranges) > 1:
        output_offset = 0.0
        for keep_start, keep_end in keep_ranges:
            for word_start, word_end in words:
                overlap_start = max(word_start, keep_start)
                overlap_end = min(word_end, keep_end)
                if overlap_end > overlap_start:
                    mapped.append(
                        (
                            output_offset + overlap_start - keep_start,
                            output_offset + overlap_end - keep_start,
                        )
                    )
            output_offset += keep_end - keep_start
        timeline_duration = output_offset
    else:
        timeline_start = max(0.0, start - max(0.0, float(start_padding)))
        timeline_end = end + max(0.0, float(end_padding))
        source_limit = _number(source_duration)
        if source_limit is not None and source_limit > 0:
            timeline_end = min(timeline_end, source_limit)
        timeline_duration = max(0.0, timeline_end - timeline_start)
        for word_start, word_end in words:
            overlap_start = max(word_start, timeline_start)
            overlap_end = min(word_end, timeline_end)
            if overlap_end > overlap_start:
                mapped.append((overlap_start - timeline_start, overlap_end - timeline_start))
    if not mapped or timeline_duration <= 0:
        return []
    merged: list[list[float]] = []
    for raw_start, raw_end in sorted(mapped):
        range_start = max(0.0, min(timeline_duration, raw_start))
        range_end = max(range_start, min(timeline_duration, raw_end))
        if range_end <= range_start:
            continue
        if merged and range_start - merged[-1][1] < float(merge_gap):
            merged[-1][1] = max(merged[-1][1], range_end)
        else:
            merged.append([range_start, range_end])
    return [
        {"start": round(item[0], 3), "end": round(item[1], 3)}
        for item in merged
    ]


def music_volume_expression(
    level: Any,
    speech_ranges: Iterable[Mapping[str, Any]],
    *,
    duck: bool,
    ramp_seconds: float = MUSIC_DUCK_RAMP_SECONDS,
) -> str:
    base_gain = music_linear_gain(level)
    if base_gain <= 0:
        return "0"
    if not duck:
        return f"{base_gain:.9f}"
    duck_gain = math.pow(10.0, MUSIC_DUCK_DB / 20.0)
    ramp = max(0.01, float(ramp_seconds))
    expression = f"{base_gain:.9f}"
    valid_ranges: list[tuple[float, float]] = []
    for item in speech_ranges or []:
        if not isinstance(item, Mapping):
            continue
        start = _number(item.get("start"))
        end = _number(item.get("end"))
        if start is None or end is None or end <= start:
            continue
        valid_ranges.append((max(0.0, start), max(0.0, end)))
    for start, end in reversed(valid_ranges):
        pre_start = max(0.0, start - ramp)
        post_end = end + ramp
        ducked = base_gain * duck_gain
        pre_expr = (
            f"{base_gain:.9f}-(t-{pre_start:.3f})/{ramp:.3f}*"
            f"{(base_gain - ducked):.9f}"
        )
        post_expr = (
            f"{ducked:.9f}+(t-{end:.3f})/{ramp:.3f}*"
            f"{(base_gain - ducked):.9f}"
        )
        expression = (
            f"if(between(t,{start:.3f},{end:.3f}),{ducked:.9f},"
            f"if(between(t,{pre_start:.3f},{start:.3f}),{pre_expr},"
            f"if(between(t,{end:.3f},{post_end:.3f}),{post_expr},{expression})))"
        )
    return expression


def normalize_audio_file(source: Path, *, suffix: str) -> Path:
    codec_by_suffix = {
        ".mp3": "libmp3lame",
        ".wav": "pcm_s16le",
        ".m4a": "aac",
        ".aac": "aac",
        ".ogg": "libvorbis",
        ".flac": "flac",
    }
    normalized_suffix = suffix.lower() if suffix else source.suffix.lower()
    handle = tempfile.NamedTemporaryFile(prefix="minti-music-normalized-", suffix=normalized_suffix, delete=False)
    handle.close()
    output = Path(handle.name)
    cmd = [
        _resolve_ffmpeg(),
        "-y",
        "-i",
        str(source),
        "-vn",
        "-af",
        "loudnorm=I=-14:TP=-1.5:LRA=11",
        "-c:a",
        codec_by_suffix.get(normalized_suffix, "aac"),
        str(output),
    ]
    run_media_subprocess(
        cmd,
        operation="normalize_music_upload",
        context=f"source={source.name}",
        output_paths=[output],
        check=True,
        capture_output=True,
        timeout=FFMPEG_SHORT_TIMEOUT,
    )
    if not output.exists() or output.stat().st_size <= 0:
        output.unlink(missing_ok=True)
        raise RuntimeError("Normalized music output is empty.")
    return output


def compute_audio_waveform(path: Path, *, peak_count: int = MUSIC_WAVEFORM_PEAK_COUNT) -> dict[str, Any]:
    ffmpeg = _resolve_ffmpeg()
    ffprobe = str(Path(ffmpeg).with_name("ffprobe"))
    probe = run_media_subprocess(
        [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
        operation="probe_music_duration",
        context=f"source={path.name}",
        check=True,
        capture_output=True,
        text=True,
        timeout=FFPROBE_TIMEOUT,
    )
    try:
        duration = max(0.0, float((probe.stdout or "0").strip()))
    except (TypeError, ValueError):
        duration = 0.0
    decoded = run_media_subprocess(
        [ffmpeg, "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", "8000", "-f", "s16le", "pipe:1"],
        operation="decode_music_waveform",
        context=f"source={path.name}",
        check=True,
        capture_output=True,
        timeout=FFMPEG_SHORT_TIMEOUT,
    )
    samples = array("h")
    samples.frombytes(decoded.stdout or b"")
    count = max(1, int(peak_count))
    if not samples:
        peaks = [0.0] * count
    else:
        bucket_size = max(1, math.ceil(len(samples) / count))
        raw_peaks = [
            max((abs(sample) for sample in samples[offset:offset + bucket_size]), default=0) / 32768.0
            for offset in range(0, len(samples), bucket_size)
        ][:count]
        peaks = raw_peaks + ([0.0] * max(0, count - len(raw_peaks)))
    return {
        "duration_seconds": round(duration, 3),
        "peaks": [round(max(0.0, min(1.0, peak)), 4) for peak in peaks],
    }
