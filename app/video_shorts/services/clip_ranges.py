from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Optional


RANGE_END_OVERSHOOT_TOLERANCE_SECONDS = 1.5
UNKNOWN_MEDIA_DURATION_FALLBACK_SECONDS = 1.0
RANGE_END_ERROR = "Selection goes past the end of the video."


def _as_float(value: Any) -> Optional[float]:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed else None


def effective_media_end(
    media_duration_seconds: Any,
    fallback_duration_seconds: Any,
) -> Optional[float]:
    media_duration = _as_float(media_duration_seconds)
    if media_duration is not None and media_duration > 0:
        return media_duration
    fallback_duration = _as_float(fallback_duration_seconds)
    if fallback_duration is not None and fallback_duration > 0:
        return fallback_duration + UNKNOWN_MEDIA_DURATION_FALLBACK_SECONDS
    return None


@dataclass(frozen=True)
class ClipRangeResolution:
    ok: bool
    start: float
    end: float
    ranges: list[dict[str, Any]]
    error: str = ""
    clamped: bool = False


def resolve_clip_ranges(
    *,
    start: Any,
    end: Any,
    keep_ranges: Optional[Iterable[dict[str, Any]]] = None,
    media_duration_seconds: Any = None,
    fallback_duration_seconds: Any = None,
    transcript_word_selection: bool = False,
    overshoot_tolerance_seconds: float = RANGE_END_OVERSHOOT_TOLERANCE_SECONDS,
) -> ClipRangeResolution:
    """Normalize and clamp clip ranges against the exact playable media end.

    Transcript-word selections are always accepted and trimmed because speech
    timestamps can drift beyond the source. Manually supplied times retain a
    bounded tolerance so obviously invalid input still gets a useful error.
    """
    parsed_start = _as_float(start)
    parsed_end = _as_float(end)
    if parsed_start is None or parsed_end is None or parsed_end <= parsed_start:
        return ClipRangeResolution(False, 0.0, 0.0, [], "End time must be greater than Start.")

    raw_ranges: list[dict[str, Any]] = []
    for item in keep_ranges or []:
        if not isinstance(item, dict):
            continue
        range_start = _as_float(item.get("start"))
        range_end = _as_float(item.get("end"))
        if range_start is None or range_end is None or range_end <= range_start:
            continue
        parsed = {"start": range_start, "end": range_end}
        try:
            first_index = int(item.get("start_word_index"))
            last_index = int(item.get("end_word_index"))
        except (TypeError, ValueError):
            first_index = last_index = -1
        if first_index >= 0 and last_index >= first_index:
            parsed["start_word_index"] = first_index
            parsed["end_word_index"] = last_index
        raw_ranges.append(parsed)
    if not raw_ranges:
        raw_ranges = [{"start": parsed_start, "end": parsed_end}]

    media_end = effective_media_end(media_duration_seconds, fallback_duration_seconds)
    max_requested_end = max(item["end"] for item in raw_ranges)
    if media_end is not None:
        overshoot = max_requested_end - media_end
        if overshoot > max(0.0, float(overshoot_tolerance_seconds)) and not transcript_word_selection:
            return ClipRangeResolution(False, 0.0, 0.0, [], RANGE_END_ERROR)

    resolved: list[dict[str, Any]] = []
    clamped = False
    for item in raw_ranges:
        range_start = max(0.0, item["start"])
        range_end = max(0.0, item["end"])
        if range_start != item["start"] or range_end != item["end"]:
            clamped = True
        if media_end is not None:
            bounded_start = min(media_end, range_start)
            bounded_end = min(media_end, range_end)
            clamped = clamped or bounded_start != range_start or bounded_end != range_end
            range_start, range_end = bounded_start, bounded_end
        if range_end <= range_start:
            clamped = True
            continue
        parsed = {"start": round(range_start, 6), "end": round(range_end, 6)}
        if "start_word_index" in item and "end_word_index" in item:
            parsed["start_word_index"] = item["start_word_index"]
            parsed["end_word_index"] = item["end_word_index"]
        resolved.append(parsed)

    if not resolved:
        message = RANGE_END_ERROR if media_end is not None else "The selected clip has no usable duration."
        return ClipRangeResolution(False, 0.0, 0.0, [], message, clamped)

    return ClipRangeResolution(
        True,
        resolved[0]["start"],
        resolved[-1]["end"],
        resolved,
        clamped=clamped,
    )
