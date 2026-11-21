"""Shared mutations for pending Instagram and Facebook schedule rows."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from app.video_shorts.services.social_queue_identity import queue_identity_filter


TABLES = {"instagram": "shorts_instagram_queue", "facebook": "shorts_facebook_queue"}
ACTIVE_STATUSES = ("pending", "retry", "uploading", "queued", "scheduled")


def cancel_pending_social_schedules(
    conn,
    *,
    platform: str,
    owner_user_id: str,
    brand_id: Optional[str],
    queue_id: Optional[str] = None,
    video_id: Optional[str] = None,
    plan_index: Optional[int] = None,
    clip_filename: Optional[str] = None,
) -> int:
    """Cancel pending rows in one tenant using the same predicate for every caller."""
    table_name = TABLES.get(str(platform or "").strip().lower())
    if not table_name:
        raise ValueError("Unsupported social platform")
    identity_sql, identity_params = queue_identity_filter(owner_user_id, brand_id)
    clauses = [identity_sql, "lower(coalesce(status, '')) IN ('pending','retry','uploading','queued','scheduled')"]
    params: list[Any] = [*identity_params]
    if queue_id:
        clauses.append("CAST(id AS VARCHAR) = CAST(? AS VARCHAR)")
        params.append(str(queue_id))
    else:
        if not video_id or plan_index is None or not clip_filename:
            raise ValueError("A queue id or complete clip identity is required")
        clauses.extend(["video_id = ?", "plan_index = ?", "clip_filename = ?"])
        params.extend([str(video_id), str(plan_index), str(clip_filename)])
    params.insert(0, datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"))
    result = conn.execute(
        f"""
        UPDATE {table_name}
           SET status = 'canceled',
               status_detail = 'Canceled by user before publish.',
               updated_at = ?
         WHERE {' AND '.join(clauses)}
        """,
        params,
    )
    return int(getattr(result, "rowcount", 0) or 0)
