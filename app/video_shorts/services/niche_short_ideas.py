from __future__ import annotations

import hashlib
import json
import re
import shutil
from typing import Any

from app.video_shorts.config import _openai_client
from app.video_shorts.services.db import get_db, get_db_readonly, table_columns
from app.video_shorts.services.niche_outliers import (
    _current_pool,
    customer_visible_outlier_rows,
    load_latest_niche_outliers,
)


IDEAS_TABLE = "niche_short_ideas"
TRANSCRIPTS_TABLE = "niche_short_transcripts"
CHAT_MODEL = "gpt-4.1"
WHISPER_USD_PER_MINUTE = 0.006
CHAT_INPUT_PER_MILLION = 2.0
CHAT_OUTPUT_PER_MILLION = 8.0
FORMAT_TAGS = {"Story", "Q&A", "Tip", "Motivation"}


class NicheShortIdeasError(RuntimeError):
    pass


def _usage(response: Any) -> tuple[int, int]:
    usage = getattr(response, "usage", None)
    return (
        int(getattr(usage, "prompt_tokens", 0) or getattr(usage, "input_tokens", 0) or 0),
        int(getattr(usage, "completion_tokens", 0) or getattr(usage, "output_tokens", 0) or 0),
    )


def _chat_cost(input_tokens: int, output_tokens: int) -> float:
    return (
        input_tokens * CHAT_INPUT_PER_MILLION
        + output_tokens * CHAT_OUTPUT_PER_MILLION
    ) / 1_000_000


def _decode_json(value: Any, fallback: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


def _json_response(response: Any) -> dict[str, Any]:
    content = response.choices[0].message.content if response.choices else ""
    text = str(content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise NicheShortIdeasError("idea generator returned invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise NicheShortIdeasError("idea generator must return a JSON object")
    return parsed


def _words(value: Any) -> list[str]:
    return re.findall(r"[\w'’-]+", str(value or "").casefold(), flags=re.UNICODE)


def _has_verbatim_phrase(value: Any, transcript: str, length: int = 7) -> bool:
    candidate = _words(value)
    source = _words(transcript)
    if len(candidate) < length or len(source) < length:
        return False
    source_ngrams = {tuple(source[index:index + length]) for index in range(len(source) - length + 1)}
    return any(
        tuple(candidate[index:index + length]) in source_ngrams
        for index in range(len(candidate) - length + 1)
    )


def _validate_generated_idea(payload: dict[str, Any], transcript: str) -> dict[str, Any]:
    why = " ".join(str(payload.get("why_it_worked") or "").split())
    version = payload.get("your_version") if isinstance(payload.get("your_version"), dict) else {}
    title = " ".join(str(version.get("title") or "").split())
    first_line = " ".join(str(version.get("first_line") or "").split())
    beats = [" ".join(str(item or "").split()) for item in (version.get("beats") or [])]
    ideal_length = " ".join(str(version.get("ideal_length") or "").split())
    format_tag = " ".join(str(payload.get("format") or "").split())
    if not why or len(_words(why)) > 35:
        raise NicheShortIdeasError("why_it_worked must contain 1–35 words")
    if not title or not first_line or len(beats) != 3 or not all(beats) or not ideal_length:
        raise NicheShortIdeasError("your_version is incomplete")
    if format_tag not in FORMAT_TAGS:
        raise NicheShortIdeasError("idea format is invalid")
    for value in (title, first_line, *beats):
        if _has_verbatim_phrase(value, transcript):
            raise NicheShortIdeasError("your_version copied more than six transcript words")
    return {
        "why_it_worked": why,
        "idea_title": title,
        "first_line": first_line,
        "beats": beats,
        "ideal_length": ideal_length,
        "format_tag": format_tag,
    }


def _source_rows(niche_slug: str) -> tuple[dict[str, Any], str, list[dict[str, Any]]]:
    niche, pool = _current_pool(niche_slug)
    pool_by_channel = {
        str(row.get("youtube_channel_id") or ""): row
        for row in pool
        if row.get("youtube_channel_id")
    }
    conn = get_db_readonly()
    try:
        as_of, rows = load_latest_niche_outliers(conn, niche["id"], signal_type="weekly")
    finally:
        conn.close()
    eligible = customer_visible_outlier_rows(
        [
            row for row in rows
            if str(row.get("youtube_channel_id") or "") in pool_by_channel
        ],
        views_floor=int(niche.get("shorts_views_floor") or 10_000),
    )[:2]
    for row in eligible:
        channel = pool_by_channel.get(str(row.get("youtube_channel_id") or "")) or {}
        row["source_channel_title"] = str(channel.get("title") or "Untitled channel")
    return niche, as_of, eligible


def _cached_transcript(video_id: str) -> dict[str, Any] | None:
    conn = get_db_readonly()
    try:
        if not table_columns(conn, TRANSCRIPTS_TABLE):
            return None
        row = conn.execute(
            f"""SELECT transcript,detected_language,duration_s,whisper_minutes,whisper_cost_usd
                FROM {TRANSCRIPTS_TABLE} WHERE video_id=?""",
            [video_id],
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return {
        "transcript": str(row[0] or ""),
        "detected_language": str(row[1] or ""),
        "duration_s": int(row[2] or 0),
        "whisper_minutes": float(row[3] or 0),
        "whisper_cost_usd": float(row[4] or 0),
        "cache_hit": True,
    }


def _transcribe_source(video_id: str, duration_s: int, language: str) -> dict[str, Any]:
    from app.video_shorts.services.transcript_service import _transcribe_with_whisper
    from app.video_shorts.worker import _download_youtube_audio_with_proxy

    audio_path = None
    download_error: Exception | None = None
    for _attempt in range(3):
        try:
            audio_path, _proxy_used, _cookies = _download_youtube_audio_with_proxy(
                f"https://www.youtube.com/watch?v={video_id}", video_id,
            )
            break
        except Exception as exc:
            download_error = exc
    if audio_path is None:
        if download_error:
            raise download_error
        raise NicheShortIdeasError(f"audio download failed for {video_id}")
    try:
        full_text, segments = _transcribe_with_whisper(
            audio_path,
            spoken_language=language if language in {"en", "tr"} else "auto",
        )
        detected_language = next(
            (str(item.get("lang") or "") for item in segments if item.get("lang")),
            language,
        )
        if not str(full_text or "").strip():
            raise NicheShortIdeasError(f"Whisper returned no transcript for {video_id}")
        minutes = max(0.0, float(duration_s or 0) / 60.0)
        return {
            "transcript": str(full_text).strip(),
            "detected_language": detected_language,
            "duration_s": int(duration_s or 0),
            "whisper_minutes": minutes,
            "whisper_cost_usd": minutes * WHISPER_USD_PER_MINUTE,
            "cache_hit": False,
        }
    finally:
        if audio_path:
            shutil.rmtree(audio_path.parent, ignore_errors=True)


def _persist_transcript(video_id: str, item: dict[str, Any]) -> None:
    conn = get_db()
    try:
        conn.execute(
            f"""INSERT INTO {TRANSCRIPTS_TABLE}
                (video_id,transcript,detected_language,duration_s,whisper_minutes,whisper_cost_usd,updated_at)
                VALUES (?,?,?,?,?,?,CURRENT_TIMESTAMP)
                ON CONFLICT (video_id) DO UPDATE SET transcript=EXCLUDED.transcript,
                    detected_language=EXCLUDED.detected_language,duration_s=EXCLUDED.duration_s,
                    whisper_minutes=EXCLUDED.whisper_minutes,
                    whisper_cost_usd=EXCLUDED.whisper_cost_usd,updated_at=CURRENT_TIMESTAMP""",
            [video_id, item["transcript"], item["detected_language"], item["duration_s"],
             item["whisper_minutes"], item["whisper_cost_usd"]],
        )
        conn.commit()
    finally:
        conn.close()


def _generate_idea(source: dict[str, Any], transcript: dict[str, Any], language: str, client: Any) -> dict[str, Any]:
    response = client.chat.completions.create(
        model=CHAT_MODEL,
        temperature=0.2,
        max_tokens=700,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": (
                "Analyze one successful YouTube Short and create an original idea inspired by its structure, "
                "not its wording. Return JSON only with: why_it_worked (English, 1–2 sentences, at most 35 "
                "words, focused on hook/structure/format), format (exactly Story, Q&A, Tip, or Motivation), and "
                "your_version containing title, first_line, beats (exactly three short phrases), ideal_length. "
                "Write title, first_line, and beats in the requested niche language. Never copy a phrase of "
                "seven or more consecutive words from the transcript. Make the premise and wording original."
            )},
            {"role": "user", "content": json.dumps({
                "niche_language": language,
                "source_title": str(source.get("title") or ""),
                "duration_seconds": int(source.get("duration_s") or 0),
                "ratio_to_creator_usual": round(float(source.get("outlier_ratio") or 0), 2),
                "transcript": transcript["transcript"],
            }, ensure_ascii=False)},
        ],
    )
    generated = _validate_generated_idea(_json_response(response), transcript["transcript"])
    input_tokens, output_tokens = _usage(response)
    return {
        **generated,
        "llm_input_tokens": input_tokens,
        "llm_output_tokens": output_tokens,
        "llm_cost_usd": _chat_cost(input_tokens, output_tokens),
    }


def compute_niche_short_ideas(niche_slug: str, *, client: Any = None) -> dict[str, Any]:
    selected_client = client or _openai_client
    if selected_client is None:
        raise NicheShortIdeasError("OpenAI client is not configured")
    niche, as_of, sources = _source_rows(niche_slug)
    if not as_of or not sources:
        raise NicheShortIdeasError("niche has no qualifying weekly winners")
    conn = get_db_readonly()
    try:
        if not table_columns(conn, IDEAS_TABLE) or not table_columns(conn, TRANSCRIPTS_TABLE):
            raise NicheShortIdeasError("niche short ideas migration has not been applied")
    finally:
        conn.close()
    language = str(niche.get("language") or "en")
    generated_rows = []
    whisper_cost = llm_cost = 0.0
    whisper_minutes = 0.0
    cache_hits = 0
    for rank, source in enumerate(sources, start=1):
        video_id = str(source.get("video_id") or "")
        transcript = _cached_transcript(video_id)
        if transcript:
            cache_hits += 1
        else:
            transcript = _transcribe_source(video_id, int(source.get("duration_s") or 0), language)
            _persist_transcript(video_id, transcript)
            whisper_minutes += transcript["whisper_minutes"]
            whisper_cost += transcript["whisper_cost_usd"]
        idea = _generate_idea(source, transcript, language, selected_client)
        llm_cost += idea["llm_cost_usd"]
        generated_rows.append({**source, **idea, "selected_rank": rank})
    conn = get_db()
    try:
        conn.execute(f"DELETE FROM {IDEAS_TABLE} WHERE niche_id=? AND as_of=?", [niche["id"], as_of])
        for item in generated_rows:
            conn.execute(
                f"""INSERT INTO {IDEAS_TABLE}
                    (niche_id,as_of,source_video_id,source_channel_id,source_channel_title,
                     source_title,source_views,source_ratio,source_url,source_duration_s,selected_rank,
                     why_it_worked,idea_title,first_line,beats_json,ideal_length,format_tag,
                     llm_input_tokens,llm_output_tokens,llm_cost_usd,computed_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?::jsonb,?,?,?,?,?,CURRENT_TIMESTAMP)""",
                [niche["id"], as_of, str(item.get("video_id") or ""),
                 str(item.get("youtube_channel_id") or ""), item["source_channel_title"],
                 str(item.get("title") or ""), int(item.get("views") or 0),
                 float(item.get("outlier_ratio") or 0),
                 f"https://youtube.com/shorts/{item.get('video_id')}",
                 int(item.get("duration_s") or 0), item["selected_rank"], item["why_it_worked"],
                 item["idea_title"], item["first_line"], json.dumps(item["beats"], ensure_ascii=False),
                 item["ideal_length"], item["format_tag"], item["llm_input_tokens"],
                 item["llm_output_tokens"], item["llm_cost_usd"]],
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {
        "niche": niche_slug,
        "as_of": as_of,
        "idea_count": len(generated_rows),
        "cache_hits": cache_hits,
        "whisper_minutes": whisper_minutes,
        "whisper_cost_usd": whisper_cost,
        "llm_cost_usd": llm_cost,
        "total_cost_usd": whisper_cost + llm_cost,
        "ideas": [{
            "source_video_id": str(item.get("video_id") or ""),
            "source_title": str(item.get("title") or ""),
            "source_ratio": float(item.get("outlier_ratio") or 0),
            "why_it_worked": item["why_it_worked"],
            "your_version": {
                "title": item["idea_title"], "first_line": item["first_line"],
                "beats": item["beats"], "ideal_length": item["ideal_length"],
            },
            "format": item["format_tag"],
        } for item in generated_rows],
    }


def load_niche_short_ideas(conn: Any, niche_id: Any) -> tuple[str, list[dict[str, Any]]]:
    if not table_columns(conn, IDEAS_TABLE):
        return "", []
    latest = conn.execute(
        f"SELECT MAX(as_of) FROM {IDEAS_TABLE} WHERE niche_id=?", [niche_id]
    ).fetchone()
    as_of = str(latest[0]) if latest and latest[0] else ""
    if not as_of:
        return "", []
    result = conn.execute(
        f"""SELECT source_video_id,source_channel_title,source_title,source_views,source_ratio,
                   source_url,source_duration_s,selected_rank,why_it_worked,idea_title,first_line,
                   beats_json,ideal_length,format_tag
            FROM {IDEAS_TABLE} WHERE niche_id=? AND as_of=? ORDER BY selected_rank""",
        [niche_id, as_of],
    )
    columns = [str(item[0]) for item in result.description or []]
    rows = [dict(zip(columns, row)) for row in result.fetchall()]
    for row in rows:
        row["beats"] = _decode_json(row.pop("beats_json", []), [])
    return as_of, rows


def enqueue_niche_short_ideas(niche_slug: str, *, run_key: str) -> dict[str, Any]:
    from app.video_shorts.services.render_jobs import JOB_TYPE_NICHE_SHORT_IDEAS, enqueue_worker_job

    conn = get_db_readonly()
    try:
        row = conn.execute(
            """SELECT CAST(b.owner_user_id AS VARCHAR) FROM shorts_brands b
               JOIN niches n ON n.id=b.niche_id WHERE n.slug=? ORDER BY CAST(b.id AS VARCHAR) LIMIT 1""",
            [niche_slug],
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise NicheShortIdeasError("niche has no brand owner for worker job")
    digest = hashlib.sha256(f"niche-short-ideas:{niche_slug}:{run_key}".encode()).hexdigest()
    return enqueue_worker_job(
        user_id=str(row[0]),
        job_type=JOB_TYPE_NICHE_SHORT_IDEAS,
        payload={"niche_slug": niche_slug, "job_origin": "niche_weekly"},
        input_hash=digest,
        max_attempts=1,
        priority=-20,
    )
