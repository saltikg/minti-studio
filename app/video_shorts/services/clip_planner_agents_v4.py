import json
import logging
import re
import unicodedata
from typing import Any, Dict, List, Optional, Tuple

from app.video_shorts.services.planner_rules_v4 import load_planner_rules_v4

logger = logging.getLogger(__name__)

MAX_REASON_CHARS = 160


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _normalize_for_compare(text: str) -> str:
    if not text:
        return ""
    normalized = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    return stripped.lower()


def _segment_text(seg: Dict[str, Any]) -> str:
    return (seg.get("tr_text") or seg.get("text") or seg.get("ar_text") or "").strip()


def _segment_time(seg: Dict[str, Any], key: str) -> float:
    return _safe_float(seg.get(key), 0.0) or 0.0


def _range_times(segments: List[Dict[str, Any]], start_idx: int, end_idx: int) -> Tuple[float, float]:
    if not segments:
        return 0.0, 0.0
    start_idx = max(0, min(start_idx, len(segments) - 1))
    end_idx = max(start_idx, min(end_idx, len(segments) - 1))
    start = _segment_time(segments[start_idx], "start")
    end = _segment_time(segments[end_idx], "end")
    if end < start:
        end = start
    return start, end


def _range_duration(segments: List[Dict[str, Any]], start_idx: int, end_idx: int) -> float:
    start, end = _range_times(segments, start_idx, end_idx)
    return max(0.0, end - start)


def _range_text(segments: List[Dict[str, Any]], start_idx: int, end_idx: int) -> str:
    if not segments:
        return ""
    start_idx = max(0, min(start_idx, len(segments) - 1))
    end_idx = max(start_idx, min(end_idx, len(segments) - 1))
    parts = []
    for seg in segments[start_idx : end_idx + 1]:
        text = _segment_text(seg)
        if text:
            parts.append(text)
    return " ".join(parts)


def _segment_override_type(segment: Dict[str, Any]) -> str:
    override = (segment.get("non_speech_type") or "").strip().lower()
    if override in {"music", "applause", "silence", "other", "speech"}:
        return override
    return ""


def _prepare_rules(raw_rules: Dict[str, Any]) -> Dict[str, Any]:
    rules = dict(raw_rules or {})
    keywords = rules.get("non_speech_keywords") or []
    patterns = rules.get("non_speech_regex_patterns") or []
    rules["non_speech_keywords_norm"] = [
        v for v in (_normalize_for_compare(v) for v in keywords if v) if v
    ]
    conjunctions = rules.get("conjunction_prefixes") or []
    rules["conjunction_prefixes_norm"] = [
        v for v in (_normalize_for_compare(v) for v in conjunctions if v) if v
    ]
    qa_settings = rules.get("qa_settings") or {}
    qa_keywords = qa_settings.get("qa_keywords") or []
    rules["qa_keywords_norm"] = [
        v for v in (_normalize_for_compare(v) for v in qa_keywords if v) if v
    ]
    compiled = []
    for pattern in patterns:
        try:
            compiled.append(re.compile(pattern, re.IGNORECASE))
        except re.error:
            continue
    rules["regex_compiled"] = compiled
    return rules


def _is_non_speech_segment(text: str, rules: Dict[str, Any]) -> Tuple[bool, str]:
    if not text or not text.strip():
        return True, "empty"
    stripped = text.strip()
    normalized = _normalize_for_compare(stripped)
    for keyword in rules.get("non_speech_keywords_norm") or []:
        if keyword and keyword in normalized:
            return True, "keyword"
    for pattern in rules.get("regex_compiled") or []:
        if pattern.search(stripped):
            return True, "regex"
    min_chars = int(rules.get("min_speech_chars") or 0)
    if len(stripped) < min_chars:
        return True, "min_chars"
    words = stripped.split()
    if len(words) <= 1:
        max_chars = int(rules.get("max_single_word_chars") or 2)
        letters = [ch for ch in stripped if ch.isalnum()]
        if len(letters) <= max_chars:
            return True, "single_word"
    return False, ""


def _build_speech_mask(
    segments: List[Dict[str, Any]],
    rules: Dict[str, Any],
) -> Tuple[List[bool], Dict[str, Any]]:
    speech_mask = []
    reasons = {}
    non_speech_count = 0
    override_count = 0
    for idx, seg in enumerate(segments):
        override = _segment_override_type(seg)
        if override:
            override_count += 1
            if override == "speech":
                speech_mask.append(True)
                continue
            speech_mask.append(False)
            non_speech_count += 1
            reasons[idx] = override
            continue
        is_non_speech, reason = _is_non_speech_segment(_segment_text(seg), rules)
        if is_non_speech:
            speech_mask.append(False)
            non_speech_count += 1
            reasons[idx] = reason
        else:
            speech_mask.append(True)
    summary = {
        "total_segments": len(segments),
        "non_speech_count": non_speech_count,
        "speech_count": len(segments) - non_speech_count,
        "override_applied_count": override_count,
    }
    return speech_mask, {"summary": summary, "reasons": reasons}


def _starts_with_conjunction(text: str, conjunctions_norm: List[str]) -> bool:
    if not text or not conjunctions_norm:
        return False
    first = text.strip().split()[0] if text.strip() else ""
    return _normalize_for_compare(first) in conjunctions_norm


def _estimate_target_clip_count(duration_seconds: float, default_count: int) -> int:
    if not duration_seconds:
        return default_count
    scale = duration_seconds / 1200.0
    scaled = int(round(default_count * scale))
    return max(4, min(15, scaled or default_count))


def _format_example_segments(segments: List[Dict[str, Any]]) -> str:
    lines = []
    for seg in segments:
        idx = seg.get("idx")
        text = seg.get("text") or ""
        start = seg.get("start") or ""
        end = seg.get("end") or ""
        if idx is None:
            continue
        lines.append(f"idx-{idx} | {start} - {end} | {text}")
    return "\n".join(lines)


def _format_example_gold(gold_rows: List[Dict[str, Any]]) -> str:
    lines = []
    for row in gold_rows:
        label = row.get("label") or ""
        start_idx = row.get("start_idx")
        end_idx = row.get("end_idx")
        why_selected = row.get("why_selected") or ""
        lines.append(
            f"- {{label:\"{label}\", start_idx: {start_idx}, end_idx: {end_idx}, "
            f"why_selected:\"{why_selected}\"}}"
        )
    return "\n".join(lines)


def _format_examples(rules: Dict[str, Any]) -> str:
    examples = rules.get("few_shot_examples") or []
    blocks = []
    for example in examples:
        name = example.get("name") or "EXAMPLE"
        segment_blocks = example.get("segment_blocks") or []
        if segment_blocks:
            segment_texts = []
            for block in segment_blocks:
                title = block.get("title") or "Segments subset"
                segments = _format_example_segments(block.get("segments") or [])
                segment_texts.append(f"{title}:\n{segments}")
            segments_text = "\n\n".join(segment_texts)
        else:
            segments = _format_example_segments(example.get("segments") or [])
            segments_text = f"Segments subset (idx | start-end | text):\n{segments}"
        gold = _format_example_gold(example.get("gold") or [])
        blocks.append(f"{name}\n{segments_text}\n\nGold:\n{gold}")
    return "\n\n".join(blocks)


def _build_prompt(segments_payload: List[Dict[str, Any]], rules: Dict[str, Any]) -> str:
    examples_text = _format_examples(rules)
    payload_json = json.dumps(segments_payload, ensure_ascii=False)
    return (
        "You are ClipSelector. Your job is to select short clips from a long video transcript "
        "that has already been segmented.\n\n"
        "CRITICAL RULES\n"
        "1) Do NOT write or paraphrase new text. Do NOT rewrite the transcript. Selection only.\n"
        "2) Each short must be a contiguous range of existing segments: start_idx and end_idx.\n"
        "3) Avoid non-speech segments: music-only, silence, applause, laughter, noise, sfx, or metadata. "
        "If a segment looks non-speech, do not include it.\n"
        "4) Prefer self-contained clips with a clear start and a clear ending; avoid starting/ending mid-thought.\n"
        "5) Output MUST be JSON only. No extra commentary outside JSON.\n"
        "6) For each selected clip output:\n"
        "   - label: story|qa|tefsir|principle|other\n"
        "   - start_idx (int)\n"
        "   - end_idx (int)\n"
        "   - confidence (0.0-1.0)\n"
        "   - why_selected (exactly 1 sentence, Turkish, short, concrete, must NOT quote transcript)\n\n"
        "OUTPUT SCHEMA (JSON)\n"
        "{\n"
        "  \"shorts\": [\n"
        "    { \"label\":\"\", \"start_idx\": 0, \"end_idx\": 0, \"confidence\": 0.0, \"why_selected\": \"\" }\n"
        "  ]\n"
        "}\n\n"
        "FEW SHOT EXAMPLES\n\n"
        f"{examples_text}\n\n"
        "NOW YOUR TASK\n"
        "Given a NEW long transcript (segments with idx, start/end, text), select 6 to 10 shorts.\n"
        "Return JSON only in the exact schema.\n\n"
        "INPUT (NEW TRANSCRIPT SEGMENTS)\n"
        f"{payload_json}"
    )


def _call_selector_llm(client, model: str, prompt: str) -> Tuple[str, bool]:
    if not client:
        return "", False
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": prompt}],
            response_format={"type": "json_object"},
        )
    except Exception:
        return "", False
    raw = resp.choices[0].message.content if resp.choices else ""
    return raw or "", bool(raw)


def _sanitize_reason(reason: str, clip_text: str) -> Tuple[str, bool]:
    reason = (reason or "").strip()
    if not reason:
        return "", False
    if len(reason) > MAX_REASON_CHARS:
        reason = reason[:MAX_REASON_CHARS].rstrip()
    if clip_text and clip_text in reason:
        return "", True
    return reason, False


def _parse_llm_json(raw: str) -> Tuple[Optional[Dict[str, Any]], bool]:
    if not raw:
        return None, False
    try:
        return json.loads(raw), True
    except Exception:
        return None, False


def _validate_candidates(
    data: Dict[str, Any],
    segments: List[Dict[str, Any]],
    speech_mask: List[bool],
    rules: Dict[str, Any],
    min_seconds: float,
    max_seconds: float,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    allowed_labels = rules.get("allowed_labels") or []
    conjunctions = rules.get("conjunction_prefixes_norm") or []
    candidates = []
    rejected = []
    shorts = data.get("shorts") or []
    for idx, item in enumerate(shorts):
        allowed_keys = {"label", "start_idx", "end_idx", "confidence", "why_selected"}
        extra_keys = set(item.keys()) - allowed_keys if isinstance(item, dict) else {"invalid_item"}
        if extra_keys:
            rejected.append({"idx": idx, "reason": "extra_keys", "raw": item})
            continue
        raw_label = (item.get("label") or "").strip()
        label = raw_label if raw_label in allowed_labels else (allowed_labels[0] if allowed_labels else raw_label)
        try:
            start_idx = int(item.get("start_idx"))
            end_idx = int(item.get("end_idx"))
        except Exception:
            rejected.append({"idx": idx, "reason": "invalid_index", "raw": item})
            continue
        if start_idx < 0 or end_idx < 0 or start_idx > end_idx or end_idx >= len(segments):
            rejected.append({"idx": idx, "reason": "out_of_bounds", "raw": item})
            continue
        if not (speech_mask[start_idx] and speech_mask[end_idx]):
            rejected.append({"idx": idx, "reason": "edge_non_speech", "raw": item})
            continue
        if any(not speech_mask[i] for i in range(start_idx, end_idx + 1)):
            rejected.append({"idx": idx, "reason": "contains_non_speech", "raw": item})
            continue
        if _starts_with_conjunction(_segment_text(segments[start_idx]), conjunctions) and start_idx > 0:
            if speech_mask[start_idx - 1]:
                start_idx -= 1
        duration = _range_duration(segments, start_idx, end_idx)
        if duration < min_seconds or duration > max_seconds:
            rejected.append({"idx": idx, "reason": "duration_bounds", "raw": item})
            continue
        confidence = max(0.0, min(1.0, _safe_float(item.get("confidence"), 0.0)))
        clip_text = _range_text(segments, start_idx, end_idx)
        reason, had_snippet = _sanitize_reason(item.get("why_selected") or "", clip_text)
        if had_snippet:
            rejected.append({"idx": idx, "reason": "snippet_stripped", "raw": item})
            reason = ""
        candidates.append(
            {
                "label": label or "other",
                "start_segment_idx": start_idx,
                "end_segment_idx": end_idx,
                "confidence": confidence,
                "reason": reason,
            }
        )
    return candidates, rejected


def _dedupe_by_overlap(
    candidates: List[Dict[str, Any]],
    segments: List[Dict[str, Any]],
    max_overlap_ratio: float,
    min_gap_seconds: float,
) -> List[Dict[str, Any]]:
    sorted_cands = sorted(candidates, key=lambda c: c.get("confidence", 0.0), reverse=True)
    chosen = []
    for cand in sorted_cands:
        start_time, end_time = _range_times(segments, cand["start_segment_idx"], cand["end_segment_idx"])
        cand["start_time"] = start_time
        cand["end_time"] = end_time
        cand["duration"] = max(0.0, end_time - start_time)
        overlaps = False
        for existing in chosen:
            overlap = max(0.0, min(end_time, existing["end_time"]) - max(start_time, existing["start_time"]))
            duration = max(cand["duration"], 1.0)
            if overlap / duration >= max_overlap_ratio:
                overlaps = True
                break
            if min_gap_seconds and abs(start_time - existing["end_time"]) < min_gap_seconds:
                overlaps = True
                break
            if min_gap_seconds and abs(existing["start_time"] - end_time) < min_gap_seconds:
                overlaps = True
                break
        if not overlaps:
            chosen.append(cand)
    return chosen


def _heuristic_candidates(
    segments: List[Dict[str, Any]],
    speech_mask: List[bool],
    min_seconds: float,
    max_seconds: float,
) -> List[Dict[str, Any]]:
    candidates = []
    idx = 0
    while idx < len(segments):
        if not speech_mask[idx]:
            idx += 1
            continue
        start_idx = idx
        end_idx = idx
        while end_idx < len(segments) and speech_mask[end_idx]:
            duration = _range_duration(segments, start_idx, end_idx)
            if duration >= min_seconds:
                break
            end_idx += 1
        if end_idx >= len(segments) or not speech_mask[end_idx]:
            idx = end_idx + 1
            continue
        duration = _range_duration(segments, start_idx, end_idx)
        while duration > max_seconds and end_idx > start_idx:
            end_idx -= 1
            duration = _range_duration(segments, start_idx, end_idx)
        if min_seconds <= duration <= max_seconds:
            candidates.append(
                {
                    "label": "other",
                    "start_segment_idx": start_idx,
                    "end_segment_idx": end_idx,
                    "confidence": 0.3,
                    "reason": "heuristic",
                }
            )
            idx = end_idx + 1
        else:
            idx += 1
    return candidates


def propose_clips_with_agents_v4(
    segments: List[Dict[str, Any]],
    transcript_text: str,
    duration_seconds: float,
    client,
    model: str,
    debug: bool = False,
    target_clip_count: Optional[int] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    debug_info: Dict[str, Any] = {
        "rules_snapshot": {},
        "non_speech_filtering": {},
        "llm": {"raw_output": "", "json_valid": False, "retry_used": False},
        "candidates": {"normalized": [], "rejected": []},
        "final_selected": [],
    }

    raw_rules = load_planner_rules_v4()
    rules = _prepare_rules(raw_rules)
    debug_info["rules_snapshot"] = dict(raw_rules)

    speech_mask, speech_meta = _build_speech_mask(segments, rules)
    debug_info["non_speech_filtering"] = {
        "summary": speech_meta.get("summary"),
        "reasons": speech_meta.get("reasons"),
    }

    filtered_segments = []
    for idx, seg in enumerate(segments):
        if idx < len(speech_mask) and speech_mask[idx]:
            filtered_segments.append(
                {
                    "idx": idx,
                    "start": _segment_time(seg, "start"),
                    "end": _segment_time(seg, "end"),
                    "text": _segment_text(seg),
                }
            )

    min_clip_seconds = float(rules.get("min_clip_seconds") or 25.0)
    max_clip_seconds = float(rules.get("max_clip_seconds") or 60.0)
    max_overlap_ratio = float(rules.get("max_overlap_ratio") or 0.85)
    min_gap_seconds = float(rules.get("min_gap_seconds") or 0.0)
    max_per_label = int(rules.get("max_per_label") or 3)
    default_target = int(rules.get("target_clip_count_default") or 8)

    if target_clip_count is None:
        target_clip_count = _estimate_target_clip_count(duration_seconds, default_target)
    logger.info("V4 target clip count: %s", target_clip_count)

    llm_raw = ""
    llm_data = None
    json_valid = False
    if client and filtered_segments:
        prompt = _build_prompt(filtered_segments, rules)
        llm_raw, _ = _call_selector_llm(client, model, prompt)
        llm_data, json_valid = _parse_llm_json(llm_raw)
        if not json_valid:
            debug_info["llm"]["retry_used"] = True
            llm_raw, _ = _call_selector_llm(client, model, prompt)
            llm_data, json_valid = _parse_llm_json(llm_raw)

    debug_info["llm"]["raw_output"] = llm_raw
    debug_info["llm"]["json_valid"] = json_valid

    candidates: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []
    if llm_data and json_valid:
        candidates, rejected = _validate_candidates(
            llm_data, segments, speech_mask, rules, min_clip_seconds, max_clip_seconds
        )

    if not candidates:
        rejected.append({"idx": -1, "reason": "llm_empty_or_invalid"})
        candidates = _heuristic_candidates(segments, speech_mask, min_clip_seconds, max_clip_seconds)

    deduped = _dedupe_by_overlap(candidates, segments, max_overlap_ratio, min_gap_seconds)

    selected = []
    per_label = {}
    for cand in deduped:
        label = cand.get("label") or "other"
        if max_per_label and per_label.get(label, 0) >= max_per_label:
            rejected.append({"idx": -1, "reason": "max_per_label", "candidate": cand})
            continue
        selected.append(cand)
        per_label[label] = per_label.get(label, 0) + 1
        if len(selected) >= target_clip_count:
            break

    final_plan = []
    for cand in selected:
        start_idx = cand["start_segment_idx"]
        end_idx = cand["end_segment_idx"]
        start_time, end_time = _range_times(segments, start_idx, end_idx)
        text = _range_text(segments, start_idx, end_idx)
        final_plan.append(
            {
                "start_segment_idx": start_idx,
                "end_segment_idx": end_idx,
                "start_time": round(start_time, 2),
                "end_time": round(end_time, 2),
                "duration": round(max(0.0, end_time - start_time), 2),
                "label": cand.get("label") or "other",
                "confidence": cand.get("confidence", 0.0),
                "reason": cand.get("reason") or "",
                "text": text,
                "excerpt": text,
            }
        )

    debug_info["candidates"]["normalized"] = candidates
    debug_info["candidates"]["rejected"] = rejected
    debug_info["final_selected"] = final_plan

    return final_plan, debug_info
