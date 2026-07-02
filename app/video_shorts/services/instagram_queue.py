import json
import json
import time
import uuid
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import duckdb

from app.video_shorts.services.db import (
    get_db,
    get_db_readonly,
    ensure_instagram_queue_schema,
    table_columns,
)
from app.video_shorts.services.brands import brand_scoped_user_id, current_brand_id
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
            try_strptime({column_name}, '%Y-%m-%dT%H:%M:%S%z'),
            try_strptime({column_name}, '%Y-%m-%dT%H:%M%z'),
            try_strptime({column_name}, '%Y-%m-%dT%H:%M:%S'),
            try_strptime({column_name}, '%Y-%m-%dT%H:%M')
        )
    """.strip()


def _ensure_comment_cache_schema(conn):
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS instagram_comment_cache (
                media_id VARCHAR PRIMARY KEY,
                like_count INTEGER,
                comment_count INTEGER,
                last_synced VARCHAR,
                comments_json TEXT
            )
            """
        )
    except Exception as exc:
        if "read-only" in str(exc).lower():
            return
        raise


def enqueue_instagram_clip(
    *,
    user_id: Optional[str],
    video_id: Optional[str],
    plan_index: Optional[str],
    clip_filename: str,
    caption_text: str,
    publish_at_iso: Optional[str],
    instagram_business_account_id: Optional[str],
    instagram_username: Optional[str],
    youtube_video_id: Optional[str],
    youtube_short_id: Optional[str],
    plan_title: Optional[str],
    media_type: str = "reel",
    force_requeue: bool = False,
) -> str:
    media_type_norm = (media_type or "reel").strip().lower()
    if media_type_norm not in {"reel", "feed"}:
        media_type_norm = "reel"
    scoped_user_id = brand_scoped_user_id(user_id)
    queue_id = str(uuid.uuid4())
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
        ensure_instagram_queue_schema(conn)
        if not force_requeue:
            published = conn.execute(
                """
                SELECT id
                FROM shorts_instagram_queue
                WHERE video_id = ?
                  AND plan_index = ?
                  AND media_type = ?
                  AND status = 'published'
                ORDER BY created_at DESC
                LIMIT 1
                """,
                [video_id, plan_index or "", media_type_norm],
            ).fetchone()
            if published:
                return published[0]
        existing = conn.execute(
            """
            SELECT id
            FROM shorts_instagram_queue
            WHERE video_id = ?
              AND plan_index = ?
              AND media_type = ?
              AND status != 'published'
            ORDER BY created_at DESC
            LIMIT 1
            """,
            [video_id, plan_index or "", media_type_norm],
        ).fetchone()
        if existing:
            queue_id = existing[0]
            now = _utc_now_iso()
            conn.execute(
                """
                UPDATE shorts_instagram_queue
                SET caption_text = ?,
                    publish_at = ?,
                    status = 'pending',
                    status_detail = NULL,
                    updated_at = ?,
                    instagram_business_account_id = ?,
                    instagram_username = ?,
                    youtube_video_id = ?,
                    youtube_short_id = ?,
                    plan_title = ?
                WHERE id = ?
                """,
                [
                    caption_text[:2200],
                    publish_at_iso,
                    now,
                    instagram_business_account_id,
                    instagram_username,
                    youtube_video_id,
                    youtube_short_id,
                    plan_title,
                    queue_id,
                ],
            )
            conn.commit()
            return queue_id
        now = _utc_now_iso()
        conn.execute(
            """
            INSERT INTO shorts_instagram_queue (
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
                instagram_business_account_id,
                instagram_username,
                youtube_video_id,
                youtube_short_id,
                plan_title,
                media_type
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', NULL, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                queue_id,
                scoped_user_id,
                video_id,
                plan_index or "",
                clip_filename,
                caption_text[:2200],
                publish_at_iso,
                now,
                now,
                instagram_business_account_id,
                instagram_username,
                youtube_video_id,
                youtube_short_id,
                plan_title,
                media_type_norm,
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
            youtube_video_id=youtube_short_id,
            planned_publish_at=publish_at_iso,
        )
    except Exception:
        pass
    return queue_id


def fetch_due_instagram_jobs(limit: int = 5) -> List[Dict[str, Optional[str]]]:
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
        ensure_instagram_queue_schema(conn)
        now = _utc_now_iso()
        backend_name = getattr(conn, "backend_name", "duckdb")
        publish_ts_expr = _queue_timestamp_expr("publish_at_clean", backend_name)
        created_ts_expr = _queue_timestamp_expr("created_at_clean", backend_name)
        z_suffix_like = "'%%Z'" if backend_name == "postgres" else "'%Z'"
        stale_cutoff = (
            datetime.utcnow().replace(microsecond=0) - timedelta(seconds=STALE_UPLOAD_SECONDS)
        ).isoformat() + "Z"
        if backend_name == "postgres":
            sort_ts_expr = (
                f"COALESCE(EXTRACT(EPOCH FROM {publish_ts_expr}), "
                f"EXTRACT(EPOCH FROM {created_ts_expr}))"
            )
        else:
            sort_ts_expr = f"COALESCE(epoch({publish_ts_expr}), epoch({created_ts_expr}))"
        conn.execute(
            """
            UPDATE shorts_instagram_queue
            SET status = 'retry',
                status_detail = 'Upload lock expired; requeued automatically.',
                updated_at = ?
            WHERE status = 'uploading' AND updated_at < ?
            """,
            [_utc_now_iso(), stale_cutoff],
        )
        status_counts = conn.execute(
            "SELECT status, COUNT(*) FROM shorts_instagram_queue GROUP BY 1 ORDER BY 1"
        ).fetchall()
        due_count = conn.execute(
            f"""
            WITH normalized AS (
                SELECT *,
                       CASE
                           WHEN publish_at IS NULL THEN NULL
                           WHEN CAST(publish_at AS VARCHAR) LIKE {z_suffix_like}
                               THEN replace(CAST(publish_at AS VARCHAR), 'Z', '+00:00')
                           ELSE CAST(publish_at AS VARCHAR)
                       END AS publish_at_norm
                FROM shorts_instagram_queue
            ),
            cleaned AS (
                SELECT *,
                       split_part(replace(publish_at_norm, 'Z', ''), '+', 1) AS publish_at_clean
                FROM normalized
            ),
            queue AS (
                SELECT *,
                       {publish_ts_expr} AS publish_ts,
                       CASE
                           WHEN publish_at IS NULL OR btrim(CAST(publish_at AS VARCHAR)) = '' THEN TRUE
                           WHEN {publish_ts_expr} IS NULL THEN FALSE
                           ELSE {publish_ts_expr} <= NOW()
                       END AS ready_now
                FROM cleaned
            )
            SELECT COUNT(*) FROM queue
            WHERE status IN ('pending','retry')
              AND ready_now
            """
        ).fetchone()[0]
        print(f"Instagram queue status counts={status_counts} due_now={due_count}")
        rows = conn.execute(
            f"""
            WITH normalized AS (
                SELECT *,
                       CASE
                           WHEN publish_at IS NULL THEN NULL
                           WHEN CAST(publish_at AS VARCHAR) LIKE {z_suffix_like}
                               THEN replace(CAST(publish_at AS VARCHAR), 'Z', '+00:00')
                           ELSE CAST(publish_at AS VARCHAR)
                       END AS publish_at_norm,
                       CASE
                           WHEN created_at IS NULL THEN NULL
                           WHEN CAST(created_at AS VARCHAR) LIKE {z_suffix_like}
                               THEN replace(CAST(created_at AS VARCHAR), 'Z', '+00:00')
                           ELSE CAST(created_at AS VARCHAR)
                       END AS created_at_norm
                FROM shorts_instagram_queue
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
                       {sort_ts_expr} AS sort_ts,
                       CASE
                           WHEN publish_at IS NULL OR btrim(CAST(publish_at AS VARCHAR)) = '' THEN TRUE
                           WHEN {publish_ts_expr} IS NULL THEN FALSE
                           ELSE {publish_ts_expr} <= NOW()
                       END AS ready_now
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
                instagram_business_account_id,
                instagram_username,
                instagram_media_id,
                published_at,
                youtube_video_id,
                youtube_short_id,
                plan_title,
                media_type
            FROM queue
            WHERE status IN ('pending','retry')
              AND ready_now
            ORDER BY sort_ts ASC
            LIMIT ?
            """,
            [limit],
        ).fetchall()
        cols = [d[0] for d in conn.description]
        return [dict(zip(cols, row)) for row in rows]
    finally:
        conn.close()


def update_instagram_job_status(
    queue_id: str,
    *,
    status: str,
    status_detail: Optional[str] = None,
    instagram_media_id: Optional[str] = None,
    published_at_iso: Optional[str] = None,
    permalink: Optional[str] = None,
    like_count: Optional[int] = None,
    comment_count: Optional[int] = None,
) -> None:
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
    lifecycle_row = None
    try:
        ensure_instagram_queue_schema(conn)
        now = _utc_now_iso()
        conn.execute(
            """
            UPDATE shorts_instagram_queue
            SET status = ?,
                status_detail = ?,
                instagram_media_id = COALESCE(?, instagram_media_id),
                published_at = COALESCE(?, published_at),
                updated_at = ?,
                permalink = COALESCE(?, permalink),
                like_count = COALESCE(?, like_count),
                comment_count = COALESCE(?, comment_count)
            WHERE id = ?
            """,
            [
                status,
                status_detail,
                instagram_media_id,
                published_at_iso,
                now,
                permalink,
                like_count,
                comment_count,
                queue_id,
            ],
        )
        lifecycle_row = conn.execute(
            """
            SELECT video_id, clip_filename, publish_at, youtube_short_id, instagram_media_id
            FROM shorts_instagram_queue
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
        publish_status = (
            "published" if status == "published"
            else ("failed" if status == "failed" else "queued")
        )
        row_media_id = instagram_media_id or lifecycle_row[4]
        clip_filename = str(lifecycle_row[1] or "").strip()
        upsert_generated_video_record(
            source_video_id=str(lifecycle_row[0] or "").strip(),
            source_channel_type="youtube",
            clip_filename=clip_filename,
            output_filename=clip_filename,
            storage_file_key=f"short:{clip_filename}",
            generation_status="created",
            publish_status=publish_status,
            youtube_video_id=lifecycle_row[3],
            instagram_media_id=row_media_id,
            planned_publish_at=lifecycle_row[2],
            published_at=published_at_iso,
            instagram_published_at=published_at_iso if publish_status == "published" else None,
            primary_publish_platform="instagram" if publish_status == "published" else None,
        )
    except Exception:
        pass


def retry_instagram_job_as_reel(queue_id: str) -> None:
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
        ensure_instagram_queue_schema(conn)
        now = _utc_now_iso()
        conn.execute(
            """
            UPDATE shorts_instagram_queue
            SET status = 'pending',
                status_detail = 'VIDEO media_type reddedildi; REELS ile tekrar denenecek.',
                media_type = 'reel',
                updated_at = ?
            WHERE id = ?
            """,
            [now, queue_id],
        )
        conn.commit()
    finally:
        conn.close()


def mark_job_retry(queue_id: str, detail: str) -> None:
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
        ensure_instagram_queue_schema(conn)
        now = _utc_now_iso()
        conn.execute(
            """
            UPDATE shorts_instagram_queue
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


def update_instagram_metrics(
    queue_id: str,
    *,
    like_count: Optional[int] = None,
    comment_count: Optional[int] = None,
    impressions: Optional[int] = None,
    reach: Optional[int] = None,
    saved: Optional[int] = None,
    shares: Optional[int] = None,
    permalink: Optional[str] = None,
) -> None:
    updates = []
    params: List[object] = []
    if like_count is not None:
        updates.append("like_count = ?")
        params.append(like_count)
    if comment_count is not None:
        updates.append("comment_count = ?")
        params.append(comment_count)
    if impressions is not None:
        updates.append("impressions = ?")
        params.append(impressions)
    if reach is not None:
        updates.append("reach = ?")
        params.append(reach)
    if saved is not None:
        updates.append("saved = ?")
        params.append(saved)
    if shares is not None:
        updates.append("shares = ?")
        params.append(shares)
    if permalink:
        updates.append("permalink = ?")
        params.append(permalink)
    if not updates:
        return
    updates.append("updated_at = ?")
    params.append(_utc_now_iso())
    params.append(queue_id)
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
        ensure_instagram_queue_schema(conn)
        conn.execute(
            f"""
            UPDATE shorts_instagram_queue
            SET {', '.join(updates)}
            WHERE id = ?
            """,
            params,
        )
        conn.commit()
    finally:
        conn.close()


def upsert_instagram_comment_cache(
    media_id: str,
    like_count: Optional[int],
    comment_count: Optional[int],
    comments: Optional[List[Dict[str, object]]],
) -> None:
    if not media_id:
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
        _ensure_comment_cache_schema(conn)
        payload = json.dumps(comments or [])
        conn.execute(
            """
            INSERT INTO instagram_comment_cache (media_id, like_count, comment_count, last_synced, comments_json)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(media_id) DO UPDATE
            SET like_count=excluded.like_count,
                comment_count=excluded.comment_count,
                last_synced=excluded.last_synced,
                comments_json=excluded.comments_json
            """,
            [media_id, like_count, comment_count, _utc_now_iso(), payload],
        )
        conn.commit()
    finally:
        conn.close()


def get_instagram_comment_cache(media_id: Optional[str]) -> Optional[Dict[str, object]]:
    if not media_id:
        return None
    conn = None
    for attempt in range(3):
        try:
            conn = get_db_readonly()
            break
        except duckdb.IOException as exc:
            if "lock" not in str(exc).lower() or attempt == 2:
                return None
            time.sleep(0.05)
    if conn is None:
        return None
    try:
        _ensure_comment_cache_schema(conn)
        try:
            row = conn.execute(
                """
                SELECT like_count, comment_count, last_synced, comments_json
                FROM instagram_comment_cache
                WHERE media_id = ?
                """,
                [media_id],
            ).fetchone()
        except Exception as exc:
            if "instagram_comment_cache" in str(exc).lower():
                return None
            raise
        if not row:
            return None
        like_count, comment_count, last_synced, comments_json = row
        comments: List[Dict[str, object]] = []
        if comments_json:
            try:
                comments = json.loads(comments_json)
            except json.JSONDecodeError:
                comments = []
        return {
            "media_id": media_id,
            "like_count": like_count,
            "comment_count": comment_count,
            "last_synced": last_synced,
            "comments": comments,
        }
    finally:
        conn.close()


def load_instagram_comment_cache_map(media_ids: List[str]) -> Dict[str, Dict[str, object]]:
    normalized_ids = [str(media_id).strip() for media_id in media_ids if str(media_id).strip()]
    if not normalized_ids:
        return {}
    conn = None
    for attempt in range(3):
        try:
            conn = get_db_readonly()
            break
        except duckdb.IOException as exc:
            if "lock" not in str(exc).lower() or attempt == 2:
                return {}
            time.sleep(0.05)
    if conn is None:
        return {}
    try:
        _ensure_comment_cache_schema(conn)
        placeholders = ", ".join("?" for _ in normalized_ids)
        try:
            rows = conn.execute(
                f"""
                SELECT media_id, like_count, comment_count, last_synced, comments_json
                FROM instagram_comment_cache
                WHERE media_id IN ({placeholders})
                """,
                normalized_ids,
            ).fetchall()
        except Exception as exc:
            if "instagram_comment_cache" in str(exc).lower():
                return {}
            raise
        result: Dict[str, Dict[str, object]] = {}
        for media_id, like_count, comment_count, last_synced, comments_json in rows:
            comments: List[Dict[str, object]] = []
            if comments_json:
                try:
                    comments = json.loads(comments_json)
                except json.JSONDecodeError:
                    comments = []
            result[str(media_id)] = {
                "media_id": media_id,
                "like_count": like_count,
                "comment_count": comment_count,
                "last_synced": last_synced,
                "comments": comments,
            }
        return result
    finally:
        conn.close()


def fetch_instagram_media_jobs(limit: Optional[int] = None) -> List[Dict[str, Optional[str]]]:
    conn = None
    for attempt in range(3):
        try:
            conn = get_db_readonly()
            break
        except duckdb.IOException as exc:
            if "lock" not in str(exc).lower() or attempt == 2:
                return []
            time.sleep(0.05)
    if conn is None:
        return []
    try:
        ensure_instagram_queue_schema(conn)
        query = """
            SELECT id, user_id, instagram_media_id, plan_title, media_type
            FROM shorts_instagram_queue
            WHERE instagram_media_id IS NOT NULL
            ORDER BY updated_at DESC
        """
        try:
            if limit:
                rows = conn.execute(query + " LIMIT ?", [limit]).fetchall()
            else:
                rows = conn.execute(query).fetchall()
        except Exception as exc:
            if "shorts_instagram_queue" in str(exc).lower():
                return []
            raise
        cols = [d[0] for d in conn.description]
        return [dict(zip(cols, row)) for row in rows]
    finally:
        conn.close()


def get_instagram_queue_entry(queue_id: str) -> Optional[Dict[str, Optional[str]]]:
    conn = get_db_readonly()
    try:
        ensure_instagram_queue_schema(conn)
        cols = table_columns(conn, "shorts_instagram_queue")
        last_seen_expr = "last_seen_comment_count" if "last_seen_comment_count" in cols else "0"
        row = conn.execute(
            """
            SELECT
                id,
                user_id,
                video_id,
                instagram_media_id,
                instagram_business_account_id,
                instagram_username,
                plan_title,
                media_type,
                permalink,
                like_count,
                comment_count,
                {last_seen_expr} AS last_seen_comment_count,
                impressions,
                reach,
                saved,
                shares,
                publish_at,
                published_at,
                status,
                status_detail
            FROM shorts_instagram_queue
            WHERE id = ?
            """.format(last_seen_expr=last_seen_expr),
            [queue_id],
        ).fetchone()
        if not row:
            return None
        cols = [d[0] for d in conn.description]
        return dict(zip(cols, row))
    finally:
        conn.close()


def get_instagram_queue_entry_by_media_id(media_id: str) -> Optional[Dict[str, Optional[str]]]:
    if not media_id:
        return None
    conn = get_db_readonly()
    try:
        ensure_instagram_queue_schema(conn)
        cols = table_columns(conn, "shorts_instagram_queue")
        last_seen_expr = "last_seen_comment_count" if "last_seen_comment_count" in cols else "0"
        row = conn.execute(
            """
            SELECT
                id,
                user_id,
                video_id,
                instagram_media_id,
                instagram_business_account_id,
                instagram_username,
                plan_title,
                media_type,
                permalink,
                like_count,
                comment_count,
                {last_seen_expr} AS last_seen_comment_count,
                impressions,
                reach,
                saved,
                shares,
                publish_at,
                published_at,
                status,
                status_detail
            FROM shorts_instagram_queue
            WHERE instagram_media_id = ?
            ORDER BY updated_at DESC, created_at DESC
            LIMIT 1
            """.format(last_seen_expr=last_seen_expr),
            [media_id],
        ).fetchone()
        if not row:
            return None
        cols = [d[0] for d in conn.description]
        return dict(zip(cols, row))
    finally:
        conn.close()


def update_instagram_metrics(
    queue_id: str,
    *,
    like_count: Optional[int] = None,
    comment_count: Optional[int] = None,
    impressions: Optional[int] = None,
    reach: Optional[int] = None,
    saved: Optional[int] = None,
    shares: Optional[int] = None,
    permalink: Optional[str] = None,
) -> None:
    updates = []
    params: List[object] = []
    if like_count is not None:
        updates.append("like_count = ?")
        params.append(like_count)
    if comment_count is not None:
        updates.append("comment_count = ?")
        params.append(comment_count)
    if impressions is not None:
        updates.append("impressions = ?")
        params.append(impressions)
    if reach is not None:
        updates.append("reach = ?")
        params.append(reach)
    if saved is not None:
        updates.append("saved = ?")
        params.append(saved)
    if shares is not None:
        updates.append("shares = ?")
        params.append(shares)
    if permalink:
        updates.append("permalink = ?")
        params.append(permalink)
    if not updates:
        return
    updates.append("updated_at = ?")
    params.append(_utc_now_iso())
    params.append(queue_id)
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
        ensure_instagram_queue_schema(conn)
        conn.execute(
            f"""
            UPDATE shorts_instagram_queue
            SET {', '.join(updates)}
            WHERE id = ?
            """,
            params,
        )
        conn.commit()
    finally:
        conn.close()


def update_instagram_last_seen_comment_count(queue_id: str, last_seen_count: int) -> None:
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
        ensure_instagram_queue_schema(conn)
        try:
            conn.execute(
                """
                UPDATE shorts_instagram_queue
                SET last_seen_comment_count = ?, updated_at = ?
                WHERE id = ?
                """,
                [last_seen_value, _utc_now_iso(), queue_id],
            )
            conn.commit()
        except Exception:
            conn.rollback()
    finally:
        conn.close()


def upsert_instagram_comment_cache(
    media_id: str,
    like_count: Optional[int],
    comment_count: Optional[int],
    comments: Optional[List[Dict[str, object]]],
) -> None:
    if not media_id:
        return
    conn = get_db()
    try:
        _ensure_comment_cache_schema(conn)
        payload = json.dumps(comments or [])
        conn.execute(
            """
            INSERT INTO instagram_comment_cache (media_id, like_count, comment_count, last_synced, comments_json)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(media_id) DO UPDATE
            SET like_count=excluded.like_count,
                comment_count=excluded.comment_count,
                last_synced=excluded.last_synced,
                comments_json=excluded.comments_json
            """,
            [media_id, like_count, comment_count, _utc_now_iso(), payload],
        )
        conn.commit()
    finally:
        conn.close()


def fetch_instagram_media_jobs(limit: Optional[int] = None) -> List[Dict[str, Optional[str]]]:
    conn = None
    for attempt in range(3):
        try:
            conn = get_db_readonly()
            break
        except duckdb.IOException as exc:
            if "lock" not in str(exc).lower() or attempt == 2:
                return []
            time.sleep(0.05)
    if conn is None:
        return []
    try:
        ensure_instagram_queue_schema(conn)
        query = """
            SELECT id, user_id, instagram_media_id, plan_title, media_type
            FROM shorts_instagram_queue
            WHERE instagram_media_id IS NOT NULL
            ORDER BY updated_at DESC
        """
        if limit:
            rows = conn.execute(query + " LIMIT ?", [limit]).fetchall()
        else:
            rows = conn.execute(query).fetchall()
        cols = [d[0] for d in conn.description]
        return [dict(zip(cols, row)) for row in rows]
    finally:
        conn.close()


def load_instagram_queue_map(video_ids: List[str]) -> Dict[Tuple[str, str], List[Dict[str, Optional[str]]]]:
    if not video_ids:
        return {}
    conn = get_db_readonly()
    try:
        ensure_instagram_queue_schema(conn)
        placeholders = ", ".join("?" for _ in video_ids)
        cols = table_columns(conn, "shorts_instagram_queue")
        last_seen_expr = "last_seen_comment_count" if "last_seen_comment_count" in cols else "0"
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
                    instagram_media_id,
                    published_at,
                    instagram_username,
                    youtube_short_id,
                    plan_title,
                    permalink,
                    like_count,
                    comment_count,
                    {last_seen_expr} AS last_seen_comment_count,
                    impressions,
                    reach,
                    saved,
                    shares,
                    media_type,
                    created_at
                FROM shorts_instagram_queue
                WHERE video_id IN ({placeholders})
                ORDER BY COALESCE(publish_at, created_at)
                """,
                video_ids,
            ).fetchall()
        except Exception as exc:
            if "shorts_instagram_queue" in str(exc).lower():
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
