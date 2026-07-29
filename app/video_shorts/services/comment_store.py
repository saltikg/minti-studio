import logging
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence

import duckdb

from app.video_shorts.config import COMMENT_AUTO_MODERATION_MODE
from app.video_shorts.services.db import table_columns
from app.video_shorts.services.db import get_db, get_db_readonly


logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_filter_values(value: Optional[str | Sequence[str]]) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw_values = [part.strip().lower() for part in value.split(",")]
    else:
        raw_values = []
        for item in value:
            raw_values.extend(part.strip().lower() for part in str(item or "").split(","))
    normalized: List[str] = []
    for item in raw_values:
        if not item or item == "all" or item in normalized:
            continue
        normalized.append(item)
    return normalized


def _append_status_filter(where: List[str], params: List[object], status: Optional[str | Sequence[str]]) -> None:
    values = _normalize_filter_values(status)
    if not values:
        return
    pending_selected = "pending" in values
    exact_values = [value for value in values if value != "pending"]
    clauses: List[str] = []
    if pending_selected:
        clauses.append("LOWER(status) IN ('heldforreview', 'likelyspam', 'pending')")
    if exact_values:
        placeholders = ", ".join("?" for _ in exact_values)
        clauses.append(f"LOWER(status) IN ({placeholders})")
        params.extend(exact_values)
    if clauses:
        where.append("(" + " OR ".join(clauses) + ")")


def _append_platform_filter(where: List[str], params: List[object], platform: Optional[str | Sequence[str]]) -> None:
    values = _normalize_filter_values(platform)
    if not values:
        return
    placeholders = ", ".join("?" for _ in values)
    where.append(f"platform IN ({placeholders})")
    params.extend(values)


def owner_user_id_matches(filter_user_id: Optional[str], row_owner_user_id: Optional[str]) -> bool:
    filter_text = str(filter_user_id or "").strip()
    row_text = str(row_owner_user_id or "").strip()
    if not filter_text or not row_text:
        return False
    return row_text == filter_text or row_text.startswith(f"{filter_text}::")


def _append_owner_user_filter(where: List[str], params: List[object], owner_user_id: Optional[str]) -> None:
    owner_text = str(owner_user_id or "").strip()
    if not owner_text:
        return
    where.append("(owner_user_id = ? OR owner_user_id LIKE ?)")
    params.extend([owner_text, f"{owner_text}::%"])


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
                auto_moderation_action VARCHAR,
                auto_moderation_at TIMESTAMP,
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
    try:
        columns = table_columns(conn, "social_comment_cache")
    except Exception:
        columns = set()
    extra_columns = [
        ("auto_moderation_action", "VARCHAR"),
        ("auto_moderation_at", "TIMESTAMP"),
    ]
    for column_name, definition in extra_columns:
        if column_name in columns:
            continue
        try:
            conn.execute(
                f"ALTER TABLE social_comment_cache ADD COLUMN {column_name} {definition}"
            )
            columns.add(column_name)
        except Exception as exc:
            if "read-only" in str(exc).lower():
                return
            raise


def _prepare_auto_moderation_records(
    records: List[Dict[str, object]],
    now: datetime,
) -> List[Dict[str, object]]:
    mode = COMMENT_AUTO_MODERATION_MODE
    if mode == "off":
        return records
    has_flagged = any(bool(record.get("moderation_flagged")) for record in records)
    if mode == "enforce" and has_flagged:
        logger.warning(
            "COMMENT_AUTO_MODERATION_MODE=enforce is not implemented yet; running in shadow mode."
        )
        mode = "shadow"
    for record in records:
        if not bool(record.get("moderation_flagged")):
            continue
        record["auto_moderation_action"] = "would_hide"
        record["auto_moderation_at"] = record.get("auto_moderation_at") or now
        logger.info(
            "SHADOW: would hide comment_id=%s target_id=%s platform=%s reason=%s",
            record.get("comment_id"),
            record.get("instagram_media_id") or record.get("video_id") or record.get("queue_id"),
            record.get("platform"),
            record.get("moderation_reason") or "",
        )
    return records


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
        prepared_records = _prepare_auto_moderation_records(records, now)
        for record in prepared_records:
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
                    auto_moderation_action,
                    auto_moderation_at,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (platform, comment_id) DO UPDATE SET
                    parent_id = excluded.parent_id,
                    thread_id = excluded.thread_id,
                    video_id = excluded.video_id,
                    instagram_media_id = excluded.instagram_media_id,
                    queue_id = excluded.queue_id,
                    owner_user_id = excluded.owner_user_id,
                    video_title = excluded.video_title,
                    author = COALESCE(NULLIF(excluded.author, ''), social_comment_cache.author),
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
                    auto_moderation_action = COALESCE(
                        social_comment_cache.auto_moderation_action,
                        excluded.auto_moderation_action
                    ),
                    auto_moderation_at = COALESCE(
                        social_comment_cache.auto_moderation_at,
                        excluded.auto_moderation_at
                    ),
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
                    record.get("auto_moderation_action"),
                    record.get("auto_moderation_at"),
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
    status: Optional[str | Sequence[str]] = None,
    platform: Optional[str | Sequence[str]] = None,
    sort_key: Optional[str] = None,
    sort_dir: str = "desc",
) -> List[Dict[str, object]]:
    conn = get_db_readonly()
    try:
        ensure_comment_cache_schema(conn)
        where: List[str] = []
        params: List[object] = []
        _append_owner_user_filter(where, params, owner_user_id)
        _append_status_filter(where, params, status)
        _append_platform_filter(where, params, platform)
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
    status: Optional[str | Sequence[str]] = None,
    platform: Optional[str | Sequence[str]] = None,
    sort_key: Optional[str] = None,
    sort_dir: str = "desc",
    owner_user_id: Optional[str] = None,
    cursor_sort_value: Optional[str] = None,
    cursor_platform: Optional[str] = None,
    cursor_comment_id: Optional[str] = None,
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
        _append_owner_user_filter(where, params, owner_user_id)
        _append_status_filter(where, params, status)
        _append_platform_filter(where, params, platform)
        updated_expr = "COALESCE(updated_at::text, '')"
        sort_expr = (
            f"COALESCE(NULLIF(published_at, ''), {updated_expr}, '')"
            if (sort_key or "").lower() == "date"
            else updated_expr
        )
        cursor_sort_text = str(cursor_sort_value or "")
        cursor_platform_text = str(cursor_platform or "")
        cursor_comment_text = str(cursor_comment_id or "")
        if cursor_sort_text and cursor_platform_text and cursor_comment_text:
            if (sort_dir or "").lower() == "asc":
                where.append(
                    "("
                    f"{sort_expr} > ? "
                    f"OR ({sort_expr} = ? AND platform > ?) "
                    f"OR ({sort_expr} = ? AND platform = ? AND comment_id > ?)"
                    ")"
                )
            else:
                where.append(
                    "("
                    f"{sort_expr} < ? "
                    f"OR ({sort_expr} = ? AND platform > ?) "
                    f"OR ({sort_expr} = ? AND platform = ? AND comment_id > ?)"
                    ")"
                )
            params.extend(
                [
                    cursor_sort_text,
                    cursor_sort_text,
                    cursor_platform_text,
                    cursor_sort_text,
                    cursor_platform_text,
                    cursor_comment_text,
                ]
            )
        where_clause = " AND ".join(where)
        sort_dir_clean = "ASC" if (sort_dir or "").lower() == "asc" else "DESC"
        order_clause = (
            f"{sort_expr} {sort_dir_clean}, {updated_expr} {sort_dir_clean}, platform ASC, comment_id ASC"
        )
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
                updated_at,
                {sort_expr} AS _cursor_sort_value
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
        where = ["video_id = ?"]
        params: List[object] = [video_id]
        _append_owner_user_filter(where, params, owner_user_id)
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
                WHERE (owner_user_id = ? OR owner_user_id LIKE ?)
                  AND platform = ?
                  AND published_at IS NOT NULL
                  AND published_at <> ''
                GROUP BY video_id
                """,
                [owner_user_id, f"{owner_user_id}::%", platform],
            ).fetchall()
        except Exception as exc:
            if "social_comment_cache" in str(exc).lower():
                return {}
            raise
        return {row[0]: row[1] for row in rows if row[0]}
    finally:
        conn.close()


def fetch_top_level_comment_counts(
    video_ids: Sequence[str],
    *,
    platform: str,
) -> Dict[str, int]:
    normalized_video_ids = [str(video_id or "").strip() for video_id in video_ids if str(video_id or "").strip()]
    if not normalized_video_ids:
        return {}
    conn = get_db_readonly()
    try:
        ensure_comment_cache_schema(conn)
        placeholders = ", ".join(["?"] * len(normalized_video_ids))
        params: List[object] = [platform, *normalized_video_ids]
        try:
            rows = conn.execute(
                f"""
                SELECT video_id, COUNT(*) AS top_level_count
                FROM social_comment_cache
                WHERE platform = ?
                  AND video_id IN ({placeholders})
                  AND (parent_id IS NULL OR parent_id = '')
                GROUP BY video_id
                """,
                params,
            ).fetchall()
        except Exception as exc:
            if "social_comment_cache" in str(exc).lower():
                return {}
            raise
        return {
            str(row[0]): int(row[1] or 0)
            for row in rows
            if row and row[0]
        }
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
                WHERE (owner_user_id = ? OR owner_user_id LIKE ?)
                  AND moderation_flagged IS NULL
                  AND text IS NOT NULL
                  AND text <> ''
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                [owner_user_id, f"{owner_user_id}::%", limit],
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


def fetch_comment_moderation_state(
    platform: str,
    comment_ids: Sequence[str],
    *,
    owner_user_id: Optional[str] = None,
) -> Dict[str, Dict[str, object]]:
    normalized_comment_ids = [
        str(comment_id or "").strip()
        for comment_id in comment_ids
        if str(comment_id or "").strip()
    ]
    if not platform or not normalized_comment_ids:
        return {}
    conn = get_db_readonly()
    try:
        ensure_comment_cache_schema(conn)
        placeholders = ", ".join(["?"] * len(normalized_comment_ids))
        where = [f"platform = ?", f"comment_id IN ({placeholders})"]
        params: List[object] = [platform, *normalized_comment_ids]
        _append_owner_user_filter(where, params, owner_user_id)
        where_clause = " AND ".join(where)
        try:
            rows = conn.execute(
                f"""
                SELECT
                    comment_id,
                    text,
                    moderation_flagged,
                    moderation_reason,
                    moderation_checked_at
                FROM social_comment_cache
                WHERE {where_clause}
                """,
                params,
            ).fetchall()
        except Exception as exc:
            if "social_comment_cache" in str(exc).lower():
                return {}
            raise
        return {
            str(comment_id): {
                "text": text,
                "moderation_flagged": moderation_flagged,
                "moderation_reason": moderation_reason,
                "moderation_checked_at": moderation_checked_at,
            }
            for comment_id, text, moderation_flagged, moderation_reason, moderation_checked_at in rows
            if comment_id
        }
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


def fetch_instagram_comments_for_queue(
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
                    updated_at,
                    LOWER(COALESCE(status, '')) AS status
                FROM social_comment_cache
                WHERE platform = 'instagram'
                  AND queue_id = ?
                ORDER BY published_at DESC NULLS LAST, updated_at DESC
                LIMIT ?
                """,
                [queue_id, limit],
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
                    "author": author,
                    "username": author,
                    "text": text,
                    "timestamp": published_at,
                    "like_count": like_count,
                    "status": status or "published",
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
