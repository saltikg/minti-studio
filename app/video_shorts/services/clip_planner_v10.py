import concurrent.futures
import json
import logging
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

- List clips from strongest to weakest.
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
   "idea": "<one short sentence: what this clip says>",
   "why_start_here": "<short reason>",
   "why_end_here": "<short reason>"}}
]}}"""

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
) -> List[Dict[str, Any]]:
    prompt = V10_STAGE1_PROMPT_TEMPLATE.format(
        min_s=int(MIN_CLIP_SECONDS),
        max_s=int(MAX_CLIP_SECONDS),
        target_count=target_count + 3,
    )
    lines = [f"[{sentence['id']}] ({_fmt_time(sentence['start'])}) {sentence['text']}" for sentence in sentences]
    title_line = f"Video title: {video_title}\n" if str(video_title or "").strip() else ""
    user_message = f"{title_line}Transcript language: {_language_name(language)}\n\n" + "\n".join(lines)
    result = _chat_json(
        client,
        stats,
        messages=[{"role": "system", "content": prompt}, {"role": "user", "content": user_message}],
        temperature=0.2,
    )
    return list((result["data"] or {}).get("clips") or [])


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
            "reason": item.get("drop_reason") or item.get("reason") or "",
            "idea": (item.get("candidate") or {}).get("idea") or item.get("idea") or "",
        }
    )


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
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    del transcript_text, model, debug, plan_focus, focus_categories
    started_at = time.monotonic()
    stats = _CallStats()
    resolved_language = normalize_planning_language(language)
    target_count = target_clip_count or _target_clip_count_for_duration(duration_seconds)
    sentences = build_v10_sentences(segments)
    debug_info: Dict[str, Any] = {
        "planner": "v10",
        "model": CLIP_PLANNER_V10_MODEL,
        "target_clip_count": target_count,
        "sentence_count": len(sentences),
        "dropped_candidates": [],
    }
    if not client:
        raise RuntimeError("OpenAI client unavailable for planner v10")
    if not sentences:
        return [], {**debug_info, **stats.snapshot(), "wall_seconds": round(time.monotonic() - started_at, 2)}

    raw_candidates = _run_stage1(
        client,
        stats,
        video_title=video_title,
        language=resolved_language,
        sentences=sentences,
        target_count=target_count,
    )
    title_anchor = _title_anchor_candidate(video_title, sentences)
    if title_anchor:
        anchor_start = int(title_anchor["start_sentence"])
        raw_candidates = [
            title_anchor,
            *[
                candidate
                for candidate in raw_candidates
                if _candidate_start_id(candidate) != anchor_start
            ],
        ]
    debug_info["stage1_candidates"] = raw_candidates

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

    kept: List[Dict[str, Any]] = []
    for item in refined:
        overlaps = any(max(float(item["start"]), float(clip["start"])) < min(float(item["end"]), float(clip["end"])) for clip in kept)
        if overlaps:
            dropped.append({"rank": item.get("rank"), "reason": "overlap", "idea": (item.get("candidate") or {}).get("idea") or ""})
            continue
        if len(kept) < target_count:
            kept.append(item)

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
            }
        )

    wall_seconds = round(time.monotonic() - started_at, 2)
    debug_info.update(stats.snapshot())
    debug_info["title_call_count"] = len(final_plan)
    debug_info["openai_call_count"] = int(debug_info.get("openai_call_count") or 0)
    debug_info["wall_seconds"] = wall_seconds
    debug_info["clips_after_selector_count"] = len(final_plan)
    debug_info["final_plan"] = final_plan
    debug_info["dropped_candidates"] = dropped
    logger.info(
        "clip_planner_v10_generated clips=%s dropped=%s llm_calls=%s wall_seconds=%.2f",
        len(final_plan),
        len(dropped),
        debug_info.get("openai_call_count") or 0,
        wall_seconds,
    )
    return final_plan, debug_info
