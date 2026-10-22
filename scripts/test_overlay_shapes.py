#!/usr/bin/env python3
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PIL import Image

from app.video_shorts.routes.generation import _normalize_overlay_shapes
from app.video_shorts.services.compositor import _render_overlay_shapes
from app.video_shorts.services.overlay_assets import OverlayAssetError, normalize_overlay_image_upload


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
    upload_source = Image.new("RGBA", (2160, 540), (50, 205, 181, 128))
    from io import BytesIO
    upload_buffer = BytesIO()
    upload_source.save(upload_buffer, format="PNG")
    normalized_upload, normalized_width, normalized_height = normalize_overlay_image_upload(upload_buffer.getvalue(), "logo.png")
    check("upload_size_cap", (normalized_width, normalized_height) == (1080, 270))
    check("upload_alpha_preserved", Image.open(BytesIO(normalized_upload)).convert("RGBA").getpixel((10, 10))[3] == 128)
    try:
        svg_upload, svg_width, svg_height = normalize_overlay_image_upload(
            b'<svg xmlns="http://www.w3.org/2000/svg" width="400" height="200"><rect width="400" height="200" fill="#50cdb5"/></svg>',
            "logo.svg",
        )
        check("svg_rasterized", (svg_width, svg_height) == (400, 200) and Image.open(BytesIO(svg_upload)).format == "PNG")
    except OverlayAssetError as exc:
        if "could not be converted" not in str(exc):
            raise
        print("SKIP svg_rasterized (local Cairo library unavailable)")
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
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as image_handle:
        image_path = Path(image_handle.name)
    try:
        logo = Image.new("RGBA", (200, 100), (255, 0, 0, 0))
        for x in range(20, 180):
            for y in range(10, 90):
                logo.putpixel((x, y), (255, 0, 0, 255))
        logo.save(image_path)
        image_shape = _normalize_overlay_shapes([
            {"type": "image", "id": "logo", "asset_id": "asset-1", "x": 860, "y": 40, "w": 180, "h": 90, "alpha": 50, "aspect_locked": True},
        ])
        check("image_normalized", image_shape == [{"id": "logo", "x": 860, "y": 40, "w": 180, "h": 90, "alpha": 50, "type": "image", "asset_id": "asset-1", "aspect_locked": True}])
        image_shape[0]["_image_path"] = str(image_path)
        image_overlay_path = _render_overlay_shapes(image_shape, target_width=1080, target_height=1920)
        check("image_png_created", bool(image_overlay_path and image_overlay_path.exists()))
        try:
            rendered = Image.open(image_overlay_path).convert("RGBA")
            image_bounds = bounds_for(rendered, lambda pixel: 110 <= pixel[3] <= 140 and pixel[0] > 240)
            check("image_bounds", all(abs(value - expected) <= 2 for value, expected in zip(image_bounds, (878, 49, 1022, 121))), str(image_bounds))
        finally:
            if image_overlay_path:
                image_overlay_path.unlink(missing_ok=True)
    finally:
        image_path.unlink(missing_ok=True)
    check("max_five", len(_normalize_overlay_shapes([{"id": str(i), "w": 1, "h": 1} for i in range(8)])) == 5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
