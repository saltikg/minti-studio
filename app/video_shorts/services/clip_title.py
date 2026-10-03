import json
import logging
import re
from typing import Any, Optional

from app.video_shorts.config import TITLE_CHECK_MODEL, TITLE_MODEL, _openai_client

logger = logging.getLogger(__name__)

_TURKISH_TITLE_CASE_CONNECTORS = {"ve", "ile", "de", "da", "ki", "mı", "mi", "mu", "mü"}
_ENGLISH_TITLE_CASE_CONNECTORS = {
    "a", "an", "the", "and", "or", "but", "of", "in", "on", "at", "to", "for",
    "with", "from", "by", "as", "is", "vs",
}
LANGUAGE_NAMES = {"tr": "Turkish", "en": "English", "ar": "Arabic"}
EXAMPLES = {
    "tr": """BAD (topic, no idea): 'Sabır Konusunu Anlatıyor'
GOOD: 'Sabır Sıkıntıyla Olgunlaşır'
GOOD (contrast): 'Zenginlik Değil, Kanaat Huzur Verir'
GOOD (question): 'Dua Neden Hemen Kabul Olmaz'
BAD (noun pile): 'Sabrın insan hayatındaki öneminin anlatılması'
GOOD: 'Sabırsız İnsan Hep Kaybeder'""",
    "en": """BAD (topic, no idea): 'He Talks About Patience'
GOOD: 'Patience Is Earned Through Trials'
GOOD (contrast): 'Not Wealth, Contentment Brings Peace'
GOOD (question): 'Why Prayers Are Not Answered Right Away'
BAD (noun pile): 'The importance of patience in human life'
GOOD: 'Impatient People Always Lose'""",
}
UNIVERSAL_TITLE_PROMPT = """You write titles for short vertical clips (YouTube Shorts) cut from longer
talk, lecture, and Q&A videos. Write the title in the language named in the
user message.

ACCURACY (most important, overrides everything else):
- The title's information must come only from this clip's transcript.
- Never add any person, work, political party, institution, place, date, or
  event name that does not literally appear in the transcript.
- Never add a judgment, adjective, or metaphor the speaker did not use.
- NEVER flip a positive into a negative or a negative into a positive. If the
  speaker says "X is Y", the title cannot say "X is not Y".
- Preserve modality: "could not" is not "did not"; "tends to" is not "does".
- If the speaker is quoting, questioning, or criticizing a view, do not
  present it as the speaker's own judgment.
- If the speaker asks a question and does not answer it in the clip, do not
  invent an answer; make the question the title.
- Do not merge separate statements from the transcript into a new claim the
  speaker did not make (e.g. "gave orders" + "betrayal" -> "gave betrayal orders").
- Keep who the statement is about, including singular vs plural: if the
  speaker describes a group, do not title it as one person.

CONTENT:
- Title the clip's central point, the one most of the clip is about. Do not
  build the title on a side detail, an anecdote's punchline, or a single
  vivid image unless that is clearly the point of the whole clip.
- Where possible, state the clip's main IDEA, not just its topic: the claim,
  conclusion, or cause-effect the speaker clearly makes.
- If stating it as a firm claim would strain the meaning, use a contrast or
  question form instead.
- Do not end on a verb with an unclear subject (like "...Explains" or
  "...Says", or the equivalent in the title's language).
- A curiosity gap is not vagueness: state the idea clearly, leave the
  reasoning to the clip.

FORM:
- Prefer 3-5 words. Maximum 45 characters, one line.
- Use verbs; avoid long noun phrases.
- No clickbait, no exaggeration. Serious, respectful tone.
- No quotes, no trailing punctuation, no hashtags, no emojis, no ALL-CAPS words.
- When the speaker describes a revered or religious figure with a simile or
  image, keep the simile (e.g. "like a bee") or leave the image out; never
  strip it into a literal statement that could sound mocking or disrespectful.

EXAMPLES (style reference only; never copy their words or topics):
{examples}

Output exactly one line containing only the title."""
UNIVERSAL_CHECK_PROMPT = """You are an accuracy auditor. You will receive a video clip's transcript and a
title written for it, possibly in any language. Check whether the title is
faithful to the transcript's meaning.

The title is NOT faithful if ANY of these is true:
- It flips a positive into a negative or a negative into a positive.
- It changes modality (e.g. "could not" vs "did not", "tends to" vs "does").
- It adds a judgment, adjective, metaphor, or conclusion the speaker did not make.
- It attaches a judgment to the wrong person or the wrong subject.
- It presents a view the speaker is quoting or criticizing as the speaker's own.
- It invents an answer to a question the speaker left unanswered.
- It contains a person, institution, place, date, or event name not in the transcript.
- It merges separate statements from the transcript into a new claim the
  speaker did not make.

Differences in style, shortening, or word choice are NOT problems; judge
meaning only. Return only this JSON: {"faithful": true or false, "reason": "short reason"}"""


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


def _title_language_name(language: Optional[str]) -> str:
    value = str(language or "").strip().lower()
    return LANGUAGE_NAMES.get(value, value or "unknown")


def _title_system_prompt(language: Optional[str]) -> str:
    examples = EXAMPLES.get(str(language or "").lower()) or EXAMPLES["en"]
    return UNIVERSAL_TITLE_PROMPT.replace("{examples}", examples)


def _post_process_title(raw: str, language: Optional[str]) -> str:
    suggestion = (raw or "").strip()
    suggestion = suggestion.strip().strip("\"'“”‘’")
    suggestion = re.sub(r"[.!?,:;]+$", "", suggestion).strip()
    suggestion = suggestion[:80].strip()
    if (language or "").lower() == "tr":
        suggestion = _turkish_title_case(suggestion)
    elif (language or "").lower() == "en":
        suggestion = _english_title_case(suggestion)
    return suggestion


def _title_messages(excerpt: str, language: Optional[str], *, safe_mode: bool = False) -> list[dict[str, str]]:
    language_name = _title_language_name(language)
    user_message = f"Transcript language: {language_name}. Write the title in that language.\n\n{excerpt}"
    if safe_mode:
        user_message += (
            f"\n\nSAFE MODE: Do not make a claim. State the clip topic neutrally and briefly, "
            f"maximum 45 characters. Write in {language_name}."
        )
    return [
        {"role": "system", "content": _title_system_prompt(language)},
        {"role": "user", "content": user_message},
    ]


def _length_retry_message(language: Optional[str]) -> str:
    return (
        "This title is over 45 characters. Keep the same meaning, make it shorter. "
        f"Write in {_title_language_name(language)}."
    )


def _regen_message(language: Optional[str], reason: str) -> str:
    return (
        f"The previous title was rejected. Reason: {reason or 'too long'}. "
        "Write a new title faithful to the text, maximum 45 characters. "
        f"Write in {_title_language_name(language)}."
    )


def _truncate_whole_word(title: str, limit: int = 45) -> str:
    if len(title) <= limit:
        return title
    cut = title[:limit].rstrip()
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0].rstrip()
    return cut or title[:limit].rstrip()


def _rejection_reason(faithful: bool, checker_reason: str, title: str) -> str:
    reasons = []
    if not faithful:
        reasons.append(checker_reason or "not faithful")
    if len(title) > 45:
        reasons.append("too long")
    return "; ".join(reasons) or "rejected"


def generate_clip_title(transcript_text: str, language_hint: str | None = None) -> str:
    source_text = str(transcript_text or "").strip()
    if not _openai_client or not source_text:
        return ""

    safe_excerpt = source_text[:2000]
    resolved_language = _normalize_language_hint(language_hint) or _detect_title_language(safe_excerpt)
    llm_calls = 0

    def generator_call(messages: list[dict[str, str]]) -> tuple[str, str]:
        nonlocal llm_calls
        response = _openai_client.chat.completions.create(
            model=TITLE_MODEL,
            messages=messages,
            temperature=0.3,
        )
        llm_calls += 1
        raw = response.choices[0].message.content if response.choices else ""
        return raw or "", _post_process_title(raw or "", resolved_language)

    def check_faithfulness(title: str) -> tuple[bool, str, bool]:
        nonlocal llm_calls
        try:
            response = _openai_client.chat.completions.create(
                model=TITLE_CHECK_MODEL,
                messages=[
                    {"role": "system", "content": UNIVERSAL_CHECK_PROMPT},
                    {"role": "user", "content": f"METİN:\n{safe_excerpt}\n\nBAŞLIK:\n{title}"},
                ],
                temperature=0,
                response_format={"type": "json_object"},
            )
            llm_calls += 1
            raw = response.choices[0].message.content if response.choices else "{}"
            data = json.loads(raw or "{}")
        except Exception as exc:
            logger.warning("clip_title faithfulness check failed open: %s", exc)
            return True, "", True
        return bool(data.get("faithful")), str(data.get("reason") or "").strip(), False

    def finish(title: str, path: str) -> str:
        logger.info(
            "clip_title_generated language=%s path=%s llm_calls=%s final_length=%s",
            resolved_language or "unknown",
            path,
            llm_calls,
            len(title),
        )
        return title

    messages = _title_messages(safe_excerpt, resolved_language)
    raw, title = generator_call(messages)
    messages.append({"role": "assistant", "content": raw})
    if len(title) > 45:
        messages.append({"role": "user", "content": _length_retry_message(resolved_language)})
        raw, title = generator_call(messages)
        messages.append({"role": "assistant", "content": raw})

    faithful, reason, fail_open = check_faithfulness(title)
    if (faithful and len(title) <= 45) or fail_open:
        return finish(title, "first")

    reject_reason = _rejection_reason(faithful, reason, title)
    messages.append({"role": "user", "content": _regen_message(resolved_language, reject_reason)})
    raw, title = generator_call(messages)
    messages.append({"role": "assistant", "content": raw})
    if len(title) > 45:
        messages.append({"role": "user", "content": _length_retry_message(resolved_language)})
        raw, title = generator_call(messages)
        messages.append({"role": "assistant", "content": raw})

    faithful, reason, fail_open = check_faithfulness(title)
    if (faithful and len(title) <= 45) or fail_open:
        return finish(title, "regen")

    safe_messages = _title_messages(safe_excerpt, resolved_language, safe_mode=True)
    _raw, title = generator_call(safe_messages)
    title = _truncate_whole_word(title, 45)
    return finish(title, "safe")
