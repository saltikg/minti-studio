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


def _write_wav(path: Path, samples: np.ndarray) -> None:
    pcm = np.clip(samples, -1.0, 1.0)
    pcm16 = (pcm * 32767.0).astype(np.int16)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(pcm16.tobytes())


def _tone(seconds: float, *, frequency: float = 440.0) -> np.ndarray:
    samples = int(round(seconds * SAMPLE_RATE))
    t = np.arange(samples, dtype=np.float32) / float(SAMPLE_RATE)
    return (np.sin(2.0 * np.pi * frequency * t) * 0.6).astype(np.float32)


def _silence(seconds: float) -> np.ndarray:
    return np.zeros(int(round(seconds * SAMPLE_RATE)), dtype=np.float32)


def _check(name: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{name} failed {detail}".strip())
    print(f"PASS {name}{(' ' + detail) if detail else ''}")


def main() -> None:
    app = create_app()
    with app.app_context(), tempfile.TemporaryDirectory() as tmp_raw:
        tmp = Path(tmp_raw)
        tone_silence_tone = tmp / "tone_silence_tone.wav"
        _write_wav(tone_silence_tone, np.concatenate([_tone(1.0), _silence(0.35), _tone(1.0)]))
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
    print("MULTI_RANGE_AUDIO_SNAP_OK")


if __name__ == "__main__":
    main()
