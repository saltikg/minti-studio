#!/usr/bin/env python3
"""Focused checks for the shared preview/Pillow title-effect resolver."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.video_shorts.services.compositor import _resolve_title_text_effects


def resolve(text_color: str, bg_alpha: int) -> dict:
    return _resolve_title_text_effects(
        subtitle_preset="opus",
        title_text_color=text_color,
        title_bg_alpha=bg_alpha,
        title_outline_color="#000000",
        title_outline_width=3,
    )


def main() -> None:
    white_no_box = resolve("#FFFFFF", 0)
    assert white_no_box["outline_width"] == 3
    assert white_no_box["shadow"]["shadow_color"] == "#000000"
    assert white_no_box["shadow"]["shadow_opacity"] > 0

    black_no_box = resolve("#000000", 0)
    assert black_no_box["outline_width"] == 0
    assert black_no_box["shadow"]["shadow_opacity"] == 0

    white_with_box = resolve("#FFFFFF", 70)
    assert white_with_box["outline_width"] == 0
    assert white_with_box["shadow"]["shadow_opacity"] == 0

    black_with_box = resolve("#000000", 70)
    assert black_with_box["outline_width"] == 0
    assert black_with_box["shadow"]["shadow_opacity"] == 0
    print("TITLE_TEXT_EFFECTS_OK")


if __name__ == "__main__":
    main()
