import uuid
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from app.video_shorts.services.brands import brand_scoped_user_id
from app.video_shorts.services.db import get_db, get_db_readonly, ensure_tiktok_queue_schema
from app.video_shorts.services.generated_video_lifecycle import upsert_generated_video_record


def _utc_now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


STALE_UPLOAD_SECONDS = 15 * 60


def _queue_timestamp_expr(column_name: str, backend_name: str) -> str:
    if backend_name == "postgres":
        return f"""
            CASE
                WHEN {column_name} IS NULL OR btrim({column_name}) = '' THEN NULL
                WHEN {column_name} ~ '(Z|[+-][0-9]{{2}}:[0-9]{{2}})$'
                    THEN replace({column_name}, 'Z', '+00:00')::timestamptz
                ELSE (replace({column_name}, 'T', ' ')::timestamp AT TIME ZONE 'UTC')
            END
        """.strip()
    return f"""
        COALESCE(
            try_strptime({column_name}, '%Y-%m-%dT%H:%M:%S'),
            try_strptime({column_name}, '%Y-%m-%dT%H:%M')
        )
    """.strip()


def enqueue_tiktok_clip(
    *,
    user_id: Optional[str],
    video_id: Optional[str],
    plan_index: Optional[str],
    clip_filename: str,
    caption_text: str,
    publish_at_iso: Optional[str],
    tiktok_open_id: Optional[str],
    tiktok_username: Optional[str],
    plan_title: Optional[str],
    force_requeue: bool = False,
) -> str:
    queue_id = str(uuid.uuid4())
    scoped_user_id = brand_scoped_user_id(user_id)
    conn = get_db()
    try:
        ensure_tiktok_queue_schema(conn)
        if not force_requeue:
            published = conn.execute(
                """
                SELECT id
                FROM shorts_tiktok_queue
                WHERE video_id = ?
                  AND plan_index = ?
                  AND status = 'published'
                ORDER BY created_at DESC
                LIMIT 1
                """,
                [video_id, plan_index or ""],
            ).fetchone()
            if published:
                return published[0]
        existing = conn.execute(
            """
            SELECT id
            FROM shorts_tiktok_queue
            WHERE video_id = ?
              AND plan_index = ?
              AND status IN ('pending', 'retry', 'uploading')
            ORDER BY created_at DESC
            LIMIT 1
            """,
            [video_id, plan_index or ""],
        ).fetchone()
        if existing:
            return existing[0]
        now = _utc_now_iso()
        conn.execute(
            """
            INSERT INTO shorts_tiktok_queue (
                id,
                user_id,
                video_id,
                plan_index,
                clip_filename,
                caption_text,
                publish_at,
                status,
                status_detail,
                created_at,
                updated_at,
                tiktok_open_id,
                tiktok_username,
                plan_title
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', NULL, ?, ?, ?, ?, ?)
            """,
            [
                queue_id,
                scoped_user_id,
                video_id,
                plan_index or "",
                clip_filename,
                (caption_text or "")[:2200],
                publish_at_iso,
                now,
                now,
                tiktok_open_id,
                tiktok_username,
                plan_title,
            ],
        )
        conn.commit()
    finally:
        conn.close()
    try:
        upsert_generated_video_record(
            source_video_id=str(video_id or "").strip(),
            source_channel_type="youtube",
            clip_filename=clip_filename,
            output_filename=clip_filename,
            storage_file_key=f"short:{clip_filename}",
            generation_status="created",
            publish_status="queued",
            planned_publish_at=publish_at_iso,
        )
    except Exception:
        pass
    return queue_id


def fetch_due_tiktok_jobs(limit: int = 5) -> List[Dict[str, Optional[str]]]:
    conn = get_db()
    try:
        ensure_tiktok_queue_schema(conn)
        backend_name = getattr(conn, "backend_name", "duckdb")
        publish_ts_expr = _queue_timestamp_expr("publish_at_clean", backend_name)
        created_ts_expr = _queue_timestamp_expr("created_at_clean", backend_name)
        if backend_name == "postgres":
            sort_ts_expr = f"COALESCE(EXTRACT(EPOCH FROM {publish_ts_expr}), EXTRACT(EPOCH FROM {created_ts_expr}))"
        else:
            sort_ts_expr = f"COALESCE(epoch({publish_ts_expr}), epoch({created_ts_expr}))"
        z_suffix_like = "'%%Z'" if backend_name == "postgres" else "'%Z'"
        stale_cutoff = (datetime.utcnow() - timedelta(seconds=STALE_UPLOAD_SECONDS)).isoformat()
        conn.execute(
            """
            UPDATE shorts_tiktok_queue
            SET status = 'retry',
                status_detail = 'Upload lock expired; requeued automatically.',
                updated_at = ?
            WHERE status = 'uploading' AND updated_at < ?
            """,
            [_utc_now_iso(), stale_cutoff],
        )
        rows = conn.execute(
            f"""
            WITH normalized AS (
                SELECT *,
                       CASE
                           WHEN publish_at IS NULL THEN NULL
                           WHEN CAST(publish_at AS VARCHAR) LIKE {z_suffix_like} THEN replace(CAST(publish_at AS VARCHAR), 'Z', '+00:00')
                           ELSE CAST(publish_at AS VARCHAR)
                       END AS publish_at_norm,
                       CASE
                           WHEN created_at IS NULL THEN NULL
                           WHEN CAST(created_at AS VARCHAR) LIKE {z_suffix_like} THEN replace(CAST(created_at AS VARCHAR), 'Z', '+00:00')
                           ELSE CAST(created_at AS VARCHAR)
                       END AS created_at_norm
                FROM shorts_tiktok_queue
            ),
            cleaned AS (
                SELECT *,
                       split_part(replace(publish_at_norm, 'Z', ''), '+', 1) AS publish_at_clean,
                       split_part(replace(created_at_norm, 'Z', ''), '+', 1) AS created_at_clean
                FROM normalized
            ),
            queue AS (
                SELECT *,
                       {publish_ts_expr} AS publish_ts,
                       {created_ts_expr} AS created_ts,
                       {sort_ts_expr} AS sort_ts
                FROM cleaned
            )
            SELECT
                id,
                user_id,
                video_id,
                plan_index,
                clip_filename,
                caption_text,
                publish_at,
                status,
                status_detail,
                created_at,
                updated_at,
                tiktok_open_id,
                tiktok_username,
                tiktok_video_id,
                tiktok_publish_id,
                published_at,
                plan_title,
                last_error_code,
                last_error_message,
                last_error_logid,
                last_error_payload,
                last_http_status,
                last_step
            FROM queue
            WHERE status IN ('pending','retry')
              AND (publish_ts IS NULL OR publish_ts <= NOW())
            ORDER BY sort_ts ASC
            LIMIT ?
            """,
            [limit],
        ).fetchall()
        cols = [d[0] for d in conn.description]
        return [dict(zip(cols, row)) for row in rows]
    finally:
        conn.close()


def update_tiktok_job_status(
    queue_id: str,
    *,
    status: str,
    status_detail: Optional[str] = None,
    tiktok_video_id: Optional[str] = None,
    tiktok_publish_id: Optional[str] = None,
    published_at_iso: Optional[str] = None,
    last_step: Optional[str] = None,
    last_http_status: Optional[int] = None,
    last_error_code: Optional[str] = None,
    last_error_message: Optional[str] = None,
    last_error_logid: Optional[str] = None,
    last_error_payload: Optional[str] = None,
) -> None:
    conn = get_db()
    lifecycle_row = None
    try:
        ensure_tiktok_queue_schema(conn)
        now = _utc_now_iso()
        conn.execute(
            """
            UPDATE shorts_tiktok_queue
            SET status = ?,
                status_detail = ?,
                tiktok_video_id = COALESCE(?, tiktok_video_id),
                tiktok_publish_id = COALESCE(?, tiktok_publish_id),
                published_at = COALESCE(?, published_at),
                last_step = COALESCE(?, last_step),
                last_http_status = COALESCE(?, last_http_status),
                last_error_code = COALESCE(?, last_error_code),
                last_error_message = COALESCE(?, last_error_message),
                last_error_logid = COALESCE(?, last_error_logid),
                last_error_payload = COALESCE(?, last_error_payload),
                updated_at = ?
            WHERE id = ?
            """,
            [
                status,
                status_detail,
                tiktok_video_id,
                tiktok_publish_id,
                published_at_iso,
                last_step,
                last_http_status,
                last_error_code,
                last_error_message,
                last_error_logid,
                last_error_payload,
                now,
                queue_id,
            ],
        )
        lifecycle_row = conn.execute(
            """
            SELECT video_id, clip_filename, publish_at, tiktok_video_id
            FROM shorts_tiktok_queue
            WHERE id = ?
            """,
            [queue_id],
        ).fetchone()
        conn.commit()
    finally:
        conn.close()
    try:
        if not lifecycle_row:
            return
        clip_filename = str(lifecycle_row[1] or "").strip()
        resolved_tiktok_video_id = tiktok_video_id or lifecycle_row[3]
        publish_status = (
            "published" if status == "published"
            else ("failed" if status == "failed" else "queued")
        )
        upsert_generated_video_record(
            source_video_id=str(lifecycle_row[0] or "").strip(),
            source_channel_type="youtube",
            clip_filename=clip_filename,
            output_filename=clip_filename,
            storage_file_key=f"short:{clip_filename}",
            generation_status="created",
            publish_status=publish_status,
            tiktok_video_id=resolved_tiktok_video_id,
            planned_publish_at=lifecycle_row[2],
            published_at=published_at_iso,
            tiktok_published_at=published_at_iso if publish_status == "published" else None,
            primary_publish_platform="tiktok" if publish_status == "published" else None,
        )
    except Exception:
        pass


def mark_tiktok_job_retry(queue_id: str, detail: str) -> None:
    conn = get_db()
    try:
        ensure_tiktok_queue_schema(conn)
        now = _utc_now_iso()
        conn.execute(
            """
            UPDATE shorts_tiktok_queue
            SET status = 'retry',
                status_detail = ?,
                updated_at = ?
            WHERE id = ?
            """,
            [detail, now, queue_id],
        )
        conn.commit()
    finally:
        conn.close()


def get_tiktok_queue_entry(queue_id: str) -> Optional[Dict[str, Optional[str]]]:
    conn = get_db_readonly()
    try:
        ensure_tiktok_queue_schema(conn)
        row = conn.execute(
            """
            SELECT
                id,
                user_id,
                tiktok_open_id,
                tiktok_username,
                tiktok_video_id,
                tiktok_publish_id,
                plan_title,
                publish_at,
                published_at,
                status,
                status_detail,
                last_error_code,
                last_error_message,
                last_error_logid,
                last_error_payload,
                last_http_status,
                last_step
            FROM shorts_tiktok_queue
            WHERE id = ?
            """,
            [queue_id],
        ).fetchone()
        if not row:
            return None
        cols = [d[0] for d in conn.description]
        return dict(zip(cols, row))
    finally:
        conn.close()


def load_tiktok_queue_map(video_ids: List[str]) -> Dict[Tuple[str, str], List[Dict[str, Optional[str]]]]:
    if not video_ids:
        return {}
    conn = get_db_readonly()
    try:
        ensure_tiktok_queue_schema(conn)
        placeholders = ", ".join("?" for _ in video_ids)
        try:
            rows = conn.execute(
                f"""
            SELECT
                id,
                user_id,
                brand_id,
                video_id,
                plan_index,
                clip_filename,
                status,
                status_detail,
                publish_at,
                tiktok_video_id,
                published_at,
                tiktok_username,
                plan_title,
                created_at,
                last_error_code,
                last_error_message,
                last_error_logid,
                last_error_payload,
                last_http_status,
                last_step
            FROM shorts_tiktok_queue
            WHERE video_id IN ({placeholders})
                ORDER BY COALESCE(publish_at, created_at)
                """,
                video_ids,
            ).fetchall()
        except Exception as exc:
            if "shorts_tiktok_queue" in str(exc).lower():
                return {}
            raise
        cols = [d[0] for d in conn.description]
        result: Dict[Tuple[str, str], List[Dict[str, Optional[str]]]] = {}
        for row in rows:
            data = dict(zip(cols, row))
            key = (data.get("video_id") or "", data.get("plan_index") or "")
            result.setdefault(key, []).append(data)
        return result
    finally:
        conn.close()
