from __future__ import annotations

import hashlib
import json
import re
import shutil
from typing import Any

from app.video_shorts.config import _openai_client
from app.video_shorts.services.brand_niches import brand_publishing_channel_id
from app.video_shorts.services.db import get_db, get_db_readonly, table_columns
from app.video_shorts.services.niche_outliers import (
    _current_pool,
    customer_visible_outlier_rows,
    load_latest_niche_outliers,
)


IDEAS_TABLE = "niche_brand_short_ideas"
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


def _normalize_length_range(value: Any) -> str:
    text = " ".join(str(value or "").split())
    numbers = [int(item) for item in re.findall(r"\d+", text)]
    if len(numbers) >= 2:
        return f"{numbers[0]}–{numbers[1]} seconds"
    if len(numbers) == 1:
        center = numbers[0]
        return f"{max(10, center - 5)}–{min(180, center + 5)} seconds"
    raise NicheShortIdeasError("ideal_length must contain a numeric range")


def _looks_english(value: str) -> bool:
    words = set(_words(value))
    turkish_markers = {"bir", "ile", "ve", "için", "izleyici", "güçlü", "kısa", "ardından", "kurdu"}
    return not any(character in value.casefold() for character in "çğıöşü") and len(words & turkish_markers) < 2


def _validate_generated_idea(
    payload: dict[str, Any], transcript: str, *, niche_language: str = "en"
) -> dict[str, Any]:
    why = " ".join(str(payload.get("why_it_worked") or "").split())
    version = payload.get("your_version") if isinstance(payload.get("your_version"), dict) else {}
    title = " ".join(str(version.get("title") or "").split())
    first_line = " ".join(str(version.get("first_line") or "").split())
    beats = [" ".join(str(item or "").split()) for item in (version.get("beats") or [])]
    ideal_length = _normalize_length_range(version.get("ideal_length"))
    format_tag = " ".join(str(payload.get("format") or "").split())
    if not why or len(_words(why)) > 35:
        raise NicheShortIdeasError("why_it_worked must contain 1–35 words")
    if niche_language != "en" and not _looks_english(why):
        raise NicheShortIdeasError("why_it_worked must be English")
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


def _clip_beats_are_source_segments(beats: list[str]) -> bool:
    markers = ("sohbet", "kesit", "kayıt", "kayit", "konuş", "konus", "bölüm", "bolum", "segment")
    return all(any(marker in beat.casefold() for marker in markers) for beat in beats)


def _brand_profiles(niche_id: Any) -> list[dict[str, Any]]:
    conn = get_db_readonly()
    try:
        brands = conn.execute(
            """SELECT CAST(id AS VARCHAR),CAST(owner_user_id AS VARCHAR),name
               FROM shorts_brands WHERE niche_id=? ORDER BY lower(name),CAST(id AS VARCHAR)""",
            [niche_id],
        ).fetchall()
        profiles = []
        for brand_id, owner_user_id, brand_name in brands:
            brand_id, owner_user_id = str(brand_id), str(owner_user_id)
            publishing_channel_id = brand_publishing_channel_id(conn, owner_user_id, brand_id)
            channel = conn.execute(
                """SELECT channel_name,channel_description FROM youtube_channels
                   WHERE CAST(brand_id AS VARCHAR)=CAST(? AS VARCHAR)
                   ORDER BY CASE WHEN youtube_channel_id=? THEN 0 ELSE 1 END,
                            COALESCE(is_active,false) DESC,added_at DESC LIMIT 1""",
                [brand_id, publishing_channel_id],
            ).fetchone()
            channel_name = str((channel[0] if channel else "") or brand_name or "Untitled brand")
            description = str((channel[1] if channel else "") or "").strip()
            titles = []
            if table_columns(conn, "niche_brand_channel_uploads"):
                titles.extend(str(row[0] or "").strip() for row in conn.execute(
                    """SELECT title FROM niche_brand_channel_uploads
                       WHERE CAST(brand_id AS VARCHAR)=CAST(? AS VARCHAR)
                         AND TRIM(COALESCE(title,''))<>''
                       ORDER BY published_at DESC NULLS LAST,refreshed_at DESC LIMIT 15""",
                    [brand_id],
                ).fetchall())
            if len(titles) < 15:
                titles.extend(str(row[0] or "").strip() for row in conn.execute(
                    """SELECT generated_title FROM shorts_generated_videos
                       WHERE CAST(brand_id AS VARCHAR)=CAST(? AS VARCHAR)
                         AND TRIM(COALESCE(generated_title,''))<>''
                       ORDER BY COALESCE(youtube_published_at,instagram_published_at,published_at,
                                         planned_publish_at,created_at) DESC LIMIT 15""",
                    [brand_id],
                ).fetchall())
            deduped = []
            seen = set()
            for title in titles:
                key = " ".join(title.casefold().split())
                if key and key not in seen:
                    seen.add(key)
                    deduped.append(title)
                if len(deduped) == 15:
                    break
            identity_text = f"{brand_name or ''} {channel_name}".casefold()
            is_hocaefendi_clips = "hocaefendi" in identity_text
            is_clip_channel = is_hocaefendi_clips or any(
                marker in f"{identity_text} {description.casefold()}"
                for marker in ("clip channel", "clips channel", "sohbet kesit", "video kesit")
            )
            profiles.append({
                "brand_id": brand_id,
                "brand_name": str(brand_name or channel_name),
                "channel_name": channel_name,
                "channel_description": description,
                "recent_short_titles": deduped,
                "is_clip_channel": is_clip_channel,
                "content_identity": (
                    "M. Fethullah Gülen sohbet clips"
                    if is_hocaefendi_clips else ""
                ),
            })
        return profiles
    finally:
        conn.close()


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


def _generate_idea(
    source: dict[str, Any], transcript: dict[str, Any], niche: dict[str, Any],
    brand: dict[str, Any], client: Any,
) -> dict[str, Any]:
    language = str(niche.get("language") or "en")
    system = (
        "Analyze one successful YouTube Short and create a brand-specific version. KEEP THE SAME specific "
        "topic, core message, claim or teaching, and the same content structure as the source. Change only "
        "the wording and presentation voice. Do not replace the premise with an analogous story, generic "
        "habit, or adjacent topic. For example, a specific dua/zikir and its benefit must remain a dua/zikir "
        "with that benefit and hook→practice→call structure. The version must fit the supplied niche definition. "
        "When brand.is_clip_channel is true, EVERY beat must describe a concrete source segment to locate "
        "in that speaker's existing recordings (for example, 'a sohbet segment where …'), not a script, "
        "new teaching, or invented claim. Respect brand.content_identity exactly. Otherwise, beats are three "
        "concrete content points, never plot "
        "devices. Match the brand's subject and voice using its channel profile and recent titles. "
        "Return JSON only with why_it_worked (English, unchanged analysis rules: 1–2 sentences, <=35 words, "
        "about hook/structure/format), format (Story, Q&A, Tip, or Motivation), topic_check containing "
        "same_topic and fits_niche booleans plus reason, and your_version containing title, first_line, exactly "
        "three beats, and ideal_length as a lower–upper seconds range. title/first_line/beats use the niche "
        "language. Never copy seven consecutive transcript words."
    )
    request_data = {
        "niche": {
            "language": language,
            "description": str(niche.get("description") or ""),
            "core_topics": niche.get("core_topics") or [],
        },
        "brand": brand,
        "source": {
            "title": str(source.get("title") or ""),
            "duration_seconds": int(source.get("duration_s") or 0),
            "ratio_to_creator_usual": round(float(source.get("outlier_ratio") or 0), 2),
            "transcript": transcript["transcript"],
        },
    }
    total_input = total_output = 0
    last_reason = ""
    for attempt in range(2):
        payload = dict(request_data)
        if attempt:
            payload["correction"] = (
                "The previous draft failed validation. Regenerate once, keeping the source's exact subject, "
                "message, and structure, and follow every language, length, format, and originality rule. "
                "why_it_worked must be English. " + last_reason
            )
        response = client.chat.completions.create(
            model=CHAT_MODEL, temperature=0.2, max_tokens=800,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
        )
        used_input, used_output = _usage(response)
        total_input += used_input
        total_output += used_output
        parsed = _json_response(response)
        topic_check = parsed.get("topic_check") if isinstance(parsed.get("topic_check"), dict) else {}
        same_topic = topic_check.get("same_topic") is True
        fits_niche = topic_check.get("fits_niche") is True
        last_reason = " ".join(str(topic_check.get("reason") or "").split())
        if not same_topic or not fits_niche:
            if attempt == 0:
                continue
            raise NicheShortIdeasError("brand idea failed topic/niche validation after regeneration")
        try:
            generated = _validate_generated_idea(
                parsed, transcript["transcript"], niche_language=language
            )
        except NicheShortIdeasError as exc:
            if attempt == 0:
                last_reason = str(exc)
                continue
            raise
        if brand.get("is_clip_channel") and not _clip_beats_are_source_segments(generated["beats"]):
            last_reason = "Every beat for this clip channel must identify a concrete source recording segment."
            if attempt == 0:
                continue
            raise NicheShortIdeasError(last_reason)
        return {
            **generated,
            "regeneration_count": attempt,
            "llm_input_tokens": total_input,
            "llm_output_tokens": total_output,
            "llm_cost_usd": _chat_cost(total_input, total_output),
        }
    raise NicheShortIdeasError("brand idea generation failed")


def compute_niche_short_ideas(niche_slug: str, *, client: Any = None) -> dict[str, Any]:
    selected_client = client or _openai_client
    if selected_client is None:
        raise NicheShortIdeasError("OpenAI client is not configured")
    niche, as_of, sources = _source_rows(niche_slug)
    if not as_of or not sources:
        raise NicheShortIdeasError("niche has no qualifying weekly winners")
    brands = _brand_profiles(niche["id"])
    if not brands:
        raise NicheShortIdeasError("niche has no brands")
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
    source_transcripts = {}
    for source in sources:
        video_id = str(source.get("video_id") or "")
        transcript = _cached_transcript(video_id)
        if transcript:
            cache_hits += 1
        else:
            transcript = _transcribe_source(video_id, int(source.get("duration_s") or 0), language)
            _persist_transcript(video_id, transcript)
            whisper_minutes += transcript["whisper_minutes"]
            whisper_cost += transcript["whisper_cost_usd"]
        source_transcripts[video_id] = transcript
    for brand in brands:
        for rank, source in enumerate(sources, start=1):
            video_id = str(source.get("video_id") or "")
            idea = _generate_idea(source, source_transcripts[video_id], niche, brand, selected_client)
            llm_cost += idea["llm_cost_usd"]
            generated_rows.append({
                **source, **idea, "selected_rank": rank, "brand_id": brand["brand_id"],
                "brand_name": brand["brand_name"],
            })
    conn = get_db()
    try:
        conn.execute(f"DELETE FROM {IDEAS_TABLE} WHERE niche_id=? AND as_of=?", [niche["id"], as_of])
        for item in generated_rows:
            conn.execute(
                f"""INSERT INTO {IDEAS_TABLE}
                    (niche_id,as_of,brand_id,source_video_id,source_channel_id,source_channel_title,
                     source_title,source_views,source_ratio,source_url,source_duration_s,selected_rank,
                     why_it_worked,idea_title,first_line,beats_json,ideal_length,format_tag,
                     regeneration_count,llm_input_tokens,llm_output_tokens,llm_cost_usd,computed_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?::jsonb,?,?,?,?,?,?,CURRENT_TIMESTAMP)""",
                [niche["id"], as_of, item["brand_id"], str(item.get("video_id") or ""),
                 str(item.get("youtube_channel_id") or ""), item["source_channel_title"],
                 str(item.get("title") or ""), int(item.get("views") or 0),
                 float(item.get("outlier_ratio") or 0),
                 f"https://youtube.com/shorts/{item.get('video_id')}",
                 int(item.get("duration_s") or 0), item["selected_rank"], item["why_it_worked"],
                 item["idea_title"], item["first_line"], json.dumps(item["beats"], ensure_ascii=False),
                 item["ideal_length"], item["format_tag"], item["regeneration_count"], item["llm_input_tokens"],
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
        "brand_count": len(brands),
        "idea_count": len(generated_rows),
        "cache_hits": cache_hits,
        "whisper_minutes": whisper_minutes,
        "whisper_cost_usd": whisper_cost,
        "llm_cost_usd": llm_cost,
        "total_cost_usd": whisper_cost + llm_cost,
        "ideas": [{
            "source_video_id": str(item.get("video_id") or ""),
            "brand_id": item["brand_id"],
            "brand_name": item["brand_name"],
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


def load_niche_short_ideas(
    conn: Any, niche_id: Any, brand_id: str
) -> tuple[str, list[dict[str, Any]]]:
    if not table_columns(conn, IDEAS_TABLE):
        return "", []
    latest = conn.execute(
        f"SELECT MAX(as_of) FROM {IDEAS_TABLE} WHERE niche_id=? AND CAST(brand_id AS VARCHAR)=CAST(? AS VARCHAR)",
        [niche_id, brand_id],
    ).fetchone()
    as_of = str(latest[0]) if latest and latest[0] else ""
    if not as_of:
        return "", []
    result = conn.execute(
        f"""SELECT source_video_id,source_channel_title,source_title,source_views,source_ratio,
                   source_url,source_duration_s,selected_rank,why_it_worked,idea_title,first_line,
                   beats_json,ideal_length,format_tag
            FROM {IDEAS_TABLE} WHERE niche_id=? AND as_of=?
              AND CAST(brand_id AS VARCHAR)=CAST(? AS VARCHAR) ORDER BY selected_rank""",
        [niche_id, as_of, brand_id],
    )
    columns = [str(item[0]) for item in result.description or []]
    rows = [dict(zip(columns, row)) for row in result.fetchall()]
    for row in rows:
        row["beats"] = _decode_json(row.pop("beats_json", []), [])
        row["ideal_length"] = _normalize_length_range(row.get("ideal_length"))
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
