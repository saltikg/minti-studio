import json
import logging
import re
import unicodedata
from typing import List, Dict, Any, Tuple, Optional

from app.video_shorts.services.clip_planner_agents import (
    merge_segments_into_sentences,
    build_windows,
    extract_segments_for_window,
)
from app.video_shorts.services.non_speech_rules import load_non_speech_rules

MIN_CLIP_SECONDS = 25.0
MAX_ROUTER_TEXT_CHARS = 1200
MAX_ROUTER_SEGMENTS = 12
MAX_REASON_CHARS = 160
CONJUNCTION_PREFIXES = {
    "ama",
    "fakat",
    "cunku",
    "ve",
    "binaenaleyh",
    "lakin",
    "ancak",
}

logger = logging.getLogger(__name__)


def _safe_float(value: Any, default: Optional[float] = 0.0) -> Optional[float]:
    try:
        return float(value)
    except Exception:
        return default


def _trim_text(text: str, limit: int) -> str:
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit]


def _normalize_indexed_segments(segments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    for idx, seg in enumerate(segments):
        normalized.append(
            {
                "idx": idx,
                "start": seg.get("start"),
                "end": seg.get("end"),
                "duration": seg.get("duration"),
                "text": (seg.get("text") or "").strip(),
            }
        )
    return normalized


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
        text = (seg.get("text") or "").strip()
        if text:
            parts.append(text)
    return " ".join(parts)


def _starts_with_conjunction(text: str) -> bool:
    if not text:
        return False
    first = text.strip().split()[0] if text.strip() else ""
    normalized = _normalize_for_compare(first)
    return normalized in CONJUNCTION_PREFIXES


def _normalize_for_compare(text: str) -> str:
    if not text:
        return ""
    normalized = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    return stripped.lower()


def _sanitize_reason(reason: str, segments: List[Dict[str, Any]]) -> str:
    if not reason:
        return ""
    reason_lower = reason.lower()
    for seg in segments:
        seg_text = (seg.get("text") or "").strip().lower()
        if len(seg_text) < 12:
            continue
        if seg_text in reason_lower:
            return ""
    return reason


def _normalize_keywords(values: List[str]) -> List[str]:
    return [v for v in (_normalize_for_compare(v) for v in values if v) if v]


def _prepare_rules(raw_rules: Dict[str, Any]) -> Dict[str, Any]:
    rules = dict(raw_rules or {})
    rules["non_speech_keywords_norm"] = _normalize_keywords(rules.get("keywords") or [])
    rules["qa_keywords_norm"] = _normalize_keywords(rules.get("qa_keywords") or [])
    compiled = []
    for pattern in rules.get("regex_patterns") or []:
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


def _classify_non_speech(text: str, rules: Dict[str, Any]) -> Tuple[bool, str]:
    if not text:
        return True, "empty"
    stripped = text.strip()
    if not stripped:
        return True, "empty"
    normalized = _normalize_for_compare(stripped)
    for keyword in rules.get("non_speech_keywords_norm") or []:
        if keyword and keyword in normalized:
            if "music" in keyword or "muzik" in keyword:
                return True, "music"
            if "silence" in keyword or "sessizlik" in keyword:
                return True, "silence"
            if "alkis" in keyword or "applause" in keyword:
                return True, "silence"
            return True, "non_speech"
    for pattern in rules.get("regex_compiled") or []:
        if pattern.search(stripped):
            return True, "music"
    words = stripped.split()
    if len(words) <= 1:
        letters = [ch for ch in stripped if ch.isalnum()]
        max_chars = int(rules.get("max_single_word_chars") or 2)
        if len(letters) <= max_chars:
            return True, "silence"
    min_chars = int(rules.get("min_chars") or 0)
    if len(stripped) < min_chars:
        return True, "silence"
    return False, ""


def _build_non_speech_mask(
    segments: List[Dict[str, Any]],
    rules: Dict[str, Any],
) -> Tuple[List[bool], List[str], int]:
    mask = []
    reasons = []
    override_count = 0
    for seg in segments:
        override = _segment_override_type(seg)
        if override:
            override_count += 1
            if override == "speech":
                mask.append(False)
                reasons.append("")
            else:
                mask.append(True)
                reasons.append(override)
            continue
        is_non_speech, reason = _classify_non_speech(seg.get("text") or "", rules)
        mask.append(is_non_speech)
        reasons.append(reason if is_non_speech else "")
    return mask, reasons, override_count


def _apply_non_speech_overrides(
    sentence_segments: List[Dict[str, Any]],
    raw_segments: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    overrides = []
    for seg in raw_segments:
        override = _segment_override_type(seg)
        if override and override != "speech":
            overrides.append(
                {
                    "start": _segment_time(seg, "start"),
                    "end": _segment_time(seg, "end"),
                    "type": override,
                }
            )
    if not overrides:
        return sentence_segments
    for sent in sentence_segments:
        sent_start = _segment_time(sent, "start")
        sent_end = _segment_time(sent, "end")
        for ov in overrides:
            if sent_end < ov["start"] or sent_start > ov["end"]:
                continue
            sent["non_speech_type"] = ov["type"]
            break
    return sentence_segments


def _find_speech_start_time(
    segments: List[Dict[str, Any]],
    mask: List[bool],
    reasons: List[str],
) -> Tuple[float, str]:
    last_reason = ""
    for idx, seg in enumerate(segments):
        if idx < len(mask) and mask[idx]:
            if idx < len(reasons):
                last_reason = reasons[idx]
            continue
        start = _segment_time(seg, "start")
        if last_reason in {"music", "silence", "empty"}:
            reason = last_reason
        elif last_reason in {"applause", "other", "non_speech"}:
            reason = "silence"
        else:
            reason = "first_speech"
        return start, reason
    return 0.0, "no_speech_detected"


def _find_first_speech_idx_at_or_after_time(
    segments: List[Dict[str, Any]],
    mask: List[bool],
    time_value: float,
) -> Optional[int]:
    for idx, seg in enumerate(segments):
        if _segment_time(seg, "start") < time_value:
            continue
        if idx < len(mask) and mask[idx]:
            continue
        return idx
    return None


def _is_question_signal(text: str, qa_keywords: List[str]) -> bool:
    if not text:
        return False
    lowered = text.lower()
    if "?" in text:
        return True
    normalized = _normalize_for_compare(lowered)
    for keyword in qa_keywords:
        if keyword and keyword in normalized:
            return True
    return False


def _find_question_start_idx(
    segments: List[Dict[str, Any]],
    start_idx: int,
    qa_keywords: List[str],
    max_seconds: float,
) -> Optional[int]:
    if not segments:
        return None
    start_time = _segment_time(segments[start_idx], "start")
    earliest_time = max(0.0, start_time - max_seconds)
    for idx in range(start_idx, -1, -1):
        if _segment_time(segments[idx], "start") < earliest_time:
            break
        if _is_question_signal(segments[idx].get("text") or "", qa_keywords):
            return idx
    return None


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


def _build_router_segments(segments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not segments:
        return []
    if len(segments) <= MAX_ROUTER_SEGMENTS:
        chosen = segments
    else:
        step = max(1, len(segments) // MAX_ROUTER_SEGMENTS)
        chosen = segments[::step][:MAX_ROUTER_SEGMENTS]
    payload = []
    for seg in chosen:
        payload.append(
            {
                "idx": seg.get("idx"),
                "text": _trim_text((seg.get("text") or "").strip(), 200),
            }
        )
    return payload


def _build_candidate_snippet(text: str, head_chars: int = 120, tail_chars: int = 120) -> str:
    if not text:
        return ""
    if len(text) <= head_chars + tail_chars:
        return text
    head = text[:head_chars].rstrip()
    tail = text[-tail_chars:].lstrip()
    return f"{head} ... {tail}"


def _extract_segments_for_window_with_padding(
    segments: List[Dict[str, Any]],
    window: Dict[str, Any],
    padding_seconds: float,
) -> List[Dict[str, Any]]:
    if not window:
        return []
    start = _safe_float(window.get("start"), 0.0) or 0.0
    end = _safe_float(window.get("end"), 0.0) or 0.0
    padded = {
        "context_start": max(0.0, start - padding_seconds),
        "context_end": end + padding_seconds,
    }
    return extract_segments_for_window(segments, padded)


def _call_router_agent(
    client,
    model: str,
    segments: List[Dict[str, Any]],
    window_text: str,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    if not client:
        return (
            {"labels": ["other"], "confidence": 0.0, "evidence_segment_idxs": []},
            {"json_valid": False, "fallback_reason": "no_client"},
        )
    system_prompt = (
        "Turkce transcript pencereleri icin hafif router ol.\n"
        "Sadece JSON dondur:\n"
        "{ \"labels\": [\"story\"|\"sahaba\"|\"tafsir\"|\"qa\"|\"other\"], \"confidence\": 0..1, \"evidence_segment_idxs\": [int] }\n"
        "qa: soru-cevap girisi olabilecek bolumleri isaretle.\n"
        "Metin uretme."
    )
    payload = {
        "window_text": _trim_text(window_text, MAX_ROUTER_TEXT_CHARS),
        "segments": _build_router_segments(segments),
    }
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
        return (
            {"labels": ["other"], "confidence": 0.0, "evidence_segment_idxs": []},
            {"json_valid": False, "fallback_reason": "llm_error"},
        )
    raw = resp.choices[0].message.content if resp.choices else ""
    try:
        data = json.loads(raw)
    except Exception:
        return (
            {"labels": ["other"], "confidence": 0.0, "evidence_segment_idxs": []},
            {"json_valid": False, "fallback_reason": "json_parse_error"},
        )
    labels = data.get("labels") or []
    if isinstance(labels, str):
        labels = [labels]
    allowed = {"story", "sahaba", "tafsir", "qa", "other"}
    cleaned_labels = [lbl for lbl in labels if lbl in allowed]
    if not cleaned_labels:
        cleaned_labels = ["other"]
    confidence = data.get("confidence")
    try:
        confidence_val = float(confidence)
    except Exception:
        confidence_val = 0.0
    confidence_val = max(0.0, min(1.0, confidence_val))
    raw_idxs = data.get("evidence_segment_idxs") or []
    evidence_idxs = []
    for idx in raw_idxs:
        try:
            idx_val = int(idx)
        except Exception:
            continue
        if 0 <= idx_val < len(segments):
            evidence_idxs.append(idx_val)
    return (
        {
            "labels": cleaned_labels,
            "confidence": confidence_val,
            "evidence_segment_idxs": evidence_idxs,
        },
        {"json_valid": True, "fallback_reason": ""},
    )


def _call_story_agent(
    client,
    model: str,
    segments: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], str]:
    if not client:
        return [], ""
    payload = {
        "segments": [
            {
                "idx": seg.get("idx"),
                "start": seg.get("start"),
                "end": seg.get("end"),
                "text": _trim_text(seg.get("text") or "", 220),
            }
            for seg in segments
        ]
    }
    system_prompt = (
        "Turkce hikaye odakli klip secici ajansin.\n"
        "Sadece JSON dondur. Metin uretme, transcript yazma.\n"
        "Cikti:\n"
        "{ \"candidates\": [ {\"start_segment_idx\": int, \"end_segment_idx\": int, "
        "\"confidence\": 0..1, \"reason\": \"kisa neden\", \"evidence_segment_idxs\": [int] } ] }\n"
        "Kurallar:\n"
        "- Adaylar bitisik segment araliklari olmalı.\n"
        "- Sure hedefi 25-60 sn; 25 altina dusme.\n"
        "- Mumkunse bagimsiz baslangic; 'ama', 'fakat', 'cunku', 've' ile baslama.\n"
        "- Soru-cevap girisi varsa soruyu kisa tut (<=10 sn) ve cevap baskin olsun.\n"
        "- Hikayenin sonucu/mesaji olan bolumleri tercih et.\n"
        "- En fazla 3 aday ver."
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
        return [], ""
    raw = resp.choices[0].message.content if resp.choices else ""
    try:
        data = json.loads(raw)
        candidates = data.get("candidates") or []
    except Exception:
        return [], raw
    normalized = []
    for cand in candidates:
        try:
            start_idx = int(cand.get("start_segment_idx"))
            end_idx = int(cand.get("end_segment_idx"))
        except Exception:
            continue
        if start_idx < 0 or end_idx < 0 or start_idx > end_idx:
            continue
        if end_idx >= len(segments):
            continue
        confidence = _safe_float(cand.get("confidence"), 0.0) or 0.0
        confidence = max(0.0, min(1.0, confidence))
        reason = _trim_text((cand.get("reason") or "").strip(), MAX_REASON_CHARS)
        evidence_raw = cand.get("evidence_segment_idxs") or []
        evidence_idxs = []
        for idx in evidence_raw:
            try:
                idx_val = int(idx)
            except Exception:
                continue
            if 0 <= idx_val < len(segments):
                evidence_idxs.append(idx_val)
        normalized.append(
            {
                "start_segment_idx": start_idx,
                "end_segment_idx": end_idx,
                "confidence": confidence,
                "reason": _sanitize_reason(reason, segments),
                "evidence_segment_idxs": evidence_idxs,
                "start_time": _range_times(segments, start_idx, end_idx)[0],
                "end_time": _range_times(segments, start_idx, end_idx)[1],
                "duration": _range_duration(segments, start_idx, end_idx),
            }
        )
    return normalized, raw


def _fix_candidate_range(
    segments: List[Dict[str, Any]],
    start_idx: int,
    end_idx: int,
    non_speech_mask: Optional[List[bool]] = None,
    min_seconds: float = MIN_CLIP_SECONDS,
) -> Optional[Tuple[int, int]]:
    if not segments:
        return None
    start_idx = max(0, min(start_idx, len(segments) - 1))
    end_idx = max(start_idx, min(end_idx, len(segments) - 1))
    if end_idx - start_idx < 1 and len(segments) > 1:
        end_idx = min(start_idx + 1, len(segments) - 1)
    allow_left = _starts_with_conjunction(segments[start_idx].get("text") or "")
    if allow_left and start_idx > 0:
        if not non_speech_mask or not non_speech_mask[start_idx - 1]:
            start_idx -= 1
    duration = _range_duration(segments, start_idx, end_idx)
    if duration >= min_seconds:
        return start_idx, end_idx
    left = start_idx - 1
    right = end_idx + 1
    if not allow_left:
        while duration < min_seconds and right < len(segments):
            if not non_speech_mask or not non_speech_mask[right]:
                end_idx = right
            right += 1
            duration = _range_duration(segments, start_idx, end_idx)
        if duration < min_seconds:
            while duration < min_seconds and left >= 0:
                if not non_speech_mask or not non_speech_mask[left]:
                    start_idx = left
                left -= 1
                duration = _range_duration(segments, start_idx, end_idx)
    else:
        while duration < min_seconds and left >= 0:
            if not non_speech_mask or not non_speech_mask[left]:
                start_idx = left
            left -= 1
            duration = _range_duration(segments, start_idx, end_idx)
        while duration < min_seconds and right < len(segments):
            if not non_speech_mask or not non_speech_mask[right]:
                end_idx = right
            right += 1
            duration = _range_duration(segments, start_idx, end_idx)
    if _range_duration(segments, start_idx, end_idx) < min_seconds:
        return None
    if _starts_with_conjunction(segments[start_idx].get("text") or "") and start_idx > 0:
        if not non_speech_mask or not non_speech_mask[start_idx - 1]:
            start_idx -= 1
    return start_idx, end_idx


def _dedupe_candidates(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    sorted_cands = sorted(candidates, key=lambda x: x.get("start_time", 0.0))
    deduped: List[Dict[str, Any]] = []
    for cand in sorted_cands:
        if not deduped:
            deduped.append(cand)
            continue
        skip = False
        for existing in reversed(deduped[-4:]):
            start_a = _safe_float(cand.get("start_time"), 0.0) or 0.0
            end_a = _safe_float(cand.get("end_time"), 0.0) or 0.0
            start_b = _safe_float(existing.get("start_time"), 0.0) or 0.0
            end_b = _safe_float(existing.get("end_time"), 0.0) or 0.0
            if start_a == start_b and end_a == end_b:
                skip = True
                break
            overlap = max(0.0, min(end_a, end_b) - max(start_a, start_b))
            duration = max(end_a - start_a, 1.0)
            overlap_ratio = overlap / duration
            if overlap_ratio >= 0.85:
                skip = True
                break
        if not skip:
            deduped.append(cand)
    return deduped


def _score_clips_with_selector_agent(
    client,
    model: str,
    candidates: List[Dict[str, Any]],
) -> Tuple[Optional[Dict[int, Dict[str, Any]]], Dict[str, Any]]:
    if not client or not candidates:
        return None, {"json_valid": False, "fallback_reason": "no_client_or_candidates"}
    payload = {
        "clips": [
            {
                "id": idx,
                "label": clip.get("label"),
                "duration_seconds": clip.get("duration"),
                "segment_count": clip.get("segment_count"),
                "snippet": clip.get("snippet") or "",
                "question_seconds": clip.get("question_duration") or 0.0,
                "answer_seconds": clip.get("answer_duration") or 0.0,
            }
            for idx, clip in enumerate(candidates)
        ]
    }
    system_prompt = (
        "Klip secici ajansin. Her klibe 1-5 puan ver ve kisa neden yaz.\n"
        "- clarity: tek basina anlasilirlik\n"
        "- payoff: tamamlanmis dusunce/sonuc\n"
        "- filler: gereksiz giris, laf kalabaligi (5 = cok filler)\n"
        "- intensity: duygu/ilke yogunlugu\n"
        "- qa kliplerinde soru <=10 sn ve cevap baskin olmali\n"
        "Sadece JSON dondur: { \"scores\": [ {\"id\": int, \"clarity\": 1-5, "
        "\"payoff\": 1-5, \"filler\": 1-5, \"intensity\": 1-5, \"reason\": \"kisa\"} ] }"
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
    score_map: Dict[int, Dict[str, Any]] = {}
    for row in scores:
        try:
            idx = int(row.get("id"))
        except Exception:
            continue
        if idx < 0 or idx >= len(candidates):
            continue
        reason = _trim_text((row.get("reason") or "").strip(), MAX_REASON_CHARS)
        snippet = candidates[idx].get("snippet") if idx < len(candidates) else ""
        if snippet and snippet in reason:
            reason = ""
        score_map[idx] = {
            "clarity": float(row.get("clarity") or 0),
            "payoff": float(row.get("payoff") or 0),
            "filler": float(row.get("filler") or 0),
            "intensity": float(row.get("intensity") or 0),
            "reason": reason,
        }
    if not score_map:
        return None, {"json_valid": False, "fallback_reason": "empty_scores"}
    return score_map, {"json_valid": True, "fallback_reason": ""}


def _heuristic_scores(candidates: List[Dict[str, Any]]) -> Dict[int, Dict[str, Any]]:
    scores = {}
    for idx, clip in enumerate(candidates):
        duration = clip.get("duration") or 0.0
        segment_count = clip.get("segment_count") or 0
        clarity = 3.0 + (1.0 if segment_count >= 3 else 0.0)
        payoff = 3.0 + (1.0 if duration >= 35 else 0.0)
        filler = 2.0 if segment_count >= 3 else 3.0
        intensity = 3.0 + (1.0 if duration <= 60 else 0.0)
        scores[idx] = {
            "clarity": min(5.0, clarity),
            "payoff": min(5.0, payoff),
            "filler": min(5.0, filler),
            "intensity": min(5.0, intensity),
            "reason": "heuristic",
        }
    return scores


def _select_clips(
    candidates: List[Dict[str, Any]],
    client,
    model: str,
    target_clip_count: Optional[int],
    max_per_window_label: int,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    if not candidates:
        return [], {"scores": {}, "ordered_ids": [], "selector_json_valid": False, "selector_fallback_reason": "no_candidates"}
    score_map, selector_meta = _score_clips_with_selector_agent(client, model, candidates)
    if not score_map:
        score_map = _heuristic_scores(candidates)
    ranked = []
    for idx, clip in enumerate(candidates):
        scores = score_map.get(idx) or {}
        clarity = scores.get("clarity", 0.0)
        payoff = scores.get("payoff", 0.0)
        filler = scores.get("filler", 0.0)
        intensity = scores.get("intensity", 0.0)
        total = clarity + payoff + intensity + (6.0 - filler)
        ranked.append((total, idx, clip))
    ranked.sort(key=lambda r: r[0], reverse=True)
    desired_count = target_clip_count if target_clip_count is not None else len(ranked)
    selected: List[Dict[str, Any]] = []
    per_bucket: Dict[Tuple[Any, Any], int] = {}
    leftovers: List[Tuple[float, int, Dict[str, Any]]] = []
    for total, idx, clip in ranked:
        bucket = (clip.get("window_id"), clip.get("label"))
        count = per_bucket.get(bucket, 0)
        if count >= max_per_window_label:
            leftovers.append((total, idx, clip))
            continue
        per_bucket[bucket] = count + 1
        selected.append(clip)
        if len(selected) >= desired_count:
            break
    if len(selected) < desired_count and leftovers:
        for total, idx, clip in leftovers:
            if len(selected) >= desired_count:
                break
            selected.append(clip)
    selected_sorted = sorted(selected, key=lambda c: (c.get("window_id", 0), c.get("start_segment_idx", 0)))
    debug_payload = {
        "scores": score_map,
        "ordered_ids": [idx for _, idx, _ in ranked],
        "selector_json_valid": selector_meta.get("json_valid", False),
        "selector_fallback_reason": selector_meta.get("fallback_reason", ""),
    }
    return selected_sorted, debug_payload


def propose_clips_with_agents_v2(
    segments: List[Dict[str, Any]],
    transcript_text: str,
    duration_seconds: float,
    client,
    model: str,
    debug: bool = False,
    target_clip_count: Optional[int] = None,
    router_threshold: float = 0.6,
    max_per_window_label: int = 2,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    debug_info: Dict[str, Any] = {
        "duration_seconds": duration_seconds,
        "windows": [],
        "router_outputs": [],
        "router_json_valid": True,
        "router_fallback_reasons": [],
        "rules_loaded": False,
        "override_applied_count": 0,
        "non_speech_reject_count": 0,
        "speech_start_time": 0.0,
        "intro_skipped_seconds": 0.0,
        "intro_skipped_reason": "",
        "qa_detected_count": 0,
        "qa_clips_selected_count": 0,
        "window_candidates": [],
        "deduped_candidates": [],
        "deduped_count": 0,
        "final_count": 0,
        "selector_scores": {},
        "selector_json_valid": False,
        "selector_fallback_reason": "",
        "final_plan": [],
        "target_clip_count": target_clip_count,
        "fixed_count": 0,
    }
    if not client:
        return [], debug_info
    raw_rules = load_non_speech_rules()
    rules = _prepare_rules(raw_rules)
    debug_info["rules_loaded"] = True
    logger.info("V2 non-speech rules loaded.")
    padding_seconds = float(rules.get("padding_seconds") or 0.0)
    qa_max_seconds = float(rules.get("qa_max_seconds") or 0.0)
    qa_keywords = rules.get("qa_keywords_norm") or []
    sentence_segments = merge_segments_into_sentences(segments)
    sentence_segments = _apply_non_speech_overrides(sentence_segments, segments)
    non_speech_mask, non_speech_reasons, override_count = _build_non_speech_mask(
        sentence_segments, rules
    )
    debug_info["override_applied_count"] = override_count
    if override_count:
        logger.info("V2 non-speech overrides applied: %s", override_count)
    speech_start_time, intro_reason = _find_speech_start_time(
        sentence_segments, non_speech_mask, non_speech_reasons
    )
    debug_info["speech_start_time"] = speech_start_time
    debug_info["intro_skipped_seconds"] = max(0.0, speech_start_time)
    debug_info["intro_skipped_reason"] = intro_reason
    windows = build_windows(duration_seconds)
    debug_info["windows"] = windows
    all_candidates: List[Dict[str, Any]] = []
    for idx, win in enumerate(windows, 1):
        win_segments = _extract_segments_for_window_with_padding(sentence_segments, win, padding_seconds)
        indexed_segments = _normalize_indexed_segments(win_segments)
        win_non_speech_mask, _, _ = _build_non_speech_mask(indexed_segments, rules)
        excerpt_text = " ".join(s.get("text", "") for s in indexed_segments if s.get("text"))
        router_output, router_meta = _call_router_agent(client, model, indexed_segments, excerpt_text)
        if "qa" in (router_output.get("labels") or []):
            debug_info["qa_detected_count"] += 1
        if not router_meta.get("json_valid", False):
            debug_info["router_json_valid"] = False
        if router_meta.get("fallback_reason"):
            debug_info["router_fallback_reasons"].append(router_meta.get("fallback_reason"))
        debug_info["router_outputs"].append(
            {
                "window": win,
                "router": router_output,
                "router_json_valid": router_meta.get("json_valid", False),
                "router_fallback_reason": router_meta.get("fallback_reason", ""),
            }
        )
        labels = router_output.get("labels") or []
        agent_candidates: List[Dict[str, Any]] = []
        raw_story = ""
        if "story" in labels and router_output.get("confidence", 0) >= router_threshold:
            agent_candidates, raw_story = _call_story_agent(client, model, indexed_segments)
        accepted: List[Dict[str, Any]] = []
        fixed_count = 0
        min_allowed_idx = _find_first_speech_idx_at_or_after_time(
            indexed_segments, win_non_speech_mask, speech_start_time
        )
        if min_allowed_idx is None:
            continue
        for cand in agent_candidates:
            start_idx = cand.get("start_segment_idx")
            end_idx = cand.get("end_segment_idx")
            if start_idx is None or end_idx is None:
                continue
            if start_idx < min_allowed_idx:
                start_idx = min_allowed_idx
            if end_idx < start_idx:
                continue
            if any(win_non_speech_mask[start_idx : end_idx + 1]):
                debug_info["non_speech_reject_count"] += 1
                logger.info("V2 candidate rejected due to non-speech segment.")
                continue
            question_start_idx = None
            question_end_idx = None
            question_duration = 0.0
            answer_duration = 0.0
            if "qa" in labels:
                question_start_idx = _find_question_start_idx(
                    indexed_segments, start_idx, qa_keywords, qa_max_seconds
                )
                if question_start_idx is not None:
                    start_idx = question_start_idx
                question_end_idx = _find_question_end_idx(
                    indexed_segments, start_idx, end_idx, qa_keywords
                )
                if question_end_idx is None:
                    continue
                question_duration = _range_duration(indexed_segments, start_idx, question_end_idx)
                total_duration = _range_duration(indexed_segments, start_idx, end_idx)
                answer_duration = max(0.0, total_duration - question_duration)
                if question_duration <= 0.0 or question_duration > qa_max_seconds:
                    continue
                if answer_duration <= question_duration:
                    continue
            if (
                _starts_with_conjunction(indexed_segments[start_idx].get("text") or "")
                and start_idx > 0
            ):
                test_start = start_idx - 1
                test_duration = _range_duration(indexed_segments, test_start, end_idx)
                if test_duration >= MIN_CLIP_SECONDS:
                    start_idx = test_start
            duration = _range_duration(indexed_segments, start_idx, end_idx)
            if duration < MIN_CLIP_SECONDS:
                fixed = _fix_candidate_range(indexed_segments, start_idx, end_idx, win_non_speech_mask)
                if not fixed:
                    continue
                start_idx, end_idx = fixed
                fixed_count += 1
            segment_count = max(0, end_idx - start_idx + 1)
            if segment_count < 2:
                continue
            if any(win_non_speech_mask[start_idx : end_idx + 1]):
                debug_info["non_speech_reject_count"] += 1
                logger.info("V2 candidate rejected after fix due to non-speech segment.")
                continue
            accepted.append(
                {
                    "start_segment_idx": start_idx,
                    "end_segment_idx": end_idx,
                    "confidence": cand.get("confidence", 0.0),
                    "reason": cand.get("reason") or "",
                    "evidence_segment_idxs": cand.get("evidence_segment_idxs") or [],
                    "label": "qa" if "qa" in labels else ("story" if "story" in labels else "other"),
                    "window_id": idx,
                    "duration": _range_duration(indexed_segments, start_idx, end_idx),
                    "segment_count": segment_count,
                    "start_time": _range_times(indexed_segments, start_idx, end_idx)[0],
                    "end_time": _range_times(indexed_segments, start_idx, end_idx)[1],
                    "snippet": _build_candidate_snippet(
                        _range_text(indexed_segments, start_idx, end_idx)
                    ),
                    "question_start_idx": question_start_idx,
                    "question_end_idx": question_end_idx,
                    "question_duration": question_duration,
                    "answer_duration": answer_duration,
                }
            )
        debug_info["fixed_count"] += fixed_count
        debug_info["window_candidates"].append(
            {
                "window": win,
                "seg_count": len(indexed_segments),
                "raw_story": _trim_text(raw_story, 2000),
                "candidates": agent_candidates,
                "accepted": accepted,
            }
        )
        all_candidates.extend(accepted)
    deduped = _dedupe_candidates(all_candidates)
    debug_info["deduped_candidates"] = deduped
    debug_info["deduped_count"] = len(deduped)
    final_candidates, selector_debug = _select_clips(
        deduped,
        client,
        model,
        target_clip_count,
        max_per_window_label,
    )
    debug_info["selector_json_valid"] = selector_debug.get("selector_json_valid", False)
    debug_info["selector_fallback_reason"] = selector_debug.get("selector_fallback_reason", "")
    final_plan: List[Dict[str, Any]] = []
    for cand in final_candidates:
        if cand.get("label") == "qa":
            debug_info["qa_clips_selected_count"] += 1
        win_idx = cand.get("window_id")
        window = windows[win_idx - 1] if win_idx and win_idx - 1 < len(windows) else None
        win_segments = (
            _extract_segments_for_window_with_padding(sentence_segments, window, padding_seconds)
            if window
            else []
        )
        indexed_segments = _normalize_indexed_segments(win_segments)
        start_idx = cand.get("start_segment_idx")
        end_idx = cand.get("end_segment_idx")
        if start_idx is None or end_idx is None:
            continue
        clip_text = _range_text(indexed_segments, start_idx, end_idx)
        start_time, end_time = _range_times(indexed_segments, start_idx, end_idx)
        final_plan.append(
            {
                "title": "",
                "start": round(start_time, 2),
                "end": round(end_time, 2),
                "excerpt": clip_text,
                "text": clip_text,
                "start_segment_idx": start_idx,
                "end_segment_idx": end_idx,
                "label": cand.get("label") or "other",
                "confidence": cand.get("confidence", 0.0),
                "reason": cand.get("reason") or "",
                "question_duration": cand.get("question_duration"),
                "answer_duration": cand.get("answer_duration"),
            }
        )
    debug_info["selector_scores"] = selector_debug
    debug_info["final_plan"] = final_plan
    debug_info["final_count"] = len(final_plan)
    return final_plan, debug_info
