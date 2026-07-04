import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

import duckdb

from app.video_shorts.services.db import get_db, get_db_readonly


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_comment_cache_schema(conn) -> None:
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS social_comment_cache (
                platform VARCHAR,
                comment_id VARCHAR,
                parent_id VARCHAR,
                thread_id VARCHAR,
                video_id VARCHAR,
                instagram_media_id VARCHAR,
                queue_id VARCHAR,
                owner_user_id VARCHAR,
                video_title VARCHAR,
                author VARCHAR,
                text TEXT,
                status VARCHAR,
                comment_url VARCHAR,
                published_at VARCHAR,
                like_count INTEGER,
                moderation_flagged BOOLEAN,
                moderation_reason VARCHAR,
                moderation_checked_at TIMESTAMP,
                created_at TIMESTAMP,
                updated_at TIMESTAMP,
                PRIMARY KEY (platform, comment_id)
            )
            """
        )
    except Exception as exc:
        if "read-only" in str(exc).lower():
            return
        raise


def upsert_comment_records(records: List[Dict[str, object]]) -> None:
    if not records:
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
        ensure_comment_cache_schema(conn)
        now = _utc_now()
        for record in records:
            conn.execute(
                """
                INSERT INTO social_comment_cache (
                    platform,
                    comment_id,
                    parent_id,
                    thread_id,
                    video_id,
                    instagram_media_id,
                    queue_id,
                    owner_user_id,
                    video_title,
                    author,
                    text,
                    status,
                    comment_url,
                    published_at,
                    like_count,
                    moderation_flagged,
                    moderation_reason,
                    moderation_checked_at,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (platform, comment_id) DO UPDATE SET
                    parent_id = excluded.parent_id,
                    thread_id = excluded.thread_id,
                    video_id = excluded.video_id,
                    instagram_media_id = excluded.instagram_media_id,
                    queue_id = excluded.queue_id,
                    owner_user_id = excluded.owner_user_id,
                    video_title = excluded.video_title,
                    author = excluded.author,
                    text = excluded.text,
                    status = CASE
                        WHEN social_comment_cache.platform = 'instagram'
                             AND LOWER(COALESCE(social_comment_cache.status, '')) = 'hidden'
                             AND LOWER(COALESCE(excluded.status, '')) = 'published'
                        THEN social_comment_cache.status
                        ELSE excluded.status
                    END,
                    comment_url = excluded.comment_url,
                    published_at = excluded.published_at,
                    like_count = excluded.like_count,
                    moderation_flagged = excluded.moderation_flagged,
                    moderation_reason = excluded.moderation_reason,
                    moderation_checked_at = excluded.moderation_checked_at,
                    updated_at = excluded.updated_at
                """,
                [
                    record.get("platform"),
                    record.get("comment_id"),
                    record.get("parent_id"),
                    record.get("thread_id"),
                    record.get("video_id"),
                    record.get("instagram_media_id"),
                    record.get("queue_id"),
                    record.get("owner_user_id"),
                    record.get("video_title"),
                    record.get("author"),
                    record.get("text"),
                    record.get("status"),
                    record.get("comment_url"),
                    record.get("published_at"),
                    record.get("like_count"),
                    record.get("moderation_flagged"),
                    record.get("moderation_reason"),
                    record.get("moderation_checked_at"),
                    record.get("created_at") or now,
                    now,
                ],
            )
        conn.commit()
    finally:
        conn.close()


def fetch_comment_records(
    owner_user_id: str,
    *,
    limit: int = 200,
    status: Optional[str] = None,
    platform: Optional[str] = None,
    sort_key: Optional[str] = None,
    sort_dir: str = "desc",
) -> List[Dict[str, object]]:
    conn = get_db_readonly()
    try:
        ensure_comment_cache_schema(conn)
        where = ["owner_user_id = ?"]
        params: List[object] = [owner_user_id]
        if status and status != "all":
            if status.lower() == "pending":
                where.append("LOWER(status) IN ('heldforreview', 'likelyspam', 'pending')")
            else:
                where.append("LOWER(status) = ?")
                params.append(status.lower())
        if platform and platform != "all":
            where.append("platform = ?")
            params.append(platform)
        where_clause = " AND ".join(where)
        sort_dir_clean = "ASC" if (sort_dir or "").lower() == "asc" else "DESC"
        if (sort_key or "").lower() == "date":
            order_clause = f"published_at {sort_dir_clean} NULLS LAST, updated_at {sort_dir_clean}"
        else:
            order_clause = f"updated_at {sort_dir_clean}"
        query = f"""
            SELECT
                platform,
                comment_id,
                parent_id,
                thread_id,
                video_id,
                instagram_media_id,
                queue_id,
                owner_user_id,
                video_title,
                author,
                text,
                status,
                comment_url,
                published_at,
                like_count,
                moderation_flagged,
                moderation_reason,
                moderation_checked_at,
                updated_at
            FROM social_comment_cache
            WHERE {where_clause}
            ORDER BY {order_clause}
            LIMIT ?
        """
        params.append(limit)
        try:
            rows = conn.execute(query, params).fetchall()
        except Exception as exc:
            if "social_comment_cache" in str(exc).lower():
                return []
            raise
        cols = [d[0] for d in conn.description]
        return [dict(zip(cols, row)) for row in rows]
    finally:
        conn.close()


def fetch_comment_records_for_video_ids(
    video_ids: List[str],
    *,
    limit: int = 200,
    status: Optional[str] = None,
    platform: Optional[str] = None,
    sort_key: Optional[str] = None,
    sort_dir: str = "desc",
) -> List[Dict[str, object]]:
    normalized_video_ids = [str(video_id or "").strip() for video_id in video_ids if str(video_id or "").strip()]
    if not normalized_video_ids:
        return []
    conn = get_db_readonly()
    try:
        ensure_comment_cache_schema(conn)
        placeholders = ", ".join(["?"] * len(normalized_video_ids))
        where = [f"video_id IN ({placeholders})"]
        params: List[object] = list(normalized_video_ids)
        if status and status != "all":
            if status.lower() == "pending":
                where.append("LOWER(status) IN ('heldforreview', 'likelyspam', 'pending')")
            else:
                where.append("LOWER(status) = ?")
                params.append(status.lower())
        if platform and platform != "all":
            where.append("platform = ?")
            params.append(platform)
        where_clause = " AND ".join(where)
        sort_dir_clean = "ASC" if (sort_dir or "").lower() == "asc" else "DESC"
        if (sort_key or "").lower() == "date":
            order_clause = f"published_at {sort_dir_clean} NULLS LAST, updated_at {sort_dir_clean}"
        else:
            order_clause = f"updated_at {sort_dir_clean}"
        query = f"""
            SELECT
                platform,
                comment_id,
                parent_id,
                thread_id,
                video_id,
                instagram_media_id,
                queue_id,
                owner_user_id,
                video_title,
                author,
                text,
                status,
                comment_url,
                published_at,
                like_count,
                moderation_flagged,
                moderation_reason,
                moderation_checked_at,
                updated_at
            FROM social_comment_cache
            WHERE {where_clause}
            ORDER BY {order_clause}
            LIMIT ?
        """
        params.append(limit)
        try:
            rows = conn.execute(query, params).fetchall()
        except Exception as exc:
            if "social_comment_cache" in str(exc).lower():
                return []
            raise
        cols = [d[0] for d in conn.description]
        return [dict(zip(cols, row)) for row in rows]
    finally:
        conn.close()


def fetch_comment_records_for_video(
    owner_user_id: str,
    *,
    video_id: str,
    limit: int = 200,
    status: Optional[str] = None,
    platform: Optional[str] = None,
    sort_key: Optional[str] = None,
    sort_dir: str = "desc",
) -> List[Dict[str, object]]:
    if not video_id:
        return []
    conn = get_db_readonly()
    try:
        ensure_comment_cache_schema(conn)
        where = ["owner_user_id = ?", "video_id = ?"]
        params: List[object] = [owner_user_id, video_id]
        if status and status != "all":
            if status.lower() == "pending":
                where.append("LOWER(status) IN ('heldforreview', 'likelyspam', 'pending')")
            else:
                where.append("LOWER(status) = ?")
                params.append(status.lower())
        if platform and platform != "all":
            where.append("platform = ?")
            params.append(platform)
        where_clause = " AND ".join(where)
        sort_dir_clean = "ASC" if (sort_dir or "").lower() == "asc" else "DESC"
        if (sort_key or "").lower() == "date":
            order_clause = f"published_at {sort_dir_clean} NULLS LAST, updated_at {sort_dir_clean}"
        else:
            order_clause = f"updated_at {sort_dir_clean}"
        query = f"""
            SELECT
                platform,
                comment_id,
                parent_id,
                thread_id,
                video_id,
                instagram_media_id,
                queue_id,
                owner_user_id,
                video_title,
                author,
                text,
                status,
                comment_url,
                published_at,
                like_count,
                moderation_flagged,
                moderation_reason,
                moderation_checked_at,
                updated_at
            FROM social_comment_cache
            WHERE {where_clause}
            ORDER BY {order_clause}
            LIMIT ?
        """
        params.append(limit)
        try:
            rows = conn.execute(query, params).fetchall()
        except Exception as exc:
            if "social_comment_cache" in str(exc).lower():
                return []
            raise
        cols = [d[0] for d in conn.description]
        return [dict(zip(cols, row)) for row in rows]
    finally:
        conn.close()


def fetch_latest_comment_timestamps(
    owner_user_id: str,
    *,
    platform: str,
) -> Dict[str, Optional[str]]:
    conn = get_db_readonly()
    try:
        ensure_comment_cache_schema(conn)
        try:
            rows = conn.execute(
                """
                SELECT video_id, MAX(published_at) AS latest_published_at
                FROM social_comment_cache
                WHERE owner_user_id = ?
                  AND platform = ?
                  AND published_at IS NOT NULL
                  AND published_at <> ''
                GROUP BY video_id
                """,
                [owner_user_id, platform],
            ).fetchall()
        except Exception as exc:
            if "social_comment_cache" in str(exc).lower():
                return {}
            raise
        return {row[0]: row[1] for row in rows if row[0]}
    finally:
        conn.close()


def fetch_comments_missing_moderation(
    owner_user_id: str,
    *,
    limit: int = 200,
) -> List[Dict[str, object]]:
    conn = get_db_readonly()
    try:
        ensure_comment_cache_schema(conn)
        try:
            rows = conn.execute(
                """
                SELECT platform, comment_id, text
                FROM social_comment_cache
                WHERE owner_user_id = ?
                  AND moderation_flagged IS NULL
                  AND text IS NOT NULL
                  AND text <> ''
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                [owner_user_id, limit],
            ).fetchall()
        except Exception as exc:
            if "social_comment_cache" in str(exc).lower():
                return []
            raise
        return [
            {"platform": row[0], "comment_id": row[1], "text": row[2]} for row in rows
        ]
    finally:
        conn.close()


def update_comment_moderation(records: List[Dict[str, object]]) -> None:
    if not records:
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
        ensure_comment_cache_schema(conn)
        now = _utc_now()
        for record in records:
            conn.execute(
                """
                UPDATE social_comment_cache
                SET moderation_flagged = ?,
                    moderation_reason = ?,
                    moderation_checked_at = ?,
                    updated_at = ?
                WHERE platform = ? AND comment_id = ?
                """,
                [
                    record.get("moderation_flagged"),
                    record.get("moderation_reason"),
                    record.get("moderation_checked_at") or now,
                    now,
                    record.get("platform"),
                    record.get("comment_id"),
                ],
            )
        conn.commit()
    finally:
        conn.close()


def update_comment_status(platform: str, comment_id: str, status: str) -> None:
    if not platform or not comment_id or not status:
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
        ensure_comment_cache_schema(conn)
        now = _utc_now()
        conn.execute(
            """
            UPDATE social_comment_cache
            SET status = ?,
                updated_at = ?
            WHERE platform = ? AND comment_id = ?
            """,
            [status, now, platform, comment_id],
        )
        conn.commit()
    finally:
        conn.close()


def delete_comment_record(platform: str, comment_id: str) -> None:
    if not platform or not comment_id:
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
        ensure_comment_cache_schema(conn)
        conn.execute(
            """
            DELETE FROM social_comment_cache
            WHERE platform = ? AND comment_id = ?
            """,
            [platform, comment_id],
        )
        conn.commit()
    finally:
        conn.close()


def fetch_comment_owner(platform: str, comment_id: str) -> Optional[str]:
    if not platform or not comment_id:
        return None
    conn = get_db_readonly()
    try:
        ensure_comment_cache_schema(conn)
        try:
            row = conn.execute(
                """
                SELECT owner_user_id
                FROM social_comment_cache
                WHERE platform = ? AND comment_id = ?
                """,
                [platform, comment_id],
            ).fetchone()
        except Exception as exc:
            if "social_comment_cache" in str(exc).lower():
                return None
            raise
        return row[0] if row else None
    finally:
        conn.close()


def fetch_instagram_deleted_comments(
    queue_id: str,
    *,
    limit: int = 200,
) -> List[Dict[str, object]]:
    if not queue_id:
        return []
    conn = get_db_readonly()
    try:
        ensure_comment_cache_schema(conn)
        try:
            rows = conn.execute(
                """
                SELECT
                    comment_id,
                    parent_id,
                    thread_id,
                    author,
                    text,
                    published_at,
                    like_count,
                    moderation_flagged,
                    moderation_reason,
                    moderation_checked_at,
                    updated_at
                FROM social_comment_cache
                WHERE platform = 'instagram'
                  AND queue_id = ?
                  AND LOWER(COALESCE(status, '')) = 'deleted'
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                [queue_id, limit],
            ).fetchall()
        except Exception as exc:
            if "social_comment_cache" in str(exc).lower():
                return []
            raise
        deleted: List[Dict[str, object]] = []
        for row in rows:
            (
                comment_id,
                parent_id,
                thread_id,
                author,
                text,
                published_at,
                like_count,
                moderation_flagged,
                moderation_reason,
                moderation_checked_at,
                updated_at,
            ) = row
            deleted.append(
                {
                    "id": comment_id,
                    "comment_id": comment_id,
                    "parent_id": parent_id,
                    "thread_id": thread_id,
                    "username": author,
                    "text": text,
                    "timestamp": published_at,
                    "like_count": like_count,
                    "status": "deleted",
                    "is_deleted": True,
                    "deleted_at": updated_at,
                    "moderation_flagged": moderation_flagged,
                    "moderation_reason": moderation_reason,
                    "moderation_checked_at": moderation_checked_at,
                }
            )
        return deleted
    finally:
        conn.close()


def fetch_instagram_comments_with_statuses(
    queue_id: str,
    statuses: List[str],
    *,
    limit: int = 200,
) -> List[Dict[str, object]]:
    if not queue_id or not statuses:
        return []
    normalized_statuses = [
        str(status).strip().lower()
        for status in statuses
        if str(status).strip()
    ]
    if not normalized_statuses:
        return []
    placeholders = ", ".join("?" for _ in normalized_statuses)
    conn = get_db_readonly()
    try:
        ensure_comment_cache_schema(conn)
        try:
            rows = conn.execute(
                f"""
                SELECT
                    comment_id,
                    parent_id,
                    thread_id,
                    author,
                    text,
                    published_at,
                    like_count,
                    moderation_flagged,
                    moderation_reason,
                    moderation_checked_at,
                    updated_at,
                    LOWER(COALESCE(status, '')) AS status
                FROM social_comment_cache
                WHERE platform = 'instagram'
                  AND queue_id = ?
                  AND LOWER(COALESCE(status, '')) IN ({placeholders})
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                [queue_id, *normalized_statuses, limit],
            ).fetchall()
        except Exception as exc:
            if "social_comment_cache" in str(exc).lower():
                return []
            raise
        results: List[Dict[str, object]] = []
        for row in rows:
            (
                comment_id,
                parent_id,
                thread_id,
                author,
                text,
                published_at,
                like_count,
                moderation_flagged,
                moderation_reason,
                moderation_checked_at,
                updated_at,
                status,
            ) = row
            results.append(
                {
                    "id": comment_id,
                    "comment_id": comment_id,
                    "parent_id": parent_id,
                    "thread_id": thread_id,
                    "username": author,
                    "text": text,
                    "timestamp": published_at,
                    "like_count": like_count,
                    "status": status,
                    "is_reply": parent_id is not None,
                    "moderation_flagged": moderation_flagged,
                    "moderation_reason": moderation_reason,
                    "moderation_checked_at": moderation_checked_at,
                    "updated_at": updated_at,
                    "is_deleted": status == "deleted",
                    "is_hidden": status == "hidden",
                }
            )
        return results
    finally:
        conn.close()
