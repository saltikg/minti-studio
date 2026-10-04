import concurrent.futures
import json
import logging
import math
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from app.video_shorts.config import CLIP_PLANNER_V10_MODEL
from app.video_shorts.services.clip_plan_focus_prompts import normalize_planning_language
from app.video_shorts.services.clip_planner_agents import (
    MAX_CLIP_SECONDS,
    MIN_CLIP_SECONDS,
    OPENAI_PLANNER_TIMEOUT_SECONDS,
    _target_clip_count_for_duration,
)
from app.video_shorts.services.clip_title import generate_clip_title

logger = logging.getLogger(__name__)

STAGE3_MAX_ELIGIBLE_SECONDS = 63.0
MAX_WORKERS = 8
WORD_RE = re.compile(r"[\wÇĞİÖŞÜçğıöşü'’]+", re.UNICODE)
TR_LOWER = str.maketrans({"İ": "i", "I": "ı"})
TITLE_STOPWORDS = {
    "bir",
    "gibi",
    "için",
    "with",
    "that",
    "this",
    "from",
    "your",
    "video",
    "short",
}
STRONG_PHRASES = [
    "onun için",
    "bu yüzden",
    "bu sebeple",
    "o halde",
    "o zaman",
    "oysa ki",
    "that's why",
    "for example",
]
STRONG_WORDS = {
    "ama",
    "fakat",
    "lakin",
    "ancak",
    "çünkü",
    "zira",
    "dolayısıyla",
    "oysa",
    "mesela",
    "örneğin",
    "yani",
    "neden",
    "niye",
    "ve",
    "hatta",
    "ayrıca",
    "but",
    "so",
    "because",
    "which",
    "and",
    "also",
}
QUESTION_HOOKS = {"neden", "niye", "why"}

V10_STAGE1_PROMPT_TEMPLATE = """You pick the best short clips (YouTube Shorts) from a full talk, lecture, or
Q&A transcript. The transcript is given as numbered sentences with
timestamps. Read the WHOLE transcript before choosing.

Pick about {target_count} clips. Each clip must be between {min_s} and
{max_s} seconds long (use the timestamps to estimate).

A good clip:
- Carries ONE complete idea, story, or question-and-answer that a viewer who
  never saw the rest of the video can fully understand.
- STARTS at a sentence that stands on its own. Never start on a sentence that
  only makes sense as a continuation of the previous one (it opens with words
  like "but", "because", "therefore", "for example", "so", "that's why", or
  their equivalent in the transcript's language, or it refers back to
  something not in the clip, like "this", "that", "as I said"). If the idea
  needs the previous sentence, start the clip at that previous sentence
  instead.
- A short question that the clip goes on to answer is a strong start.
- ENDS where the idea is complete: after the conclusion, the punchline, or the
  answer — not in the middle of an explanation and not on a new topic.
- Is one of the strongest, most interesting, or most quotable moments of the
  talk. Skip greetings, logistics, housekeeping, and filler.

- Go through EVERY part. From each part, propose up to {per_part} clips
  (fewer if a part has no worthwhile moment). Do not favor the beginning
  of the video; late moments are just as valuable.
- Give each clip a "strength" score from 1 to 10 (10 = most compelling,
  quotable, and self-contained).
- Never pick greetings, introductions of the stream or guests, program
  logistics, or "let's start with your questions" moments.
- Do not worry about exact length here; boundaries will be refined later.
  Just pick the sentences that carry the idea.
- The video title usually names its central message. If a moment in the
  transcript delivers that message, include it among your strongest clips.

Rules:
- Clips must not overlap and must not repeat the same idea.
- Use only sentence ids that exist in the transcript.

Return only JSON:
{{"clips": [
  {{"start_sentence": <id>, "end_sentence": <id>,
   "part": <k>, "strength": <1-10>,
   "idea": "<one short sentence: what this clip says>",
   "why_start_here": "<short reason>",
   "why_end_here": "<short reason>"}}
]}}"""

V10_STAGE1_PER_PART_PROMPT_TEMPLATE = V10_STAGE1_PROMPT_TEMPLATE.replace(
    "- Go through EVERY part. From each part, propose up to {per_part} clips\n"
    "  (fewer if a part has no worthwhile moment). Do not favor the beginning\n"
    "  of the video; late moments are just as valuable.\n"
    "- Give each clip a \"strength\" score from 1 to 10 (10 = most compelling,\n"
    "  quotable, and self-contained).",
    "- Propose up to {per_part} clips from this part (fewer if nothing is worthwhile). "
    "Give each a strength score from 1 to 10.",
)

START_PROMPT = """You choose where a short video clip should START. The clip will be watched on
its own as a YouTube Short. The clip's idea is given. You see numbered
sentences around the current start.
Pick the sentence where a viewer who saw nothing before can follow the idea.
Never start on a sentence that only makes sense as a continuation (opening
with words like "but", "because", "therefore", "for example", "so",
"that's why", or the equivalent in the transcript's language) or that refers
back to something not in the clip. If the idea needs an earlier setup
sentence, start there. A short question that the clip goes on to answer is a
strong start. Do not start on greetings or filler.
If the current start is in the middle of a question, start where the
question begins.
Return only JSON: {"start_sentence": <id>, "reason": "<short>"}"""

END_PROMPT = """You choose where a short video clip should END. The clip's idea and its
sentences from the start are given. Each line shows the clip length if the
clip ended at that sentence; only lines marked ✓ are allowed.
Pick the ✓ sentence where the idea is complete: after the conclusion, the
punchline, or the answer. Never end in the middle of an explanation, on a
sentence that sets up something new, or on a dangling question.
Longer is NOT better. Pick the EARLIEST ✓ sentence where the idea is
complete. Never end on a sentence that starts a new point, a new example,
a new list, or a new topic (for example "In another place he also says...",
or a new short statement that the next part goes on to explain).
If no ✓ sentence lets the idea finish properly, return
{"end_sentence": null, "reason": "<short>"} instead of cutting it short.
Return only JSON: {"end_sentence": <id or null>, "reason": "<short>"}"""


def _language_name(language: str) -> str:
    value = normalize_planning_language(language)
    return {"tr": "Turkish", "en": "English", "ar": "Arabic"}.get(value, value or "unknown")


def _title_tokens(text: str) -> List[str]:
    tokens = []
    for raw in WORD_RE.findall(str(text or "")):
        token = raw.translate(TR_LOWER).lower().strip("'’")
        if len(token) < 5 or token in TITLE_STOPWORDS:
            continue
        tokens.append(token)
    return tokens


def _normalized_words(text: str) -> List[Tuple[str, str]]:
    pairs = []
    for raw in WORD_RE.findall(str(text or "")):
        normalized = raw.translate(TR_LOWER).lower().strip("'’.,!?…;:()[]{}\"“”")
        if normalized:
            pairs.append((normalized, raw.strip("'’.,!?…;:()[]{}\"“”")))
    return pairs


def _question_word_hook(text: str) -> bool:
    stripped = str(text or "").strip()
    if not stripped.endswith("?"):
        return False
    tokens = [token for token, _raw in _normalized_words(stripped)]
    return len(tokens) == 1 and tokens[0] in QUESTION_HOOKS


def _strong_connector_match(text: str, language: str) -> str:
    resolved = normalize_planning_language(language)
    if resolved not in {"tr", "en"} or _question_word_hook(text):
        return ""
    pairs = _normalized_words(text)
    tokens = [token for token, _raw in pairs]
    if not tokens:
        return ""
    match_len = 0
    for phrase in sorted(STRONG_PHRASES, key=lambda item: len(item.split()), reverse=True):
        phrase_tokens = [part.translate(TR_LOWER).lower() for part in phrase.split()]
        if resolved == "tr" and any(part in {"that's", "why", "for", "example"} for part in phrase_tokens):
            continue
        if resolved == "en" and any(part in {"onun", "için", "bu", "yüzden", "sebep", "halde", "zaman", "oysa", "ki"} for part in phrase_tokens):
            continue
        if tokens[: len(phrase_tokens)] == phrase_tokens:
            match_len = len(phrase_tokens)
            break
    if not match_len and tokens[0] in STRONG_WORDS:
        match_len = 1
    if not match_len:
        return ""
    if resolved == "tr" and len(tokens) > match_len and tokens[match_len] == "diyor":
        match_len += 1
        if len(tokens) > match_len and tokens[match_len] == "ki":
            match_len += 1
    return " ".join(raw for _token, raw in pairs[:match_len])


def _title_anchor_candidate(video_title: str, sentences: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    title_tokens = _title_tokens(video_title)
    if len(title_tokens) < 2:
        return None
    best_sentence = None
    best_score = 0
    title_set = set(title_tokens)
    for sentence in sentences:
        sentence_tokens = set(_title_tokens(str(sentence.get("text") or "")))
        score = len(title_set.intersection(sentence_tokens))
        if score > best_score:
            best_score = score
            best_sentence = sentence
    if not best_sentence or best_score < 2:
        return None
    return {
        "start_sentence": int(best_sentence["id"]),
        "end_sentence": int(best_sentence["id"]),
        "idea": f"Moment that delivers the video title: {video_title}",
        "why_start_here": "This sentence closely matches the video title.",
        "why_end_here": "Boundary will be refined later.",
        "_title_anchor": True,
    }


def _candidate_start_id(candidate: Any) -> Optional[int]:
    if not isinstance(candidate, dict):
        return None
    try:
        return int(candidate.get("start_sentence"))
    except Exception:
        return None


def _fmt_time(seconds: float) -> str:
    seconds = max(0, int(round(float(seconds or 0))))
    return f"{seconds // 60}:{seconds % 60:02d}"


def _clamp_strength(value: Any) -> int:
    try:
        strength = int(round(float(value)))
    except Exception:
        strength = 5
    return max(1, min(10, strength))


def _part_for_time(seconds: float, duration_seconds: float, n_parts: int) -> int:
    if n_parts <= 1:
        return 1
    duration = max(float(duration_seconds or 0.0), 0.01)
    index = int(max(0.0, min(float(seconds or 0.0), duration - 0.001)) / (duration / n_parts))
    return max(1, min(n_parts, index + 1))


def _build_sentence_parts(
    sentences: List[Dict[str, Any]],
    duration_seconds: float,
    n_parts: int,
) -> List[Dict[str, Any]]:
    duration = max(float(duration_seconds or 0.0), float((sentences[-1] or {}).get("end") or 0.0), 0.01)
    parts = []
    for part_number in range(1, n_parts + 1):
        start = duration * (part_number - 1) / n_parts
        end = duration * part_number / n_parts
        parts.append({"part": part_number, "start": start, "end": end, "sentences": []})
    for sentence in sentences:
        part_number = _part_for_time(float(sentence.get("start") or 0.0), duration, n_parts)
        parts[part_number - 1]["sentences"].append(sentence)
    return parts


def _existing_ranges_for_part(
    ranges: List[Tuple[float, float]],
    part: Dict[str, Any],
) -> List[Tuple[float, float]]:
    part_start = float(part.get("start") or 0.0)
    part_end = float(part.get("end") or 0.0)
    return [
        (float(start), float(end))
        for start, end in ranges or []
        if max(float(start), part_start) < min(float(end), part_end)
    ]


def _existing_ranges_block(ranges: List[Tuple[float, float]]) -> str:
    if not ranges:
        return ""
    lines = ["Existing clips to avoid:"]
    lines.extend(f"- {_fmt_time(start)}-{_fmt_time(end)}" for start, end in ranges)
    return "\n".join(lines) + "\n\n"


def _candidate_part(
    candidate: Dict[str, Any],
    sentences_by_id: Dict[int, Dict[str, Any]],
    duration_seconds: float,
    n_parts: int,
) -> int:
    try:
        part = int(candidate.get("part"))
    except Exception:
        part = 0
    if 1 <= part <= n_parts:
        return part
    start_id = _candidate_start_id(candidate)
    sentence = sentences_by_id.get(start_id or -1)
    if sentence:
        return _part_for_time(float(sentence.get("start") or 0.0), duration_seconds, n_parts)
    return 1


def _seg_start_end(seg: Dict[str, Any]) -> Tuple[float, float]:
    start = float(seg.get("start") or 0.0)
    if seg.get("end") is not None:
        end = float(seg.get("end") or start)
    else:
        end = start + max(float(seg.get("duration") or 0.0), 0.0)
    return start, max(end, start)


def _segment_text(seg: Dict[str, Any]) -> str:
    return str(seg.get("tr_text") or seg.get("text") or seg.get("ar_text") or "").strip()


def _word_text(word: Dict[str, Any]) -> str:
    return str(word.get("word") or "").strip()


def _word_start_end(word: Dict[str, Any], fallback_start: float, fallback_end: float) -> Tuple[float, float]:
    try:
        start = float(word.get("start"))
    except Exception:
        start = fallback_start
    try:
        end = float(word.get("end"))
    except Exception:
        end = fallback_end
    return start, max(end, start)


def _sentence_from_words(words: List[Dict[str, Any]]) -> Dict[str, Any]:
    text = " ".join(_word_text(word) for word in words if _word_text(word)).strip()
    start, _ = _word_start_end(words[0], 0.0, 0.0)
    _, end = _word_start_end(words[-1], start, start)
    return {"start": start, "end": end, "text": text, "words": list(words)}


def _split_long_sentence(sentence: Dict[str, Any], max_words: int = 40) -> List[Dict[str, Any]]:
    words = sentence.get("words") or []
    if len(words) <= max_words:
        return [sentence]
    chunks = []
    start_index = 0
    while start_index < len(words):
        end_index = min(len(words), start_index + max_words)
        chunk_words = words[start_index:end_index]
        if not chunk_words:
            break
        chunks.append(_sentence_from_words(chunk_words))
        start_index = end_index
    return chunks or [sentence]


def _fallback_words_for_segment(seg: Dict[str, Any]) -> List[Dict[str, Any]]:
    text = _segment_text(seg)
    tokens = re.findall(r"\S+", text)
    if not tokens:
        return []
    start, end = _seg_start_end(seg)
    step = max(end - start, 0.01) / len(tokens)
    return [
        {"word": token, "start": start + index * step, "end": start + (index + 1) * step}
        for index, token in enumerate(tokens)
    ]


def build_v10_sentences(segments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    sentences: List[Dict[str, Any]] = []
    next_id = 1
    for seg in sorted(segments or [], key=lambda item: float(item.get("start") or 0.0)):
        seg_start, seg_end = _seg_start_end(seg)
        raw_words = seg.get("words") or []
        words: List[Dict[str, Any]] = []
        if isinstance(raw_words, list):
            for word in raw_words:
                if not isinstance(word, dict) or not _word_text(word):
                    continue
                word_start, word_end = _word_start_end(word, seg_start, seg_end)
                words.append({"word": _word_text(word), "start": word_start, "end": word_end})
        if not words:
            words = _fallback_words_for_segment(seg)
        if not words:
            continue
        current: List[Dict[str, Any]] = []
        for word in words:
            current.append(word)
            if _word_text(word).rstrip().endswith((".", "?", "!", "…")):
                for sentence in _split_long_sentence(_sentence_from_words(current)):
                    sentence["id"] = next_id
                    next_id += 1
                    sentences.append(sentence)
                current = []
        if current:
            for sentence in _split_long_sentence(_sentence_from_words(current)):
                sentence["id"] = next_id
                next_id += 1
                sentences.append(sentence)
    return sentences


def _clip_text_from_sentences(sentences: List[Dict[str, Any]], start_id: int, end_id: int) -> str:
    return " ".join(
        str(sentence.get("text") or "")
        for sentence in sentences
        if start_id <= int(sentence.get("id") or 0) <= end_id
    ).strip()


def _mapped_times(
    sentences_by_id: Dict[int, Dict[str, Any]],
    start_id: int,
    end_id: int,
    video_duration: float,
) -> Tuple[float, float, float]:
    start_sentence = sentences_by_id[start_id]
    end_sentence = sentences_by_id[end_id]
    previous_sentence = sentences_by_id.get(start_id - 1)
    start = float(start_sentence["start"]) - 0.10
    if previous_sentence:
        start = max(start, float(previous_sentence["end"]))
    start = max(0.0, start)
    end = min(float(video_duration or float(end_sentence["end"]) + 0.15), float(end_sentence["end"]) + 0.15)
    return start, end, end - start


def _usage(response: Any) -> Tuple[int, int]:
    usage = getattr(response, "usage", None)
    if not usage:
        return 0, 0
    return int(getattr(usage, "prompt_tokens", 0) or 0), int(getattr(usage, "completion_tokens", 0) or 0)


class _CallStats:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0

    def add(self, response: Any) -> None:
        input_tokens, output_tokens = _usage(response)
        with self.lock:
            self.calls += 1
            self.input_tokens += input_tokens
            self.output_tokens += output_tokens

    def snapshot(self) -> Dict[str, int]:
        with self.lock:
            return {
                "openai_call_count": self.calls,
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
            }


def _chat_json(client: Any, stats: _CallStats, *, messages: List[Dict[str, str]], temperature: float) -> Dict[str, Any]:
    response = client.chat.completions.create(
        model=CLIP_PLANNER_V10_MODEL,
        messages=messages,
        temperature=temperature,
        response_format={"type": "json_object"},
        timeout=OPENAI_PLANNER_TIMEOUT_SECONDS,
    )
    stats.add(response)
    raw = response.choices[0].message.content if response.choices else "{}"
    return {"raw": raw or "{}", "data": json.loads(raw or "{}")}


def _parse_start_response(raw: str) -> int:
    data = json.loads(raw or "{}")
    return int(data.get("start_sentence"))


def _run_stage1(
    client: Any,
    stats: _CallStats,
    *,
    video_title: str,
    language: str,
    sentences: List[Dict[str, Any]],
    target_count: int,
    duration_seconds: float,
    n_parts: int,
    per_part: int,
) -> List[Dict[str, Any]]:
    prompt = V10_STAGE1_PROMPT_TEMPLATE.format(
        min_s=int(MIN_CLIP_SECONDS),
        max_s=int(MAX_CLIP_SECONDS),
        target_count=target_count + 3,
        per_part=per_part,
    )
    lines = []
    for part in _build_sentence_parts(sentences, duration_seconds, n_parts):
        lines.append(
            f"=== PART {part['part']} of {n_parts} ({_fmt_time(part['start'])}-{_fmt_time(part['end'])}) ==="
        )
        lines.extend(
            f"[{sentence['id']}] ({_fmt_time(sentence['start'])}) {sentence['text']}"
            for sentence in part["sentences"]
        )
    title_line = f"Video title: {video_title}\n" if str(video_title or "").strip() else ""
    user_message = f"{title_line}Transcript language: {_language_name(language)}\n\n" + "\n".join(lines)
    result = _chat_json(
        client,
        stats,
        messages=[{"role": "system", "content": prompt}, {"role": "user", "content": user_message}],
        temperature=0.2,
    )
    return list((result["data"] or {}).get("clips") or [])


def _run_stage1_for_part(
    client: Any,
    stats: _CallStats,
    *,
    video_title: str,
    language: str,
    part: Dict[str, Any],
    next_part: Optional[Dict[str, Any]],
    per_part: int,
    existing_ranges: List[Tuple[float, float]],
) -> List[Dict[str, Any]]:
    prompt = V10_STAGE1_PER_PART_PROMPT_TEMPLATE.format(
        min_s=int(MIN_CLIP_SECONDS),
        max_s=int(MAX_CLIP_SECONDS),
        target_count=per_part,
        per_part=per_part,
    )
    part_number = int(part.get("part") or 1)
    lines = [
        f"=== PART {part_number} ({_fmt_time(part.get('start') or 0.0)}-{_fmt_time(part.get('end') or 0.0)}) ==="
    ]
    part_sentences = list(part.get("sentences") or [])
    lines.extend(f"[{sentence['id']}] ({_fmt_time(sentence['start'])}) {sentence['text']}" for sentence in part_sentences)

    context_ids = set()
    if next_part:
        context_limit = float(next_part.get("start") or 0.0) + 60.0
        context_sentences = [
            sentence
            for sentence in next_part.get("sentences") or []
            if float(sentence.get("start") or 0.0) < context_limit
        ]
        if context_sentences:
            lines.append("=== CONTEXT ONLY: start of next part (do not start clips here) ===")
            for sentence in context_sentences:
                context_ids.add(int(sentence["id"]))
                lines.append(f"[{sentence['id']}] ({_fmt_time(sentence['start'])}) {sentence['text']}")

    title_line = f"Video title: {video_title}\n" if str(video_title or "").strip() else ""
    user_message = (
        f"{title_line}"
        f"Transcript language: {_language_name(language)}\n\n"
        f"{_existing_ranges_block(_existing_ranges_for_part(existing_ranges, part))}"
        + "\n".join(lines)
    )
    result = _chat_json(
        client,
        stats,
        messages=[{"role": "system", "content": prompt}, {"role": "user", "content": user_message}],
        temperature=0.2,
    )
    candidates = []
    for candidate in list((result["data"] or {}).get("clips") or []):
        if not isinstance(candidate, dict):
            candidates.append(candidate)
            continue
        if _candidate_start_id(candidate) in context_ids:
            continue
        candidate = dict(candidate)
        candidate["part"] = part_number
        candidates.append(candidate)
    return candidates


def _run_stage1_per_part(
    client: Any,
    stats: _CallStats,
    *,
    video_title: str,
    language: str,
    parts: List[Dict[str, Any]],
    per_part: int,
    existing_ranges: List[Tuple[float, float]],
) -> List[Dict[str, Any]]:
    if not parts:
        return []

    def call(index_part: Tuple[int, Dict[str, Any]]) -> List[Dict[str, Any]]:
        index, part = index_part
        next_part = parts[index + 1] if index + 1 < len(parts) else None
        return _run_stage1_for_part(
            client,
            stats,
            video_title=video_title,
            language=language,
            part=part,
            next_part=next_part,
            per_part=per_part,
            existing_ranges=existing_ranges,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(1, len(parts)))) as executor:
        per_part_results = list(executor.map(call, list(enumerate(parts))))
    candidates: List[Dict[str, Any]] = []
    for result in per_part_results:
        candidates.extend(result)
    return candidates


def _refine_start(
    client: Any,
    stats: _CallStats,
    *,
    candidate: Dict[str, Any],
    sentences: List[Dict[str, Any]],
    language: str,
    rank: int,
) -> Dict[str, Any]:
    by_id = {int(sentence["id"]): sentence for sentence in sentences}
    try:
        original_start = int(candidate.get("start_sentence"))
    except Exception:
        return {"rank": rank, "candidate": candidate, "drop_reason": "bad id: missing start_sentence"}
    if original_start not in by_id:
        return {"rank": rank, "candidate": candidate, "drop_reason": f"bad id: unknown start {original_start}"}

    window_start = max(1, original_start - 8)
    window_end = min(len(sentences), original_start + 4)
    window = [sentence for sentence in sentences if window_start <= int(sentence["id"]) <= window_end]
    lines = [f"[{sentence['id']}] {sentence['text']}" for sentence in window]
    user_message = f"Language: {_language_name(language)}\nIdea: {candidate.get('idea') or ''}\n\n" + "\n".join(lines)
    messages = [{"role": "system", "content": START_PROMPT}, {"role": "user", "content": user_message}]
    try:
        result = _chat_json(
            client,
            stats,
            messages=messages,
            temperature=0,
        )
        data = result["data"] or {}
        chosen = int(data.get("start_sentence"))
    except Exception as exc:
        return {"rank": rank, "candidate": candidate, "drop_reason": f"bad id: start parse error {exc}"}
    if chosen not in {int(sentence["id"]) for sentence in window}:
        return {"rank": rank, "candidate": candidate, "drop_reason": f"bad id: start {chosen} outside window"}
    feedback = None
    chosen_sentence = by_id.get(chosen)
    matched = _strong_connector_match(str((chosen_sentence or {}).get("text") or ""), language)
    if matched:
        old_id = chosen
        messages.append({"role": "assistant", "content": result["raw"]})
        messages.append(
            {
                "role": "user",
                "content": (
                    f'The sentence you chose starts with "{matched}", which usually continues something said before it. '
                    "Choose a start that a viewer can follow without the earlier part — an earlier setup sentence if "
                    "the idea needs it, otherwise a later sentence. If you are sure this sentence already works on "
                    "its own, return the same id."
                ),
            }
        )
        try:
            retry = _chat_json(client, stats, messages=messages, temperature=0)
            new_id = _parse_start_response(retry["raw"])
            if new_id in {int(sentence["id"]) for sentence in window}:
                chosen = new_id
        except Exception:
            new_id = old_id
        feedback = {"matched": matched, "old_id": old_id, "new_id": chosen}
        logger.info('v10_start_feedback matched="%s" old_id=%s new_id=%s', matched, old_id, chosen)
    return {
        "rank": rank,
        "candidate": candidate,
        "stage1_start": original_start,
        "stage2_start": chosen,
        "start_reason": str(data.get("reason") or "").strip(),
        "start_feedback": feedback,
    }


def _refine_end(
    client: Any,
    stats: _CallStats,
    *,
    item: Dict[str, Any],
    sentences: List[Dict[str, Any]],
    video_duration: float,
) -> Dict[str, Any]:
    if item.get("drop_reason"):
        return item
    by_id = {int(sentence["id"]): sentence for sentence in sentences}
    start_id = int(item["stage2_start"])
    start_sentence = by_id[start_id]
    lines = []
    eligible = set()
    for sentence_id in range(start_id, len(sentences) + 1):
        sentence = by_id[sentence_id]
        duration = float(sentence["end"]) - float(start_sentence["start"])
        if duration > 75.0:
            break
        mark = " ✓" if MIN_CLIP_SECONDS <= duration <= STAGE3_MAX_ELIGIBLE_SECONDS else ""
        if mark:
            eligible.add(sentence_id)
        lines.append(f"[{sentence_id}] (→ {duration:.0f}s){mark} {sentence['text']}")
    if not eligible:
        item["drop_reason"] = "no eligible end"
        return item

    user_message = f"Idea: {(item.get('candidate') or {}).get('idea') or ''}\n\n" + "\n".join(lines)
    try:
        result = _chat_json(
            client,
            stats,
            messages=[{"role": "system", "content": END_PROMPT}, {"role": "user", "content": user_message}],
            temperature=0,
        )
        data = result["data"] or {}
        raw_end = data.get("end_sentence")
        if raw_end is None:
            item["drop_reason"] = f"null end: {str(data.get('reason') or '').strip()}"
            return item
        end_id = int(raw_end)
    except Exception as exc:
        item["drop_reason"] = f"bad id: end parse error {exc}"
        return item
    if end_id not in eligible:
        item["drop_reason"] = f"bad id: end {end_id} not eligible"
        return item
    start, end, duration = _mapped_times(by_id, start_id, end_id, video_duration)
    item.update(
        {
            "end_sentence": end_id,
            "end_reason": str(data.get("reason") or "").strip(),
            "start": round(start, 2),
            "end": round(end, 2),
            "duration": round(duration, 2),
            "text": _clip_text_from_sentences(sentences, start_id, end_id),
        }
    )
    return item


def _append_drop(dropped: List[Dict[str, Any]], item: Dict[str, Any]) -> None:
    dropped.append(
        {
            "rank": item.get("rank"),
            "part": (item.get("candidate") or {}).get("part"),
            "strength": (item.get("candidate") or {}).get("strength"),
            "reason": item.get("drop_reason") or item.get("reason") or "",
            "idea": (item.get("candidate") or {}).get("idea") or item.get("idea") or "",
        }
    )


def _overlaps_existing(item: Dict[str, Any], ranges: List[Tuple[float, float]]) -> bool:
    item_start = float(item["start"])
    item_end = float(item["end"])
    return any(max(item_start, float(start)) < min(item_end, float(end)) for start, end in ranges)


def _item_sort_key(item: Dict[str, Any]) -> Tuple[int, float]:
    candidate = item.get("candidate") or {}
    return (-_clamp_strength(candidate.get("strength")), float(item.get("start") or 0.0))


def _select_balanced_clips(
    refined: List[Dict[str, Any]],
    *,
    target_count: int,
    n_parts: int,
    existing_ranges: List[Tuple[float, float]],
    dropped: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    survivors: List[Dict[str, Any]] = []
    for item in sorted(refined, key=_item_sort_key):
        if _overlaps_existing(item, existing_ranges):
            dropped.append(
                {
                    "rank": item.get("rank"),
                    "part": (item.get("candidate") or {}).get("part"),
                    "strength": (item.get("candidate") or {}).get("strength"),
                    "reason": "overlap existing range",
                    "idea": (item.get("candidate") or {}).get("idea") or "",
                }
            )
            continue
        overlaps = any(
            max(float(item["start"]), float(clip["start"])) < min(float(item["end"]), float(clip["end"]))
            for clip in survivors
        )
        if overlaps:
            dropped.append(
                {
                    "rank": item.get("rank"),
                    "part": (item.get("candidate") or {}).get("part"),
                    "strength": (item.get("candidate") or {}).get("strength"),
                    "reason": "overlap",
                    "idea": (item.get("candidate") or {}).get("idea") or "",
                }
            )
            continue
        survivors.append(item)

    grouped: Dict[int, List[Dict[str, Any]]] = {part: [] for part in range(1, n_parts + 1)}
    for item in survivors:
        part = int((item.get("candidate") or {}).get("part") or 1)
        grouped.setdefault(max(1, min(n_parts, part)), []).append(item)
    for items in grouped.values():
        items.sort(key=_item_sort_key)

    selected: List[Dict[str, Any]] = []
    selected_ids = set()
    base_slots = target_count // max(1, n_parts)
    for part in range(1, n_parts + 1):
        for item in grouped.get(part, [])[:base_slots]:
            selected.append(item)
            selected_ids.add(id(item))

    leftovers = [item for item in survivors if id(item) not in selected_ids]
    for item in sorted(leftovers, key=_item_sort_key):
        if len(selected) >= target_count:
            break
        selected.append(item)
        selected_ids.add(id(item))

    title_anchors = [item for item in survivors if (item.get("candidate") or {}).get("_title_anchor")]
    for anchor in title_anchors:
        if id(anchor) in selected_ids:
            continue
        part = int((anchor.get("candidate") or {}).get("part") or 1)
        same_part = [item for item in selected if int((item.get("candidate") or {}).get("part") or 1) == part]
        if same_part:
            weakest = sorted(same_part, key=lambda item: (_clamp_strength((item.get("candidate") or {}).get("strength")), -float(item.get("start") or 0.0)))[0]
            selected.remove(weakest)
            selected_ids.discard(id(weakest))
            selected.append(anchor)
            selected_ids.add(id(anchor))
        elif len(selected) < target_count:
            selected.append(anchor)
            selected_ids.add(id(anchor))
        elif selected:
            weakest = sorted(
                selected,
                key=lambda item: (
                    _clamp_strength((item.get("candidate") or {}).get("strength")),
                    -float(item.get("start") or 0.0),
                ),
            )[0]
            selected.remove(weakest)
            selected_ids.discard(id(weakest))
            selected.append(anchor)
            selected_ids.add(id(anchor))

    selected = sorted(selected, key=lambda item: float(item.get("start") or 0.0))[:target_count]
    selected_ids = {id(item) for item in selected}
    for item in survivors:
        if id(item) not in selected_ids:
            dropped.append(
                {
                    "rank": item.get("rank"),
                    "part": (item.get("candidate") or {}).get("part"),
                    "strength": (item.get("candidate") or {}).get("strength"),
                    "reason": "rank cut",
                    "idea": (item.get("candidate") or {}).get("idea") or "",
                }
            )
    return selected


def propose_clips_v10(
    segments: List[Dict[str, Any]],
    transcript_text: str,
    duration_seconds: float,
    client: Any,
    model: Optional[str] = None,
    *,
    debug: bool = False,
    plan_focus: str = "",
    focus_categories: Optional[List[str]] = None,
    language: str = "tr",
    video_title: str = "",
    target_clip_count: Optional[int] = None,
    existing_ranges: Optional[List[Tuple[float, float]]] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    del transcript_text, model, debug, plan_focus, focus_categories
    started_at = time.monotonic()
    stats = _CallStats()
    resolved_language = normalize_planning_language(language)
    target_count = target_clip_count or _target_clip_count_for_duration(duration_seconds)
    sentences = build_v10_sentences(segments)
    n_parts = max(2, min(6, int(math.ceil(float(duration_seconds or 0.0) / 900.0))))
    per_part = int(math.ceil((target_count + 3) / n_parts)) + 1
    stage1_mode = "per_part" if float(duration_seconds or 0.0) > 1800.0 else "single"
    debug_info: Dict[str, Any] = {
        "planner": "v10",
        "model": CLIP_PLANNER_V10_MODEL,
        "target_clip_count": target_count,
        "sentence_count": len(sentences),
        "n_parts": n_parts,
        "per_part": per_part,
        "stage1_mode": stage1_mode,
        "dropped_candidates": [],
    }
    if not client:
        raise RuntimeError("OpenAI client unavailable for planner v10")
    if not sentences:
        return [], {**debug_info, **stats.snapshot(), "wall_seconds": round(time.monotonic() - started_at, 2)}

    sentence_parts = _build_sentence_parts(sentences, float(duration_seconds or 0.0), n_parts)
    if stage1_mode == "per_part":
        raw_candidates = _run_stage1_per_part(
            client,
            stats,
            video_title=video_title,
            language=resolved_language,
            parts=sentence_parts,
            per_part=per_part,
            existing_ranges=existing_ranges or [],
        )
    else:
        raw_candidates = _run_stage1(
            client,
            stats,
            video_title=video_title,
            language=resolved_language,
            sentences=sentences,
            target_count=target_count,
            duration_seconds=float(duration_seconds or 0.0),
            n_parts=n_parts,
            per_part=per_part,
        )
    sentences_by_id = {int(sentence["id"]): sentence for sentence in sentences}
    title_anchor = _title_anchor_candidate(video_title, sentences)
    if title_anchor:
        title_anchor["part"] = _candidate_part(title_anchor, sentences_by_id, float(duration_seconds or 0.0), n_parts)
        title_anchor["strength"] = 10
        anchor_start = int(title_anchor["start_sentence"])
        raw_candidates = [
            title_anchor,
            *[
                candidate
                for candidate in raw_candidates
                if _candidate_start_id(candidate) != anchor_start
            ],
        ]
    normalized_candidates = []
    for candidate in raw_candidates:
        if isinstance(candidate, dict):
            candidate = dict(candidate)
            candidate["part"] = _candidate_part(candidate, sentences_by_id, float(duration_seconds or 0.0), n_parts)
            candidate["strength"] = _clamp_strength(candidate.get("strength"))
        normalized_candidates.append(candidate)
    raw_candidates = normalized_candidates
    debug_info["stage1_candidates"] = raw_candidates
    stage1_counts: Dict[int, int] = {part: 0 for part in range(1, n_parts + 1)}
    for candidate in raw_candidates:
        if isinstance(candidate, dict):
            stage1_counts[int(candidate.get("part") or 1)] = stage1_counts.get(int(candidate.get("part") or 1), 0) + 1
    debug_info["stage1_candidates_per_part"] = stage1_counts

    candidate_pairs = []
    dropped: List[Dict[str, Any]] = []
    for rank, candidate in enumerate(raw_candidates, 1):
        if not isinstance(candidate, dict):
            dropped.append({"rank": rank, "reason": "bad candidate", "idea": ""})
            continue
        candidate_pairs.append((rank, candidate))

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(1, len(candidate_pairs)))) as executor:
        start_items = list(
            executor.map(
                lambda pair: _refine_start(
                    client,
                    stats,
                    candidate=pair[1],
                    sentences=sentences,
                    language=resolved_language,
                    rank=pair[0],
                ),
                candidate_pairs,
            )
        )
    debug_info["start_feedback"] = [item["start_feedback"] for item in start_items if item.get("start_feedback")]
    eligible_start_items = []
    for item in start_items:
        if item.get("drop_reason"):
            _append_drop(dropped, item)
        else:
            eligible_start_items.append(item)

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(1, len(eligible_start_items)))) as executor:
        end_items = list(
            executor.map(
                lambda item: _refine_end(
                    client,
                    stats,
                    item=item,
                    sentences=sentences,
                    video_duration=float(duration_seconds or 0.0),
                ),
                eligible_start_items,
            )
        )

    refined = []
    for item in sorted(end_items, key=lambda value: int(value.get("rank") or 0)):
        if item.get("drop_reason"):
            _append_drop(dropped, item)
        else:
            refined.append(item)

    kept = _select_balanced_clips(
        refined,
        target_count=target_count,
        n_parts=n_parts,
        existing_ranges=existing_ranges or [],
        dropped=dropped,
    )
    final_counts: Dict[int, int] = {part: 0 for part in range(1, n_parts + 1)}
    for item in kept:
        part = int((item.get("candidate") or {}).get("part") or 1)
        final_counts[part] = final_counts.get(part, 0) + 1

    final_plan: List[Dict[str, Any]] = []
    for item in kept:
        text = item.get("text") or ""
        title = generate_clip_title(text, language_hint=resolved_language)
        stats.add(None)
        final_plan.append(
            {
                "title": title,
                "start": round(float(item["start"]), 2),
                "end": round(float(item["end"]), 2),
                "duration": round(float(item["duration"]), 2),
                "excerpt": text,
                "transcript_full": text,
                "score": None,
                "score_breakdown": None,
                "v10_idea": (item.get("candidate") or {}).get("idea") or "",
                "v10_start_sentence": item.get("stage2_start"),
                "v10_end_sentence": item.get("end_sentence"),
                "v10_part": (item.get("candidate") or {}).get("part"),
                "v10_strength": (item.get("candidate") or {}).get("strength"),
            }
        )

    wall_seconds = round(time.monotonic() - started_at, 2)
    debug_info.update(stats.snapshot())
    debug_info["title_call_count"] = len(final_plan)
    debug_info["openai_call_count"] = int(debug_info.get("openai_call_count") or 0)
    debug_info["wall_seconds"] = wall_seconds
    debug_info["clips_after_selector_count"] = len(final_plan)
    debug_info["final_plan"] = final_plan
    debug_info["final_clips_per_part"] = final_counts
    debug_info["dropped_candidates"] = dropped
    logger.info(
        "clip_planner_v10_generated clips=%s dropped=%s llm_calls=%s wall_seconds=%.2f mode=%s n_parts=%s stage1_per_part=%s final_per_part=%s",
        len(final_plan),
        len(dropped),
        debug_info.get("openai_call_count") or 0,
        wall_seconds,
        stage1_mode,
        n_parts,
        stage1_counts,
        final_counts,
    )
    return final_plan, debug_info
