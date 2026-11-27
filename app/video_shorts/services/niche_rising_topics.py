from __future__ import annotations

import json
import re
import unicodedata
from datetime import date, datetime, timedelta, timezone
from typing import Any

from app.video_shorts.config import _openai_client
from app.video_shorts.services.db import get_db, get_db_readonly, table_columns
from app.video_shorts.services.niche_definitions import load_niche_definition
from app.video_shorts.services.niche_outliers import load_latest_niche_outliers


MODEL = "gpt-4.1-mini"
TOPICS_TABLE = "niche_rising_topics"
MATCHES_TABLE = "niche_rising_topic_brand_matches"
STATUS_COVERED = "covered"
STATUS_ARCHIVE = "archive"
STATUS_IDEA = "idea"
STATUS_LABELS = {
    STATUS_COVERED: "You covered this ✓",
    STATUS_ARCHIVE: "In your archive · Short ready",
    STATUS_IDEA: "Idea for your next video",
}
GENERIC_LABELS = {
    "menopause", "hormones", "health", "din", "islam", "islâm", "sohbet",
    "menopause symptoms and management", "hormone replacement therapy issues",
    "women's health advocacy and guidelines", "aile ve ilişkiler", "dua ve kabulü",
    "symptoms", "management", "issues", "advocacy", "genel",
}
GENERIC_THEMES = {
    "dua", "din", "islam", "islâm", "sohbet", "menopause", "hormones", "health",
    "women's health", "women health", "menopause symptoms", "hormone issues",
    "aile ve ilişkiler", "genel sağlık", "general health",
    "hormone replacement therapy", "menopause symptoms and management",
    "body changes and physical appearance", "health advocacy and awareness",
    "midlife mental health and behavior", "midlife nutrition and supplements",
    "weight and metabolic health", "dini ve ahlaki uyarılar",
    "peygamber ve sahabe hikayeleri", "aile ve sosyal ilişkiler",
    "dini bilgi ve öğütler", "dini ritüeller ve gelenekler",
    "dini hikayeler ve örnekler", "dini uygulamalar ve ibadetler",
    "dini sorumluluklar ve ibadet", "dini kavramlar ve inançlar",
    "dini vaaz ve sohbet",
    "menopause symptoms and effects", "midlife health and wellness",
    "menopause hormone therapy", "dini vecizeler ve hadisler",
    "dini uyarılar ve ahlak", "dini uygulamalar ve ritüeller",
    "aile ve toplum", "dini hikayeler ve kıssalar", "manevi sohbet ve vaaz",
}
MATCH_STOPWORDS = {
    "about", "after", "before", "from", "into", "with", "without", "your", "yourself",
    "this", "that", "what", "when", "where", "which", "whose", "women", "midlife",
    "and", "or", "the", "for", "are", "was", "were", "has", "have", "health", "system",
    "regulation",
    "icin", "için", "ile", "ve", "veya", "bir", "bu", "su", "şu", "olan", "olarak",
    "olma", "olmasi", "olması",
}


class RisingTopicsError(RuntimeError):
    pass


def _usage(response: Any) -> tuple[int, int]:
    usage = getattr(response, "usage", None)
    return (
        int(getattr(usage, "prompt_tokens", 0) or getattr(usage, "input_tokens", 0) or 0),
        int(getattr(usage, "completion_tokens", 0) or getattr(usage, "output_tokens", 0) or 0),
    )


def _cost(input_tokens: int, output_tokens: int) -> float:
    return (input_tokens * 0.40 + output_tokens * 1.60) / 1_000_000


def _json_response(response: Any) -> dict[str, Any]:
    text = str(response.choices[0].message.content if response.choices else "{}").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RisingTopicsError("rising topics model returned invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise RisingTopicsError("rising topics model must return one JSON object")
    return parsed


def _key(value: Any) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or "").casefold())
    asciiish = "".join(char for char in normalized if not unicodedata.combining(char))
    return "-".join(re.findall(r"[\w]+", asciiish, flags=re.UNICODE))[:120]


def _clean_label(value: Any) -> str:
    label = " ".join(str(value or "").strip().split()).strip(" .,:;!?-–—")
    if not 3 <= len(label.split()) <= 7 or _key(label) in {_key(item) for item in GENERIC_LABELS}:
        return ""
    return label


def _clean_theme(value: Any) -> str:
    label = " ".join(str(value or "").strip().split()).strip(" .,:;!?-–—")
    label_key = _key(label)
    broad_prefixes = (
        "dini-", "islam-kulturu", "midlife-health", "midlife-women-s-health",
        "women-s-health", "menopause-symptom", "mental-health-", "aile-ve-toplum",
    )
    if (
        not 2 <= len(label.split()) <= 5
        or label_key in {_key(item) for item in GENERIC_THEMES}
        or any(label_key.startswith(prefix) for prefix in broad_prefixes)
    ):
        return ""
    return label


def _display_title(value: Any) -> str:
    original = " ".join(str(value or "").split())
    cleaned = re.sub(r"#[\w-]+", " ", original, flags=re.UNICODE)
    cleaned = " ".join(cleaned.split()).rstrip(" .,:;!?-–—|/")
    return cleaned or original


def _eligible_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    for row in rows:
        if row.get("in_category", row.get("on_topic")) is False:
            continue
        engagement = float(row.get("engagement") or 0)
        baseline_engagement = float(row.get("baseline_engagement") or 0)
        if baseline_engagement > 0 and engagement < baseline_engagement / 3:
            continue
        output.append(row)
    return output


def _label_rows(
    rows: list[dict[str, Any]], definition: dict[str, Any], client: Any,
) -> tuple[dict[str, str], int, int]:
    if not rows:
        return {}, 0, 0
    payload = [
        {"video_id": str(row.get("video_id") or ""), "title": str(row.get("title") or "")}
        for row in rows
    ]
    response = client.chat.completions.create(
        model=MODEL, temperature=0, max_tokens=max(300, len(rows) * 24),
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": (
                "Label every Short with exactly one specific question, claim, or angle of 3–7 words in the "
                "niche language. The label must describe something a creator could make ONE video about, not "
                "a category or broad area. Good examples: 'Vaginal estrogen cream for dryness', 'HRT after "
                "age 60', 'Premarin vs bioidentical estrogen', 'Kabul olmayan dua sanmak', 'Ayetle Müslüman "
                "olan profesör'. Bad examples: 'Menopause Symptoms and Management', 'HRT issues', "
                "'Aile ve İlişkiler', 'Dua ve Kabulü', or any label containing only a broad subject. "
                "Return JSON only: {\"labels\":[{\"video_id\":\"...\",\"label\":\"...\"}]}. "
                "Do not use a generic label equal to the niche itself. Forbidden standalone labels: "
                "menopause, hormones, health, din, İslam, sohbet. Preserve every supplied video_id."
            )},
            {"role": "user", "content": json.dumps({
                "language": definition.get("language"),
                "niche_name": definition.get("name"),
                "core_topics": definition.get("core_topics") or [],
                "adjacent_topics": definition.get("adjacent_topics") or [],
                "shorts": payload,
            }, ensure_ascii=False)},
        ],
    )
    parsed = _json_response(response)
    valid_ids = {item["video_id"] for item in payload}
    labels = {}
    for item in parsed.get("labels") or []:
        if not isinstance(item, dict):
            continue
        video_id = str(item.get("video_id") or "")
        label = _clean_label(item.get("label"))
        if video_id in valid_ids and label:
            labels[video_id] = label
    input_tokens, output_tokens = _usage(response)
    return labels, input_tokens, output_tokens


def _merge_labels(
    labels: dict[str, str], language: str, client: Any,
) -> tuple[list[dict[str, Any]], int, int]:
    if not labels:
        return [], 0, 0
    response = client.chat.completions.create(
        model=MODEL, temperature=0, max_tokens=max(300, len(labels) * 30),
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": (
                "The inputs are specific Short angles. Group them into middle-level THEMES that a creator could "
                "cover in one video or a short series. A theme must be 2–5 words in the supplied language. It must "
                "be broader than one Short angle but narrower than the niche. Group related prayer-acceptance angles "
                "under a theme such as 'Duanın kabul olması', but keep unrelated prayer themes separate. Good themes: "
                "'Duanın kabul olması', 'Hızır ve gizli yardım', 'HRT after breast cancer', 'Vaginal estrogen for "
                "dryness', 'Feeling lost at midlife'. Bad broad themes: 'Dua', 'Din', 'Menopause symptoms', "
                "'Women's health'. Bad narrow themes: a single Short's exact title. Return JSON only: "
                "{\"topics\":[{\"label\":\"...\",\"video_ids\":[\"...\"]}]}. "
                "Every input video_id must appear exactly once."
            )},
            {"role": "user", "content": json.dumps({
                "language": language,
                "items": [{"video_id": video_id, "label": label} for video_id, label in labels.items()],
            }, ensure_ascii=False)},
        ],
    )
    parsed = _json_response(response)
    initial_topics = parsed.get("topics") or []
    first_input, first_output = _usage(response)
    revision = client.chat.completions.create(
        model=MODEL, temperature=0, max_tokens=max(300, len(labels) * 32),
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": (
                "Audit and revise proposed rising themes. Return JSON only in the same topics schema. A valid "
                "theme is a focused question/claim cluster a creator could cover in ONE video or a short series, "
                "2–5 words, broader than one angle but not a content category. Split any group held together only "
                "by HRT, symptoms, body changes, health, nutrition, religion, morality, family, worship, stories, "
                "or advice. BAD themes that must be split: 'Hormone Replacement Therapy', 'Menopause Symptoms "
                "and Management', 'Body Changes and Physical Appearance', 'Health Advocacy and Awareness', "
                "'Dinî ve ahlaki uyarılar', 'Peygamber ve sahabe hikayeleri', 'Aile ve sosyal ilişkiler'. "
                "GOOD middle-level themes: 'Duanın kabul olması', 'Hızır ve gizli yardım', 'HRT after breast "
                "cancer', 'Vaginal estrogen for dryness', 'Feeling lost at midlife'. Preserve every video_id once."
            )},
            {"role": "user", "content": json.dumps({
                "language": language,
                "angles": [{"video_id": video_id, "label": label} for video_id, label in labels.items()],
                "proposed_themes": parsed.get("topics") or [],
            }, ensure_ascii=False)},
        ],
    )
    parsed = _json_response(revision)
    valid_ids = set(labels)
    used: set[str] = set()
    topics = []
    candidate_topics = [
        item for item in [*initial_topics, *(parsed.get("topics") or [])]
        if isinstance(item, dict)
    ]
    candidate_topics.sort(
        key=lambda item: -len({str(value) for value in item.get("video_ids") or []} & valid_ids)
    )
    for item in candidate_topics:
        if not isinstance(item, dict):
            continue
        label = _clean_theme(item.get("label"))
        video_ids = [
            str(value) for value in item.get("video_ids") or []
            if str(value) in valid_ids and str(value) not in used
        ]
        if label and video_ids:
            used.update(video_ids)
            topics.append({"label": label, "video_ids": video_ids})
    for video_id in sorted(valid_ids - used):
        fallback = _clean_theme(" ".join(labels[video_id].split()[:5]))
        if fallback:
            topics.append({"label": fallback, "video_ids": [video_id]})
    revision_input, revision_output = _usage(revision)
    return topics, first_input + revision_input, first_output + revision_output


def _aggregate_topics(
    merged: list[dict[str, Any]], rows: list[dict[str, Any]], channel_titles: dict[str, str],
    angle_labels: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    by_video = {str(row.get("video_id") or ""): row for row in rows}
    angles = angle_labels or {}
    topics = []
    for item in merged:
        members = [by_video[video_id] for video_id in item["video_ids"] if video_id in by_video]
        channel_ids = {str(row.get("youtube_channel_id") or "") for row in members}
        channel_ids.discard("")
        examples = []
        for row in sorted(members, key=lambda value: -int(value.get("views") or 0))[:2]:
            channel_id = str(row.get("youtube_channel_id") or "")
            video_id = str(row.get("video_id") or "")
            examples.append({
                "title": _display_title(angles.get(video_id) or row.get("title") or ""),
                "short_title": _display_title(row.get("title") or ""),
                "channel": channel_titles.get(channel_id, channel_id),
                "views": int(row.get("views") or 0),
                "url": f"https://youtube.com/shorts/{row.get('video_id')}",
            })
        label = item["label"]
        topics.append({
            "topic_key": _key(label), "label": label,
            "channel_count": len(channel_ids),
            "total_views": sum(int(row.get("views") or 0) for row in members),
            "max_outlier_ratio": max((float(row.get("outlier_ratio") or 0) for row in members), default=0),
            "is_breakout": len(channel_ids) == 1,
            "examples": examples,
        })
    rising = [topic for topic in topics if topic["channel_count"] >= 2]
    rising.sort(key=lambda item: (-item["channel_count"], -item["total_views"], item["label"].casefold()))
    if len(rising) < 5:
        breakouts = [topic for topic in topics if topic["channel_count"] == 1]
        breakouts.sort(
            key=lambda item: (-item["max_outlier_ratio"], -item["total_views"], item["label"].casefold())
        )
        rising.extend(breakouts[:5 - len(rising)])
    return rising


def _decode_timed_segments(value: Any) -> list[dict[str, Any]]:
    parsed = value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
    if isinstance(parsed, dict):
        parsed = parsed.get("segments") or parsed.get("words") or []
    if not isinstance(parsed, list):
        return []
    output = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or item.get("word") or "").strip()
        try:
            start = float(item.get("start", item.get("start_seconds", item.get("start_time"))))
            end = float(item.get("end", item.get("end_seconds", item.get("end_time"))))
        except (TypeError, ValueError):
            continue
        if text and end > start >= 0:
            output.append({"start": start, "end": end, "text": text})
    return sorted(output, key=lambda item: (item["start"], item["end"]))


def _segment_windows(value: Any, *, minimum_seconds: float = 30, limit: int = 12) -> list[dict[str, Any]]:
    segments = _decode_timed_segments(value)
    windows = []
    current: list[dict[str, Any]] = []
    for segment in segments:
        current.append(segment)
        if current[-1]["end"] - current[0]["start"] >= minimum_seconds:
            windows.append({
                "start_seconds": round(current[0]["start"], 1),
                "end_seconds": round(current[-1]["end"], 1),
                "text": " ".join(item["text"] for item in current)[:1400],
            })
            current = []
    if current and windows:
        windows[-1]["end_seconds"] = round(current[-1]["end"], 1)
        windows[-1]["text"] = (windows[-1]["text"] + " " + " ".join(
            item["text"] for item in current
        ))[:1400]
    elif current and current[-1]["end"] - current[0]["start"] >= minimum_seconds * 0.8:
        windows.append({
            "start_seconds": round(current[0]["start"], 1),
            "end_seconds": round(current[-1]["end"], 1),
            "text": " ".join(item["text"] for item in current)[:1400],
        })
    if len(windows) <= limit:
        return windows
    indexes = sorted({round(index * (len(windows) - 1) / (limit - 1)) for index in range(limit)})
    return [windows[index] for index in indexes]


def _brand_content(
    conn: Any, owner_user_id: str, brand_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    shorts = []
    if table_columns(conn, "shorts_generated_videos"):
        rows = conn.execute(
            """
            SELECT id, generated_title,
                   COALESCE(youtube_published_at, instagram_published_at, published_at, planned_publish_at)
            FROM shorts_generated_videos
            WHERE CAST(brand_id AS VARCHAR) = CAST(? AS VARCHAR)
              AND CAST(user_id AS VARCHAR) = CAST(? AS VARCHAR)
              AND publish_status = 'published'
              AND COALESCE(youtube_published_at, instagram_published_at, published_at, planned_publish_at)
                    >= CURRENT_TIMESTAMP - INTERVAL '90 days'
            ORDER BY 3 DESC NULLS LAST
            LIMIT 100
            """,
            [brand_id, owner_user_id],
        ).fetchall()
        shorts = [{"id": int(row[0]), "title": str(row[1] or "")} for row in rows if str(row[1] or "").strip()]
    archive = []
    if table_columns(conn, "youtube_videos"):
        transcript_columns = table_columns(conn, "youtube_transcripts")
        full_text_columns = [
            f"t.{column}" for column in ("full_text", "full_text_en")
            if column in transcript_columns
        ]
        segment_columns = [
            f"t.{column}" for column in ("whisper_segments_json", "segments_json", "segments_en_json")
            if column in transcript_columns
        ]
        full_text_sql = (
            f"COALESCE({','.join(full_text_columns)}, '')" if len(full_text_columns) > 1
            else (full_text_columns[0] if full_text_columns else "''")
        )
        segments_sql = (
            f"COALESCE({','.join(segment_columns)})" if len(segment_columns) > 1
            else (segment_columns[0] if segment_columns else "NULL")
        )
        rows = conn.execute(
            f"""
            SELECT v.id, v.title, {full_text_sql}, {segments_sql}
            FROM youtube_videos v
            LEFT JOIN youtube_channels c ON c.channel_id = v.channel_id
            LEFT JOIN youtube_transcripts t ON t.video_id = v.video_id
            WHERE CAST(v.brand_id AS VARCHAR) = CAST(? AS VARCHAR)
              AND CAST(v.owner_user_id AS VARCHAR) = CAST(? AS VARCHAR)
              AND (v.channel_id IS NULL OR lower(COALESCE(c.channel_url, '')) = 'local://uploads')
              AND COALESCE(v.duration_seconds, v.media_duration_seconds, 0) > 180
              AND v.deleted_at IS NULL
            ORDER BY COALESCE(v.published_at, v.created_at) DESC NULLS LAST
            LIMIT 30
            """,
            [brand_id, owner_user_id],
        ).fetchall()
        archive = [
            {
                "id": int(row[0]), "title": str(row[1] or ""),
                "transcript": str(row[2] or ""),
                "segments": _segment_windows(row[3]),
            }
            for row in rows if str(row[1] or "").strip()
        ]
    content_counts = {
        "long_videos": len(archive),
        "transcripts": sum(1 for item in archive if item["transcript"].strip()),
        "timestamped_transcripts": sum(1 for item in archive if item["segments"]),
    }
    return shorts, [
        {"id": item["id"], "title": item["title"], "segments": item["segments"]}
        for item in archive if item["segments"]
    ], content_counts


def _truncate_evidence(value: Any) -> str:
    words = " ".join(str(value or "").split()).split()
    return " ".join(words[:20])


def _format_seconds(value: Any) -> str:
    try:
        seconds = max(0, int(round(float(value))))
    except (TypeError, ValueError):
        return ""
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


def _verified_transcript_evidence(value: Any, transcript: Any) -> str:
    evidence = _truncate_evidence(value)
    normalized_evidence = " ".join(evidence.casefold().split())
    normalized_transcript = " ".join(str(transcript or "").casefold().split())
    return evidence if normalized_evidence and normalized_evidence in normalized_transcript else ""


def _match_terms(value: Any) -> set[str]:
    normalized = unicodedata.normalize("NFKD", str(value or "").casefold())
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    normalized = re.sub(r"\bhrt\b", "hormone therapy", normalized)
    terms = set()
    for token in re.findall(r"[\w]+", normalized, flags=re.UNICODE):
        if len(token) < 3 or token in MATCH_STOPWORDS:
            continue
        for root in (
            "dua", "kabul", "namaz", "peygamber", "hormon", "estrojen", "menopoz",
            "menopause", "breast", "cancer", "hizir", "hızır",
        ):
            if token.startswith(root):
                token = root
                break
        terms.add(token[:6] if len(token) > 6 else token)
    return terms


def _evidence_matches_theme(topic_label: Any, evidence: Any) -> bool:
    topic_terms = _match_terms(topic_label)
    evidence_terms = _match_terms(evidence)
    return bool(topic_terms & evidence_terms)


def _evidence_matches_archive_theme(topic_label: Any, evidence: Any) -> bool:
    topic_terms = _match_terms(topic_label)
    evidence_terms = _match_terms(evidence)
    required = min(2, len(topic_terms))
    return required > 0 and len(topic_terms & evidence_terms) >= required


def _matching_archive_segment(
    archive_video: dict[str, Any], start_seconds: Any, evidence: Any,
) -> tuple[dict[str, Any] | None, str]:
    try:
        target = float(start_seconds)
    except (TypeError, ValueError):
        return None, ""
    for segment in archive_video.get("segments") or []:
        if abs(float(segment.get("start_seconds") or 0) - target) > 2:
            continue
        verified = _verified_transcript_evidence(evidence, segment.get("text"))
        if verified:
            return segment, verified
    return None, ""


def _archive_candidates_for_topics(
    topics: list[dict[str, Any]], archive: list[dict[str, Any]], *, limit_per_theme: int = 16,
) -> dict[str, list[dict[str, Any]]]:
    output: dict[str, list[dict[str, Any]]] = {}
    for topic in topics:
        topic_terms = _match_terms(topic["label"])
        candidates = []
        for video in archive:
            for segment in video.get("segments") or []:
                overlap = len(topic_terms & _match_terms(segment.get("text")))
                if overlap < 1:
                    continue
                candidates.append({
                    "video_id": video["id"], "video_title": video["title"],
                    "start_seconds": segment["start_seconds"],
                    "end_seconds": segment["end_seconds"],
                    "text": segment["text"], "term_overlap": overlap,
                })
        candidates.sort(key=lambda item: (-item["term_overlap"], item["video_id"], item["start_seconds"]))
        output[topic["topic_key"]] = candidates[:limit_per_theme]
    return output


def _match_brand(
    topics: list[dict[str, Any]], shorts: list[dict[str, Any]], archive: list[dict[str, Any]], client: Any,
) -> tuple[list[dict[str, Any]], int, int]:
    if not shorts and not archive:
        return [
            {"topic_key": topic["topic_key"], "status": STATUS_IDEA, "evidence": "", "archive_video_id": None}
            for topic in topics
        ], 0, 0
    response = client.chat.completions.create(
        model=MODEL, temperature=0, max_tokens=max(400, len(topics) * 80),
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": (
                "For every middle-level theme, follow this order. First, find a published Short title on the SAME "
                "theme; the exact angle may differ. If none exists, inspect the supplied timestamped transcript "
                "segments. An archive match requires a roughly 30+ second segment mainly about the theme, not a "
                "passing mention. Short wins over archive. Return JSON only: "
                "{\"matches\":[{\"topic_key\":\"...\","
                "\"short_id\":null|integer,\"archive_video_id\":null|integer,"
                "\"segment_start_seconds\":null|number,\"evidence\":\"...\"}]}. "
                "For a Short match, evidence is its exact title. For archive, evidence must be a VERBATIM transcript "
                "quote of no more than 20 words."
            )},
            {"role": "user", "content": json.dumps({
                "topics": [{"topic_key": item["topic_key"], "label": item["label"]} for item in topics],
                "published_shorts": shorts,
                "archive_candidates_by_theme": _archive_candidates_for_topics(topics, archive),
            }, ensure_ascii=False)},
        ],
    )
    parsed = _json_response(response)
    valid_topics = {item["topic_key"] for item in topics}
    shorts_by_id = {item["id"]: item for item in shorts}
    archive_by_id = {item["id"]: item for item in archive}
    by_key = {}
    for item in parsed.get("matches") or []:
        if not isinstance(item, dict) or str(item.get("topic_key") or "") not in valid_topics:
            continue
        topic_key = str(item["topic_key"])
        archive_id = item.get("archive_video_id")
        try:
            archive_id = int(archive_id) if archive_id is not None else None
        except (TypeError, ValueError):
            archive_id = None
        short_id = item.get("short_id")
        try:
            short_id = int(short_id) if short_id is not None else None
        except (TypeError, ValueError):
            short_id = None
        archive_segment, transcript_evidence = (
            _matching_archive_segment(
                archive_by_id[archive_id], item.get("segment_start_seconds"), item.get("evidence"),
            )
            if archive_id in archive_by_id else (None, "")
        )
        topic_label = next(topic["label"] for topic in topics if topic["topic_key"] == topic_key)
        if (
            short_id in shorts_by_id
            and _evidence_matches_theme(topic_label, shorts_by_id[short_id]["title"])
        ):
            status, archive_id = STATUS_COVERED, None
            evidence = _truncate_evidence(shorts_by_id[short_id]["title"])
            archive_start_seconds = None
        elif (
            archive_id in archive_by_id
            and archive_segment
            and transcript_evidence
            and _evidence_matches_archive_theme(topic_label, transcript_evidence)
        ):
            status = STATUS_ARCHIVE
            evidence = transcript_evidence
            archive_start_seconds = float(archive_segment["start_seconds"])
        else:
            status, archive_id = STATUS_IDEA, None
            evidence = ""
            archive_start_seconds = None
        by_key[topic_key] = {
            "topic_key": topic_key, "status": status,
            "evidence": evidence,
            "archive_video_id": archive_id,
            "archive_start_seconds": archive_start_seconds,
            "archive_title": (
                archive_by_id[archive_id]["title"]
                if status == STATUS_ARCHIVE else ""
            ),
        }
    matches = [
        by_key.get(topic["topic_key"], {
            "topic_key": topic["topic_key"], "status": STATUS_IDEA,
            "evidence": "", "archive_video_id": None,
        })
        for topic in topics
    ]
    input_tokens, output_tokens = _usage(response)
    return matches, input_tokens, output_tokens


def compute_and_persist_rising_topics(
    niche_slug: str, *, client: Any = None,
) -> dict[str, Any]:
    selected_client = client or _openai_client
    if selected_client is None:
        raise RisingTopicsError("OpenAI client is not configured")
    conn = get_db_readonly()
    try:
        definition = load_niche_definition(conn, slug=niche_slug)
        if not definition:
            raise RisingTopicsError(f"unknown niche: {niche_slug}")
        as_of, rows = load_latest_niche_outliers(conn, definition["id"])
        if not as_of:
            raise RisingTopicsError(f"no outlier snapshot for niche: {niche_slug}")
        channel_rows = conn.execute(
            """
            SELECT youtube_channel_id, title FROM niche_channels
            WHERE niche_id = ? AND snapshot_date = (
                SELECT MAX(snapshot_date) FROM niche_channels WHERE niche_id = ?
            )
            """,
            [definition["id"], definition["id"]],
        ).fetchall()
        channel_titles = {str(row[0]): str(row[1] or row[0]) for row in channel_rows}
        brand_rows = conn.execute(
            """
            SELECT CAST(b.id AS VARCHAR), CAST(b.owner_user_id AS VARCHAR), b.name
            FROM shorts_brands b WHERE b.niche_id = ? ORDER BY lower(b.name)
            """,
            [definition["id"]],
        ).fetchall()
    finally:
        conn.close()
    eligible = _eligible_rows(rows)
    total_input = total_output = 0
    labels, used_in, used_out = _label_rows(eligible, definition, selected_client)
    total_input += used_in
    total_output += used_out
    merged, used_in, used_out = _merge_labels(labels, definition.get("language") or "en", selected_client)
    total_input += used_in
    total_output += used_out
    topics = _aggregate_topics(merged, eligible, channel_titles, labels)
    brand_matches: dict[str, list[dict[str, Any]]] = {}
    brand_content_counts: dict[str, dict[str, Any]] = {}
    match_usage: dict[str, tuple[int, int]] = {}
    conn = get_db_readonly()
    try:
        for brand_id, owner_user_id, brand_name in brand_rows:
            shorts, archive, content_counts = _brand_content(conn, str(owner_user_id), str(brand_id))
            matches, used_in, used_out = _match_brand(topics, shorts, archive, selected_client)
            total_input += used_in
            total_output += used_out
            for match in matches:
                match["brand_name"] = str(brand_name or "")
            brand_matches[str(brand_id)] = matches
            brand_content_counts[str(brand_id)] = {
                "brand_name": str(brand_name or ""), **content_counts,
            }
            match_usage[str(brand_id)] = (used_in, used_out)
    finally:
        conn.close()
    conn = get_db()
    try:
        if not table_columns(conn, TOPICS_TABLE) or not table_columns(conn, MATCHES_TABLE):
            raise RisingTopicsError("rising topics migration has not been applied")
        conn.execute(f"DELETE FROM {MATCHES_TABLE} WHERE niche_id = ? AND as_of = ?", [definition["id"], as_of])
        conn.execute(f"DELETE FROM {TOPICS_TABLE} WHERE niche_id = ? AND as_of = ?", [definition["id"], as_of])
        topic_input = total_input - sum(value[0] for value in match_usage.values())
        topic_output = total_output - sum(value[1] for value in match_usage.values())
        for topic in topics:
            conn.execute(
                f"""
                INSERT INTO {TOPICS_TABLE}
                (niche_id,as_of,topic_key,label,channel_count,total_views,is_breakout,max_outlier_ratio,
                 examples_json,model,
                 input_tokens,output_tokens,estimated_cost_usd,computed_at)
                VALUES (?,?,?,?,?,?,?,?,?::jsonb,?,?,?,?,CURRENT_TIMESTAMP)
                """,
                [definition["id"], as_of, topic["topic_key"], topic["label"],
                 topic["channel_count"], topic["total_views"], topic["is_breakout"],
                 topic["max_outlier_ratio"], json.dumps(topic["examples"], ensure_ascii=False),
                 MODEL, topic_input, topic_output, _cost(topic_input, topic_output)],
            )
        for brand_id, matches in brand_matches.items():
            used_in, used_out = match_usage[brand_id]
            for match in matches:
                archive_id = match.get("archive_video_id")
                archive_url = (
                    f"/video_shorts/generate/{archive_id}"
                    if archive_id else None
                )
                conn.execute(
                    f"""
                    INSERT INTO {MATCHES_TABLE}
                    (niche_id,as_of,topic_key,brand_id,status,evidence,archive_video_id,
                     archive_start_seconds,archive_url,
                     model,input_tokens,output_tokens,estimated_cost_usd,computed_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
                    """,
                    [definition["id"], as_of, match["topic_key"], brand_id, match["status"],
                     match["evidence"], archive_id, match.get("archive_start_seconds"), archive_url,
                     MODEL, used_in, used_out,
                     _cost(used_in, used_out)],
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {
        "niche": niche_slug, "niche_id": definition["id"], "as_of": as_of,
        "eligible_shorts": len(eligible), "topics": topics, "brand_matches": brand_matches,
        "brand_content_counts": brand_content_counts,
        "input_tokens": total_input, "output_tokens": total_output,
        "estimated_cost_usd": _cost(total_input, total_output),
    }


def load_rising_topics(conn: Any, niche_id: Any, *, brand_id: str = "") -> tuple[str, list[dict[str, Any]]]:
    if not table_columns(conn, TOPICS_TABLE):
        return "", []
    latest = conn.execute(f"SELECT MAX(as_of) FROM {TOPICS_TABLE} WHERE niche_id = ?", [niche_id]).fetchone()
    as_of = str(latest[0]) if latest and latest[0] else ""
    if not as_of:
        return "", []
    match_join = ""
    match_columns = (
        "NULL AS status, NULL AS evidence, NULL AS archive_url, NULL AS brand_id, "
        "NULL AS archive_start_seconds"
    )
    params: list[Any] = [niche_id, as_of]
    if brand_id and table_columns(conn, MATCHES_TABLE):
        match_join = (
            f"LEFT JOIN {MATCHES_TABLE} m ON m.niche_id=t.niche_id AND m.as_of=t.as_of "
            "AND m.topic_key=t.topic_key AND CAST(m.brand_id AS VARCHAR)=CAST(? AS VARCHAR)"
        )
        match_columns = "m.status, m.evidence, m.archive_url, m.brand_id, m.archive_start_seconds"
        params = [brand_id, niche_id, as_of]
    result = conn.execute(
        f"""
        SELECT t.topic_key,t.label,t.channel_count,t.total_views,t.is_breakout,t.max_outlier_ratio,
               t.examples_json,
               {match_columns}
        FROM {TOPICS_TABLE} t {match_join}
        WHERE t.niche_id=? AND t.as_of=?
        ORDER BY t.is_breakout ASC,t.channel_count DESC,
                 CASE WHEN t.is_breakout THEN t.max_outlier_ratio ELSE 0 END DESC,
                 t.total_views DESC,lower(t.label)
        """,
        params,
    )
    columns = [str(item[0]) for item in result.description or []]
    rows = [dict(zip(columns, row)) for row in result.fetchall()]
    for row in rows:
        examples = row.get("examples_json")
        if isinstance(examples, str):
            try:
                examples = json.loads(examples)
            except json.JSONDecodeError:
                examples = []
        row["examples"] = examples if isinstance(examples, list) else []
        row["status"] = str(row.get("status") or STATUS_IDEA)
        row["status_label"] = STATUS_LABELS.get(row["status"], STATUS_LABELS[STATUS_IDEA])
        row["archive_timestamp"] = _format_seconds(row.get("archive_start_seconds"))
    return as_of, rows


def load_admin_rising_topics(conn: Any, niche_id: Any) -> tuple[str, list[dict[str, Any]]]:
    as_of, topics = load_rising_topics(conn, niche_id)
    if not as_of or not table_columns(conn, MATCHES_TABLE):
        return as_of, topics
    matches = conn.execute(
        f"""
        SELECT m.topic_key,m.brand_id,b.name,m.status,m.evidence,m.archive_url,
               m.archive_start_seconds
        FROM {MATCHES_TABLE} m
        LEFT JOIN shorts_brands b ON CAST(b.id AS VARCHAR)=CAST(m.brand_id AS VARCHAR)
        WHERE m.niche_id=? AND m.as_of=?
        ORDER BY lower(COALESCE(b.name,''))
        """,
        [niche_id, as_of],
    ).fetchall()
    by_topic: dict[str, list[dict[str, Any]]] = {}
    for topic_key, brand_id, brand_name, status, evidence, archive_url, archive_start in matches:
        by_topic.setdefault(str(topic_key), []).append({
            "brand_id": str(brand_id), "brand_name": str(brand_name or brand_id),
            "status": str(status), "status_label": STATUS_LABELS.get(str(status), str(status)),
            "evidence": str(evidence or ""), "archive_url": str(archive_url or ""),
            "archive_timestamp": _format_seconds(archive_start),
        })
    for topic in topics:
        topic["matches"] = by_topic.get(str(topic["topic_key"]), [])
    return as_of, topics
