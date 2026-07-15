import re
from typing import Any, Optional

from app.video_shorts.config import OPENAI_MODEL, _openai_client

_TURKISH_TITLE_CASE_CONNECTORS = {"ve", "ile", "de", "da", "ki", "mı", "mi", "mu", "mü"}
_ENGLISH_TITLE_CASE_CONNECTORS = {
    "a", "an", "the", "and", "or", "but", "of", "in", "on", "at", "to", "for",
    "with", "from", "by", "as", "is", "vs",
}


def _turkish_lower(text: str) -> str:
    return str(text or "").replace("I", "ı").replace("İ", "i").lower()


def _turkish_upper_char(ch: str) -> str:
    if ch == "i":
        return "İ"
    if ch == "ı":
        return "I"
    return ch.upper()


def _turkish_title_case(text: str) -> str:
    raw_text = str(text or "")
    if not raw_text:
        return raw_text

    transformed = []
    tokens = raw_text.split()
    word_re = re.compile(r"^([^A-Za-zÇĞİIÖŞÜçğıöşü]*)([A-Za-zÇĞİIÖŞÜçğıöşü]+)(.*)$")

    for index, token in enumerate(tokens):
        if len(token) >= 2 and token.isupper():
            transformed.append(token)
            continue
        match = word_re.match(token)
        if not match:
            transformed.append(token)
            continue
        prefix, core, suffix = match.groups()
        lowered_core = _turkish_lower(core)
        if index > 0 and lowered_core in _TURKISH_TITLE_CASE_CONNECTORS:
            transformed.append(f"{prefix}{lowered_core}{suffix}")
            continue
        titled_core = _turkish_upper_char(lowered_core[:1]) + lowered_core[1:]
        transformed.append(f"{prefix}{titled_core}{suffix}")
    return " ".join(transformed)


def _english_title_case(text: str) -> str:
    raw_text = str(text or "")
    if not raw_text:
        return raw_text

    transformed = []
    tokens = raw_text.split()
    word_re = re.compile(r"^([^A-Za-z]*)([A-Za-z]+)(.*)$")

    for index, token in enumerate(tokens):
        if len(token) >= 2 and token.isupper():
            transformed.append(token)
            continue
        match = word_re.match(token)
        if not match:
            transformed.append(token)
            continue
        prefix, core, suffix = match.groups()
        lowered_core = core.lower()
        if index > 0 and lowered_core in _ENGLISH_TITLE_CASE_CONNECTORS:
            transformed.append(f"{prefix}{lowered_core}{suffix}")
            continue
        titled_core = lowered_core[:1].upper() + lowered_core[1:]
        transformed.append(f"{prefix}{titled_core}{suffix}")
    return " ".join(transformed)


def _normalize_language_hint(raw: Any) -> Optional[str]:
    value = str(raw or "").strip().lower()
    if not value:
        return None
    if value.startswith("en") or value == "english":
        return "en"
    if value.startswith("tr") or value in {"turkish", "turkce"}:
        return "tr"
    if value.startswith("ar") or value == "arabic":
        return "ar"
    return None


def _detect_title_language(transcript_text: str) -> Optional[str]:
    text = " ".join(str(transcript_text or "").strip().split())
    if not text:
        return None
    if re.search(r"[\u0600-\u06FF]", text):
        return "ar"
    lowered = f" {text.lower()} "
    if any(ch in text for ch in "çğıöşüÇĞİÖŞÜ"):
        return "tr"

    turkish_hints = {
        " ve ", " bir ", " bu ", " şu ", " için ", " ama ", " gibi ", " daha ",
        " çok ", " değil ", " neden ", " nasıl ", " çünkü ", " sonra ", " önce ",
    }
    english_hints = {
        " the ", " and ", " you ", " your ", " what ", " why ", " how ", " this ",
        " that ", " with ", " from ", " about ", " when ", " where ", " should ",
    }
    turkish_score = sum(1 for hint in turkish_hints if hint in lowered)
    english_score = sum(1 for hint in english_hints if hint in lowered)
    if turkish_score >= english_score + 1 and turkish_score >= 2:
        return "tr"
    if english_score >= turkish_score + 1 and english_score >= 2:
        return "en"
    return None


def generate_clip_title(transcript_text: str, language_hint: str | None = None) -> str:
    source_text = str(transcript_text or "").strip()
    if not _openai_client or not source_text:
        return ""

    safe_excerpt = source_text[:2000]
    resolved_language = _normalize_language_hint(language_hint) or _detect_title_language(safe_excerpt)
    system_prompt = (
        "You write titles for short vertical clips (YouTube Shorts) cut from longer\n"
        "talk, lecture, and Q&A videos.\n"
        "Write the title in the EXACT same language as the transcript. Never translate.\n"
        "\n"
        "ACCURACY (strict):\n"
        "- The title's INFORMATION must come only from this clip's transcript.\n"
        "- Never add any person, political party, institution, organization, place,\n"
        "  date, or event name that does not literally appear in the transcript.\n"
        "- Never assert a claim the speaker did not make. Do not distort the meaning.\n"
        "\n"
        "WORDING (free):\n"
        "- You do NOT have to reuse the transcript's exact words.\n"
        "- Rephrase the same meaning in your own, sharper words.\n"
        "- The title is a HOOK, not a summary.\n"
        "\n"
        "FORM:\n"
        "- 3-6 words. Maximum 45 characters. Must fit on one line.\n"
        "- Use verbs. Do not nominalize.\n"
        "  BAD:  'Boş binalara top atılması ve halkın üzerine gidilmesi senaryosu'\n"
        "  GOOD: 'Boş Binaları Vurup Halka Saldırdılar'\n"
        "- Leave a curiosity gap: don't say everything.\n"
        "- No clickbait, no exaggeration, no distortion. Keep a serious, respectful tone.\n"
        "- No quotes, no trailing punctuation, no hashtags, no emojis, no ALL-CAPS words.\n"
        "Output exactly one line containing only the title."
    )
    user_message = (
        (f"Transcript language hint: {resolved_language}. Write the title in that same language.\n\n"
         if resolved_language else "")
        + safe_excerpt
    )
    response = _openai_client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        temperature=0.3,
    )
    suggestion = (response.choices[0].message.content or "").strip()
    suggestion = suggestion.strip().strip("\"'“”‘’")
    suggestion = re.sub(r"[.!?,:;]+$", "", suggestion).strip()
    suggestion = suggestion[:80].strip()
    if (resolved_language or "").lower() == "tr":
        suggestion = _turkish_title_case(suggestion)
    elif (resolved_language or "").lower() == "en":
        suggestion = _english_title_case(suggestion)
    return suggestion
