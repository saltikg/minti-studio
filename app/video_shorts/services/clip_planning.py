import json
from typing import Any, Dict, List, Tuple

from flask import current_app

from app.video_shorts.config import MAX_CLIP_LEN, OPENAI_MODEL, _openai_client
from app.video_shorts.services.clip_plan_focus_prompts import get_llm_focus_block


def _fallback_clip_plan(duration_seconds: int):
    """Generate simple evenly spaced clips if LLM returns nothing."""
    if not duration_seconds or duration_seconds < 25:
        return []
    clips = []
    max_len = MAX_CLIP_LEN
    # Prefer longer chunks (≈45-120s) when possible
    if duration_seconds >= 90:
        target_len = min(max_len, 110)
    else:
        target_len = min(max_len, max(30, duration_seconds // 2 or 30))
    start = 0.0
    idx = 1
    while start + 25 <= duration_seconds and len(clips) < 3:
        end = min(start + target_len, duration_seconds)
        if end - start < 25:
            break
        clips.append({"title": f"Segment {idx}", "start": start, "end": end})
        start = end
        idx += 1
    return clips


def _propose_clips_with_llm(
    segments: List[Dict[str, Any]],
    transcript: str,
    duration_seconds: int,
    language: str = "tr",
    plan_focus: str = "",
) -> Tuple[List[Dict[str, Any]], str]:
    """
    Ask LLM for 2-4 short clip suggestions with start/end seconds.
    """
    if not _openai_client:
        return [], ""
    # Trim segments to keep prompt small
    trimmed_segments = segments[:200] if segments else []
    lang = (language or "tr").strip().lower()
    focus_block = get_llm_focus_block(plan_focus)
    if lang == "en":
        system_prompt = (
            "You are an expert short form video editor. Your job is to extract YouTube Shorts "
            "clips from a single long English lecture.\n\n"
            "LANGUAGE AND CONTENT RULES:\n"
            "- The transcript you receive is in English.\n"
            "- Work only in English. Do not translate or summarize into any other language.\n"
            "- Keep a respectful, serious tone that matches a lecture format.\n\n"
            f"{focus_block}"
            "INPUT:\n"
            "- You will receive the full transcript of one video, in English, split into segments.\n"
            "- Each segment has: start (seconds), duration (seconds), and text.\n\n"
            "YOUR TASK:\n"
            "- Propose 2 to 4 strong clip candidates from this lecture.\n"
            "- Each clip must be one continuous time interval (no gaps, no reordering).\n"
            "- Each clip must be fully within the video duration.\n"
            "- Primary rule: pick complete, meaningful segments that deliver a clear idea or hook without cutting mid-sentence or mid-thought. "
            "If a strong idea runs longer than 120s, do not include it. If it fits naturally within 45–120s, prefer that length; only drop to "
            "around 25–40s if the idea truly ends there. Never exceed 120s.\n\n"
            "SELECTION RULES:\n"
            "- Favor parts that start with a natural hook, question, or strong statement.\n"
            "- Prefer moments of clear insight, emotion, advice, or concise explanation.\n"
            "- Avoid slow introductions, greetings, logistics, or long conclusions.\n"
            "- Avoid technical or administrative details that are not interesting for Shorts.\n"
            "- Try not to cut in the middle of a sentence at the beginning or end of a clip.\n"
            "- If needed, slightly adjust start and end timestamps so the clip begins and ends on a complete sentence.\n"
            "- Keep overlap between clips minimal. Each clip should focus on a distinct idea.\n"
            "- Do not invent, paraphrase, or add new sentences that do not exist in the transcript.\n\n"
            "TITLES:\n"
            "- For each clip, create a short, punchy title in English.\n"
            "- Titles must be respectful in tone and must not distort the meaning of the lecture.\n"
            "- Titles should spark curiosity or emotion while staying accurate. When natural, prefer question based or thought provoking "
            "phrases that reflect the core message of the clip.\n"
            "- Avoid clickbait style, all caps, or excessive emojis.\n"
            "- Keep each title under 80 characters.\n\n"
            "OUTPUT FORMAT:\n"
            "- Return a single valid JSON object.\n"
            "- Do not include any explanation, comments, or text outside of valid JSON.\n"
            "- The JSON must have this shape:\n"
            "{\n"
            "  \"clips\": [\n"
            "    {\"title\": string, \"start\": number, \"end\": number},\n"
            "    ...\n"
            "  ]\n"
            "}\n"
        )
    else:
        system_prompt = (
            "You are an expert short form video editor. Your job is to extract YouTube Shorts "
            "clips from a single long Turkish lecture.\n\n"
            "LANGUAGE AND CONTENT RULES:\n"
            "- The transcript you receive is in Turkish.\n"
            "- Work only in Turkish. Do not translate or summarize into any other language.\n"
            "- Keep a respectful, serious tone that matches a religious lecture.\n\n"
            f"{focus_block}"
            "INPUT:\n"
            "- You will receive the full transcript of one video, in Turkish, split into segments.\n"
            "- Each segment has: start (seconds), duration (seconds), and text.\n\n"
            "YOUR TASK:\n"
            "- Propose 2 to 4 strong clip candidates from this lecture.\n"
            "- Each clip must be one continuous time interval (no gaps, no reordering).\n"
            "- Each clip must be fully within the video duration.\n"
            "- Primary rule: pick complete, meaningful segments that deliver a clear idea or hook without cutting mid-sentence or mid-thought. "
            "If a strong idea runs longer than 120s, do not include it. If it fits naturally within 45–120s, prefer that length; only drop to "
            "around 25–40s if the idea truly ends there. Never exceed 120s.\n\n"
            "SELECTION RULES:\n"
            "- Favor parts that start with a natural hook, question, or strong statement.\n"
            "- Prefer moments of clear insight, emotion, advice, or concise explanation.\n"
            "- Avoid slow introductions, greetings, logistics, or long conclusions.\n"
            "- Avoid technical or administrative details that are not interesting for Shorts.\n"
            "- Try not to cut in the middle of a sentence at the beginning or end of a clip.\n"
            "- If needed, slightly adjust start and end timestamps so the clip begins and ends on a complete sentence.\n"
            "- Keep overlap between clips minimal. Each clip should focus on a distinct idea.\n"
            "- Do not invent, paraphrase, or add new sentences that do not exist in the transcript.\n\n"
            "TITLES:\n"
            "- For each clip, create a short, punchy title in Turkish.\n"
            "- Titles must be respectful in tone and must not distort the meaning of the lecture.\n"
            "- Titles should spark curiosity or emotion while staying accurate. When natural, prefer question based or thought provoking "
            "phrases that reflect the core message of the clip.\n"
            "- Avoid clickbait style, all caps, or excessive emojis.\n"
            "- Keep each title under 80 characters.\n\n"
            "OUTPUT FORMAT:\n"
            "- Return a single valid JSON object.\n"
            "- Do not include any explanation, comments, or text outside of valid JSON.\n"
            "- The JSON must have this shape:\n"
            "{\n"
            "  \"clips\": [\n"
            "    {\"title\": string, \"start\": number, \"end\": number},\n"
            "    ...\n"
            "  ]\n"
            "}\n"
        )
    prompt = {"role": "system", "content": system_prompt}

    user_content = [
        {
            "type": "text",
            "text": json.dumps(
                {
                    "duration_seconds": duration_seconds,
                    "segments": trimmed_segments,
                    "transcript_excerpt": (transcript or "")[:4000],
                }
            ),
        }
    ]
    resp = _openai_client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[prompt, {"role": "user", "content": user_content}],
        response_format={"type": "json_object"},
    )
    raw_message = resp.choices[0].message.content if resp.choices else ""
    current_app.logger.info("[LLM CLIP RAW] %s", raw_message[:500])
    try:
        data = json.loads(raw_message)
        clips = data.get("clips") or []
        cleaned = []
        for c in clips:
            start = float(c.get("start"))
            end = float(c.get("end"))
            if duration_seconds:
                end = min(end, float(duration_seconds))
            if end <= start:
                continue
            clip_len = end - start
            if clip_len < 25:
                # try to extend up to 45-120s window if possible
                max_extend = 120.0
                desired_end = start + max_extend
                if duration_seconds:
                    desired_end = min(desired_end, float(duration_seconds))
                if desired_end - start >= 25:
                    end = desired_end
                    clip_len = end - start
            if clip_len > 120:
                end = start + 120
                clip_len = 120
            if clip_len >= 25 and clip_len <= 120:
                cleaned.append(
                    {
                        "title": c.get("title") or "",
                        "start": round(start, 2),
                        "end": round(end, 2),
                    }
                )
        return cleaned, raw_message
    except Exception:
        return [], raw_message
