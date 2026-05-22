import json
import logging
import re
import unicodedata
from typing import List, Dict, Any, Tuple, Optional

from app.video_shorts.services.non_speech_rules import load_non_speech_rules

logger = logging.getLogger(__name__)

BLOCK_MIN_SECONDS = 60.0
BLOCK_MAX_SECONDS = 180.0
MAX_REASON_CHARS = 160
MAX_SNIPPET_CHARS = 120

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


def _trim_text(text: str, limit: int) -> str:
    if not text:
        return ""
    return text if len(text) <= limit else text[:limit]


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
    start_idx = max(0, min(start_idx, len(segments) - 1))
    end_idx = max(start_idx, min(end_idx, len(segments) - 1))
    parts = []
    for seg in segments[start_idx : end_idx + 1]:
        text = (seg.get("text") or "").strip()
        if text:
            parts.append(text)
    return " ".join(parts)


def _starts_with_conjunction(text: str, conjunctions_norm: List[str]) -> bool:
    if not text or not conjunctions_norm:
        return False
    first = text.strip().split()[0] if text.strip() else ""
    return _normalize_for_compare(first) in conjunctions_norm


def _prepare_rules(raw_rules: Dict[str, Any]) -> Dict[str, Any]:
    rules = dict(raw_rules or {})
    keywords = rules.get("non_speech_keywords") or rules.get("keywords") or []
    patterns = rules.get("non_speech_regex_patterns") or rules.get("regex_patterns") or []
    rules["non_speech_keywords_norm"] = [
        v for v in (_normalize_for_compare(v) for v in keywords if v) if v
    ]
    rules["qa_keywords_norm"] = [
        v for v in (_normalize_for_compare(v) for v in (rules.get("qa_keywords") or []) if v) if v
    ]
    conjunctions = rules.get("conjunction_prefixes") or []
    rules["conjunction_prefixes_norm"] = [
        v for v in (_normalize_for_compare(v) for v in conjunctions if v) if v
    ]
    compiled = []
    for pattern in patterns:
        try:
            compiled.append(re.compile(pattern, re.IGNORECASE))
        except re.error:
            continue
    rules["regex_compiled"] = compiled
    return rules


def _segment_override_type(segment: Dict[str, Any]) -> str:
    override = (segment.get("non_speech_type") or "").strip().lower()
    if override in {"music", "applause", "silence", "other", "speech"}:
        return override
    return ""


def _is_non_speech_segment(text: str, rules: Dict[str, Any]) -> Tuple[bool, str]:
    if not text or not text.strip():
        return True, "empty"
    stripped = text.strip()
    normalized = _normalize_for_compare(stripped)
    for keyword in rules.get("non_speech_keywords_norm") or []:
        if keyword and keyword in normalized:
            if "music" in keyword or "muzik" in keyword:
                return True, "music"
            if "alkis" in keyword or "applause" in keyword:
                return True, "applause"
            if "kahkaha" in keyword or "laughter" in keyword:
                return True, "laughter"
            if "noise" in keyword:
                return True, "noise"
            if "sfx" in keyword or "sound effect" in keyword or "efekt" in keyword:
                return True, "sfx"
            if "silence" in keyword or "sessizlik" in keyword:
                return True, "silence"
            return True, "non_speech"
    for pattern in rules.get("regex_compiled") or []:
        if pattern.search(stripped):
            return True, "non_speech"
    min_chars = int(rules.get("min_speech_chars") or rules.get("min_chars") or 0)
    if len(stripped) < min_chars:
        return True, "silence"
    words = stripped.split()
    if len(words) <= 1:
        max_chars = int(rules.get("max_single_word_chars") or 2)
        letters = [ch for ch in stripped if ch.isalnum()]
        if len(letters) <= max_chars:
            return True, "silence"
    return False, ""


def _build_speech_mask(
    segments: List[Dict[str, Any]],
    rules: Dict[str, Any],
) -> Tuple[List[bool], Dict[str, Any]]:
    speech_mask = []
    non_speech_count = 0
    reasons = {}
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
        is_non_speech, reason = _is_non_speech_segment(seg.get("text") or "", rules)
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


def _first_speech_time(segments: List[Dict[str, Any]], speech_mask: List[bool]) -> float:
    for idx, seg in enumerate(segments):
        if idx < len(speech_mask) and speech_mask[idx]:
            return _segment_time(seg, "start")
    return 0.0


def _speech_ratio_in_range(mask: List[bool], start_idx: int, end_idx: int) -> float:
    if start_idx > end_idx or not mask:
        return 0.0
    total = end_idx - start_idx + 1
    speech = sum(1 for idx in range(start_idx, end_idx + 1) if idx < len(mask) and mask[idx])
    return speech / max(total, 1)


def _clip_non_speech_ratio(mask: List[bool], start_idx: int, end_idx: int) -> float:
    return 1.0 - _speech_ratio_in_range(mask, start_idx, end_idx)


def _find_nearest_speech_start(mask: List[bool], start_idx: int) -> Optional[int]:
    for idx in range(start_idx, len(mask)):
        if mask[idx]:
            return idx
    return None


def _find_nearest_speech_end(mask: List[bool], end_idx: int) -> Optional[int]:
    for idx in range(end_idx, -1, -1):
        if mask[idx]:
            return idx
    return None


def _build_snippet(text: str) -> str:
    if not text:
        return ""
    if len(text) <= MAX_SNIPPET_CHARS * 2:
        return text
    head = text[:MAX_SNIPPET_CHARS].rstrip()
    tail = text[-MAX_SNIPPET_CHARS:].lstrip()
    return f"{head} ... {tail}"


def _sanitize_reason_by_snippet(reason: str, snippet: str) -> str:
    if not reason:
        return ""
    if snippet and snippet in reason:
        return ""
    return reason


def _is_question_signal(text: str, qa_keywords: List[str]) -> bool:
    if not text:
        return False
    if "?" in text:
        return True
    normalized = _normalize_for_compare(text)
    for keyword in qa_keywords:
        if keyword and keyword in normalized:
            return True
    return False


def _find_question_end_idx(
    segments: List[Dict[str, Any]],
    start_idx: int,
    end_idx: int,
    qa_keywords: List[str],
) -> Optional[int]:
    for idx in range(start_idx, end_idx + 1):
        if _is_question_signal(segments[idx].get("text") or "", qa_keywords):
            return idx
    return None


def _estimate_target_clip_count(duration_seconds: float, default_count: int) -> int:
    if not duration_seconds:
        return default_count
    scale = duration_seconds / 1200.0
    scaled = int(round(default_count * scale))
    return max(4, min(15, scaled or default_count))


def _chunk_segments_by_time(
    segments: List[Dict[str, Any]],
    speech_mask: List[bool],
    chunk_seconds: float = 180.0,
) -> List[Dict[str, Any]]:
    chunks = []
    current = []
    chunk_start = None
    for idx, seg in enumerate(segments):
        if idx < len(speech_mask) and not speech_mask[idx]:
            continue
        seg_start = _segment_time(seg, "start")
        if chunk_start is None:
            chunk_start = seg_start
        if seg_start - chunk_start > chunk_seconds and current:
            chunks.append({"segment_idxs": current})
            current = []
            chunk_start = seg_start
        current.append(idx)
    if current:
        chunks.append({"segment_idxs": current})
    return chunks


def _call_segmenter(
    client,
    model: str,
    segments: List[Dict[str, Any]],
    speech_mask: List[bool],
    chunk_seconds: float,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    if not client:
        return [], {"json_valid": False, "fallback_reason": "no_client"}
    chunks = _chunk_segments_by_time(segments, speech_mask, chunk_seconds)
    blocks = []
    for chunk in chunks:
        idxs = chunk.get("segment_idxs") or []
        lines = []
        for idx in idxs:
            seg = segments[idx]
            text = _trim_text(seg.get("text") or "", 160)
            lines.append(f"[{idx}] {text}")
        payload = {
            "segments": lines,
            "min_seconds": BLOCK_MIN_SECONDS,
            "max_seconds": BLOCK_MAX_SECONDS,
        }
        system_prompt = (
            "Transcript block segmenter. Return only JSON:\n"
            "{ \"blocks\": [ {\"start_segment_idx\": int, \"end_segment_idx\": int, "
            "\"label_hint\": \"qa|story|principle|tafsir|sahaba|other\", \"confidence\": 0..1} ] }\n"
            "Use only indices from input. No transcript text."
        )
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(payload)},
                ],
                response_format={"type": "json_object"},
            )
        except Exception:
            continue
        raw = resp.choices[0].message.content if resp.choices else ""
        try:
            data = json.loads(raw)
            chunk_blocks = data.get("blocks") or []
        except Exception:
            continue
        for block in chunk_blocks:
            try:
                start_idx = int(block.get("start_segment_idx"))
                end_idx = int(block.get("end_segment_idx"))
            except Exception:
                continue
            if start_idx < 0 or end_idx < 0 or start_idx > end_idx:
                continue
            blocks.append(
                {
                    "start_segment_idx": start_idx,
                    "end_segment_idx": end_idx,
                    "label_hint": block.get("label_hint") or "other",
                    "confidence": float(block.get("confidence") or 0.0),
                }
            )
    if not blocks:
        return [], {"json_valid": False, "fallback_reason": "empty_blocks"}
    return blocks, {"json_valid": True, "fallback_reason": ""}


def _heuristic_blocks(segments: List[Dict[str, Any]], speech_mask: List[bool]) -> List[Dict[str, Any]]:
    blocks = []
    current_start = None
    last_idx = None
    for idx, seg in enumerate(segments):
        if idx < len(speech_mask) and not speech_mask[idx]:
            continue
        if current_start is None:
            current_start = idx
        last_idx = idx
        duration = _range_duration(segments, current_start, last_idx)
        if duration >= BLOCK_MAX_SECONDS:
            blocks.append(
                {
                    "start_segment_idx": current_start,
                    "end_segment_idx": last_idx,
                    "label_hint": "other",
                    "confidence": 0.3,
                }
            )
            current_start = None
            last_idx = None
    if current_start is not None and last_idx is not None:
        blocks.append(
            {
                "start_segment_idx": current_start,
                "end_segment_idx": last_idx,
                "label_hint": "other",
                "confidence": 0.3,
            }
        )
    return blocks


def _postprocess_blocks(
    segments: List[Dict[str, Any]],
    blocks: List[Dict[str, Any]],
    min_seconds: float,
    max_seconds: float,
) -> List[Dict[str, Any]]:
    if not blocks:
        return []
    sorted_blocks = sorted(blocks, key=lambda b: b.get("start_segment_idx", 0))
    merged = []
    for block in sorted_blocks:
        start_idx = block.get("start_segment_idx")
        end_idx = block.get("end_segment_idx")
        if start_idx is None or end_idx is None:
            continue
        if not merged:
            merged.append(dict(block))
            continue
        last = merged[-1]
        last_start = last.get("start_segment_idx")
        last_end = last.get("end_segment_idx")
        if last_start is None or last_end is None:
            merged[-1] = dict(block)
            continue
        overlap = start_idx <= last_end
        gap_duration = _range_duration(segments, last_end, start_idx) if not overlap else 0.0
        if overlap or gap_duration <= 5.0:
            last["end_segment_idx"] = max(last_end, end_idx)
            last["label_hint"] = last.get("label_hint") or block.get("label_hint")
            last["confidence"] = max(last.get("confidence", 0.0), block.get("confidence", 0.0))
        else:
            merged.append(dict(block))
    adjusted = []
    for block in merged:
        start_idx = block.get("start_segment_idx")
        end_idx = block.get("end_segment_idx")
        if start_idx is None or end_idx is None:
            continue
        duration = _range_duration(segments, start_idx, end_idx)
        if duration < min_seconds:
            adjusted.append(block)
            continue
        if duration > max_seconds:
            new_end = end_idx
            while duration > max_seconds and new_end > start_idx:
                new_end -= 1
                duration = _range_duration(segments, start_idx, new_end)
            block["end_segment_idx"] = new_end
        adjusted.append(block)
    return adjusted


def _call_global_refine(
    client,
    model: str,
    blocks: List[Dict[str, Any]],
    min_seconds: float,
    max_seconds: float,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    if not client or not blocks:
        return blocks, {"json_valid": False, "fallback_reason": "no_client_or_blocks"}
    payload = {
        "blocks": [
            {
                "start_segment_idx": b.get("start_segment_idx"),
                "end_segment_idx": b.get("end_segment_idx"),
            }
            for b in blocks
        ],
        "min_seconds": min_seconds,
        "max_seconds": max_seconds,
    }
    system_prompt = (
        "Refine block boundaries only. Return JSON:\n"
        "{ \"blocks\": [ {\"start_segment_idx\": int, \"end_segment_idx\": int} ] }\n"
        "Use only indices, no transcript text."
    )
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(payload)},
            ],
            response_format={"type": "json_object"},
        )
    except Exception:
        return blocks, {"json_valid": False, "fallback_reason": "llm_error"}
    raw = resp.choices[0].message.content if resp.choices else ""
    try:
        data = json.loads(raw)
        refined = data.get("blocks") or []
    except Exception:
        return blocks, {"json_valid": False, "fallback_reason": "json_parse_error"}
    cleaned = []
    for block in refined:
        try:
            start_idx = int(block.get("start_segment_idx"))
            end_idx = int(block.get("end_segment_idx"))
        except Exception:
            continue
        if start_idx < 0 or end_idx < 0 or start_idx > end_idx:
            continue
        cleaned.append(
            {
                "start_segment_idx": start_idx,
                "end_segment_idx": end_idx,
                "label_hint": "other",
                "confidence": 0.2,
            }
        )
    if not cleaned:
        return blocks, {"json_valid": False, "fallback_reason": "empty_blocks"}
    return cleaned, {"json_valid": True, "fallback_reason": ""}


def _call_router(
    client,
    model: str,
    segments: List[Dict[str, Any]],
    idxs: List[int],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    if not client:
        return {"labels": ["other"], "confidence": 0.0, "evidence_segment_idxs": []}, {
            "json_valid": False,
            "fallback_reason": "no_client",
        }
    lines = []
    for idx in idxs[:40]:
        seg = segments[idx]
        text = _trim_text(seg.get("text") or "", 120)
        lines.append(f"[{idx}] {text}")
    payload = {"segments": lines}
    system_prompt = (
        "Turkce transcript block router. Return JSON only:\n"
        "{ \"labels\": [\"qa\"|\"story\"|\"tafsir\"|\"sahaba\"|\"principle\"|\"other\"], "
        "\"confidence\": 0..1, \"evidence_segment_idxs\": [int] }\n"
        "No transcript text."
    )
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(payload)},
            ],
            response_format={"type": "json_object"},
        )
    except Exception:
        return {"labels": ["other"], "confidence": 0.0, "evidence_segment_idxs": []}, {
            "json_valid": False,
            "fallback_reason": "llm_error",
        }
    raw = resp.choices[0].message.content if resp.choices else ""
    try:
        data = json.loads(raw)
    except Exception:
        return {"labels": ["other"], "confidence": 0.0, "evidence_segment_idxs": []}, {
            "json_valid": False,
            "fallback_reason": "json_parse_error",
        }
    labels = data.get("labels") or []
    if isinstance(labels, str):
        labels = [labels]
    allowed = {"qa", "story", "tafsir", "sahaba", "principle", "other"}
    cleaned = [lbl for lbl in labels if lbl in allowed] or ["other"]
    confidence = _safe_float(data.get("confidence"), 0.0)
    evidence = []
    for idx in data.get("evidence_segment_idxs") or []:
        try:
            idx_val = int(idx)
        except Exception:
            continue
        if idx_val in idxs:
            evidence.append(idx_val)
    return {
        "labels": cleaned,
        "confidence": max(0.0, min(1.0, confidence)),
        "evidence_segment_idxs": evidence,
    }, {"json_valid": True, "fallback_reason": ""}


def _call_highlight_specialist(
    client,
    model: str,
    segments: List[Dict[str, Any]],
    idxs: List[int],
    min_seconds: float,
    max_seconds: float,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    if not client:
        return [], {"json_valid": False, "fallback_reason": "no_client"}
    lines = []
    for idx in idxs:
        seg = segments[idx]
        text = _trim_text(seg.get("text") or "", 200)
        lines.append(f"[{idx}] {text}")
    payload = {
        "segments": lines,
        "min_seconds": min_seconds,
        "max_seconds": max_seconds,
    }
    system_prompt = (
        "Highlight specialist. Return JSON only:\n"
        "{ \"candidates\": [ {\"start_segment_idx\": int, \"end_segment_idx\": int, "
        "\"confidence\": 0..1, \"reason\": \"short\", \"evidence_segment_idxs\": [int], "
        "\"label\": \"qa|story|principle|tafsir|sahaba|other\"} ] }\n"
        "Use only indices from input. No transcript text."
    )
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(payload)},
            ],
            response_format={"type": "json_object"},
        )
    except Exception:
        return [], {"json_valid": False, "fallback_reason": "llm_error"}
    raw = resp.choices[0].message.content if resp.choices else ""
    try:
        data = json.loads(raw)
        candidates = data.get("candidates") or []
    except Exception:
        return [], {"json_valid": False, "fallback_reason": "json_parse_error"}
    cleaned = []
    for cand in candidates:
        try:
            start_idx = int(cand.get("start_segment_idx"))
            end_idx = int(cand.get("end_segment_idx"))
        except Exception:
            continue
        if start_idx not in idxs or end_idx not in idxs:
            continue
        if start_idx > end_idx:
            continue
        confidence = max(0.0, min(1.0, _safe_float(cand.get("confidence"), 0.0)))
        reason = _trim_text((cand.get("reason") or "").strip(), MAX_REASON_CHARS)
        evidence = []
        for eidx in cand.get("evidence_segment_idxs") or []:
            try:
                val = int(eidx)
            except Exception:
                continue
            if val in idxs:
                evidence.append(val)
        label = cand.get("label") or "other"
        cleaned.append(
            {
                "start_segment_idx": start_idx,
                "end_segment_idx": end_idx,
                "confidence": confidence,
                "reason": reason,
                "evidence_segment_idxs": evidence,
                "label": label,
            }
        )
    if not cleaned:
        return [], {"json_valid": False, "fallback_reason": "empty_candidates"}
    return cleaned, {"json_valid": True, "fallback_reason": ""}


def _fix_candidate_range(
    segments: List[Dict[str, Any]],
    speech_mask: List[bool],
    start_idx: int,
    end_idx: int,
    conjunctions_norm: List[str],
    min_seconds: float,
    max_seconds: float,
) -> Optional[Tuple[int, int]]:
    if start_idx > end_idx:
        return None
    start_idx = _find_nearest_speech_start(speech_mask, start_idx) or start_idx
    end_idx = _find_nearest_speech_end(speech_mask, end_idx) or end_idx
    duration = _range_duration(segments, start_idx, end_idx)
    if duration < min_seconds:
        right = end_idx + 1
        left = start_idx - 1
        while duration < min_seconds and right < len(segments):
            if right < len(speech_mask) and speech_mask[right]:
                end_idx = right
                duration = _range_duration(segments, start_idx, end_idx)
            right += 1
        while duration < min_seconds and left >= 0:
            if left < len(speech_mask) and speech_mask[left]:
                start_idx = left
                duration = _range_duration(segments, start_idx, end_idx)
            left -= 1
    elif duration > max_seconds:
        while duration > max_seconds and start_idx < end_idx:
            start_idx += 1
            duration = _range_duration(segments, start_idx, end_idx)
        while duration > max_seconds and start_idx < end_idx:
            end_idx -= 1
            duration = _range_duration(segments, start_idx, end_idx)
    if _starts_with_conjunction(segments[start_idx].get("text") or "", conjunctions_norm) and start_idx > 0:
        if speech_mask[start_idx - 1]:
            start_idx -= 1
    if _range_duration(segments, start_idx, end_idx) < min_seconds:
        return None
    return start_idx, end_idx


def _dedupe_candidates(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    sorted_cands = sorted(candidates, key=lambda c: c.get("start_time", 0.0))
    deduped = []
    for cand in sorted_cands:
        if not deduped:
            deduped.append(cand)
            continue
        skip = False
        for existing in reversed(deduped[-5:]):
            start_a = _safe_float(cand.get("start_time"), 0.0)
            end_a = _safe_float(cand.get("end_time"), 0.0)
            start_b = _safe_float(existing.get("start_time"), 0.0)
            end_b = _safe_float(existing.get("end_time"), 0.0)
            if start_a == start_b and end_a == end_b:
                if (cand.get("confidence") or 0.0) > (existing.get("confidence") or 0.0):
                    deduped[-1] = cand
                break
            overlap = max(0.0, min(end_a, end_b) - max(start_a, start_b))
            duration = max(end_a - start_a, 1.0)
            if overlap / duration >= 0.85:
                if (cand.get("confidence") or 0.0) > (existing.get("confidence") or 0.0):
                    deduped[-1] = cand
                else:
                    skip = True
                break
        if not skip:
            deduped.append(cand)
    return deduped


def _selector_scores(
    client,
    model: str,
    candidates: List[Dict[str, Any]],
) -> Tuple[Optional[Dict[str, Dict[str, Any]]], Dict[str, Any]]:
    if not client or not candidates:
        return None, {"json_valid": False, "fallback_reason": "no_client_or_candidates"}
    payload = {
        "clips": [
            {
                "candidate_id": c["candidate_id"],
                "label": c.get("label"),
                "duration_seconds": c.get("duration"),
                "snippet": c.get("snippet") or "",
            }
            for c in candidates
        ]
    }
    system_prompt = (
        "Score candidates. Return JSON only:\n"
        "{ \"scores\": [ {\"candidate_id\": str, \"score\": 0..100, \"reason\": \"short\", "
        "\"subs\": {\"clarity\":0..10,\"payoff\":0..10,\"filler\":0..10,\"intensity\":0..10} } ] }\n"
        "Do not output transcript text."
    )
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(payload)},
            ],
            response_format={"type": "json_object"},
        )
    except Exception:
        return None, {"json_valid": False, "fallback_reason": "llm_error"}
    raw = resp.choices[0].message.content if resp.choices else ""
    try:
        data = json.loads(raw)
        scores = data.get("scores") or []
    except Exception:
        return None, {"json_valid": False, "fallback_reason": "json_parse_error"}
    score_map = {}
    for row in scores:
        cid = row.get("candidate_id")
        if not cid or cid not in {c["candidate_id"] for c in candidates}:
            continue
        reason = _trim_text((row.get("reason") or "").strip(), MAX_REASON_CHARS)
        snippet = next((c.get("snippet") for c in candidates if c["candidate_id"] == cid), "")
        if snippet and snippet in reason:
            reason = ""
        score_map[cid] = {
            "score": _safe_float(row.get("score"), 0.0),
            "reason": reason,
            "subs": row.get("subs") or {},
        }
    if not score_map:
        return None, {"json_valid": False, "fallback_reason": "empty_scores"}
    return score_map, {"json_valid": True, "fallback_reason": ""}


def _heuristic_scores(candidates: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    scores = {}
    for cand in candidates:
        duration = cand.get("duration") or 0.0
        score = 50.0
        if 30 <= duration <= 60:
            score += 15.0
        scores[cand["candidate_id"]] = {"score": score, "reason": "heuristic", "subs": {}}
    return scores


def propose_clips_with_agents_v3(
    segments: List[Dict[str, Any]],
    transcript_text: str,
    duration_seconds: float,
    client,
    model: str,
    debug: bool = False,
    target_clip_count: Optional[int] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    debug_info: Dict[str, Any] = {
        "rules_loaded": False,
        "rules_snapshot": {},
        "speech_mask_summary": {},
        "non_speech_skipped_count": 0,
        "intro_skipped_seconds": 0.0,
        "candidates_rejected_due_to_non_speech": 0,
        "blocks": [],
        "router_outputs": [],
        "candidates_per_block": [],
        "dedupe_results": [],
        "selector_scores": {},
        "router_json_valid": True,
        "segmenter_json_valid": True,
        "selector_json_valid": False,
        "segmenter_fallback_reason": "",
        "selector_fallback_reason": "",
        "final_plan": [],
    }
    if not client:
        return [], debug_info
    raw_rules = load_non_speech_rules()
    rules = _prepare_rules(raw_rules)
    debug_info["rules_loaded"] = True
    debug_info["rules_snapshot"] = {
        "non_speech_keywords": rules.get("non_speech_keywords") or rules.get("keywords") or [],
        "non_speech_regex_patterns": rules.get("non_speech_regex_patterns") or rules.get("regex_patterns") or [],
        "min_speech_chars": rules.get("min_speech_chars"),
        "max_non_speech_ratio_inside_clip": rules.get("max_non_speech_ratio_inside_clip"),
        "min_clip_seconds": rules.get("min_clip_seconds"),
        "max_clip_seconds": rules.get("max_clip_seconds"),
        "qa_max_question_seconds": rules.get("qa_max_question_seconds"),
        "target_clip_count_default": rules.get("target_clip_count_default"),
        "max_per_label": rules.get("max_per_label"),
        "max_per_block": rules.get("max_per_block"),
        "conjunction_prefixes": rules.get("conjunction_prefixes") or [],
    }
    speech_mask, speech_meta = _build_speech_mask(segments, rules)
    debug_info["speech_mask_summary"] = speech_meta["summary"]
    debug_info["non_speech_skipped_count"] = speech_meta["summary"].get("non_speech_count", 0)
    debug_info["intro_skipped_seconds"] = _first_speech_time(segments, speech_mask)
    min_clip_seconds = float(rules.get("min_clip_seconds") or 25.0)
    max_clip_seconds = float(rules.get("max_clip_seconds") or 60.0)
    max_non_speech_ratio = float(rules.get("max_non_speech_ratio_inside_clip") or 0.05)
    qa_max_question_seconds = float(rules.get("qa_max_question_seconds") or 10.0)
    qa_keywords = rules.get("qa_keywords_norm") or []
    conjunctions_norm = rules.get("conjunction_prefixes_norm") or []
    max_per_label = int(rules.get("max_per_label") or 3)
    max_per_block = int(rules.get("max_per_block") or 2)
    if target_clip_count is None:
        target_clip_count = _estimate_target_clip_count(
            duration_seconds, int(rules.get("target_clip_count_default") or 10)
        )
    logger.info("V3 target clip count: %s", target_clip_count)

    blocks, seg_meta = _call_segmenter(client, model, segments, speech_mask, 180.0)
    if not blocks:
        blocks = _heuristic_blocks(segments, speech_mask)
        debug_info["segmenter_json_valid"] = False
        debug_info["segmenter_fallback_reason"] = seg_meta.get("fallback_reason", "heuristic")
    else:
        debug_info["segmenter_json_valid"] = seg_meta.get("json_valid", True)
    blocks = _postprocess_blocks(segments, blocks, BLOCK_MIN_SECONDS, BLOCK_MAX_SECONDS)
    refined_blocks, refine_meta = _call_global_refine(
        client, model, blocks, BLOCK_MIN_SECONDS, BLOCK_MAX_SECONDS
    )
    if refine_meta.get("json_valid"):
        blocks = refined_blocks
    debug_info["segmenter_refine_json_valid"] = refine_meta.get("json_valid", False)
    debug_info["segmenter_refine_fallback_reason"] = refine_meta.get("fallback_reason", "")
    debug_info["blocks"] = blocks

    all_candidates = []
    for block_idx, block in enumerate(blocks, 1):
        start_idx = block.get("start_segment_idx")
        end_idx = block.get("end_segment_idx")
        if start_idx is None or end_idx is None:
            continue
        block_idxs = [idx for idx in range(start_idx, end_idx + 1) if speech_mask[idx]]
        if not block_idxs:
            continue
        router_out, router_meta = _call_router(client, model, segments, block_idxs)
        if not router_meta.get("json_valid", False):
            debug_info["router_json_valid"] = False
        debug_info["router_outputs"].append(
            {
                "block_id": block_idx,
                "router": router_out,
                "router_json_valid": router_meta.get("json_valid", False),
                "router_fallback_reason": router_meta.get("fallback_reason", ""),
            }
        )
        candidates, cand_meta = _call_highlight_specialist(
            client, model, segments, block_idxs, min_clip_seconds, max_clip_seconds
        )
        pre_fix = []
        post_fix = []
        for cand_idx, cand in enumerate(candidates):
            c_start = cand.get("start_segment_idx")
            c_end = cand.get("end_segment_idx")
            if c_start is None or c_end is None:
                continue
            fixed = _fix_candidate_range(
                segments,
                speech_mask,
                c_start,
                c_end,
                conjunctions_norm,
                min_clip_seconds,
                max_clip_seconds,
            )
            if not fixed:
                continue
            c_start, c_end = fixed
            if not speech_mask[c_start] or not speech_mask[c_end]:
                debug_info["candidates_rejected_due_to_non_speech"] += 1
                continue
            if _clip_non_speech_ratio(speech_mask, c_start, c_end) > max_non_speech_ratio:
                debug_info["candidates_rejected_due_to_non_speech"] += 1
                continue
            duration = _range_duration(segments, c_start, c_end)
            question_duration = 0.0
            answer_duration = 0.0
            if "qa" in (router_out.get("labels") or []):
                question_end_idx = _find_question_end_idx(
                    segments, c_start, c_end, qa_keywords
                )
                if question_end_idx is not None:
                    question_duration = _range_duration(segments, c_start, question_end_idx)
                    answer_duration = max(0.0, duration - question_duration)
                    if question_duration > qa_max_question_seconds:
                        continue
                    if answer_duration < question_duration * 2:
                        continue
            text = _range_text(segments, c_start, c_end)
            candidate_id = f"b{block_idx}_c{cand_idx}"
            post_fix.append(
                {
                    "candidate_id": candidate_id,
                    "block_id": block_idx,
                    "start_segment_idx": c_start,
                    "end_segment_idx": c_end,
                    "start_time": _range_times(segments, c_start, c_end)[0],
                    "end_time": _range_times(segments, c_start, c_end)[1],
                    "duration": duration,
                    "label": cand.get("label") or router_out.get("labels", ["other"])[0],
                    "confidence": cand.get("confidence", 0.0),
                    "reason": _sanitize_reason_by_snippet(cand.get("reason") or "", text),
                    "evidence_segment_idxs": cand.get("evidence_segment_idxs") or [],
                    "snippet": _build_snippet(text),
                    "question_duration": question_duration,
                    "answer_duration": answer_duration,
                }
            )
            pre_fix.append(cand)
        debug_info["candidates_per_block"].append(
            {
                "block_id": block_idx,
                "pre_fix": pre_fix,
                "post_fix": post_fix,
                "candidate_json_valid": cand_meta.get("json_valid", False),
                "candidate_fallback_reason": cand_meta.get("fallback_reason", ""),
            }
        )
        all_candidates.extend(post_fix)

    deduped = _dedupe_candidates(all_candidates)
    debug_info["dedupe_results"] = {
        "before": len(all_candidates),
        "after": len(deduped),
    }
    scores, sel_meta = _selector_scores(client, model, deduped)
    if not scores:
        scores = _heuristic_scores(deduped)
        debug_info["selector_json_valid"] = False
        debug_info["selector_fallback_reason"] = sel_meta.get("fallback_reason", "heuristic")
    else:
        debug_info["selector_json_valid"] = sel_meta.get("json_valid", True)
        debug_info["selector_fallback_reason"] = sel_meta.get("fallback_reason", "")
    debug_info["selector_scores"] = scores or {}

    ranked = []
    for cand in deduped:
        score = scores.get(cand["candidate_id"], {}).get("score", 0.0) if scores else 0.0
        ranked.append((score, cand))
    ranked.sort(key=lambda r: r[0], reverse=True)
    selected = []
    per_label = {}
    per_block = {}
    for score, cand in ranked:
        label = cand.get("label") or "other"
        block_id = cand.get("block_id")
        if per_label.get(label, 0) >= max_per_label:
            continue
        if per_block.get(block_id, 0) >= max_per_block:
            continue
        selected.append(cand)
        per_label[label] = per_label.get(label, 0) + 1
        per_block[block_id] = per_block.get(block_id, 0) + 1
        if len(selected) >= target_clip_count:
            break

    final_plan = []
    for cand in selected:
        text = _range_text(segments, cand["start_segment_idx"], cand["end_segment_idx"])
        final_plan.append(
            {
                "start_segment_idx": cand["start_segment_idx"],
                "end_segment_idx": cand["end_segment_idx"],
                "start_time": round(cand["start_time"], 2),
                "end_time": round(cand["end_time"], 2),
                "duration": round(cand["duration"], 2),
                "label": cand.get("label") or "other",
                "confidence": cand.get("confidence", 0.0),
                "reason": cand.get("reason") or "",
                "text": text,
                "excerpt": text,
            }
        )
    debug_info["final_plan"] = final_plan
    return final_plan, debug_info
