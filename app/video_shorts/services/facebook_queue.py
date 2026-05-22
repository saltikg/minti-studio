import uuid
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from app.video_shorts.services.db import (
    ensure_facebook_queue_schema,
    get_db,
    get_db_readonly,
    table_columns,
)
from app.video_shorts.services.brands import brand_scoped_user_id
from app.video_shorts.services.generated_video_lifecycle import upsert_generated_video_record

STALE_UPLOAD_SECONDS = 60 * 30


def _utc_now_iso() -> str:
    return datetime.utcnow().isoformat()


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


def _connect(read_only: bool = False, retries: int = 6):
    last_exc: Optional[Exception] = None
    for attempt in range(retries):
        try:
            return get_db_readonly() if read_only else get_db()
        except Exception as exc:
            last_exc = exc
            message = str(exc)
            if "lock" in message.lower() and attempt + 1 < retries:
                import time

                time.sleep(0.2 * (attempt + 1))
                continue
            raise
    raise last_exc  # pragma: no cover


def enqueue_facebook_clip(
    *,
    user_id: Optional[str],
    video_id: Optional[str],
    plan_index: Optional[str],
    clip_filename: str,
    caption_text: str,
    publish_at_iso: Optional[str],
    page_id: Optional[str],
    page_name: Optional[str],
    plan_title: Optional[str],
    media_type: str,
) -> str:
    conn = _connect(read_only=False)
    scoped_user_id = brand_scoped_user_id(user_id)
    try:
        ensure_facebook_queue_schema(conn)
        now = _utc_now_iso()
        existing = conn.execute(
            """
            SELECT id FROM shorts_facebook_queue
            WHERE video_id = ? AND plan_index = ? AND media_type = ?
            LIMIT 1
            """,
            [video_id, plan_index or "", media_type],
        ).fetchone()
        if existing:
            queue_id = existing[0]
            conn.execute(
                """
                UPDATE shorts_facebook_queue
                SET caption_text = ?,
                    publish_at = ?,
                    status = 'pending',
                    status_detail = NULL,
                    updated_at = ?,
                    page_id = ?,
                    page_name = ?,
                    plan_title = ?
                WHERE id = ?
                """,
                [
                    caption_text[:2200],
                    publish_at_iso,
                    now,
                    page_id,
                    page_name,
                    plan_title,
                    queue_id,
                ],
            )
            conn.commit()
            return queue_id
        queue_id = str(uuid.uuid4())
        conn.execute(
            """
            INSERT INTO shorts_facebook_queue (
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
                page_id,
                page_name,
                plan_title,
                media_type
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                queue_id,
                scoped_user_id,
                video_id,
                plan_index or "",
                clip_filename,
                caption_text[:2200],
                publish_at_iso,
                "pending",
                None,
                now,
                now,
                page_id,
                page_name,
                plan_title,
                media_type,
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


def fetch_due_facebook_jobs(limit: int = 5) -> List[Dict[str, Optional[str]]]:
    conn = _connect(read_only=False)
    try:
        ensure_facebook_queue_schema(conn)
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
            UPDATE shorts_facebook_queue
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
                FROM shorts_facebook_queue
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
                page_id,
                page_name,
                plan_title,
                media_type
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


def update_facebook_job_status(
    queue_id: str,
    *,
    status: str,
    status_detail: Optional[str] = None,
    facebook_video_id: Optional[str] = None,
    published_at_iso: Optional[str] = None,
    permalink: Optional[str] = None,
    view_count: Optional[int] = None,
    reach: Optional[int] = None,
    impressions: Optional[int] = None,
    reactions: Optional[int] = None,
    comment_count: Optional[int] = None,
) -> None:
    conn = _connect(read_only=False)
    lifecycle_row = None
    try:
        ensure_facebook_queue_schema(conn)
        conn.execute(
            """
            UPDATE shorts_facebook_queue
            SET status = ?,
                status_detail = ?,
                facebook_video_id = COALESCE(?, facebook_video_id),
                published_at = COALESCE(?, published_at),
                permalink = COALESCE(?, permalink),
                view_count = COALESCE(?, view_count),
                reach = COALESCE(?, reach),
                impressions = COALESCE(?, impressions),
                reactions = COALESCE(?, reactions),
                comment_count = COALESCE(?, comment_count),
                updated_at = ?
            WHERE id = ?
            """,
            [
                status,
                status_detail,
                facebook_video_id,
                published_at_iso,
                permalink,
                view_count,
                reach,
                impressions,
                reactions,
                comment_count,
                _utc_now_iso(),
                queue_id,
            ],
        )
        lifecycle_row = conn.execute(
            """
            SELECT video_id, clip_filename, publish_at, facebook_video_id
            FROM shorts_facebook_queue
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
        resolved_facebook_video_id = facebook_video_id or lifecycle_row[3]
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
            facebook_video_id=resolved_facebook_video_id,
            planned_publish_at=lifecycle_row[2],
            published_at=published_at_iso,
            facebook_published_at=published_at_iso if publish_status == "published" else None,
            primary_publish_platform="facebook" if publish_status == "published" else None,
        )
    except Exception:
        pass


def update_facebook_queue_metrics(
    queue_id: str,
    *,
    facebook_video_id: Optional[str] = None,
    permalink: Optional[str] = None,
    view_count: Optional[int] = None,
    reach: Optional[int] = None,
    impressions: Optional[int] = None,
    reactions: Optional[int] = None,
    comment_count: Optional[int] = None,
) -> None:
    conn = _connect(read_only=False)
    try:
        ensure_facebook_queue_schema(conn)
        conn.execute(
            """
            UPDATE shorts_facebook_queue
            SET facebook_video_id = COALESCE(?, facebook_video_id),
                permalink = COALESCE(?, permalink),
                view_count = COALESCE(?, view_count),
                reach = COALESCE(?, reach),
                impressions = COALESCE(?, impressions),
                reactions = COALESCE(?, reactions),
                comment_count = COALESCE(?, comment_count),
                updated_at = ?
            WHERE id = ?
            """,
            [
                facebook_video_id,
                permalink,
                view_count,
                reach,
                impressions,
                reactions,
                comment_count,
                _utc_now_iso(),
                queue_id,
            ],
        )
        conn.commit()
    finally:
        conn.close()


def fetch_facebook_media_jobs(limit: Optional[int] = None) -> List[Dict[str, Optional[str]]]:
    try:
        conn = _connect(read_only=True, retries=3)
    except Exception as exc:
        if "lock" in str(exc).lower():
            return []
        raise
    try:
        ensure_facebook_queue_schema(conn)
        query = """
            SELECT id, user_id, facebook_video_id, page_id, page_name, plan_title, media_type
            FROM shorts_facebook_queue
            WHERE facebook_video_id IS NOT NULL
            ORDER BY updated_at DESC
        """
        try:
            if limit:
                rows = conn.execute(query + " LIMIT ?", [limit]).fetchall()
            else:
                rows = conn.execute(query).fetchall()
        except Exception as exc:
            if "shorts_facebook_queue" in str(exc).lower():
                return []
            raise
        cols = [d[0] for d in conn.description]
        return [dict(zip(cols, row)) for row in rows]
    finally:
        conn.close()


def load_facebook_queue_map(video_ids: List[str]) -> Dict[Tuple[str, str], List[Dict[str, Optional[str]]]]:
    if not video_ids:
        return {}
    conn = get_db_readonly()
    try:
        ensure_facebook_queue_schema(conn)
        cols = table_columns(conn, "shorts_facebook_queue")
        def _col(name: str) -> str:
            return name if name in cols else f"NULL AS {name}"
        placeholders = ", ".join("?" for _ in video_ids)
        try:
            rows = conn.execute(
                f"""
                SELECT
                    id,
                    {_col('user_id')},
                    {_col('brand_id')},
                    video_id,
                    plan_index,
                    {_col('clip_filename')},
                    status,
                    status_detail,
                    publish_at,
                    facebook_video_id,
                    published_at,
                    plan_title,
                    permalink,
                    media_type,
                    {_col('page_id')},
                    {_col('page_name')},
                    {_col('view_count')},
                    {_col('reach')},
                    {_col('impressions')},
                    {_col('reactions')},
                    {_col('comment_count')},
                    {_col('last_seen_comment_count')},
                    created_at
                FROM shorts_facebook_queue
                WHERE video_id IN ({placeholders})
                ORDER BY COALESCE(publish_at, created_at)
                """,
                video_ids,
            ).fetchall()
        except Exception as exc:
            if "shorts_facebook_queue" in str(exc).lower():
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


def get_facebook_queue_entry(queue_id: str) -> Optional[Dict[str, Optional[str]]]:
    conn = get_db_readonly()
    try:
        ensure_facebook_queue_schema(conn)
        cols = table_columns(conn, "shorts_facebook_queue")
        last_seen_expr = "last_seen_comment_count" if "last_seen_comment_count" in cols else "0"
        row = conn.execute(
            f"""
            SELECT
                id,
                user_id,
                video_id,
                plan_index,
                status,
                status_detail,
                publish_at,
                facebook_video_id,
                published_at,
                plan_title,
                permalink,
                media_type,
                page_id,
                page_name,
                view_count,
                reach,
                impressions,
                reactions,
                comment_count,
                {last_seen_expr} AS last_seen_comment_count
            FROM shorts_facebook_queue
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


def update_facebook_last_seen_comment_count(queue_id: str, last_seen_count: int) -> None:
    if not queue_id:
        return
    try:
        last_seen_value = max(0, int(last_seen_count))
    except (TypeError, ValueError):
        return
    conn = None
    for attempt in range(3):
        try:
            conn = get_db()
            break
        except duckdb.IOException as exc:
            if "lock" not in str(exc).lower() or attempt == 2:
                return
            time.sleep(0.05)
    if conn is None:
        return
    try:
        ensure_facebook_queue_schema(conn)
        conn.execute(
            """
            UPDATE shorts_facebook_queue
            SET last_seen_comment_count = ?, updated_at = ?
            WHERE id = ?
            """,
            [last_seen_value, _utc_now_iso(), queue_id],
        )
        conn.commit()
    finally:
        conn.close()
