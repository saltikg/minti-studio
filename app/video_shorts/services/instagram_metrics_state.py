from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
from typing import Dict

from app.video_shorts.services.db import get_db, get_db_readonly

logger = logging.getLogger(__name__)

ALERT_KEY = "instagram_metrics_failures"


def _ensure_local_schema(conn) -> None:
    if getattr(conn, "backend_name", "") == "postgres":
        return
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS instagram_metrics_alert_state (
            alert_key VARCHAR PRIMARY KEY,
            last_sent_at TIMESTAMP NOT NULL,
            summary_json TEXT
        )
        """
    )


def mark_instagram_insights_unsyncable(
    queue_id: str,
    *,
    reason_code: str,
    reason: str,
) -> None:
    conn = get_db()
    try:
        conn.execute(
            """
            UPDATE shorts_instagram_queue
            SET insights_status = 'unsyncable',
                insights_status_reason = ?,
                insights_status_updated_at = ?
            WHERE id = ?
            """,
            [
                f"{reason_code}: {reason}"[:1000],
                datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                queue_id,
            ],
        )
        conn.commit()
    finally:
        conn.close()


def fetch_instagram_unsyncable_counts() -> Dict[str, int]:
    conn = get_db_readonly()
    try:
        rows = conn.execute(
            """
            SELECT COALESCE(brand_id, 'unknown') AS brand_id, COUNT(*)
            FROM shorts_instagram_queue
            WHERE insights_status = 'unsyncable'
            GROUP BY 1
            ORDER BY 1
            """
        ).fetchall()
        return {str(brand_id): int(count) for brand_id, count in rows}
    finally:
        conn.close()


def _coerce_utc(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            logger.error("Invalid Instagram metrics alert timestamp: %s", value)
            return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def claim_instagram_metrics_alert(
    summary: Dict[str, object],
    *,
    cooldown_hours: int = 24,
    alert_key: str = ALERT_KEY,
) -> bool:
    conn = get_db()
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=cooldown_hours)
    summary_value = json.dumps(summary, default=str)
    try:
        _ensure_local_schema(conn)
        if getattr(conn, "backend_name", "") == "postgres":
            row = conn.execute(
                """
                INSERT INTO instagram_metrics_alert_state (
                    alert_key, last_sent_at, summary_json
                )
                VALUES (?, ?, CAST(? AS JSONB))
                ON CONFLICT(alert_key) DO UPDATE
                SET last_sent_at = excluded.last_sent_at,
                    summary_json = excluded.summary_json
                WHERE instagram_metrics_alert_state.last_sent_at <= ?
                RETURNING alert_key
                """,
                [alert_key, now, summary_value, cutoff],
            ).fetchone()
            conn.commit()
            return bool(row)
        row = conn.execute(
            "SELECT last_sent_at FROM instagram_metrics_alert_state WHERE alert_key = ?",
            [alert_key],
        ).fetchone()
        last_sent_at = _coerce_utc(row[0]) if row else None
        if last_sent_at is not None and last_sent_at > cutoff:
            return False
        conn.execute(
            """
            INSERT INTO instagram_metrics_alert_state (alert_key, last_sent_at, summary_json)
            VALUES (?, ?, ?)
            ON CONFLICT(alert_key) DO UPDATE
            SET last_sent_at = excluded.last_sent_at,
                summary_json = excluded.summary_json
            """,
            [alert_key, now, summary_value],
        )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        logger.exception("Instagram metrics alert cooldown could not be claimed")
        return False
    finally:
        conn.close()
