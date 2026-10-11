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
    return _group_event_times(hits, max_gap=0.08, mode="first")


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
    print("MULTI_RANGE_AUDIO_SNAP_OK")


if __name__ == "__main__":
    main()
