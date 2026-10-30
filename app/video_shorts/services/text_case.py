"""Language-aware text casing shared by previews and renderers."""
from typing import Any


_TR_UPPER_MAP = str.maketrans({"i": "İ", "ı": "I"})


def normalize_language(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized.startswith("tr") or normalized in {"tur", "turkish", "turkce", "türkçe"}:
        return "tr"
    return normalized[:2]


def uppercase_text(value: Any, language: Any = "") -> str:
    text = str(value or "")
    if normalize_language(language) == "tr":
        return text.translate(_TR_UPPER_MAP).upper()
    return text.upper()
