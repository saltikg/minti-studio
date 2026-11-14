"""Shared caption placement geometry for preview and rendered overlays."""

from __future__ import annotations

from typing import Any, Iterable

from app.video_shorts.config import DESIGN_GEOMETRY


def split_caption_line_offsets(
    line_glyph_boxes: Iterable[Iterable[Any]],
    *,
    frame_height: float,
) -> list[float]:
    """Return per-line Y offsets that keep glyphs clear of the split divider.

    A single line ends above the divider.  For multiple lines, the final line
    starts below it while every preceding line keeps its original stack and the
    last of that upper stack ends above it.
    """
    boxes: list[tuple[float, float, float, float]] = []
    for raw_box in line_glyph_boxes or []:
        try:
            left, top, right, bottom = (float(value) for value in raw_box)
        except (TypeError, ValueError):
            continue
        if right <= left or bottom <= top:
            continue
        boxes.append((left, top, right, bottom))
    if not boxes:
        return []

    height = max(1.0, float(frame_height))
    divider_y = height * float(DESIGN_GEOMETRY["split_ratio"])
    gap = height * float(DESIGN_GEOMETRY["split_caption_gap_ratio"])
    upper_bottom = divider_y - gap
    lower_top = divider_y + gap
    if len(boxes) == 1:
        return [upper_bottom - boxes[0][3]]

    upper_offset = upper_bottom - boxes[-2][3]
    lower_offset = lower_top - boxes[-1][1]
    return [upper_offset] * (len(boxes) - 1) + [lower_offset]
