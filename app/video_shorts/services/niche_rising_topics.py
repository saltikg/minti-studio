from __future__ import annotations

import json
import re
import unicodedata
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote_plus

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
    if not 2 <= len(label.split()) <= 5 or _key(label) in {_key(item) for item in GENERIC_LABELS}:
        return ""
    return label


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
                "Label every Short with exactly one specific topic of 2–5 words in the niche language. "
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
                "Merge labels that express the same specific idea. Keep different ideas separate. "
                "Use a specific 2–5 word label in the supplied language. Return JSON only: "
                "{\"topics\":[{\"label\":\"...\",\"video_ids\":[\"...\"]}]}. "
                "Every input video_id must appear exactly once. Generic niche labels are forbidden."
            )},
            {"role": "user", "content": json.dumps({
                "language": language,
                "items": [{"video_id": video_id, "label": label} for video_id, label in labels.items()],
            }, ensure_ascii=False)},
        ],
    )
    parsed = _json_response(response)
    valid_ids = set(labels)
    used: set[str] = set()
    topics = []
    for item in parsed.get("topics") or []:
        if not isinstance(item, dict):
            continue
        label = _clean_label(item.get("label"))
        video_ids = [
            str(value) for value in item.get("video_ids") or []
            if str(value) in valid_ids and str(value) not in used
        ]
        if label and video_ids:
            used.update(video_ids)
            topics.append({"label": label, "video_ids": video_ids})
    for video_id in sorted(valid_ids - used):
        topics.append({"label": labels[video_id], "video_ids": [video_id]})
    input_tokens, output_tokens = _usage(response)
    return topics, input_tokens, output_tokens


def _aggregate_topics(
    merged: list[dict[str, Any]], rows: list[dict[str, Any]], channel_titles: dict[str, str],
) -> list[dict[str, Any]]:
    by_video = {str(row.get("video_id") or ""): row for row in rows}
    topics = []
    for item in merged:
        members = [by_video[video_id] for video_id in item["video_ids"] if video_id in by_video]
        channel_ids = {str(row.get("youtube_channel_id") or "") for row in members}
        channel_ids.discard("")
        if len(channel_ids) < 2:
            continue
        examples = []
        for row in sorted(members, key=lambda value: -int(value.get("views") or 0))[:2]:
            channel_id = str(row.get("youtube_channel_id") or "")
            examples.append({
                "title": str(row.get("title") or ""),
                "channel": channel_titles.get(channel_id, channel_id),
                "views": int(row.get("views") or 0),
                "url": f"https://youtube.com/shorts/{row.get('video_id')}",
            })
        label = item["label"]
        topics.append({
            "topic_key": _key(label), "label": label,
            "channel_count": len(channel_ids),
            "total_views": sum(int(row.get("views") or 0) for row in members),
            "examples": examples,
        })
    topics.sort(key=lambda item: (-item["channel_count"], -item["total_views"], item["label"].casefold()))
    return topics


def _brand_content(conn: Any, owner_user_id: str, brand_id: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
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
        rows = conn.execute(
            """
            SELECT v.id, v.title, COALESCE(t.full_text, t.full_text_en, '')
            FROM youtube_videos v
            LEFT JOIN youtube_transcripts t ON t.video_id = v.video_id
            WHERE CAST(v.brand_id AS VARCHAR) = CAST(? AS VARCHAR)
              AND CAST(v.owner_user_id AS VARCHAR) = CAST(? AS VARCHAR)
              AND COALESCE(v.duration_seconds, v.media_duration_seconds, 0) > 180
              AND v.deleted_at IS NULL
            ORDER BY COALESCE(v.published_at, v.created_at) DESC NULLS LAST
            LIMIT 30
            """,
            [brand_id, owner_user_id],
        ).fetchall()
        archive = [
            {"id": int(row[0]), "title": str(row[1] or ""), "transcript": str(row[2] or "")[:1200]}
            for row in rows if str(row[1] or "").strip()
        ]
    return shorts, archive


def _truncate_evidence(value: Any) -> str:
    words = " ".join(str(value or "").split()).split()
    return " ".join(words[:20])


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
                "For every topic, decide whether the creator covered it in a published Short. If not, decide "
                "whether a long archive video substantially discusses it. Use semantic yes/no matching, not loose "
                "word overlap. Short wins over archive. Return JSON only: {\"matches\":[{\"topic_key\":\"...\","
                "\"short_match\":true|false,\"archive_video_id\":null|integer,\"evidence\":\"...\"}]}. "
                "Evidence must quote or closely excerpt a matched title/transcript and be no more than 20 words."
            )},
            {"role": "user", "content": json.dumps({
                "topics": [{"topic_key": item["topic_key"], "label": item["label"]} for item in topics],
                "published_shorts": shorts,
                "archive_videos": archive,
            }, ensure_ascii=False)},
        ],
    )
    parsed = _json_response(response)
    valid_topics = {item["topic_key"] for item in topics}
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
        if bool(item.get("short_match")):
            status, archive_id = STATUS_COVERED, None
        elif archive_id in archive_by_id:
            status = STATUS_ARCHIVE
        else:
            status, archive_id = STATUS_IDEA, None
        by_key[topic_key] = {
            "topic_key": topic_key, "status": status,
            "evidence": (
                _truncate_evidence(item.get("evidence"))
                if status != STATUS_IDEA else ""
            ),
            "archive_video_id": archive_id,
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
    topics = _aggregate_topics(merged, eligible, channel_titles)
    brand_matches: dict[str, list[dict[str, Any]]] = {}
    match_usage: dict[str, tuple[int, int]] = {}
    conn = get_db_readonly()
    try:
        for brand_id, owner_user_id, brand_name in brand_rows:
            shorts, archive = _brand_content(conn, str(owner_user_id), str(brand_id))
            matches, used_in, used_out = _match_brand(topics, shorts, archive, selected_client)
            total_input += used_in
            total_output += used_out
            for match in matches:
                match["brand_name"] = str(brand_name or "")
            brand_matches[str(brand_id)] = matches
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
                (niche_id,as_of,topic_key,label,channel_count,total_views,examples_json,model,
                 input_tokens,output_tokens,estimated_cost_usd,computed_at)
                VALUES (?,?,?,?,?,?,?::jsonb,?,?,?,?,CURRENT_TIMESTAMP)
                """,
                [definition["id"], as_of, topic["topic_key"], topic["label"],
                 topic["channel_count"], topic["total_views"], json.dumps(topic["examples"], ensure_ascii=False),
                 MODEL, topic_input, topic_output, _cost(topic_input, topic_output)],
            )
        for brand_id, matches in brand_matches.items():
            used_in, used_out = match_usage[brand_id]
            for match in matches:
                archive_id = match.get("archive_video_id")
                archive_url = (
                    f"/video_shorts/my-videos?q={quote_plus(match.get('archive_title') or match['evidence'])}"
                    if archive_id else None
                )
                conn.execute(
                    f"""
                    INSERT INTO {MATCHES_TABLE}
                    (niche_id,as_of,topic_key,brand_id,status,evidence,archive_video_id,archive_url,
                     model,input_tokens,output_tokens,estimated_cost_usd,computed_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
                    """,
                    [definition["id"], as_of, match["topic_key"], brand_id, match["status"],
                     match["evidence"], archive_id, archive_url, MODEL, used_in, used_out,
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
    match_columns = "NULL AS status, NULL AS evidence, NULL AS archive_url, NULL AS brand_id"
    params: list[Any] = [niche_id, as_of]
    if brand_id and table_columns(conn, MATCHES_TABLE):
        match_join = (
            f"LEFT JOIN {MATCHES_TABLE} m ON m.niche_id=t.niche_id AND m.as_of=t.as_of "
            "AND m.topic_key=t.topic_key AND CAST(m.brand_id AS VARCHAR)=CAST(? AS VARCHAR)"
        )
        match_columns = "m.status, m.evidence, m.archive_url, m.brand_id"
        params = [brand_id, niche_id, as_of]
    result = conn.execute(
        f"""
        SELECT t.topic_key,t.label,t.channel_count,t.total_views,t.examples_json,
               {match_columns}
        FROM {TOPICS_TABLE} t {match_join}
        WHERE t.niche_id=? AND t.as_of=?
        ORDER BY t.channel_count DESC,t.total_views DESC,lower(t.label)
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
    return as_of, rows


def load_admin_rising_topics(conn: Any, niche_id: Any) -> tuple[str, list[dict[str, Any]]]:
    as_of, topics = load_rising_topics(conn, niche_id)
    if not as_of or not table_columns(conn, MATCHES_TABLE):
        return as_of, topics
    matches = conn.execute(
        f"""
        SELECT m.topic_key,m.brand_id,b.name,m.status,m.evidence,m.archive_url
        FROM {MATCHES_TABLE} m
        LEFT JOIN shorts_brands b ON CAST(b.id AS VARCHAR)=CAST(m.brand_id AS VARCHAR)
        WHERE m.niche_id=? AND m.as_of=?
        ORDER BY lower(COALESCE(b.name,''))
        """,
        [niche_id, as_of],
    ).fetchall()
    by_topic: dict[str, list[dict[str, Any]]] = {}
    for topic_key, brand_id, brand_name, status, evidence, archive_url in matches:
        by_topic.setdefault(str(topic_key), []).append({
            "brand_id": str(brand_id), "brand_name": str(brand_name or brand_id),
            "status": str(status), "status_label": STATUS_LABELS.get(str(status), str(status)),
            "evidence": str(evidence or ""), "archive_url": str(archive_url or ""),
        })
    for topic in topics:
        topic["matches"] = by_topic.get(str(topic["topic_key"]), [])
    return as_of, topics
