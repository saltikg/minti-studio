#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PIL import Image

from app.video_shorts.routes.generation import _normalize_overlay_shapes
from app.video_shorts.services.compositor import _render_overlay_shapes


def check(name: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{name}: {detail}")
    print(f"PASS {name}{(' ' + detail) if detail else ''}")


def bounds_for(image: Image.Image, predicate) -> tuple[int, int, int, int]:
    pixels = image.load()
    points = [
        (x, y)
        for y in range(image.height)
        for x in range(image.width)
        if predicate(pixels[x, y])
    ]
    return min(x for x, _ in points), min(y for _, y in points), max(x for x, _ in points) + 1, max(y for _, y in points) + 1


def main() -> int:
    shapes = _normalize_overlay_shapes([
        {"id": "bottom", "x": 0, "y": 1600, "w": 1080, "h": 200, "color": "#000000", "alpha": 100, "radius": 0},
        {"id": "middle", "x": 270, "y": 700, "w": 540, "h": 240, "color": "#FF0000", "alpha": 60, "radius": 40},
    ])
    check("normalize_count", len(shapes) == 2)
    check("normalize_clamps", _normalize_overlay_shapes([{"id": "x", "x": -5, "y": 1900, "w": 1200, "h": 300, "color": "bad", "alpha": 150, "radius": 999}])[0] == {
        "id": "x", "x": 0, "y": 1620, "w": 1080, "h": 300, "color": "#000000", "alpha": 100, "radius": 150,
    })
    path = _render_overlay_shapes(shapes, target_width=720, target_height=1280)
    check("png_created", bool(path and path.exists()))
    try:
        image = Image.open(path).convert("RGBA")
        bottom = bounds_for(image, lambda pixel: pixel[3] > 250)
        red = bounds_for(image, lambda pixel: pixel[0] > 200 and pixel[1] < 40 and 140 <= pixel[3] <= 170)
        check("bottom_bounds", all(abs(value - expected) <= 2 for value, expected in zip(bottom, (0, 1067, 720, 1200))), str(bottom))
        check("middle_bounds", all(abs(value - expected) <= 2 for value, expected in zip(red, (180, 467, 540, 627))), str(red))
    finally:
        if path:
            path.unlink(missing_ok=True)
    check("max_five", len(_normalize_overlay_shapes([{"id": str(i), "w": 1, "h": 1} for i in range(8)])) == 5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
