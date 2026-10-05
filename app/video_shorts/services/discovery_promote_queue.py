from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from flask import current_app

from app.video_shorts.config import OPENAI_MODEL, _openai_client
from app.video_shorts.services.db import get_db, table_columns
from app.video_shorts.youtube_api import fetch_playlist_items_batch, get_channel_metadata


QUEUE_TABLE = "discovery_promote_requests"
ACTIVE_STATUSES = {"queued", "processing"}
TERMINAL_STATUSES = {"done", "failed"}
SYNTHETIC_SEED_PREFIX = "[Synthetic discovery seed - no transcript]"
LEAD_DISCOVERY_SEED_RECENT_TITLES = 5
NO_GREETING_NAME_MARKER = " "
_FIRST_NAME_RE = re.compile(r"[A-Za-z][A-Za-z'’]{1,39}")


def ensure_discovery_promote_queue_schema(conn) -> None:
    backend_name = getattr(conn, "backend_name", "")
    json_type = "JSONB" if getattr(conn, "backend_name", "") == "postgres" else "TEXT"
    id_type = "BIGSERIAL PRIMARY KEY" if backend_name == "postgres" else "INTEGER PRIMARY KEY AUTOINCREMENT"
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {QUEUE_TABLE} (
            id {id_type},
            discovery_lead_id BIGINT NOT NULL UNIQUE,
            status VARCHAR NOT NULL DEFAULT 'queued',
            selected_source_video_id VARCHAR,
            requested_by VARCHAR,
            requested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            started_at TIMESTAMP,
            finished_at TIMESTAMP,
            error TEXT,
            result_json {json_type}
        )
        """
    )
    columns = table_columns(conn, QUEUE_TABLE)
    additions = [
        ("selected_source_video_id", "VARCHAR"),
        ("requested_by", "VARCHAR"),
        ("requested_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
        ("started_at", "TIMESTAMP"),
        ("finished_at", "TIMESTAMP"),
        ("error", "TEXT"),
        ("result_json", json_type),
    ]
    for column, definition in additions:
        if column not in columns:
            conn.execute(f"ALTER TABLE {QUEUE_TABLE} ADD COLUMN {column} {definition}")
    try:
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{QUEUE_TABLE}_status_requested ON {QUEUE_TABLE}(status, requested_at, id)")
    except Exception:
        pass


def _json_value_sql(conn) -> str:
    return "CAST(? AS JSONB)" if getattr(conn, "backend_name", "") == "postgres" else "?"


def _fetch_recent_seed_titles(channel_id: str, *, limit: int = LEAD_DISCOVERY_SEED_RECENT_TITLES) -> List[str]:
    clean_channel_id = str(channel_id or "").strip()
    if not clean_channel_id:
        return []
    try:
        channel_meta = get_channel_metadata(f"https://www.youtube.com/channel/{clean_channel_id}")
        uploads_playlist_id = str(channel_meta.get("uploads_playlist_id") or "").strip()
        if not uploads_playlist_id:
            return []
        batch = fetch_playlist_items_batch(
            playlist_id=uploads_playlist_id,
            max_results=max(1, min(int(limit or LEAD_DISCOVERY_SEED_RECENT_TITLES), LEAD_DISCOVERY_SEED_RECENT_TITLES)),
        )
    except Exception:
        current_app.logger.exception("Could not fetch recent titles for auto seed channel=%s", clean_channel_id)
        return []
    titles: List[str] = []
    for video in batch.get("videos") or []:
        title = " ".join(str(video.get("title") or "").strip().split())
        if title and title not in titles:
            titles.append(title[:180])
    return titles[:LEAD_DISCOVERY_SEED_RECENT_TITLES]


def _summarize_auto_discovery_seed(lead: Dict[str, Any], recent_titles: List[str]) -> str:
    if not _openai_client:
        description = " ".join(str(lead.get("channel_description") or "").split())[:2600]
        return f"{SYNTHETIC_SEED_PREFIX} {description}".strip()
    prompt = (
        "Create a 3-5 sentence synthetic seed summary for this promoted YouTube discovery lead. "
        "There is no transcript, so infer only from the channel title, channel description, and recent video titles. "
        "Focus on creator persona, audience, teaching style, expertise, offer signals, and recurring topics. "
        "Return plain text only.\n\n"
        f"Channel: {lead.get('channel_title') or lead.get('youtube_channel_id')}\n"
        f"Channel description: {str(lead.get('channel_description') or '')[:1600]}\n"
        f"Recent titles: {json.dumps(recent_titles[:LEAD_DISCOVERY_SEED_RECENT_TITLES], ensure_ascii=False)}"
    )
    response = _openai_client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": "You summarize promoted creator-channel leads for seed-based ICP keyword discovery."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
    )
    summary = " ".join((response.choices[0].message.content or "").strip().split())[:2800]
    return f"{SYNTHETIC_SEED_PREFIX} {summary}".strip()


def _normalize_llm_first_name(value: Any) -> str:
    text = str(value or "").strip()
    if not text or text.lower() in {"null", "none", "n/a", "unknown"}:
        return ""
    try:
        payload = json.loads(text)
        if isinstance(payload, dict):
            text = str(payload.get("first_name") or payload.get("name") or "").strip()
        elif payload is None:
            return ""
        else:
            text = str(payload or "").strip()
    except Exception:
        text = text.strip().strip('"').strip("'")
    match = _FIRST_NAME_RE.fullmatch(text)
    return match.group(0) if match else ""


def infer_outreach_first_name_for_lead(lead: Dict[str, Any]) -> str:
    """Return a first name, or a blank marker when the LLM is not confident."""
    if not _openai_client:
        return ""
    channel_title = str(lead.get("channel_title") or "").strip()
    creator_name = str(lead.get("creator_name") or "").strip()
    channel_description = str(lead.get("channel_description") or "").strip()[:1800]
    creator_email = str(lead.get("creator_email") or "").strip()
    prompt = (
        "Extract a greeting first name for a cold outreach email.\n"
        "Return JSON only: {\"first_name\":\"Name\"} or {\"first_name\":null}.\n\n"
        "Use this confidence order:\n"
        "1. Return a name when the channel description explicitly self-identifies a person, "
        "for example \"I'm X\", \"I am X\", \"My name is X\", or \"My name's X\".\n"
        "2. Otherwise return a name when the channel title or creator name is clearly a real person's name.\n"
        "3. Use the email local-part only as a last resort. It must be a single obvious first name, "
        "not a generic/role mailbox such as info, contact, hello, team, admin, support, hi, mail, office, "
        "press, podcast, media, business, or enquiries. Do not trust dotted, initialed, or joined full-name "
        "local-parts such as john.smith, jsmith, johnsmith, drsmith, or a company/domain word.\n\n"
        "Rules:\n"
        "- Return ONLY the person's first name, without titles, degrees, channel words, brand words, or punctuation.\n"
        "- If it is a brand, company, podcast/show title, generic topic, or you are unsure, return null.\n"
        "- Never guess or invent. If no source clearly supports a person first name, return null.\n\n"
        "Examples:\n"
        "I'm Jennifer, a nurse and educator -> Jennifer\n"
        "My name's Adam and I'm here to help you garden better -> Adam\n"
        "michael@ocestateplanlawyer.com with no name in description -> Michael (email-only, lower confidence but allowed)\n"
        "info@example.com -> null\n"
        "john.smith@example.com -> null\n"
        "Etsy Consultant -> null\n"
        "Dan Haylett -> Dan\n"
        "The Retirement Cafe with Justin King -> Justin\n"
        "Dr Alex Howard -> Alex\n"
        "Midlife Anti-Crisis -> null\n"
        "Kevin Pond - Meditation -> Kevin\n\n"
        f"Channel title: {channel_title}\n"
        f"Creator name: {creator_name}\n"
        f"Channel description: {channel_description}\n"
        f"Creator email: {creator_email}"
    )
    try:
        response = _openai_client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You extract safe first names for cold email greetings. Be conservative: "
                        "return null rather than a channel, topic, brand, company, podcast, role word, "
                        "or uncertain email-derived guess."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0,
        )
    except Exception:
        current_app.logger.exception("Could not infer outreach first name for discovery lead id=%s", lead.get("id"))
        return ""
    first_name = _normalize_llm_first_name(response.choices[0].message.content if response.choices else "")
    return first_name or NO_GREETING_NAME_MARKER


def _auto_seed_promoted_discovery_lead(conn, lead: Dict[str, Any]) -> Dict[str, Any]:
    lead_id = int(lead.get("id") or 0)
    channel_id = str(lead.get("youtube_channel_id") or "").strip()
    creator_email = str(lead.get("creator_email") or "").strip()
    status = str(lead.get("status") or "email_enriched").strip()
    if not creator_email:
        return {"promoted": False, "reason": "missing_email"}
    if status != "email_enriched":
        return {"promoted": False, "reason": "status_not_email_enriched"}
    if not channel_id:
        return {"promoted": False, "reason": "missing_channel_id"}
    columns = table_columns(conn, "discovery_leads")
    if any(column not in columns for column in ("is_seed", "promoted_at", "seed_notes")):
        return {"promoted": False, "reason": "schema_missing"}

    existing_seed = conn.execute(
        """
        SELECT source_video_id, transcript_summary
        FROM seed_channel_profiles
        WHERE channel_id = ?
        LIMIT 1
        """,
        [channel_id],
    ).fetchone()
    existing_summary = str((existing_seed or [None, ""])[1] or "").strip()
    summary_created = False
    if not existing_summary or existing_summary.startswith(SYNTHETIC_SEED_PREFIX):
        recent_titles = _fetch_recent_seed_titles(channel_id)
        summary = _summarize_auto_discovery_seed(lead, recent_titles)
        summary_created = True
        if existing_seed:
            conn.execute(
                """
                UPDATE seed_channel_profiles
                SET source_video_id = NULL,
                    transcript_summary = ?
                WHERE channel_id = ?
                """,
                [summary, channel_id],
            )
        else:
            conn.execute(
                """
                INSERT INTO seed_channel_profiles (
                    channel_id, source_video_id, transcript_summary, created_at
                )
                VALUES (?, NULL, ?, CURRENT_TIMESTAMP)
                """,
                [channel_id, summary],
            )
    conn.execute(
        """
        UPDATE discovery_leads
        SET is_seed = true,
            promoted_at = COALESCE(promoted_at, CURRENT_TIMESTAMP),
            seed_notes = COALESCE(seed_notes, ?)
        WHERE id = ?
        """,
        ["auto seed from promoted discovery lead", lead_id],
    )
    return {"promoted": True, "summary_created": summary_created}


def enqueue_discovery_promote_request(
    conn,
    *,
    discovery_lead_id: int,
    selected_source_video_id: str = "",
    requested_by: str = "",
) -> Dict[str, Any]:
    ensure_discovery_promote_queue_schema(conn)
    lead_id = int(discovery_lead_id)
    selected_video_id = str(selected_source_video_id or "").strip()
    requester = str(requested_by or "").strip()

    row = conn.execute(
        f"""
        UPDATE {QUEUE_TABLE}
        SET status = 'queued',
            selected_source_video_id = ?,
            requested_by = ?,
            requested_at = CURRENT_TIMESTAMP,
            started_at = NULL,
            finished_at = NULL,
            error = NULL,
            result_json = NULL
        WHERE discovery_lead_id = ?
          AND status = 'failed'
        RETURNING id, status
        """,
        [selected_video_id or None, requester, lead_id],
    ).fetchone()
    if row:
        return {
            "id": row[0],
            "discovery_lead_id": lead_id,
            "status": str((row[1] if row else "queued") or "queued"),
            "kind": "requeued",
        }

    if selected_video_id:
        row = conn.execute(
            f"""
            UPDATE {QUEUE_TABLE}
            SET selected_source_video_id = ?,
                requested_by = ?,
                requested_at = CURRENT_TIMESTAMP
            WHERE discovery_lead_id = ?
              AND status = 'queued'
              AND started_at IS NULL
            RETURNING id, status
            """,
            [selected_video_id, requester, lead_id],
        ).fetchone()
        if row:
            return {
                "id": row[0],
                "discovery_lead_id": lead_id,
                "status": str((row[1] if row else "queued") or "queued"),
                "kind": "updated",
            }

    existing = conn.execute(
        f"""
        SELECT id, status, error
        FROM {QUEUE_TABLE}
        WHERE discovery_lead_id = ?
        LIMIT 1
        """,
        [lead_id],
    ).fetchone()
    if existing:
        status = str(existing[1] or "").strip().lower()
        return {
            "id": existing[0],
            "discovery_lead_id": lead_id,
            "status": status,
            "error": str(existing[2] or ""),
            "kind": "existing",
        }

    row = conn.execute(
        f"""
        INSERT INTO {QUEUE_TABLE} (
            discovery_lead_id,
            status,
            selected_source_video_id,
            requested_by,
            requested_at
        )
        VALUES (?, 'queued', ?, ?, CURRENT_TIMESTAMP)
        RETURNING id, status
        """,
        [lead_id, selected_video_id or None, requester],
    ).fetchone()
    return {
        "id": row[0] if row else None,
        "discovery_lead_id": lead_id,
        "status": str((row[1] if row else "queued") or "queued"),
        "kind": "queued",
    }


def promote_queue_count(conn) -> int:
    if not table_columns(conn, QUEUE_TABLE):
        return 0
    row = conn.execute(
        f"SELECT COUNT(*) FROM {QUEUE_TABLE} WHERE status = 'queued'"
    ).fetchone()
    return int((row[0] if row else 0) or 0)


def load_promote_statuses(conn, lead_ids: List[int]) -> Dict[int, Dict[str, Any]]:
    if not table_columns(conn, QUEUE_TABLE):
        return {}
    clean_ids = []
    for lead_id in lead_ids:
        try:
            value = int(lead_id)
        except Exception:
            continue
        if value > 0:
            clean_ids.append(value)
    if not clean_ids:
        return {}
    placeholders = ", ".join("?" for _ in clean_ids)
    rows = conn.execute(
        f"""
        WITH queued AS (
            SELECT
                id,
                discovery_lead_id,
                row_number() OVER (ORDER BY requested_at ASC, id ASC) AS queue_position
            FROM {QUEUE_TABLE}
            WHERE status = 'queued'
        )
        SELECT
            r.discovery_lead_id,
            r.status,
            r.error,
            r.requested_at,
            r.started_at,
            r.finished_at,
            q.queue_position
        FROM {QUEUE_TABLE} r
        LEFT JOIN queued q ON q.id = r.id
        WHERE r.discovery_lead_id IN ({placeholders})
          AND NOT (r.status = 'failed' AND COALESCE(r.error, '') = '')
        """,
        clean_ids,
    ).fetchall()
    statuses: Dict[int, Dict[str, Any]] = {}
    for row in rows:
        lead_id = int(row[0])
        statuses[lead_id] = {
            "lead_id": lead_id,
            "status": str(row[1] or ""),
            "error": str(row[2] or ""),
            "requested_at": row[3],
            "started_at": row[4],
            "finished_at": row[5],
            "queue_position": int(row[6]) if row[6] is not None else None,
            "label": _status_label(str(row[1] or ""), row[6], str(row[2] or "")),
        }
    return statuses


def load_promote_status_payload(conn, lead_ids: List[int]) -> Dict[str, Any]:
    statuses = load_promote_statuses(conn, lead_ids)
    return {
        "success": True,
        "queue_count": promote_queue_count(conn),
        "statuses": {str(key): value for key, value in statuses.items()},
    }


def _status_label(status: str, queue_position: Any, error: str) -> str:
    normalized = str(status or "").strip().lower()
    if normalized == "queued":
        return f"Sırada #{int(queue_position)}" if queue_position else "Sırada"
    if normalized == "processing":
        return "İşleniyor"
    if normalized == "done":
        return "Hazır"
    if normalized == "failed":
        return f"Hata: {error}" if error else "Hata"
    return ""


def customer_job_waiting(conn) -> bool:
    origin_sql = (
        "COALESCE(payload_json->>'job_origin', '')"
        if getattr(conn, "backend_name", "") == "postgres"
        else "COALESCE(json_extract_string(payload_json, '$.job_origin'), '')"
    )
    row = conn.execute(
        f"""
        SELECT 1
        FROM shorts_render_jobs
        WHERE status IN ('queued', 'processing')
          AND {origin_sql} <> 'discovery_demo'
        LIMIT 1
        """
    ).fetchone()
    return bool(row)


def claim_next_promote_request(conn) -> Optional[Dict[str, Any]]:
    ensure_discovery_promote_queue_schema(conn)
    if getattr(conn, "backend_name", "") == "postgres":
        row = conn.execute(
            f"""
            UPDATE {QUEUE_TABLE}
               SET status = 'processing',
                   started_at = COALESCE(started_at, CURRENT_TIMESTAMP),
                   error = NULL
             WHERE id = (
                 SELECT id
                 FROM {QUEUE_TABLE}
                 WHERE status = 'queued'
                 ORDER BY requested_at ASC, id ASC
                 FOR UPDATE SKIP LOCKED
                 LIMIT 1
             )
             RETURNING id, discovery_lead_id, selected_source_video_id
            """
        ).fetchone()
    else:
        row = conn.execute(
            f"""
            SELECT id, discovery_lead_id, selected_source_video_id
            FROM {QUEUE_TABLE}
            WHERE status = 'queued'
            ORDER BY requested_at ASC, id ASC
            LIMIT 1
            """
        ).fetchone()
        if row:
            conn.execute(
                f"""
                UPDATE {QUEUE_TABLE}
                SET status = 'processing',
                    started_at = COALESCE(started_at, CURRENT_TIMESTAMP),
                    error = NULL
                WHERE id = ?
                """,
                [row[0]],
            )
    if not row:
        return None
    return {"id": row[0], "discovery_lead_id": int(row[1]), "selected_source_video_id": str(row[2] or "").strip()}


def process_next_discovery_promote_request() -> bool:
    conn = get_db()
    try:
        ensure_discovery_promote_queue_schema(conn)
        if customer_job_waiting(conn):
            conn.commit()
            return False
        request = claim_next_promote_request(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    if not request:
        return False

    try:
        result = _execute_promote_request(
            int(request["discovery_lead_id"]),
            selected_source_video_id=str(request.get("selected_source_video_id") or ""),
        )
    except Exception as exc:
        message = _short_error(exc)
        conn_fail = get_db()
        try:
            ensure_discovery_promote_queue_schema(conn_fail)
            conn_fail.execute(
                f"""
                UPDATE {QUEUE_TABLE}
                SET status = 'failed',
                    error = ?,
                    finished_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                [message, request["id"]],
            )
            try:
                conn_fail.execute("UPDATE discovery_leads SET promotion_error = ? WHERE id = ?", [message, request["discovery_lead_id"]])
            except Exception:
                pass
            conn_fail.commit()
        except Exception:
            conn_fail.rollback()
            raise
        finally:
            conn_fail.close()
        return True

    conn_done = get_db()
    try:
        ensure_discovery_promote_queue_schema(conn_done)
        conn_done.execute(
            f"""
            UPDATE {QUEUE_TABLE}
            SET status = 'done',
                error = NULL,
                result_json = {_json_value_sql(conn_done)},
                finished_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            [__import__("json").dumps(result, ensure_ascii=False, sort_keys=True), request["id"]],
        )
        conn_done.commit()
    except Exception:
        conn_done.rollback()
        raise
    finally:
        conn_done.close()
    return True


def _short_error(exc: Exception) -> str:
    return " ".join(str(exc or "Promotion failed.").strip().split())[:500] or "Promotion failed."


def _execute_promote_request(lead_id: int, *, selected_source_video_id: str = "") -> Dict[str, Any]:
    from app.video_shorts.routes.api import (
        DISCOVERY_PROMOTION_BRAND_ID,
        DISCOVERY_PROMOTION_OWNER_USER_ID,
        create_autopilot_lead_from_video,
        fetch_video_metadata,
        select_source_video_candidates_for_channel,
        select_source_video_for_channel,
    )

    conn = get_db()
    try:
        lead_columns = table_columns(conn, "discovery_leads")
        outreach_first_name_sql = "outreach_first_name" if "outreach_first_name" in lead_columns else "NULL"
        row = conn.execute(
            f"""
            SELECT id, youtube_channel_id, channel_title, channel_description,
                   subscriber_count, creator_name, creator_email, icp_fit,
                   autopilot_lead_id, {outreach_first_name_sql} AS outreach_first_name
            FROM discovery_leads
            WHERE id = ?
            LIMIT 1
            """,
            [int(lead_id)],
        ).fetchone()
        if not row:
            raise RuntimeError("not_found")
        lead = {
            "id": int(row[0]),
            "youtube_channel_id": str(row[1] or "").strip(),
            "channel_title": str(row[2] or "").strip(),
            "channel_description": str(row[3] or "").strip(),
            "subscriber_count": row[4],
            "creator_name": str(row[5] or "").strip(),
            "creator_email": str(row[6] or "").strip(),
            "icp_fit": bool(row[7]) if row[7] is not None else None,
            "autopilot_lead_id": str(row[8] or "").strip(),
            "outreach_first_name": row[9],
        }
        if not lead["creator_email"]:
            raise RuntimeError("missing_email")
        if lead["icp_fit"] is not True:
            raise RuntimeError("icp_not_fit")
        if not lead["youtube_channel_id"]:
            raise RuntimeError("missing_channel_id")

        existing = conn.execute(
            """
            SELECT CAST(id AS VARCHAR), first_video_id
            FROM autopilot_leads
            WHERE youtube_channel_id = ?
            LIMIT 1
            """,
            [lead["youtube_channel_id"]],
        ).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE discovery_leads
                SET autopilot_lead_id = ?,
                    promoted_to_autopilot_at = COALESCE(promoted_to_autopilot_at, CURRENT_TIMESTAMP),
                    promoted_source_video_id = COALESCE(promoted_source_video_id, ?),
                    promotion_error = NULL
                WHERE id = ?
                """,
                [str(existing[0]), existing[1], lead_id],
            )
            try:
                seed_result = _auto_seed_promoted_discovery_lead(conn, {**lead, "status": "email_enriched"})
                current_app.logger.info("Auto-seeded linked discovery lead id=%s result=%s", lead_id, seed_result)
            except Exception:
                current_app.logger.exception("Auto seed failed for linked discovery lead id=%s", lead_id)
            conn.commit()
            return {"id": lead_id, "autopilot_lead_id": str(existing[0]), "reason": "already_a_lead_linked"}

        source = None
        if selected_source_video_id:
            source_options = select_source_video_candidates_for_channel(lead["youtube_channel_id"])
            source = next(
                (
                    candidate
                    for candidate in source_options.get("candidates") or []
                    if str(candidate.get("video_id") or "").strip() == selected_source_video_id
                ),
                None,
            )
            if not source:
                raise RuntimeError("selected_source_video_not_recent")
        else:
            source = select_source_video_for_channel(lead["youtube_channel_id"])
        if not source:
            raise RuntimeError("no_suitable_source_video")

        meta = fetch_video_metadata(str(source["video_id"]))
        if lead.get("channel_description"):
            meta["channel_description"] = lead["channel_description"]
        if lead.get("outreach_first_name") is None:
            inferred_recipient_name = infer_outreach_first_name_for_lead(lead)
            if "outreach_first_name" in lead_columns:
                conn.execute(
                    """
                    UPDATE discovery_leads
                    SET outreach_first_name = ?
                    WHERE id = ?
                      AND outreach_first_name IS NULL
                    """,
                    [inferred_recipient_name, lead_id],
                )
        else:
            inferred_recipient_name = str(lead.get("outreach_first_name") or "")
        autopilot = create_autopilot_lead_from_video(
            conn,
            meta=meta,
            video_id=str(source["video_id"]),
            canonical_url=str(source["canonical_url"]),
            creator_name=lead["creator_name"] or lead["channel_title"],
            creator_email=lead["creator_email"],
            recipient_name=inferred_recipient_name,
            subscriber_count=lead["subscriber_count"],
            discovery_owner_user_id=DISCOVERY_PROMOTION_OWNER_USER_ID,
            discovery_brand_id=DISCOVERY_PROMOTION_BRAND_ID,
        )
        conn.execute(
            """
            UPDATE discovery_leads
            SET autopilot_lead_id = ?,
                promoted_to_autopilot_at = CURRENT_TIMESTAMP,
                promoted_source_video_id = ?,
                promotion_error = NULL,
                sweetspot_score = ?,
                best_source_video_id = ?,
                best_source_video_minutes = ?
            WHERE id = ?
            """,
            [
                autopilot["lead_id"],
                autopilot["video_pk"],
                source.get("score"),
                source.get("video_id"),
                source.get("minutes"),
                lead_id,
            ],
        )
        try:
            seed_result = _auto_seed_promoted_discovery_lead(conn, {**lead, "status": "email_enriched"})
            current_app.logger.info("Auto-seeded promoted discovery lead id=%s result=%s", lead_id, seed_result)
        except Exception:
            current_app.logger.exception("Auto seed failed for promoted discovery lead id=%s", lead_id)
        conn.commit()
        return {
            "id": lead_id,
            "autopilot_lead_id": autopilot["lead_id"],
            "source_video_id": source.get("video_id"),
            "source_video_title": source.get("title"),
            "source_video_pk": autopilot["video_pk"],
            "source_video_minutes": source.get("minutes"),
            "sweetspot_score": source.get("score"),
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
