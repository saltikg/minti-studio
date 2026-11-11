"""Ten-minute admin-email batching for customer changes to scheduled clips."""
from __future__ import annotations

import html
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional
from uuid import uuid4

from app.video_shorts.services.db import get_db, get_db_readonly


TABLE = "scheduled_clip_customer_actions"


def record_customer_action(
    *, owner_user_id: str, brand_id: str, video_pk: int, plan_index: int,
    brand_name: str, clip_title: str, action: str, changed_fields: list[str],
    schedules: list[dict[str, Any]], clip_url: str, actor_user_id: str,
) -> bool:
    if not schedules:
        return False
    conn = get_db()
    try:
        conn.execute(
            f"""INSERT INTO {TABLE}
                (id,owner_user_id,brand_id,video_pk,plan_index,brand_name,clip_title,
                 action,changed_fields_json,schedules_json,clip_url,actor_user_id,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)""",
            [str(uuid4()), str(owner_user_id), str(brand_id), int(video_pk), int(plan_index),
             str(brand_name or brand_id), str(clip_title or f"Clip {plan_index}"), str(action),
             json.dumps(sorted(set(changed_fields or []))), json.dumps(schedules, default=str),
             str(clip_url or ""), str(actor_user_id or "")],
        )
        conn.commit()
        return True
    finally:
        conn.close()


def process_due_batches(
    *, now: Optional[datetime] = None,
    sender: Optional[Callable[..., Any]] = None,
) -> int:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    cutoff = now.astimezone(timezone.utc) - timedelta(minutes=10)
    conn = get_db_readonly()
    try:
        rows = conn.execute(
            f"""SELECT id,owner_user_id,brand_id,video_pk,plan_index,brand_name,clip_title,
                       action,changed_fields_json,schedules_json,clip_url,created_at
                FROM {TABLE}
                WHERE sent_at IS NULL AND created_at<=?
                ORDER BY owner_user_id,brand_id,video_pk,plan_index,created_at""",
            [cutoff],
        ).fetchall()
    finally:
        conn.close()
    groups: dict[tuple[str, str, int, int], list[Any]] = {}
    for row in rows:
        groups.setdefault((str(row[1]), str(row[2]), int(row[3]), int(row[4])), []).append(row)
    if not groups:
        return 0
    if sender is None:
        from app.video_shorts.services.email_verification import send_resend_email
        sender = send_resend_email
    sent = 0
    for group_rows in groups.values():
        actions = sorted({str(row[7]) for row in group_rows})
        fields: set[str] = set()
        schedules: list[dict[str, Any]] = []
        for row in group_rows:
            fields.update(json.loads(row[8] or "[]"))
            schedules.extend(json.loads(row[9] or "[]"))
        first = group_rows[0]
        schedule_lines = [
            f"{item.get('platform', 'unknown')}: {item.get('publish_at', 'unknown time')}"
            for item in schedules
        ]
        text = (
            f"Brand: {first[5]}\nClip: {first[6]}\nActions: {', '.join(actions)}\n"
            f"Changed fields: {', '.join(sorted(fields)) or 'none'}\n"
            f"Scheduled: {'; '.join(schedule_lines)}\nLink: {first[10]}"
        )
        sender(
            to_email=(os.getenv("ADMIN_NOTIFICATION_EMAIL") or "info@mintistudio.com").strip(),
            subject=f"Scheduled Short changed by customer: {first[5]}",
            html=f"<pre>{html.escape(text)}</pre>", text=text,
        )
        write = get_db()
        try:
            ids = [row[0] for row in group_rows]
            placeholders = ",".join("?" for _ in ids)
            write.execute(f"UPDATE {TABLE} SET sent_at=CURRENT_TIMESTAMP WHERE id IN ({placeholders})", ids)
            write.commit()
        finally:
            write.close()
        sent += 1
    return sent
