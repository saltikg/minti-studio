from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.video_shorts.services.db import get_db, table_columns


QUEUE_TABLE = "discovery_promote_requests"
ACTIVE_STATUSES = {"queued", "processing"}
TERMINAL_STATUSES = {"done", "failed"}


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


def enqueue_discovery_promote_request(
    conn,
    *,
    discovery_lead_id: int,
    selected_source_video_id: str = "",
    requested_by: str = "",
) -> Dict[str, Any]:
    ensure_discovery_promote_queue_schema(conn)
    existing = conn.execute(
        f"""
        SELECT id, status, error
        FROM {QUEUE_TABLE}
        WHERE discovery_lead_id = ?
        LIMIT 1
        """,
        [int(discovery_lead_id)],
    ).fetchone()
    if existing:
        status = str(existing[1] or "").strip().lower()
        return {
            "id": existing[0],
            "discovery_lead_id": int(discovery_lead_id),
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
        [int(discovery_lead_id), str(selected_source_video_id or "").strip(), str(requested_by or "").strip()],
    ).fetchone()
    return {
        "id": row[0] if row else None,
        "discovery_lead_id": int(discovery_lead_id),
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
        row = conn.execute(
            """
            SELECT id, youtube_channel_id, channel_title, channel_description,
                   subscriber_count, creator_name, creator_email, icp_fit,
                   autopilot_lead_id
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
        autopilot = create_autopilot_lead_from_video(
            conn,
            meta=meta,
            video_id=str(source["video_id"]),
            canonical_url=str(source["canonical_url"]),
            creator_name=lead["creator_name"] or lead["channel_title"],
            creator_email=lead["creator_email"],
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
