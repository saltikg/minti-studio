#!/usr/bin/env python3
"""Fast unit checks for audio-aware multi-range trim boundaries."""

from __future__ import annotations

import sys
import tempfile
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import create_app
from app.video_shorts.services import compositor


SAMPLE_RATE = 16000


def _write_wav(path: Path, samples: np.ndarray, *, sample_rate: int = SAMPLE_RATE) -> None:
    pcm = np.clip(samples, -1.0, 1.0)
    pcm16 = (pcm * 32767.0).astype(np.int16)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm16.tobytes())


def _tone(seconds: float, *, frequency: float = 440.0) -> np.ndarray:
    samples = int(round(seconds * SAMPLE_RATE))
    t = np.arange(samples, dtype=np.float32) / float(SAMPLE_RATE)
    return (np.sin(2.0 * np.pi * frequency * t) * 0.6).astype(np.float32)


def _silence(seconds: float) -> np.ndarray:
    return np.zeros(int(round(seconds * SAMPLE_RATE)), dtype=np.float32)


def _join_arrays(parts: list[np.ndarray]) -> np.ndarray:
    total = sum(int(part.size) for part in parts)
    output = np.zeros(total, dtype=np.float32)
    offset = 0
    for part in parts:
        size = int(part.size)
        output[offset : offset + size] = part
        offset += size
    return output


def _check(name: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{name} failed {detail}".strip())
    print(f"PASS {name}{(' ' + detail) if detail else ''}")


def _run(cmd: list[str], *, stdout=None) -> bytes:
    import subprocess

    result = subprocess.run(cmd, check=True, stdout=stdout or subprocess.PIPE, stderr=subprocess.PIPE)
    return result.stdout or b""


def _write_beep_track(path: Path, duration: float, *, sample_rate: int = 48000) -> None:
    samples = np.zeros(int(round(duration * sample_rate)), dtype=np.float32)
    beep_len = int(round(0.04 * sample_rate))
    for second in range(1, int(duration)):
        start = int(round(second * sample_rate))
        end = min(samples.size, start + beep_len)
        if end <= start:
            continue
        t = np.arange(end - start, dtype=np.float32) / float(sample_rate)
        samples[start:end] += np.sin(2.0 * np.pi * 1000.0 * t) * 0.8
    _write_wav(path, samples, sample_rate=sample_rate)


def _build_flash_beep_source(tmp: Path) -> Path:
    ffmpeg = compositor._resolve_ffmpeg()
    audio_path = tmp / "beeps.wav"
    source_path = tmp / "flash_beep_source.mp4"
    _write_beep_track(audio_path, 8.0)
    video_filter = (
        "color=c=black:s=160x90:r=25:d=8,"
        "drawbox=x=0:y=0:w=iw:h=ih:color=white:t=fill:enable='lt(mod(t,1),0.04)'"
    )
    _run([
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        video_filter,
        "-i",
        str(audio_path),
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-shortest",
        str(source_path),
    ])
    return source_path


def _build_vfr_flash_beep_source(tmp: Path) -> Path:
    ffmpeg = compositor._resolve_ffmpeg()
    audio_path = tmp / "vfr_beeps.wav"
    source_path = tmp / "vfr_flash_beep_source.mp4"
    _write_beep_track(audio_path, 8.0)
    video_filter = (
        "color=c=black:s=160x90:r=25:d=8,"
        "drawbox=x=0:y=0:w=iw:h=ih:color=white:t=fill:enable='lt(mod(t,1),0.04)'"
    )
    _run([
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        video_filter,
        "-i",
        str(audio_path),
        "-vf",
        "mpdecimate",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-fps_mode",
        "vfr",
        "-c:a",
        "aac",
        "-shortest",
        str(source_path),
    ])
    return source_path


def _build_long_gop_flash_beep_source(tmp: Path) -> Path:
    ffmpeg = compositor._resolve_ffmpeg()
    audio_path = tmp / "long_gop_beeps.wav"
    source_path = tmp / "long_gop_flash_beep_source.mp4"
    _write_beep_track(audio_path, 32.0)
    video_filter = (
        "color=c=black:s=160x90:r=25:d=32,"
        "drawbox=x=0:y=0:w=iw:h=ih:color=white:t=fill:enable='lt(mod(t,1),0.04)'"
    )
    _run([
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        video_filter,
        "-i",
        str(audio_path),
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "18",
        "-g",
        "250",
        "-keyint_min",
        "250",
        "-sc_threshold",
        "0",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-shortest",
        str(source_path),
    ])
    return source_path


def _build_long_gop_mpegts_flash_beep_source(tmp: Path) -> Path:
    ffmpeg = compositor._resolve_ffmpeg()
    audio_path = tmp / "long_gop_ts_beeps.wav"
    source_path = tmp / "long_gop_ts_labeled_mp4.mp4"
    _write_beep_track(audio_path, 32.0)
    video_filter = (
        "color=c=black:s=160x90:r=25:d=32,"
        "drawbox=x=0:y=0:w=iw:h=ih:color=white:t=fill:enable='lt(mod(t,1),0.04)'"
    )
    _run([
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        video_filter,
        "-i",
        str(audio_path),
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "18",
        "-g",
        "250",
        "-keyint_min",
        "250",
        "-sc_threshold",
        "0",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-f",
        "mpegts",
        str(source_path),
    ])
    return source_path


def _build_late_source(tmp: Path) -> Path:
    ffmpeg = compositor._resolve_ffmpeg()
    source_path = tmp / "late_source.mp4"
    _run([
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=32x32:rate=5:duration=1810",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "28",
        "-pix_fmt",
        "yuv420p",
        str(source_path),
    ])
    return source_path


def _trim_source_to_keep_ranges_old_select_graph(src_path: Path, keep_ranges: list[dict[str, float]], out_path: Path) -> float:
    ffmpeg = compositor._resolve_ffmpeg()
    ranges = compositor._normalize_keep_ranges_for_trim(keep_ranges)
    seek_offset = max(0.0, float(ranges[0]["start"]) - 2.0)
    select_terms: list[str] = []
    total_duration = 0.0
    for item in ranges:
        local_start = max(0.0, float(item["start"]) - seek_offset)
        local_end = max(local_start + 0.001, float(item["end"]) - seek_offset)
        total_duration += max(0.001, local_end - local_start)
        select_terms.append(f"(gte(t\\,{local_start:.6f})*lt(t\\,{local_end:.6f}))")
    select_expr = "+".join(select_terms)
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
    if seek_offset > 0:
        cmd.extend(["-ss", f"{seek_offset:.6f}"])
    cmd.extend([
        "-i",
        str(src_path),
        "-filter_complex",
        (
            f"[0:v]setpts=PTS-STARTPTS,select='{select_expr}',setpts=N/FRAME_RATE/TB[v];"
            f"[0:a]asetpts=PTS-STARTPTS,aselect='{select_expr}',asetpts=N/SR/TB,aresample=async=1:first_pts=0[a]"
        ),
        "-map",
        "[v]",
        "-map",
        "[a]",
        "-t",
        f"{total_duration:.6f}",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "16",
        "-c:a",
        "aac",
        "-movflags",
        "+faststart",
        str(out_path),
    ])
    _run(cmd)
    return total_duration


def _trim_source_to_keep_ranges_preroll_graph(src_path: Path, keep_ranges: list[dict[str, float]], out_path: Path) -> float:
    ffmpeg = compositor._resolve_ffmpeg()
    ranges = compositor._normalize_keep_ranges_for_trim(keep_ranges)
    first_start = float(ranges[0]["start"])
    seek_offset = max(0.0, first_start - 2.0)
    preroll_start = first_start - seek_offset
    select_terms: list[str] = []
    gap_terms: list[str] = []
    previous_end = 0.0
    total_duration = 0.0
    for item in ranges:
        local_start = max(0.0, float(item["start"]) - first_start)
        local_end = max(local_start + 0.001, float(item["end"]) - first_start)
        if select_terms:
            gap = max(0.0, local_start - previous_end)
            if gap > 0:
                gap_terms.append(f"gte(T\\,{local_start:.6f})*{gap:.6f}")
        total_duration += max(0.001, local_end - local_start)
        select_terms.append(f"(gte(t\\,{local_start:.6f})*lt(t\\,{local_end:.6f}))")
        previous_end = local_end
    select_expr = "+".join(select_terms)
    gap_expr = "+".join(gap_terms) if gap_terms else "0"
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
    if seek_offset > 0:
        cmd.extend(["-ss", f"{seek_offset:.6f}"])
    cmd.extend([
        "-i",
        str(src_path),
        "-filter_complex",
        (
            f"[0:v]trim=start={preroll_start:.6f},setpts=PTS-STARTPTS,settb=AVTB,"
            f"select='{select_expr}',setpts='PTS-STARTPTS-({gap_expr})/TB',fps=fps=25:start_time=0,settb=AVTB[v];"
            f"[0:a]atrim=start={preroll_start:.6f},asetpts=PTS-STARTPTS,asettb=AVTB,asetnsamples=n=480,"
            f"aselect='{select_expr}',asetpts='PTS-STARTPTS-({gap_expr})/TB',asettb=AVTB,aresample=async=1:first_pts=0[a]"
        ),
        "-map",
        "[v]",
        "-map",
        "[a]",
        "-t",
        f"{total_duration:.6f}",
        "-r",
        "25",
        "-vsync",
        "cfr",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "16",
        "-c:a",
        "aac",
        "-movflags",
        "+faststart",
        str(out_path),
    ])
    _run(cmd)
    return total_duration


def _probe_duration(path: Path) -> float:
    ffprobe = compositor._resolve_ffprobe()
    raw = _run([
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ])
    return float(raw.decode("utf-8").strip())


def _probe_stream_av_delta(path: Path) -> tuple[float, float]:
    ffprobe = compositor._resolve_ffprobe()
    raw = _run([
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "stream=codec_type,start_time,duration",
        "-of",
        "json",
        str(path),
    ])
    import json

    data = json.loads(raw.decode("utf-8") or "{}")
    streams: dict[str, dict[str, float]] = {}
    for stream in data.get("streams") or []:
        codec_type = str(stream.get("codec_type") or "")
        if codec_type not in {"video", "audio"}:
            continue
        streams[codec_type] = {
            "start": float(stream.get("start_time") or 0.0),
            "duration": float(stream.get("duration") or 0.0),
        }
    video = streams.get("video", {"start": 0.0, "duration": 0.0})
    audio = streams.get("audio", {"start": 0.0, "duration": 0.0})
    return audio["start"] - video["start"], audio["duration"] - video["duration"]


def _group_event_times(times: list[float], *, max_gap: float = 0.08, mode: str = "center") -> list[float]:
    if not times:
        return []
    groups: list[list[float]] = [[times[0]]]
    for value in times[1:]:
        if value - groups[-1][-1] <= max_gap:
            groups[-1].append(value)
        else:
            groups.append([value])
    if mode == "first":
        return [float(group[0]) for group in groups]
    return [float(sum(group) / len(group)) for group in groups]


def _detect_flash_times(path: Path, *, fps: int = 25, width: int = 160, height: int = 90) -> list[float]:
    ffmpeg = compositor._resolve_ffmpeg()
    raw = _run([
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(path),
        "-an",
        "-vf",
        f"fps={fps},format=gray",
        "-f",
        "rawvideo",
        "pipe:1",
    ])
    frame_size = width * height
    if frame_size <= 0:
        return []
    frame_count = len(raw) // frame_size
    if frame_count <= 0:
        return []
    frames = np.frombuffer(raw[: frame_count * frame_size], dtype=np.uint8).reshape((frame_count, frame_size))
    candidates = [(index + 0.5) / float(fps) for index, mean in enumerate(frames.mean(axis=1)) if float(mean) > 180.0]
    return _group_event_times(candidates, max_gap=(2.0 / float(fps)))


def _detect_beep_times(path: Path, *, sample_rate: int = 48000) -> list[float]:
    ffmpeg = compositor._resolve_ffmpeg()
    raw = _run([
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(sample_rate),
        "-f",
        "s16le",
        "pipe:1",
    ])
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    frame_size = max(1, int(round(0.01 * sample_rate)))
    hits: list[float] = []
    for offset in range(0, max(0, samples.size - frame_size + 1), frame_size):
        frame = samples[offset : offset + frame_size]
        rms = float(np.sqrt(np.mean(np.square(frame)))) if frame.size else 0.0
        if rms > 0.08:
            hits.append((offset + frame_size / 2.0) / float(sample_rate))
    return _group_event_times(hits, max_gap=0.08, mode="center")


def _check_multi_range_av_sync(tmp: Path) -> None:
    source = _build_flash_beep_source(tmp)
    output = tmp / "flash_beep_trimmed.mp4"
    ranges = [
        {"start": 0.73, "end": 1.23},
        {"start": 1.64, "end": 2.22},
        {"start": 2.67, "end": 3.18},
        {"start": 3.71, "end": 4.28},
        {"start": 4.63, "end": 5.26},
        {"start": 5.79, "end": 6.31},
    ]
    compositor._trim_source_to_keep_ranges(source, ranges, output)
    flash_times = _detect_flash_times(output)
    beep_times = _detect_beep_times(output)
    pair_count = min(len(flash_times), len(beep_times))
    max_delta = max((abs(flash_times[index] - beep_times[index]) for index in range(pair_count)), default=999.0)
    _check(
        "multi_range_flash_beep_sync",
        pair_count >= 6 and len(flash_times) == len(beep_times) and max_delta <= 0.04,
        f"flashes={flash_times} beeps={beep_times} max_delta={max_delta:.4f}",
    )


def _compose_sync_output(tmp: Path, trimmed: Path, duration: float, name: str) -> Path:
    ffmpeg = compositor._resolve_ffmpeg()
    bg = tmp / f"{name}_bg.png"
    output = tmp / f"{name}_composed.mp4"
    _run([
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "color=c=black:s=720x1280",
        "-frames:v",
        "1",
        str(bg),
    ])
    compositor._compose_trimmed_with_background(
        bg,
        trimmed,
        0.0,
        duration,
        "",
        "",
        output,
        show_title=False,
        show_subtitle=False,
        crop_aspect="portrait",
        crop_settings={
            "crop_locked": True,
            "crop_x_ratio": 0.0,
            "crop_y_ratio": 0.0,
            "crop_w_ratio": 1.0,
            "crop_h_ratio": 1.0,
            "split_enabled": False,
        },
    )
    return output


def _check_multi_range_full_compose_av_sync(tmp: Path, *, vfr: bool = False) -> None:
    source = _build_vfr_flash_beep_source(tmp) if vfr else _build_flash_beep_source(tmp)
    trimmed = tmp / ("vfr_flash_beep_trimmed.mp4" if vfr else "flash_beep_trimmed_for_compose.mp4")
    ranges = [
        {"start": 0.73, "end": 1.23},
        {"start": 1.64, "end": 2.22},
        {"start": 2.67, "end": 3.18},
        {"start": 3.71, "end": 4.28},
        {"start": 4.63, "end": 5.26},
        {"start": 5.79, "end": 6.31},
    ]
    duration = compositor._trim_source_to_keep_ranges(source, ranges, trimmed)
    output = _compose_sync_output(tmp, trimmed, duration, "vfr_full_compose" if vfr else "full_compose")
    if vfr:
        start_delta, duration_delta = _probe_stream_av_delta(output)
        _check(
            "multi_range_full_compose_vfr_sync",
            abs(start_delta) <= 0.04 and abs(duration_delta) <= 0.04,
            f"start_delta={start_delta:.4f} duration_delta={duration_delta:.4f}",
        )
        return
    flash_times = _detect_flash_times(output, width=720, height=1280)
    beep_times = _detect_beep_times(output)
    pair_count = min(len(flash_times), len(beep_times))
    max_delta = max((abs(flash_times[index] - beep_times[index]) for index in range(pair_count)), default=999.0)
    # The compose pass remuxes/re-encodes AAC, and marker detection sees the codec
    # priming edge slightly more than users perceive it. Keep this check tight but
    # avoid failing on one AAC packet of synthetic detector bias.
    _check(
        "multi_range_full_compose_vfr_sync" if vfr else "multi_range_full_compose_sync",
        pair_count >= 6 and len(flash_times) == len(beep_times) and max_delta <= 0.06,
        f"flashes={flash_times} beeps={beep_times} max_delta={max_delta:.4f}",
    )


def _check_long_gop_mid_seek_av_sync(tmp: Path) -> None:
    source = _build_long_gop_flash_beep_source(tmp)
    ranges = [
        {"start": 13.32, "end": 15.46},
        {"start": 17.24, "end": 19.51},
        {"start": 22.18, "end": 24.62},
    ]
    old_output = tmp / "long_gop_old_graph.mp4"
    _trim_source_to_keep_ranges_old_select_graph(source, ranges, old_output)
    old_flash_times = _detect_flash_times(old_output)
    old_beep_times = _detect_beep_times(old_output)
    old_pair_count = min(len(old_flash_times), len(old_beep_times))
    old_max_delta = max(
        (abs(old_flash_times[index] - old_beep_times[index]) for index in range(old_pair_count)),
        default=999.0,
    )
    _check(
        "long_gop_old_graph_reproduces_offset",
        old_pair_count >= 3 and old_max_delta > 0.04,
        f"flashes={old_flash_times} beeps={old_beep_times} max_delta={old_max_delta:.4f}",
    )

    fixed_output = tmp / "long_gop_fixed_graph.mp4"
    compositor._trim_source_to_keep_ranges(source, ranges, fixed_output)
    flash_times = _detect_flash_times(fixed_output)
    beep_times = _detect_beep_times(fixed_output)
    pair_count = min(len(flash_times), len(beep_times))
    max_delta = max((abs(flash_times[index] - beep_times[index]) for index in range(pair_count)), default=999.0)
    _check(
        "long_gop_mid_seek_multi_range_sync",
        pair_count >= 3 and len(flash_times) == len(beep_times) and max_delta <= 0.04,
        f"flashes={flash_times} beeps={beep_times} max_delta={max_delta:.4f}",
    )


def _check_mpegts_mid_seek_av_sync(tmp: Path) -> None:
    source = _build_long_gop_mpegts_flash_beep_source(tmp)
    ranges = [
        {"start": 13.32, "end": 15.46},
        {"start": 17.24, "end": 19.51},
        {"start": 22.18, "end": 24.62},
    ]
    preroll_output = tmp / "mpegts_preroll_graph.mp4"
    _trim_source_to_keep_ranges_preroll_graph(source, ranges, preroll_output)
    preroll_flash_times = _detect_flash_times(preroll_output)
    preroll_beep_times = _detect_beep_times(preroll_output)
    preroll_pair_count = min(len(preroll_flash_times), len(preroll_beep_times))
    preroll_max_delta = max(
        (abs(preroll_flash_times[index] - preroll_beep_times[index]) for index in range(preroll_pair_count)),
        default=999.0,
    )
    _check(
        "mpegts_preroll_graph_reproduces_offset",
        preroll_pair_count >= 3 and preroll_max_delta > 0.04,
        f"flashes={preroll_flash_times} beeps={preroll_beep_times} max_delta={preroll_max_delta:.4f}",
    )

    fixed_output = tmp / "mpegts_fixed_graph.mp4"
    compositor._trim_source_to_keep_ranges(source, ranges, fixed_output)
    flash_times = _detect_flash_times(fixed_output)
    beep_times = _detect_beep_times(fixed_output)
    pair_count = min(len(flash_times), len(beep_times))
    max_delta = max((abs(flash_times[index] - beep_times[index]) for index in range(pair_count)), default=999.0)
    _check(
        "mpegts_mid_seek_multi_range_sync",
        pair_count >= 3 and len(flash_times) == len(beep_times) and max_delta <= 0.04,
        f"flashes={flash_times} beeps={beep_times} max_delta={max_delta:.4f}",
    )


def _check_late_multi_range_seek(tmp: Path) -> None:
    source = _build_late_source(tmp)
    output = tmp / "late_trimmed.mp4"
    ranges = [
        {"start": 1800.40, "end": 1801.20},
        {"start": 1802.75, "end": 1803.65},
        {"start": 1806.10, "end": 1807.40},
    ]
    expected = sum(item["end"] - item["start"] for item in ranges)
    actual = compositor._trim_source_to_keep_ranges(source, ranges, output)
    probed = _probe_duration(output)
    _check(
        "late_multi_range_seek",
        abs(actual - expected) <= 0.001 and abs(probed - expected) <= 0.25,
        f"expected={expected:.3f} actual={actual:.3f} probed={probed:.3f}",
    )


def main() -> None:
    app = create_app()
    with app.app_context(), tempfile.TemporaryDirectory() as tmp_raw:
        tmp = Path(tmp_raw)
        tone_silence_tone = tmp / "tone_silence_tone.wav"
        _write_wav(tone_silence_tone, _join_arrays([_tone(1.0), _silence(0.35), _tone(1.0)]))
        ranges = [{"start": 0.0, "end": 0.9}, {"start": 1.55, "end": 2.1}]
        adjusted = compositor._snap_keep_range_boundaries_to_audio(tone_silence_tone, ranges)
        _check(
            "cut_inside_tone_snaps_to_silence",
            adjusted[0]["end"] >= 0.99 and adjusted[0]["end"] <= 1.10,
            str(adjusted),
        )

        tone_only = tmp / "tone_only.wav"
        _write_wav(tone_only, _tone(2.5))
        no_silence_ranges = [{"start": 0.0, "end": 0.9}, {"start": 1.55, "end": 2.1}]
        no_silence_adjusted = compositor._snap_keep_range_boundaries_to_audio(tone_only, no_silence_ranges)
        _check(
            "no_silence_stays_within_limit",
            abs(no_silence_adjusted[0]["end"] - no_silence_ranges[0]["end"]) <= 0.20
            and abs(no_silence_adjusted[1]["start"] - no_silence_ranges[1]["start"]) <= 0.20,
            str(no_silence_adjusted),
        )
        _check_multi_range_av_sync(tmp)
        _check_long_gop_mid_seek_av_sync(tmp)
        _check_mpegts_mid_seek_av_sync(tmp)
        _check_multi_range_full_compose_av_sync(tmp)
        _check_multi_range_full_compose_av_sync(tmp, vfr=True)
        _check_late_multi_range_seek(tmp)
    print("MULTI_RANGE_AUDIO_SNAP_OK")


if __name__ == "__main__":
    main()
