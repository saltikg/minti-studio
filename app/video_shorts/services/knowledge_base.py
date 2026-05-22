import json
import re
from typing import Dict, Iterable, List, Optional

from app.video_shorts.config import OPENAI_MODEL, _openai_client


class KnowledgeBaseGenerationError(Exception):
    pass


REQUIRED_INTENT_TYPES = ["specific", "why", "evidence"]


def _clean_text(value: object) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ")
    return re.sub(r"\s+", " ", text).strip()


def _limit_text(value: object, limit: int) -> str:
    text = _clean_text(value)
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _extract_json(content: str) -> Dict[str, object]:
    text = (content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    data = json.loads(text)
    if not isinstance(data, dict):
        raise KnowledgeBaseGenerationError("OpenAI response is not a JSON object.")
    return data


def _normalize_question(value: object) -> str:
    question = _limit_text(value, 220)
    if not question:
        return ""
    if not question.endswith("?"):
        question = question.rstrip(".!") + "?"
    return question


def _question_tokens(value: object) -> List[str]:
    normalized = _clean_text(value).lower()
    normalized = re.sub(r"[^a-z0-9çğıöşü\s]", " ", normalized)
    return [token for token in normalized.split() if len(token) > 1]


def _jaccard_similarity(left: object, right: object) -> float:
    left_tokens = set(_question_tokens(left))
    right_tokens = set(_question_tokens(right))
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def _validate_main_question(value: object) -> str:
    question = _normalize_question(value)
    if not question:
        raise KnowledgeBaseGenerationError("Model returned an empty main question.")
    if len(_question_tokens(question)) < 4:
        raise KnowledgeBaseGenerationError("Main question is too short to be useful.")
    return question


def _validate_short_answer(value: object) -> str:
    answer = _limit_text(value, 500)
    if not answer:
        raise KnowledgeBaseGenerationError("Model returned an empty short answer.")
    if len(answer) > 500:
        raise KnowledgeBaseGenerationError("Short answer is too long.")
    return answer


def _validate_transcript_summary(value: object) -> str:
    summary = _limit_text(value, 900)
    if not summary:
        raise KnowledgeBaseGenerationError("Model returned an empty transcript summary.")
    if len(summary) < 30:
        raise KnowledgeBaseGenerationError("Transcript summary is too short.")
    return summary


def _validate_similar_questions(raw_items: object, main_question: str) -> List[Dict[str, str]]:
    if not isinstance(raw_items, list) or len(raw_items) != 3:
        raise KnowledgeBaseGenerationError("Model must return exactly 3 similar questions.")
    typed_items: List[Dict[str, str]] = []
    seen_types = set()
    seen_questions: List[str] = []
    for item in raw_items:
        if not isinstance(item, dict):
            raise KnowledgeBaseGenerationError("Each similar question must be an object.")
        intent_type = _clean_text(item.get("intent_type")).lower()
        question = _normalize_question(item.get("question"))
        if intent_type not in REQUIRED_INTENT_TYPES:
            raise KnowledgeBaseGenerationError("Similar question intent types are invalid.")
        if not question:
            raise KnowledgeBaseGenerationError("A similar question is empty.")
        if _jaccard_similarity(question, main_question) >= 0.88:
            raise KnowledgeBaseGenerationError("A similar question is too close to the main question.")
        if any(_jaccard_similarity(question, existing) >= 0.88 for existing in seen_questions):
            raise KnowledgeBaseGenerationError("Similar questions are too close to each other.")
        seen_types.add(intent_type)
        seen_questions.append(question)
        typed_items.append({"intent_type": intent_type, "question": question})
    if seen_types != set(REQUIRED_INTENT_TYPES):
        raise KnowledgeBaseGenerationError("Similar questions must cover specific, why, and evidence intents.")
    typed_items.sort(key=lambda item: REQUIRED_INTENT_TYPES.index(item["intent_type"]))
    return typed_items


def extract_hashtags(*values: object) -> List[str]:
    found: List[str] = []
    seen = set()
    for value in values:
        for tag in re.findall(r"#([\wçğıöşüÇĞİÖŞÜ]+)", str(value or "")):
            normalized = tag.lower()
            if normalized in seen:
                continue
            seen.add(normalized)
            found.append(f"#{tag}")
    return found[:12]


def find_duplicate_question(
    main_question: str,
    existing_rows: Iterable[Dict[str, object]],
    *,
    threshold: float = 0.78,
) -> Optional[Dict[str, object]]:
    best_match: Optional[Dict[str, object]] = None
    best_score = 0.0
    for row in existing_rows:
        candidate = row.get("question")
        score = _jaccard_similarity(main_question, candidate)
        if score >= threshold and score > best_score:
            best_score = score
            best_match = {
                "page_id": row.get("id"),
                "question": candidate,
                "page_type": row.get("page_type"),
                "status": row.get("status"),
                "similarity": round(score, 3),
            }
    return best_match


def generate_short_qa_payload(source: Dict[str, object]) -> Dict[str, object]:
    if not _openai_client:
        raise KnowledgeBaseGenerationError("OpenAI client is not configured.")

    primary_short_text = _limit_text(source.get("primary_short_text"), 4000)
    source_title = _limit_text(source.get("source_title"), 220)
    description = _limit_text(source.get("description"), 2000)
    hashtags = source.get("hashtags") or []
    if not isinstance(hashtags, list):
        hashtags = []
    hashtags = [_clean_text(item) for item in hashtags if _clean_text(item)]
    support_transcript = _limit_text(source.get("support_transcript"), 3000)
    video_url = _clean_text(source.get("video_url"))

    if not primary_short_text and not source_title and not description:
        raise KnowledgeBaseGenerationError("No short-specific text was found for this short.")

    try:
        response = _openai_client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You generate admin-reviewed Q&A drafts for short-form video content. "
                        "Use the transcript language. "
                        "Do not invent facts. "
                        "Only turn ideas into questions if they are genuinely answered by the content. "
                        "Use primary_short_text as the strongest signal. "
                        "Use source_title, description, hashtags, and support_transcript only as supporting context. "
                        "Do not rewrite the title blindly. "
                        "Main question must be natural search language, not clickbait, and must be answerable from the content. "
                        "For religious or technical content, do not fabricate certainty; be careful when the content is ambiguous. "
                        "Return JSON only. "
                        "The schema is: "
                        "{\"main_question\": str, \"short_answer\": str, \"transcript_summary\": str, "
                        "\"similar_questions\": ["
                        "{\"intent_type\": \"specific\", \"question\": str}, "
                        "{\"intent_type\": \"why\", \"question\": str}, "
                        "{\"intent_type\": \"evidence\", \"question\": str}"
                        "]}"
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "video_url": video_url,
                            "primary_short_text": primary_short_text,
                            "source_title": source_title,
                            "description": description,
                            "hashtags": hashtags,
                            "support_transcript": support_transcript,
                            "instructions": {
                                "main_question": "Return exactly 1 main question.",
                                "short_answer": "Keep concise. Usually 1-3 sentences.",
                                "transcript_summary": "Keep reasonably brief. Usually 2-4 sentences.",
                                "similar_questions": "Return exactly 3 typed similar questions: specific, why, evidence.",
                            },
                        },
                        ensure_ascii=True,
                    ),
                },
            ],
            temperature=0.2,
            max_tokens=900,
        )
        payload = _extract_json(response.choices[0].message.content or "")
    except KnowledgeBaseGenerationError:
        raise
    except Exception as exc:
        raise KnowledgeBaseGenerationError(f"OpenAI generation failed: {exc}") from exc
    main_question = _validate_main_question(payload.get("main_question"))
    short_answer = _validate_short_answer(payload.get("short_answer"))
    transcript_summary = _validate_transcript_summary(payload.get("transcript_summary"))
    similar_items = _validate_similar_questions(payload.get("similar_questions"), main_question)
    return {
        "main_question": main_question,
        "short_answer": short_answer,
        "transcript_summary": transcript_summary,
        "similar_questions": [item["question"] for item in similar_items],
        "similar_question_items": similar_items,
        "model": OPENAI_MODEL,
        "source_video_url": video_url,
        "input_snapshot": {
            "primary_short_text": primary_short_text,
            "source_title": source_title,
            "description": description,
            "hashtags": hashtags,
            "support_transcript": support_transcript,
        },
    }
