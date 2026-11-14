from __future__ import annotations

from app.video_shorts.services.db import table_columns


SYSTEM_ACTOR = "system"
ALREADY_IMPORTED_REASON = "already imported"


def mark_imported_watcher_video_processed(
    conn,
    *,
    owner_user_id: str,
    brand_id: str,
    youtube_video_id: str,
    decided_by: str = SYSTEM_ACTOR,
    reason: str = ALREADY_IMPORTED_REASON,
) -> int:
    """Resolve a pending watcher upload after the same tenant imports it."""
    watcher_columns = table_columns(conn, "autopilot_channel_videos")
    video_columns = table_columns(conn, "youtube_videos")
    if not {"decided_by", "decided_at", "decision_reason"}.issubset(watcher_columns):
        return 0
    if not {"owner_user_id", "brand_id", "video_id", "deleted_at"}.issubset(video_columns):
        return 0
    clean_video_id = str(youtube_video_id or "").strip()
    if not clean_video_id:
        return 0
    row = conn.execute(
        """
        UPDATE autopilot_channel_videos cv
        SET status = 'processed',
            decided_by = ?,
            decided_at = CURRENT_TIMESTAMP,
            decision_reason = ?
        WHERE CAST(cv.user_id AS VARCHAR) = CAST(? AS VARCHAR)
          AND CAST(cv.brand_id AS VARCHAR) = CAST(? AS VARCHAR)
          AND cv.youtube_video_id = ?
          AND cv.status = 'new'
          AND EXISTS (
              SELECT 1
              FROM youtube_videos yv
              WHERE CAST(yv.owner_user_id AS VARCHAR) = CAST(cv.user_id AS VARCHAR)
                AND CAST(yv.brand_id AS VARCHAR) = CAST(cv.brand_id AS VARCHAR)
                AND yv.video_id = cv.youtube_video_id
                AND yv.deleted_at IS NULL
          )
        RETURNING cv.id
        """,
        [
            str(decided_by or SYSTEM_ACTOR).strip() or SYSTEM_ACTOR,
            str(reason or ALREADY_IMPORTED_REASON).strip() or ALREADY_IMPORTED_REASON,
            str(owner_user_id),
            str(brand_id),
            clean_video_id,
        ],
    ).fetchall()
    return len(row)
