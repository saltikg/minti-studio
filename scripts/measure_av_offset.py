#!/usr/bin/env python3
"""Measure source-vs-output A/V alignment by matching video frames and audio windows."""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import numpy as np


def _run(cmd: list[str], *, text: bool = False) -> bytes | str:
    result = subprocess.run(cmd, check=True, capture_output=True, text=text)
    return result.stdout if text else result.stdout


def _duration(path: Path) -> float:
    raw = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        text=True,
    )
    return float(str(raw).strip() or 0.0)


def _load_gray_frame(path: Path, t: float, *, width: int, height: int) -> np.ndarray:
    raw = _run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{max(0.0, t):.6f}",
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-vf",
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,format=gray",
            "-f",
            "rawvideo",
            "pipe:1",
        ]
    )
    expected = width * height
    if len(raw) < expected:
        raise RuntimeError(f"Could not read frame at {t:.3f}s from {path}")
    return np.frombuffer(raw[:expected], dtype=np.uint8).astype(np.int16)


def _load_gray_frames(path: Path, start: float, duration: float, *, fps: float, width: int, height: int) -> list[tuple[float, np.ndarray]]:
    raw = _run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{max(0.0, start):.6f}",
            "-t",
            f"{max(0.001, duration):.6f}",
            "-i",
            str(path),
            "-vf",
            f"fps={fps:.6f},scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,format=gray",
            "-f",
            "rawvideo",
            "pipe:1",
        ]
    )
    frame_size = width * height
    count = len(raw) // frame_size
    frames: list[tuple[float, np.ndarray]] = []
    for index in range(count):
        offset = index * frame_size
        timestamp = start + (index / fps)
        frame = np.frombuffer(raw[offset : offset + frame_size], dtype=np.uint8).astype(np.int16)
        frames.append((timestamp, frame))
    return frames


def _load_audio(path: Path, start: float, duration: float, *, sample_rate: int) -> np.ndarray:
    raw = _run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{max(0.0, start):.6f}",
            "-t",
            f"{max(0.001, duration):.6f}",
            "-i",
            str(path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            "-f",
            "f32le",
            "pipe:1",
        ]
    )
    return np.frombuffer(raw, dtype=np.float32)


def _parse_ranges(raw: str) -> list[dict[str, float]]:
    parsed = json.loads(raw)
    ranges: list[dict[str, float]] = []
    for item in parsed:
        start = float(item["start"])
        end = float(item["end"])
        if end > start:
            ranges.append({"start": start, "end": end})
    ranges.sort(key=lambda item: item["start"])
    if not ranges:
        raise ValueError("No valid ranges supplied")
    return ranges


def _source_time_for_output(output_t: float, ranges: list[dict[str, float]]) -> float:
    cursor = 0.0
    for item in ranges:
        duration = item["end"] - item["start"]
        if output_t <= cursor + duration:
            return item["start"] + max(0.0, output_t - cursor)
        cursor += duration
    return ranges[-1]["end"]


def _sample_points(duration: float, count: int = 6) -> list[float]:
    if duration <= 0:
        return []
    margin = min(1.0, max(0.05, duration * 0.05))
    if duration <= margin * 2:
        return [duration / 2.0]
    return [margin + ((duration - margin * 2) * (i + 0.5) / count) for i in range(count)]


def _best_video_match(source: Path, output_frame: np.ndarray, expected_source_t: float, *, window: float, fps: float, width: int, height: int) -> tuple[float, float]:
    start = max(0.0, expected_source_t - window)
    frames = _load_gray_frames(source, start, window * 2.0, fps=fps, width=width, height=height)
    if not frames:
        raise RuntimeError("No source frames decoded for video match")
    best_t = frames[0][0]
    best_score = math.inf
    for timestamp, frame in frames:
        score = float(np.mean(np.abs(frame - output_frame)))
        if score < best_score:
            best_score = score
            best_t = timestamp
    return best_t, best_score


def _best_audio_match(source: Path, output: Path, output_t: float, expected_source_t: float, *, window: float, sample_rate: int) -> tuple[float, float]:
    clip_len = 1.0
    out_start = max(0.0, output_t - clip_len / 2.0)
    src_start = max(0.0, expected_source_t - window - clip_len / 2.0)
    source_audio = _load_audio(source, src_start, window * 2.0 + clip_len, sample_rate=sample_rate)
    output_audio = _load_audio(output, out_start, clip_len, sample_rate=sample_rate)
    if source_audio.size < output_audio.size or output_audio.size < sample_rate // 10:
        raise RuntimeError("Not enough audio decoded for correlation")
    output_audio = output_audio - float(np.mean(output_audio))
    source_audio = source_audio - float(np.mean(source_audio))
    denom = float(np.linalg.norm(output_audio))
    if denom <= 1e-9:
        raise RuntimeError("Output audio window is near silence")
    corr = np.correlate(source_audio, output_audio, mode="valid")
    index = int(np.argmax(corr))
    score = float(corr[index] / max(1e-9, denom * float(np.linalg.norm(source_audio[index : index + output_audio.size]))))
    return src_start + (index / float(sample_rate)) + (clip_len / 2.0), score


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--ranges-json", required=True, help="Source keep ranges matching the output timeline")
    parser.add_argument("--points", type=int, default=6)
    parser.add_argument("--window", type=float, default=2.0)
    parser.add_argument("--video-fps", type=float, default=10.0)
    parser.add_argument("--width", type=int, default=160)
    parser.add_argument("--height", type=int, default=90)
    parser.add_argument("--sample-rate", type=int, default=16000)
    args = parser.parse_args()

    ranges = _parse_ranges(args.ranges_json)
    output_duration = _duration(args.output)
    rows: list[dict[str, Any]] = []
    for output_t in _sample_points(output_duration, args.points):
        expected_source_t = _source_time_for_output(output_t, ranges)
        try:
            output_frame = _load_gray_frame(args.output, output_t, width=args.width, height=args.height)
            video_source_t, video_score = _best_video_match(
                args.source,
                output_frame,
                expected_source_t,
                window=args.window,
                fps=args.video_fps,
                width=args.width,
                height=args.height,
            )
            video_offset = video_source_t - expected_source_t
        except Exception as exc:
            video_source_t = None
            video_score = None
            video_offset = None
            video_error = str(exc)
        else:
            video_error = None
        try:
            audio_source_t, audio_score = _best_audio_match(
                args.source,
                args.output,
                output_t,
                expected_source_t,
                window=args.window,
                sample_rate=args.sample_rate,
            )
            audio_offset = audio_source_t - expected_source_t
        except Exception as exc:
            audio_source_t = None
            audio_score = None
            audio_offset = None
            audio_error = str(exc)
        else:
            audio_error = None
        av_offset = None
        if video_source_t is not None and audio_source_t is not None:
            av_offset = video_source_t - audio_source_t
        rows.append(
            {
                "output_t": round(output_t, 4),
                "expected_source_t": round(expected_source_t, 4),
                "video_source_t": None if video_source_t is None else round(video_source_t, 4),
                "audio_source_t": None if audio_source_t is None else round(audio_source_t, 4),
                "video_offset": None if video_offset is None else round(video_offset, 4),
                "audio_offset": None if audio_offset is None else round(audio_offset, 4),
                "av_offset": None if av_offset is None else round(av_offset, 4),
                "video_score": None if video_score is None else round(video_score, 3),
                "audio_score": None if audio_score is None else round(audio_score, 3),
                "video_error": video_error,
                "audio_error": audio_error,
            }
        )
    print(json.dumps({"output": str(args.output), "duration": output_duration, "ranges": ranges, "rows": rows}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
