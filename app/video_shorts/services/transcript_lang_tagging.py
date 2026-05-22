import re
from typing import Any, Dict, List

_ARABIC_RE = re.compile(r"[\u0600-\u06FF]")


def _normalize_lang_value(raw: str | None) -> str | None:
    if not raw:
        return None
    val = str(raw).lower().strip()
    if val in {"ar", "ara", "arabic", "arb"}:
        return "ar"
    if val in {"tr", "turkish", "turkce", "turkish-turkey"}:
        return "tr"
    return None


def _infer_lang_from_text(text: str, threshold: float = 0.35) -> str:
    """Heuristic: if >=threshold of letters are in Arabic blocks, mark as Arabic."""
    if not text:
        return "tr"
    arabic_chars = _ARABIC_RE.findall(text)
    total_letters = sum(1 for ch in text if ch.isalpha())
    if total_letters <= 0:
        return "tr"
    ratio = len(arabic_chars) / float(total_letters)
    return "ar" if ratio >= threshold else "tr"


def infer_lang_from_text(text: str, threshold: float = 0.35) -> str:
    """Public helper to infer language from text using Arabic character ratio."""
    return _infer_lang_from_text(text, threshold=threshold)


def tag_segments_with_language(segments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Tag each segment with lang=("ar"|"tr") using provided per-segment language
    or presence of Arabic script; otherwise defaults to Turkish.
    Ensures start/end/duration fields are present.
    """
    tagged: List[Dict[str, Any]] = []
    for seg in segments or []:
        seg = seg or {}
        text = (seg.get("text") if isinstance(seg, dict) else "") or ""
        try:
            start = float(seg.get("start", 0.0))
        except Exception:
            start = 0.0
        end_val = seg.get("end")
        try:
            end = float(end_val) if end_val is not None else None
        except Exception:
            end = None
        dur_val = seg.get("duration")
        try:
            duration = float(dur_val) if dur_val is not None else None
        except Exception:
            duration = None
        if end is None and duration is not None:
            end = start + max(duration, 0.0)
        if duration is None and end is not None:
            duration = max(end - start, 0.0)

        lang_raw = seg.get("language") or seg.get("lang")
        lang = _normalize_lang_value(lang_raw)
        if not lang:
            # Script detection only; no romanized heuristics.
            lang = "ar" if _ARABIC_RE.search(text) else "tr"

        item = {
            "start": start,
            "end": end if end is not None else start + max(duration or 0.0, 0.0),
            "duration": duration if duration is not None else 0.0,
            "text": text,
            "lang": lang,
        }
        # pass through optional enriched fields for debugging/inspection
        for key in ("tr_text", "ar_text", "label", "words", "word_tags", "force_ar"):
            if key in seg:
                item[key] = seg.get(key)
        tagged.append(item)
    return tagged
