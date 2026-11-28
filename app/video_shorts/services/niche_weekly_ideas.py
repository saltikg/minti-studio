from __future__ import annotations

import json
import math
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any

from app.video_shorts.config import _openai_client
from app.video_shorts.services.db import get_db, get_db_readonly, table_columns
from app.video_shorts.services.niche_definitions import load_niche_definition
from app.video_shorts.services.niche_outliers import (
    PACIFIC, customer_visible_outlier_rows, load_latest_niche_outliers,
)
from app.video_shorts.services.niches import final_niche_pool, load_niche_snapshot
from app.video_shorts.services import niche_rising_topics as rising
from app.video_shorts.services.niche_caption_archive import ARCHIVE_TABLE, UPLOADS_TABLE, _caption_windows


IDEAS_TABLE = "niche_weekly_ideas"
CHAT_MODEL = "gpt-4.1-mini"
EMBEDDING_MODEL = "text-embedding-3-small"
CHAT_INPUT_PER_MILLION = 0.40
CHAT_OUTPUT_PER_MILLION = 1.60
EMBEDDING_PER_MILLION = 0.02
ARCHIVE_SIMILARITY_THRESHOLD = 0.35
HOCA_BRAND_ID = "63f772f8-2d31-4416-9239-c546949bfa98"
SHELLEY_BRAND_ID = "3dab32b8-a17a-4b96-a027-e49774813247"
HOCA_SOURCE_ACCOUNT_EMAILS = (
    "gokhansaltik@gmail.com",
    "hocaefendidenkisakisa@gmail.com",
)
HOCA_SOURCE_CHANNEL_IDS = (
    "UCiAv2GTh72OdtlNzptHocsw",  # HerkulNagme
    "UCsKq43fCZigi5c9QeuB6sMA",  # Maneviyatimcom
    "UCEDs-wNyPD06rS9R3OYd1Ng",  # Sakirtmen57
)


class WeeklyIdeasError(RuntimeError):
    pass


def weekly_ideas_brand_enabled(brand_id: str) -> bool:
    configured = os.getenv("NICHE_WEEKLY_IDEAS_DISABLED_BRAND_IDS")
    raw = SHELLEY_BRAND_ID if configured is None else configured
    disabled = {item.strip() for item in raw.split(",") if item.strip()}
    return str(brand_id) not in disabled


def _usage(response: Any) -> tuple[int, int]:
    usage = getattr(response, "usage", None)
    return (
        int(getattr(usage, "prompt_tokens", 0) or getattr(usage, "input_tokens", 0) or 0),
        int(getattr(usage, "completion_tokens", 0) or getattr(usage, "output_tokens", 0) or 0),
    )


def _json_response(response: Any) -> dict[str, Any]:
    return rising._json_response(response)


def _chat_cost(input_tokens: int, output_tokens: int) -> float:
    return (input_tokens * CHAT_INPUT_PER_MILLION + output_tokens * CHAT_OUTPUT_PER_MILLION) / 1_000_000


def _embedding_cost(tokens: int) -> float:
    return tokens * EMBEDDING_PER_MILLION / 1_000_000


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _candidate_rows(
    rows: list[dict[str, Any]], as_of: str, *, days: int = 30,
    allowed_channel_ids: set[str] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    end = datetime.combine(date.fromisoformat(as_of) + timedelta(days=1), datetime.min.time(), tzinfo=PACIFIC)
    start = end - timedelta(days=days)
    output = []
    channel_counts: dict[str, int] = {}
    for row in sorted(rows, key=lambda item: (
        1 if item.get("signal_type") == "early" else 0,
        -float((item.get("speed_ratio") or 0) if item.get("signal_type") == "early" else (item.get("outlier_ratio") or 0)),
    )):
        published = _parse_time(row.get("published_at"))
        channel_id = str(row.get("youtube_channel_id") or "")
        engagement = float(row.get("engagement") or 0)
        normal = float(row.get("baseline_engagement") or 0)
        if (
            not published or not start <= published.astimezone(PACIFIC) < end
            or int(row.get("views") or 0) < 5000
            or row.get("in_category", row.get("on_topic")) is False
            or (normal > 0 and engagement < normal / 3)
            or not channel_id
            or (allowed_channel_ids is not None and channel_id not in allowed_channel_ids)
            or channel_counts.get(channel_id, 0) >= 2
        ):
            continue
        channel_counts[channel_id] = channel_counts.get(channel_id, 0) + 1
        output.append(row)
    return output, days


def _within_candidate_days(row: dict[str, Any], as_of: str, days: int) -> bool:
    published = _parse_time(row.get("published_at"))
    if not published:
        return False
    end = datetime.combine(date.fromisoformat(as_of) + timedelta(days=1), datetime.min.time(), tzinfo=PACIFIC)
    return end - timedelta(days=days) <= published.astimezone(PACIFIC) < end


def _choose_candidate_window(
    rows: list[dict[str, Any]], as_of: str, done: dict[str, dict[str, Any]],
    rejected: dict[str, dict[str, Any]],
) -> int:
    for days in (7, 14, 30):
        available = [
            row for row in rows
            if _within_candidate_days(row, as_of, days)
            and not bool((done.get(str(row.get("video_id"))) or {}).get("already_done"))
            and bool((rejected.get(str(row.get("video_id"))) or {}).get("is_topic", True))
        ]
        if len(available) >= 3:
            return days
    return 30


def _channel_titles(conn: Any, niche_id: int) -> dict[str, str]:
    rows = conn.execute(
        """
        SELECT youtube_channel_id,title FROM niche_channels
        WHERE niche_id=? AND snapshot_date=(SELECT MAX(snapshot_date) FROM niche_channels WHERE niche_id=?)
        """, [niche_id, niche_id],
    ).fetchall()
    return {str(row[0]): str(row[1] or row[0]) for row in rows}


def _pool_channel_ids(conn: Any, niche_slug: str) -> set[str]:
    niche, rows = load_niche_snapshot(conn, niche_slug)
    return {
        str(row.get("youtube_channel_id") or "")
        for row in final_niche_pool(rows, clinicians_only=False, niche=niche)
        if row.get("youtube_channel_id")
    }


def _label_candidates(
    rows: list[dict[str, Any]], definition: dict[str, Any], client: Any,
) -> tuple[list[dict[str, Any]], int, int]:
    labels, input_tokens, output_tokens = rising._label_rows(rows, definition, client)
    merged, used_in, used_out = rising._merge_labels(labels, definition.get("language") or "en", client)
    input_tokens += used_in
    output_tokens += used_out
    theme_by_video = {}
    creators_by_theme: dict[str, set[str]] = {}
    for theme in merged:
        for video_id in theme["video_ids"]:
            theme_by_video[video_id] = theme["label"]
        creators_by_theme.setdefault(theme["label"], set()).update(
            str(row.get("youtube_channel_id") or "") for row in rows
            if str(row.get("video_id") or "") in set(theme["video_ids"])
        )
    output = []
    for row in rows:
        video_id = str(row.get("video_id") or "")
        angle = labels.get(video_id) or rising._display_title(row.get("title"))
        theme = theme_by_video.get(video_id) or " ".join(angle.split()[:5])
        output.append({
            **row, "angle_label": angle, "theme_label": theme,
            "worked_creator_count": len({item for item in creators_by_theme.get(theme, set()) if item}),
        })
    return output, input_tokens, output_tokens


def _brand_shorts(conn: Any, brand_id: str, owner_user_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT 'minti:' || CAST(id AS VARCHAR),generated_title,
               COALESCE(youtube_published_at,instagram_published_at,published_at,planned_publish_at)
        FROM shorts_generated_videos
        WHERE CAST(brand_id AS VARCHAR)=CAST(? AS VARCHAR)
          AND CAST(user_id AS VARCHAR)=CAST(? AS VARCHAR)
          AND publish_status='published'
          AND COALESCE(youtube_published_at,instagram_published_at,published_at,planned_publish_at)
              >= CURRENT_TIMESTAMP-INTERVAL '90 days'
          AND TRIM(COALESCE(generated_title,''))<>''
        """, [brand_id, owner_user_id],
    ).fetchall()
    output = [{"id": str(row[0]), "title": str(row[1]), "published_at": str(row[2]), "source": "minti"} for row in rows]
    if table_columns(conn, UPLOADS_TABLE):
        connected = conn.execute(
            f"""
            SELECT youtube_video_id,title,published_at FROM {UPLOADS_TABLE}
            WHERE CAST(brand_id AS VARCHAR)=CAST(? AS VARCHAR)
              AND duration_s<=180 AND published_at>=CURRENT_TIMESTAMP-INTERVAL '90 days'
            """, [brand_id],
        ).fetchall()
        output.extend({
            "id": f"youtube:{row[0]}", "title": str(row[1]), "published_at": str(row[2]), "source": "youtube",
        } for row in connected)
    seen = set()
    deduped = []
    for item in output:
        key = " ".join(item["title"].casefold().split())
        if key and key not in seen:
            seen.add(key)
            deduped.append(item)
    return deduped


def _already_done(
    ideas: list[dict[str, Any]], shorts: list[dict[str, Any]], language: str, client: Any,
) -> tuple[dict[str, dict[str, Any]], int, int, dict[str, Any]]:
    if not ideas or not shorts:
        return {}, 0, 0, {"matches": []}
    response = client.chat.completions.create(
        model=CHAT_MODEL, temperature=0, max_tokens=max(500, len(ideas) * 100),
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": (
                "For each proposed Short ANGLE, inspect every supplied Short title. already_done is true only when "
                "the brand already made a Short about the same specific question, claim, story, or teaching angle. "
                "Sharing a broad theme is not enough. Set theme_covered true when the broad theme appeared but the "
                "specific angle is new. Return JSON only: "
                "{\"matches\":[{\"source_video_id\":\"...\",\"already_done\":true|false,"
                "\"theme_covered\":true|false,\"matched_short_id\":null|\"...\","
                "\"matched_title\":\"...\",\"reason\":\"...\"}]}. "
                "Return one item for every proposed source_video_id."
            )},
            {"role": "user", "content": json.dumps({
                "language": language,
                "ideas": [{
                    "source_video_id": str(item.get("video_id")),
                    "angle": item["angle_label"], "theme": item["theme_label"],
                } for item in ideas],
                "brand_shorts": shorts,
            }, ensure_ascii=False)},
        ],
    )
    parsed = _json_response(response)
    by_id = {
        str(item.get("source_video_id")): item for item in parsed.get("matches") or [] if isinstance(item, dict)
    }
    used_in, used_out = _usage(response)
    return by_id, used_in, used_out, parsed


def _topic_gate(
    ideas: list[dict[str, Any]], language: str, client: Any,
) -> tuple[dict[str, dict[str, Any]], int, int, dict[str, Any]]:
    if not ideas:
        return {}, 0, 0, {"candidates": []}
    response = client.chat.completions.create(
        model=CHAT_MODEL, temperature=0, max_tokens=max(500, len(ideas) * 80),
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": (
                "Gate Short candidates for concrete creator ideas. is_topic=true only for a content topic, question, "
                "claim, story lesson, or teaching angle that a creator could meaningfully teach or discuss. Reject "
                "mere moments, formats, vibes, reactions, gossip, celebrity clips, or titles whose only appeal is who "
                "appears. Return JSON only: {\"candidates\":[{\"source_video_id\":\"...\","
                "\"is_topic\":true|false,\"reason\":\"...\"}]}. Return every candidate."
            )},
            {"role": "user", "content": json.dumps({
                "language": language,
                "candidates": [{
                    "source_video_id": str(item.get("video_id")),
                    "title": str(item.get("title") or ""),
                    "angle": item["angle_label"], "theme": item["theme_label"],
                } for item in ideas],
            }, ensure_ascii=False)},
        ],
    )
    parsed = _json_response(response)
    by_id = {
        str(item.get("source_video_id")): item
        for item in parsed.get("candidates") or [] if isinstance(item, dict)
    }
    used_in, used_out = _usage(response)
    return by_id, used_in, used_out, parsed


def _decode_json(value: Any, fallback: Any) -> Any:
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(str(value or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


def _json_safe(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _sync_hoca_source_archive(conn: Any, brand_id: str, language: str) -> int:
    placeholders_email = ",".join("?" for _ in HOCA_SOURCE_ACCOUNT_EMAILS)
    placeholders_channel = ",".join("?" for _ in HOCA_SOURCE_CHANNEL_IDS)
    rows = conn.execute(
        f"""
        SELECT v.id,v.video_id,v.title,COALESCE(v.published_at,v.created_at),
               t.whisper_segments_json,c.channel_name,CAST(v.brand_id AS VARCHAR)
        FROM youtube_videos v
        JOIN shorts_users u ON CAST(u.id AS VARCHAR)=CAST(v.owner_user_id AS VARCHAR)
        JOIN youtube_channels c ON c.channel_id=v.channel_id
        JOIN youtube_transcripts t ON t.video_id=v.video_id
        WHERE lower(u.email) IN ({placeholders_email})
          AND c.youtube_channel_id IN ({placeholders_channel})
          AND v.deleted_at IS NULL
          AND NULLIF(TRIM(t.whisper_segments_json),'') IS NOT NULL
        ORDER BY CASE WHEN CAST(v.brand_id AS VARCHAR)=CAST(? AS VARCHAR) THEN 0 ELSE 1 END,
                 v.id DESC
        """,
        [*HOCA_SOURCE_ACCOUNT_EMAILS, *HOCA_SOURCE_CHANNEL_IDS, brand_id],
    ).fetchall()
    seen: set[str] = set()
    synced = 0
    for video_pk, video_id, title, published_at, segments_value, channel_title, _row_brand in rows:
        archive_id = str(video_id or "").strip()
        if not archive_id or archive_id in seen:
            continue
        seen.add(archive_id)
        decoded = rising._decode_timed_segments(segments_value)
        windows = _caption_windows(decoded)
        if not windows:
            continue
        conn.execute(
            f"""
            INSERT INTO {ARCHIVE_TABLE}
            (brand_id,youtube_video_id,source_video_pk,title,published_at,language,transcript_source,
             source_channel_title,segments_json,windows_json,updated_at)
            VALUES (?,?,?,?,?,?, 'source_whisper',?,?::jsonb,?::jsonb,CURRENT_TIMESTAMP)
            ON CONFLICT (brand_id,youtube_video_id) DO UPDATE SET
                source_video_pk=EXCLUDED.source_video_pk,title=EXCLUDED.title,
                published_at=EXCLUDED.published_at,language=EXCLUDED.language,
                transcript_source='source_whisper',source_channel_title=EXCLUDED.source_channel_title,
                segments_json=EXCLUDED.segments_json,
                windows_json=CASE WHEN {ARCHIVE_TABLE}.embedding_model=?
                                  THEN {ARCHIVE_TABLE}.windows_json ELSE EXCLUDED.windows_json END,
                updated_at=CURRENT_TIMESTAMP
            """,
            [brand_id, archive_id, video_pk, str(title or archive_id), published_at, language,
             str(channel_title or ""), json.dumps(decoded, ensure_ascii=False),
             json.dumps(windows, ensure_ascii=False), EMBEDDING_MODEL],
        )
        synced += 1
    return synced


def _sync_minti_archive(conn: Any, brand_id: str, owner_user_id: str, language: str) -> None:
    transcript_columns = table_columns(conn, "youtube_transcripts")
    segment_names = [name for name in ("whisper_segments_json", "segments_json", "segments_en_json") if name in transcript_columns]
    if not segment_names:
        return
    segment_sql = "COALESCE(" + ",".join(f"t.{name}" for name in segment_names) + ")" if len(segment_names) > 1 else f"t.{segment_names[0]}"
    rows = conn.execute(
        f"""
        SELECT v.id,v.video_id,v.title,COALESCE(v.published_at,v.created_at),{segment_sql}
        FROM youtube_videos v JOIN youtube_transcripts t ON t.video_id=v.video_id
        LEFT JOIN youtube_channels c ON c.channel_id=v.channel_id
        WHERE CAST(v.brand_id AS VARCHAR)=CAST(? AS VARCHAR)
          AND CAST(v.owner_user_id AS VARCHAR)=CAST(? AS VARCHAR)
          AND (v.channel_id IS NULL OR lower(COALESCE(c.channel_url,''))='local://uploads')
          AND COALESCE(v.duration_seconds,v.media_duration_seconds,0)>180 AND v.deleted_at IS NULL
        """, [brand_id, owner_user_id],
    ).fetchall()
    for video_pk, video_id, title, published_at, segments_value in rows:
        decoded = rising._decode_timed_segments(segments_value)
        windows = _caption_windows(decoded)
        if not windows:
            continue
        archive_id = str(video_id or f"minti-{video_pk}")
        conn.execute(
            f"""
            INSERT INTO {ARCHIVE_TABLE}
            (brand_id,youtube_video_id,source_video_pk,title,published_at,language,transcript_source,
             segments_json,windows_json,updated_at)
            VALUES (?,?,?,?,?,?, 'minti',?::jsonb,?::jsonb,CURRENT_TIMESTAMP)
            ON CONFLICT (brand_id,youtube_video_id) DO UPDATE SET
                source_video_pk=EXCLUDED.source_video_pk,title=EXCLUDED.title,published_at=EXCLUDED.published_at,
                segments_json=EXCLUDED.segments_json,
                windows_json=CASE WHEN {ARCHIVE_TABLE}.embedding_model=? THEN {ARCHIVE_TABLE}.windows_json ELSE EXCLUDED.windows_json END,
                updated_at=CURRENT_TIMESTAMP
            """, [brand_id, archive_id, video_pk, str(title or archive_id), published_at, language,
                   json.dumps(decoded, ensure_ascii=False), json.dumps(windows, ensure_ascii=False), EMBEDDING_MODEL],
        )


def _embed_archive(conn: Any, brand_id: str, client: Any) -> tuple[list[dict[str, Any]], int]:
    source_clause = " AND transcript_source='source_whisper'" if brand_id == HOCA_BRAND_ID else ""
    rows = conn.execute(
        f"""
        SELECT youtube_video_id,source_video_pk,title,published_at,transcript_source,
               source_channel_title,windows_json,embedding_model
        FROM {ARCHIVE_TABLE} WHERE CAST(brand_id AS VARCHAR)=CAST(? AS VARCHAR)
        {source_clause}
        """, [brand_id],
    ).fetchall()
    output, total_tokens = [], 0
    for video_id, video_pk, title, published_at, source, source_channel_title, windows_value, model in rows:
        windows = _decode_json(windows_value, [])
        needs = model != EMBEDDING_MODEL or any(not item.get("embedding") for item in windows)
        if windows and needs:
            response = client.embeddings.create(model=EMBEDDING_MODEL, input=[str(item.get("text") or "") for item in windows])
            for item, embedding in zip(windows, response.data):
                item["embedding"] = list(embedding.embedding)
            total_tokens += int(getattr(getattr(response, "usage", None), "total_tokens", 0) or 0)
            conn.execute(
                f"UPDATE {ARCHIVE_TABLE} SET windows_json=?::jsonb,embedding_model=?,updated_at=CURRENT_TIMESTAMP "
                "WHERE CAST(brand_id AS VARCHAR)=CAST(? AS VARCHAR) AND youtube_video_id=?",
                [json.dumps(windows, ensure_ascii=False), EMBEDDING_MODEL, brand_id, video_id],
            )
        for index, item in enumerate(windows):
            if item.get("embedding"):
                output.append({
                    "youtube_video_id": str(video_id), "video_pk": int(video_pk) if video_pk else None,
                    "title": str(title), "published_at": published_at, "source": str(source),
                    "source_channel_title": str(source_channel_title or ""),
                    "window_index": index, **item,
                })
    return output, total_tokens


def _cosine(left: list[float], right: list[float]) -> float:
    numerator = sum(a * b for a, b in zip(left, right))
    denominator = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    return numerator / denominator if denominator else 0.0


def _archive_matches(
    ideas: list[dict[str, Any]], windows: list[dict[str, Any]], client: Any,
) -> tuple[dict[str, dict[str, Any]], int, int, int, dict[str, Any]]:
    if not ideas or not windows:
        return {}, 0, 0, 0, {"ideas": []}
    embed_response = client.embeddings.create(
        model=EMBEDDING_MODEL,
        input=[f"{item['theme_label']}: {item['angle_label']}" for item in ideas],
    )
    embedding_tokens = int(getattr(getattr(embed_response, "usage", None), "total_tokens", 0) or 0)
    candidates: dict[str, list[dict[str, Any]]] = {}
    for idea, embedding_item in zip(ideas, embed_response.data):
        ranked = sorted(
            windows,
            key=lambda window: -_cosine(list(embedding_item.embedding), list(window["embedding"])),
        )[:3]
        candidate_windows = []
        for candidate_index, window in enumerate(ranked):
            similarity = _cosine(list(embedding_item.embedding), list(window["embedding"]))
            candidate_windows.append({
                **{key: _json_safe(value) for key, value in window.items() if key != "embedding"},
                "candidate_index": candidate_index,
                "similarity": similarity,
                "above_threshold": similarity >= ARCHIVE_SIMILARITY_THRESHOLD,
            })
        candidates[str(idea.get("video_id"))] = candidate_windows
    response = client.chat.completions.create(
        model=CHAT_MODEL, temperature=0, max_tokens=max(700, len(ideas) * 220),
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": (
                "For each Short idea, inspect the three semantically retrieved transcript windows. A match is true "
                "only when the roughly 30-second window is mainly about the idea's specific angle; passing mentions "
                "and broad-theme overlap are false. Any window marked above_threshold=false must be false. "
                "Return every window with match true/false and a concrete reason, using its candidate_index, then "
                "best_candidate_index or null. "
                "Evidence for the best match must be a verbatim quote of at most 20 words. JSON only: "
                "{\"ideas\":[{\"source_video_id\":\"...\",\"best_candidate_index\":null|integer,"
                "\"evidence\":\"...\",\"verdicts\":[{\"candidate_index\":0,\"match\":true,\"reason\":\"...\"}]}]}."
            )},
            {"role": "user", "content": json.dumps({
                "ideas": [{
                    "source_video_id": str(item.get("video_id")), "theme": item["theme_label"],
                    "angle": item["angle_label"], "windows": candidates[str(item.get("video_id"))],
                } for item in ideas]
            }, ensure_ascii=False)},
        ],
    )
    parsed = _json_response(response)
    matches = {}
    for result in parsed.get("ideas") or []:
        source_id = str(result.get("source_video_id") or "")
        candidate_list = candidates.get(source_id) or []
        try:
            index = int(result.get("best_candidate_index"))
        except (TypeError, ValueError):
            continue
        if not 0 <= index < len(candidate_list):
            continue
        verdict = next((item for item in result.get("verdicts") or [] if int(item.get("candidate_index", -1)) == index), {})
        if not verdict.get("match"):
            continue
        window = candidate_list[index]
        if float(window.get("similarity") or 0) < ARCHIVE_SIMILARITY_THRESHOLD:
            continue
        evidence = rising._truncate_evidence(result.get("evidence"))
        if evidence.casefold() not in str(window.get("text") or "").casefold():
            evidence = rising._truncate_evidence(window.get("text"))
        matches[source_id] = {**window, "evidence": evidence}
    used_in, used_out = _usage(response)
    return matches, used_in, used_out, embedding_tokens, {"candidates": candidates, "verdicts": parsed}


def _suggest_new_ideas(
    ideas: list[dict[str, Any]], language: str, client: Any,
) -> tuple[dict[str, dict[str, str]], int, int]:
    if not ideas:
        return {}, 0, 0
    response = client.chat.completions.create(
        model=CHAT_MODEL, temperature=0.2, max_tokens=max(300, len(ideas) * 80),
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": (
                "Create one concrete video title and one spoken first-three-seconds hook for each idea, in the "
                "requested language. Do not give medical or religious advice beyond framing the source idea. "
                "Return JSON only: {\"ideas\":[{\"source_video_id\":\"...\",\"title\":\"...\",\"hook\":\"...\"}]}"
            )},
            {"role": "user", "content": json.dumps({
                "language": language,
                "ideas": [{"source_video_id": str(item.get("video_id")), "theme": item["theme_label"], "angle": item["angle_label"]} for item in ideas],
            }, ensure_ascii=False)},
        ],
    )
    parsed = _json_response(response)
    output = {str(item.get("source_video_id")): {"title": str(item.get("title") or ""), "hook": str(item.get("hook") or "")} for item in parsed.get("ideas") or [] if isinstance(item, dict)}
    used_in, used_out = _usage(response)
    return output, used_in, used_out


def compute_brand_weekly_ideas(brand_id: str, *, client: Any = None) -> dict[str, Any]:
    selected_client = client or _openai_client
    if selected_client is None:
        raise WeeklyIdeasError("OpenAI client is not configured")
    conn = get_db()
    try:
        if not table_columns(conn, IDEAS_TABLE) or not table_columns(conn, ARCHIVE_TABLE):
            raise WeeklyIdeasError("weekly ideas migration has not been applied")
        brand = conn.execute(
            """
            SELECT CAST(b.owner_user_id AS VARCHAR),b.name,n.id,n.slug,n.language
            FROM shorts_brands b JOIN niches n ON n.id=b.niche_id
            WHERE CAST(b.id AS VARCHAR)=CAST(? AS VARCHAR)
            """, [brand_id],
        ).fetchone()
        if not brand:
            raise WeeklyIdeasError("brand has no niche")
        owner_user_id, brand_name, niche_id, niche_slug, language = str(brand[0]), str(brand[1]), int(brand[2]), str(brand[3]), str(brand[4] or "en")
        definition = load_niche_definition(conn, slug=niche_slug)
        as_of, outliers = load_latest_niche_outliers(conn, niche_id, signal_type="weekly")
        if not as_of:
            raise WeeklyIdeasError("niche has no outlier snapshot")
        _early_as_of, early_outliers = load_latest_niche_outliers(conn, niche_id, signal_type="early")
        # Weekly ideas consume the exact customer card cohorts.  Early cards
        # are only a fallback pool when fewer than three weekly ideas survive.
        outliers = [
            *customer_visible_outlier_rows(outliers),
            *customer_visible_outlier_rows(early_outliers, score_key="speed_ratio"),
        ]
        pool_channel_ids = _pool_channel_ids(conn, niche_slug)
        candidates, _maximum_days = _candidate_rows(
            outliers, as_of, days=30, allowed_channel_ids=pool_channel_ids,
        )
        channels = _channel_titles(conn, niche_id)
        candidates, chat_in, chat_out = _label_candidates(candidates, definition, selected_client)
        topic_gate, used_in, used_out, topic_audit = _topic_gate(candidates, language, selected_client)
        chat_in += used_in
        chat_out += used_out
        shorts = _brand_shorts(conn, brand_id, owner_user_id)
        done, used_in, used_out, done_audit = _already_done(candidates, shorts, language, selected_client)
        chat_in += used_in
        chat_out += used_out
        candidate_days = 30
        available = [
            item for item in candidates
            if not bool((done.get(str(item.get("video_id"))) or {}).get("already_done"))
            and bool((topic_gate.get(str(item.get("video_id"))) or {}).get("is_topic", True))
        ]
        weekly_available = [item for item in available if item.get("signal_type") != "early"]
        early_available = [item for item in available if item.get("signal_type") == "early"]
        selected_candidates = [*weekly_available[:3], *early_available[:max(0, 3 - len(weekly_available))]]
        source_archive_count = 0
        if brand_id == HOCA_BRAND_ID:
            source_archive_count = _sync_hoca_source_archive(conn, brand_id, language)
        else:
            _sync_minti_archive(conn, brand_id, owner_user_id, language)
        conn.commit()
        windows, embedding_tokens = _embed_archive(conn, brand_id, selected_client)
        conn.commit()
        matches, used_in, used_out, idea_embedding_tokens, archive_audit = _archive_matches(
            selected_candidates, windows, selected_client,
        )
        chat_in += used_in
        chat_out += used_out
        embedding_tokens += idea_embedding_tokens
        new_ideas = [item for item in selected_candidates if str(item.get("video_id")) not in matches]
        suggestions, used_in, used_out = _suggest_new_ideas(new_ideas, language, selected_client)
        chat_in += used_in
        chat_out += used_out
        conn.execute(f"DELETE FROM {IDEAS_TABLE} WHERE niche_id=? AND as_of=? AND CAST(brand_id AS VARCHAR)=CAST(? AS VARCHAR)", [niche_id, as_of, brand_id])
        selected_ids = {
            str(item.get("video_id")): index
            for index, item in enumerate(selected_candidates, start=1)
        }
        saved = []
        cost = _chat_cost(chat_in, chat_out) + _embedding_cost(embedding_tokens)
        for item in candidates:
            source_id = str(item.get("video_id") or "")
            done_item = done.get(source_id) or {}
            topic_item = topic_gate.get(source_id) or {}
            rejected = not bool(topic_item.get("is_topic", True))
            skipped = bool(done_item.get("already_done"))
            match = matches.get(source_id)
            rank = selected_ids.get(source_id)
            status = (
                "topic_rejected" if rejected else
                "already_done" if skipped else
                "archive" if rank and match else
                "new_idea" if rank else "candidate_not_selected"
            )
            idea_note = (
                "New angle on a theme you've covered"
                if rank and bool(done_item.get("theme_covered")) else ""
            )
            suggestion = suggestions.get(source_id) or {}
            archive_url = ""
            if match:
                if match.get("video_pk"):
                    archive_url = f"/video_shorts/generate/{match['video_pk']}?start={float(match['start_seconds']):.1f}"
                else:
                    archive_url = (
                        f"/video_shorts/videos?video_url=https://www.youtube.com/watch?v={match['youtube_video_id']}"
                        f"&clip_start={float(match['start_seconds']):.1f}"
                    )
            audit = {
                "candidate_window_days": candidate_days,
                "source_signal": str(item.get("signal_type") or "weekly"),
                "topic_gate": topic_item,
                "short_match": done_item,
                "short_population_count": len(shorts),
                "archive": {
                    "similarity_threshold": ARCHIVE_SIMILARITY_THRESHOLD,
                    "candidates": (archive_audit.get("candidates") or {}).get(source_id, []),
                    "verdict": next((entry for entry in (archive_audit.get("verdicts") or {}).get("ideas", []) if str(entry.get("source_video_id")) == source_id), {}),
                },
            }
            conn.execute(
                f"""
                INSERT INTO {IDEAS_TABLE}
                (niche_id,as_of,brand_id,source_video_id,source_channel_id,source_channel_title,
                 source_title,source_views,source_ratio,source_url,angle_label,theme_label,
                 worked_creator_count,selected_rank,skip_reason,skip_evidence,status,
                 archive_youtube_video_id,archive_video_pk,archive_title,archive_published_at,
                 archive_start_seconds,archive_snippet,archive_url,suggested_title,suggested_hook,
                 archive_source_channel_title,idea_note,audit_json,llm_input_tokens,llm_output_tokens,
                 embedding_tokens,estimated_cost_usd,computed_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?::jsonb,?,?,?,?,CURRENT_TIMESTAMP)
                """,
                [niche_id, as_of, brand_id, source_id, str(item.get("youtube_channel_id") or ""),
                 channels.get(str(item.get("youtube_channel_id") or ""), ""), str(item.get("title") or ""),
                 int(item.get("views") or 0), float(item.get("outlier_ratio") or 0),
                 f"https://youtube.com/shorts/{source_id}", item["angle_label"], item["theme_label"],
                 int(item.get("worked_creator_count") or 1), rank,
                 "not a teachable topic" if rejected else "already did" if skipped else None,
                 str(topic_item.get("reason") or "") if rejected else str(done_item.get("reason") or "") if skipped else None,
                 status, match.get("youtube_video_id") if match else None,
                 match.get("video_pk") if match else None, match.get("title") if match else None,
                 match.get("published_at") if match else None, match.get("start_seconds") if match else None,
                 match.get("evidence") if match else None, archive_url or None,
                 suggestion.get("title") if not match else None, suggestion.get("hook") if not match else None,
                 match.get("source_channel_title") if match else None, idea_note or None,
                 json.dumps(audit, ensure_ascii=False), chat_in, chat_out, embedding_tokens, cost],
            )
            saved.append({
                "source_video_id": source_id, "theme": item["theme_label"], "angle": item["angle_label"],
                "selected_rank": rank, "status": status, "skip_reason": str(done_item.get("reason") or ""),
                "topic_rejection_reason": str(topic_item.get("reason") or "") if rejected else "",
                "idea_note": idea_note, "archive": match, "suggestion": suggestion,
            })
        conn.commit()
        return {
            "brand_id": brand_id, "brand_name": brand_name, "niche": niche_slug, "as_of": as_of,
            "candidate_window_days": candidate_days, "candidate_count": len(candidates), "shorts_checked": len(shorts),
            "ideas": [item for item in saved if item["selected_rank"]],
            "skipped": [item for item in saved if item["status"] == "already_done"],
            "rejected": [item for item in saved if item["status"] == "topic_rejected"],
            "pool_channel_count": len(pool_channel_ids), "source_archive_videos": source_archive_count,
            "archive_similarity_threshold": ARCHIVE_SIMILARITY_THRESHOLD,
            "archive_windows": len(windows), "llm_input_tokens": chat_in, "llm_output_tokens": chat_out,
            "embedding_tokens": embedding_tokens, "estimated_cost_usd": cost,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _format_seconds(value: Any) -> str:
    return rising._format_seconds(value)


def load_brand_weekly_ideas(conn: Any, niche_id: Any, brand_id: str) -> tuple[str, list[dict[str, Any]]]:
    if not table_columns(conn, IDEAS_TABLE):
        return "", []
    row = conn.execute(
        f"SELECT MAX(as_of) FROM {IDEAS_TABLE} WHERE niche_id=? AND CAST(brand_id AS VARCHAR)=CAST(? AS VARCHAR)",
        [niche_id, brand_id],
    ).fetchone()
    as_of = str(row[0]) if row and row[0] else ""
    if not as_of:
        return "", []
    result = conn.execute(
        f"""
        SELECT * FROM {IDEAS_TABLE}
        WHERE niche_id=? AND as_of=? AND CAST(brand_id AS VARCHAR)=CAST(? AS VARCHAR)
          AND selected_rank IS NOT NULL ORDER BY selected_rank
        """, [niche_id, as_of, brand_id],
    )
    columns = [str(item[0]) for item in result.description or []]
    rows = [dict(zip(columns, row)) for row in result.fetchall()]
    for item in rows:
        item["archive_timestamp"] = _format_seconds(item.get("archive_start_seconds"))
        item["worked_badge"] = (
            f"Worked for {int(item['worked_creator_count'])} creators"
            if int(item.get("worked_creator_count") or 0) >= 2 else ""
        )
        item["early_signal_badge"] = (
            "Early signal" if (_decode_json(item.get("audit_json"), {}).get("source_signal") == "early") else ""
        )
    return as_of, rows


def load_admin_weekly_ideas(conn: Any, niche_id: Any) -> tuple[str, list[dict[str, Any]]]:
    if not table_columns(conn, IDEAS_TABLE):
        return "", []
    row = conn.execute(f"SELECT MAX(as_of) FROM {IDEAS_TABLE} WHERE niche_id=?", [niche_id]).fetchone()
    as_of = str(row[0]) if row and row[0] else ""
    if not as_of:
        return "", []
    result = conn.execute(
        f"""
        SELECT i.*,b.name AS brand_name FROM {IDEAS_TABLE} i
        LEFT JOIN shorts_brands b ON CAST(b.id AS VARCHAR)=CAST(i.brand_id AS VARCHAR)
        WHERE i.niche_id=? AND i.as_of=? ORDER BY lower(COALESCE(b.name,'')),i.source_ratio DESC
        """, [niche_id, as_of],
    )
    columns = [str(item[0]) for item in result.description or []]
    rows = [dict(zip(columns, row)) for row in result.fetchall()]
    for item in rows:
        item["audit"] = _decode_json(item.get("audit_json"), {})
        item["archive_timestamp"] = _format_seconds(item.get("archive_start_seconds"))
    return as_of, rows
