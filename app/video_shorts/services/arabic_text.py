"""Shared Arabic-script detection and font fallback helpers."""

from __future__ import annotations

import re
from typing import Callable


ARABIC_FONT_NAME = "Noto Sans Arabic"
ARABIC_SCRIPT_CLASS = "\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF"
ARABIC_SCRIPT_RE = re.compile(f"[{ARABIC_SCRIPT_CLASS}]")
ARABIC_RUN_RE = re.compile(
    f"[{ARABIC_SCRIPT_CLASS}]+(?:[\\s\\u060C\\u061B\\u061F'’\"-]+(?=[{ARABIC_SCRIPT_CLASS}])[{ARABIC_SCRIPT_CLASS}]+)*"
)


def contains_arabic_script(value: object) -> bool:
    return bool(ARABIC_SCRIPT_RE.search(str(value or "")))


def find_arabic_runs(value: object) -> list[str]:
    return [match.group(0) for match in ARABIC_RUN_RE.finditer(str(value or ""))]


def replace_arabic_runs(value: object, replacer: Callable[[str], str]) -> str:
    return ARABIC_RUN_RE.sub(lambda match: replacer(match.group(0)), str(value or ""))


def add_ass_arabic_font_fallback(value: object, *, base_font: str) -> str:
    """Select the bundled Arabic font only for Arabic runs in ASS event text."""
    text = str(value or "")
    if not contains_arabic_script(text):
        return text
    safe_base_font = str(base_font or "DejaVu Sans").replace("{", "").replace("}", "")
    return replace_arabic_runs(
        text,
        lambda run: f"{{\\fn{ARABIC_FONT_NAME}}}{run}{{\\fn{safe_base_font}}}",
    )
