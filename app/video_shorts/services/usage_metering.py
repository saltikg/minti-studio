from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import uuid4
from typing import Any, Dict, Optional

from app.video_shorts.config import (
    DEFAULT_STORAGE_PLANS,
    DEFAULT_USER_PLAN_ID,
    DEFAULT_USER_STORAGE_LIMIT,
)
from app.video_shorts.services.db import ensure_storage_user_schema, get_db, table_columns


USAGE_TABLE = "shorts_user_usage"
PLANS_TABLE = "shorts_storage_plans"
TRANSCRIPTION_EVENTS_TABLE = "shorts_transcription_usage_events"


def _utc_today() -> date:
    return datetime.now(timezone.utc).date()


def _month_start(day: Optional[date] = None) -> date:
    current = day or _utc_today()
    return current.replace(day=1)


def _next_month_start(day: date) -> date:
    if day.month == 12:
        return date(day.year + 1, 1, 1)
    return date(day.year, day.month + 1, 1)


def _to_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if value in (None, ""):
        return Decimal("0")
    return Decimal(str(value))


def _decimal_to_number(value: Any) -> float | int:
    dec = _to_decimal(value)
    if dec == dec.to_integral():
        return int(dec)
    return float(round(dec, 2))


def _current_period_row_sql() -> str:
    return f"""
        SELECT
            u.period_start,
            u.exports_used,
            u.transcription_minutes_used,
            p.plan_id,
            COALESCE(p.name, p.label) AS plan_name,
            p.price_monthly,
            COALESCE(p.storage_quota_bytes, p.quota_bytes) AS storage_quota_bytes,
            p.monthly_export_limit,
            p.monthly_transcription_minutes
        FROM {USAGE_TABLE} u
        LEFT JOIN shorts_users su ON su.id = u.user_id
        LEFT JOIN {PLANS_TABLE} p ON p.plan_id = su.plan_id
        WHERE u.user_id = ?
          AND u.period_start = ?
    """


def ensure_usage_metering_schema(conn) -> None:
    ensure_storage_user_schema(conn)

    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {PLANS_TABLE} (
            plan_id VARCHAR PRIMARY KEY,
            label VARCHAR NOT NULL,
            quota_bytes BIGINT NOT NULL,
            sort_order INTEGER DEFAULT 0
        )
        """
    )

    plan_columns = table_columns(conn, PLANS_TABLE)
    extra_plan_columns = [
        ("name", "VARCHAR"),
        ("price_monthly", "NUMERIC DEFAULT 0"),
        ("price_yearly", "NUMERIC DEFAULT 0"),
        ("storage_quota_bytes", "BIGINT"),
        ("monthly_export_limit", "INTEGER"),
        ("monthly_transcription_minutes", "NUMERIC"),
        ("render_priority", "INTEGER DEFAULT 0"),
        ("max_concurrent_jobs", "INTEGER DEFAULT 1"),
        ("is_active", "BOOLEAN DEFAULT TRUE"),
    ]
    for column_name, definition in extra_plan_columns:
        if column_name not in plan_columns:
            conn.execute(f"ALTER TABLE {PLANS_TABLE} ADD COLUMN {column_name} {definition}")

    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {USAGE_TABLE} (
            user_id UUID NOT NULL,
            period_start DATE NOT NULL,
            exports_used INTEGER DEFAULT 0,
            transcription_minutes_used NUMERIC DEFAULT 0,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, period_start)
        )
        """
    )

    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {TRANSCRIPTION_EVENTS_TABLE} (
            id VARCHAR PRIMARY KEY,
            user_id VARCHAR NOT NULL,
            video_id VARCHAR NOT NULL,
            video_title VARCHAR,
            minutes NUMERIC DEFAULT 0,
            period_start DATE NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    usage_columns = table_columns(conn, USAGE_TABLE)
    extra_usage_columns = [
        ("exports_used", "INTEGER DEFAULT 0"),
        ("transcription_minutes_used", "NUMERIC DEFAULT 0"),
        ("updated_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
    ]
    for column_name, definition in extra_usage_columns:
        if column_name not in usage_columns:
            conn.execute(f"ALTER TABLE {USAGE_TABLE} ADD COLUMN {column_name} {definition}")

    event_columns = table_columns(conn, TRANSCRIPTION_EVENTS_TABLE)
    extra_event_columns = [
        ("user_id", "VARCHAR"),
        ("video_id", "VARCHAR"),
        ("video_title", "VARCHAR"),
        ("minutes", "NUMERIC DEFAULT 0"),
        ("period_start", "DATE"),
        ("created_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
    ]
    for column_name, definition in extra_event_columns:
        if column_name not in event_columns:
            conn.execute(f"ALTER TABLE {TRANSCRIPTION_EVENTS_TABLE} ADD COLUMN {column_name} {definition}")

    for plan in DEFAULT_STORAGE_PLANS:
        conn.execute(
            f"""
            INSERT INTO {PLANS_TABLE} (
                plan_id,
                label,
                quota_bytes,
                sort_order,
                name,
                price_monthly,
                price_yearly,
                storage_quota_bytes,
                monthly_export_limit,
                monthly_transcription_minutes,
                render_priority,
                max_concurrent_jobs,
                is_active
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(plan_id)
            DO UPDATE SET
                label = excluded.label,
                quota_bytes = excluded.quota_bytes,
                sort_order = excluded.sort_order,
                name = excluded.name,
                price_monthly = excluded.price_monthly,
                price_yearly = excluded.price_yearly,
                storage_quota_bytes = excluded.storage_quota_bytes,
                monthly_export_limit = excluded.monthly_export_limit,
                monthly_transcription_minutes = excluded.monthly_transcription_minutes,
                render_priority = excluded.render_priority,
                max_concurrent_jobs = excluded.max_concurrent_jobs,
                is_active = excluded.is_active
            """,
            [
                plan["plan_id"],
                plan["label"],
                plan["quota_bytes"],
                plan.get("sort_order", 0),
                plan["label"],
                plan.get("price_monthly", 0),
                plan.get("price_yearly", plan.get("price_monthly", 0) * 12),
                plan["quota_bytes"],
                plan.get("monthly_export_limit"),
                plan.get("monthly_transcription_minutes"),
                int(plan.get("render_priority", 0) or 0),
                int(plan.get("max_concurrent_jobs", 1) or 1),
                bool(plan.get("is_active", True)),
            ],
        )
    conn.commit()


def _format_storage_gb(quota_bytes: Any) -> str:
    size_gb = int(round((int(quota_bytes or 0)) / float(1024 ** 3)))
    return f"{size_gb} GB storage"


def _format_transcription_hours(minutes: Any) -> str:
    hours = int(round((int(minutes or 0)) / 60.0))
    return f"{hours}h transcription"


def load_storage_plan_catalog(conn=None, plan_ids: Optional[list[str]] = None) -> list[dict]:
    owns_connection = conn is None
    if owns_connection:
        conn = get_db()
    ensure_usage_metering_schema(conn)
    plan_rows = conn.execute(
        f"""
        SELECT
            plan_id,
            COALESCE(name, label) AS name,
            COALESCE(storage_quota_bytes, quota_bytes) AS storage_quota_bytes,
            price_monthly,
            price_yearly,
            monthly_export_limit,
            monthly_transcription_minutes
        FROM {PLANS_TABLE}
        WHERE COALESCE(is_active, TRUE) = TRUE
        ORDER BY sort_order, label
        """
    ).fetchall()
    allowed_plan_ids = [plan_id for plan_id in (plan_ids or []) if plan_id]
    plans: list[dict] = []
    for row in plan_rows:
        if allowed_plan_ids and row[0] not in allowed_plan_ids:
            continue
        monthly_price = int(row[3] or 0)
        yearly_price = int(row[4] or (monthly_price * 12))
        monthly_compare = monthly_price * 12
        quota_bytes = int(row[2] or 0)
        transcription_minutes = int(row[6] or 0)
        plans.append(
            {
                "plan_id": row[0],
                "label": row[1],
                "quota_bytes": quota_bytes,
                "quota_label": _format_storage_gb(quota_bytes).replace(" storage", ""),
                "price_monthly": monthly_price,
                "monthly_price": monthly_price,
                "price_yearly": yearly_price,
                "yearly_price": yearly_price,
                "yearly_compare_monthly": monthly_compare,
                "monthly_export_limit": int(row[5] or 0),
                "monthly_transcription_minutes": transcription_minutes,
                "transcription_limit_label": f"{int(transcription_minutes / 60)}h",
                "monthly_note": "forever" if monthly_price == 0 else "per month",
                "yearly_note": "forever" if yearly_price == 0 else f"${monthly_compare} if monthly",
                "lines": [
                    f"{int(row[5] or 0)} shorts / month",
                    _format_transcription_hours(transcription_minutes),
                    _format_storage_gb(quota_bytes),
                ],
                "is_featured": str(row[0] or "") == "plan_10gb",
                "button_label": "Start free" if monthly_price == 0 else "Get started",
            }
        )
    if owns_connection and conn is not None:
        conn.close()
    if allowed_plan_ids:
        plan_lookup = {plan["plan_id"]: plan for plan in plans}
        return [plan_lookup[plan_id] for plan_id in allowed_plan_ids if plan_id in plan_lookup]
    return plans


def _ensure_usage_row(conn, user_id: str, period_start: date) -> None:
    conn.execute(
        f"""
        INSERT INTO {USAGE_TABLE} (
            user_id,
            period_start,
            exports_used,
            transcription_minutes_used,
            updated_at
        )
        VALUES (?, ?, 0, 0, CURRENT_TIMESTAMP)
        ON CONFLICT(user_id, period_start) DO NOTHING
        """,
        [user_id, period_start],
    )


def _fetch_plan_and_usage(conn, user_id: str, period_start: date) -> Dict[str, Any]:
    row = conn.execute(_current_period_row_sql(), [user_id, period_start]).fetchone()
    if row:
        return {
            "period_start": row[0],
            "exports_used": int(row[1] or 0),
            "transcription_minutes_used": _to_decimal(row[2] or 0),
            "plan_id": row[3] or DEFAULT_USER_PLAN_ID,
            "plan_name": row[4] or "Starter",
            "price_monthly": _decimal_to_number(row[5] or 0),
            "storage_quota_bytes": int(row[6] or DEFAULT_USER_STORAGE_LIMIT),
            "monthly_export_limit": None if row[7] is None else int(row[7]),
            "monthly_transcription_minutes": None if row[8] is None else _decimal_to_number(row[8]),
        }

    user_row = conn.execute(
        f"""
        SELECT
            su.plan_id,
            COALESCE(p.name, p.label),
            p.price_monthly,
            COALESCE(p.storage_quota_bytes, p.quota_bytes),
            p.monthly_export_limit,
            p.monthly_transcription_minutes
        FROM shorts_users su
        LEFT JOIN {PLANS_TABLE} p ON p.plan_id = su.plan_id
        WHERE su.id = ?
        """,
        [user_id],
    ).fetchone()
    plan_id = DEFAULT_USER_PLAN_ID
    plan_name = "Starter"
    price_monthly = 0
    storage_quota_bytes = DEFAULT_USER_STORAGE_LIMIT
    monthly_export_limit = None
    monthly_transcription_minutes = None
    if user_row:
        plan_id = user_row[0] or DEFAULT_USER_PLAN_ID
        plan_name = user_row[1] or plan_name
        price_monthly = _decimal_to_number(user_row[2] or 0)
        storage_quota_bytes = int(user_row[3] or DEFAULT_USER_STORAGE_LIMIT)
        monthly_export_limit = None if user_row[4] is None else int(user_row[4])
        monthly_transcription_minutes = None if user_row[5] is None else _decimal_to_number(user_row[5])
    return {
        "period_start": period_start,
        "exports_used": 0,
        "transcription_minutes_used": Decimal("0"),
        "plan_id": plan_id,
        "plan_name": plan_name,
        "price_monthly": price_monthly,
        "storage_quota_bytes": storage_quota_bytes,
        "monthly_export_limit": monthly_export_limit,
        "monthly_transcription_minutes": monthly_transcription_minutes,
    }


def _storage_snapshot(conn, user_id: str) -> Dict[str, int]:
    row = conn.execute(
        f"""
        SELECT
            su.custom_limit_bytes,
            COALESCE(p.storage_quota_bytes, p.quota_bytes),
            COALESCE(SUM(a.size_bytes), 0) AS used_bytes
        FROM shorts_users su
        LEFT JOIN {PLANS_TABLE} p ON p.plan_id = su.plan_id
        LEFT JOIN shorts_storage_assets a
          ON a.user_id = su.id
         AND (a.status = 'active' OR a.status IS NULL)
        WHERE su.id = ?
        GROUP BY su.custom_limit_bytes, COALESCE(p.storage_quota_bytes, p.quota_bytes)
        """,
        [user_id],
    ).fetchone()
    if not row:
        return {"used_bytes": 0, "quota_bytes": DEFAULT_USER_STORAGE_LIMIT}
    return {
        "used_bytes": int(row[2] or 0),
        "quota_bytes": int(row[0] or row[1] or DEFAULT_USER_STORAGE_LIMIT),
    }


def _fetch_usage_history(conn, user_id: str, current_period_start: date, limit: int = 6) -> list[Dict[str, Any]]:
    rows = conn.execute(
        f"""
        SELECT period_start, exports_used, transcription_minutes_used
        FROM {USAGE_TABLE}
        WHERE user_id = ?
          AND period_start < ?
        ORDER BY period_start DESC
        LIMIT ?
        """,
        [user_id, current_period_start, max(1, int(limit))],
    ).fetchall()
    history: list[Dict[str, Any]] = []
    for row in rows:
        period_start = row[0]
        history.append(
            {
                "period_start": period_start.isoformat() if hasattr(period_start, "isoformat") else str(period_start),
                "exports_used": int(row[1] or 0),
                "transcription_used_minutes": _decimal_to_number(row[2] or 0),
            }
        )
    return history


def _fetch_transcription_events(conn, user_id: str, period_start: date, limit: int = 100) -> list[Dict[str, Any]]:
    rows = conn.execute(
        f"""
        SELECT video_id, video_title, minutes, created_at
        FROM {TRANSCRIPTION_EVENTS_TABLE}
        WHERE user_id = ?
          AND period_start = ?
        ORDER BY created_at ASC, video_title ASC, video_id ASC
        LIMIT ?
        """,
        [user_id, period_start, max(1, int(limit))],
    ).fetchall()
    events: list[Dict[str, Any]] = []
    for row in rows:
        created_at = row[3]
        events.append(
            {
                "video_id": row[0],
                "video_title": row[1] or row[0] or "Untitled video",
                "minutes": _decimal_to_number(row[2] or 0),
                "created_at": created_at.isoformat() if hasattr(created_at, "isoformat") else str(created_at),
            }
        )
    return events


def _parse_event_anchor_date(value: Any) -> Optional[date]:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    if hasattr(value, "date"):
        try:
            return value.date()
        except Exception:
            pass
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.date()
    except Exception:
        try:
            return datetime.strptime(text[:10], "%Y-%m-%d").date()
        except Exception:
            return None


def _backfill_transcription_events(conn, user_id: str, period_start: date, limit: int = 100) -> list[Dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT video_id, title, duration_seconds, published_at, downloaded_at, last_checked_at
        FROM youtube_videos
        WHERE owner_user_id = ?
          AND COALESCE(duration_seconds, 0) > 0
          AND lower(COALESCE(transcript_status, '')) = 'done'
        ORDER BY COALESCE(published_at, downloaded_at, last_checked_at) ASC, title ASC, video_id ASC
        """,
        [user_id],
    ).fetchall()
    events: list[Dict[str, Any]] = []
    for row in rows:
        anchor_date = _parse_event_anchor_date(row[3] or row[4] or row[5])
        if not anchor_date or _month_start(anchor_date) != period_start:
            continue
        duration_seconds = row[2]
        try:
            seconds = float(duration_seconds or 0)
        except Exception:
            seconds = 0.0
        minutes = round(seconds / 60.0, 2) if seconds > 0 else 0.0
        events.append(
            {
                "video_id": row[0],
                "video_title": row[1] or row[0] or "Untitled video",
                "minutes": _decimal_to_number(minutes),
                "created_at": (row[3] or row[4] or row[5]).isoformat() if hasattr((row[3] or row[4] or row[5]), "isoformat") else str((row[3] or row[4] or row[5]) or ""),
            }
        )
        if len(events) >= max(1, int(limit)):
            break
    return events


def get_current_period(user_id: str) -> Dict[str, Any]:
    conn = get_db()
    period_start = _month_start()
    try:
        ensure_usage_metering_schema(conn)
        _ensure_usage_row(conn, user_id, period_start)
        snapshot = _fetch_plan_and_usage(conn, user_id, period_start)
        conn.commit()
        return snapshot
    finally:
        conn.close()


def increment_exports(user_id: str, n: int = 1) -> Dict[str, Any]:
    conn = get_db()
    period_start = _month_start()
    try:
        ensure_usage_metering_schema(conn)
        _ensure_usage_row(conn, user_id, period_start)
        conn.execute(
            f"""
            UPDATE {USAGE_TABLE}
            SET exports_used = exports_used + ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE user_id = ?
              AND period_start = ?
            """,
            [max(0, int(n)), user_id, period_start],
        )
        snapshot = _fetch_plan_and_usage(conn, user_id, period_start)
        conn.commit()
        return snapshot
    finally:
        conn.close()


def add_transcription_minutes(
    user_id: str,
    minutes: float | int | Decimal,
    *,
    video_id: Optional[str] = None,
    video_title: Optional[str] = None,
    event_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    amount = _to_decimal(minutes)
    if amount <= 0:
        return get_current_period(user_id)
    conn = get_db()
    period_start = _month_start()
    try:
        ensure_usage_metering_schema(conn)
        _ensure_usage_row(conn, user_id, period_start)
        if video_id:
            occurred_at = event_at or datetime.now(timezone.utc)
            event_period = _month_start(occurred_at.date())
            conn.execute(
                f"""
                INSERT INTO {TRANSCRIPTION_EVENTS_TABLE} (
                    id,
                    user_id,
                    video_id,
                    video_title,
                    minutes,
                    period_start,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    str(uuid4()),
                    user_id,
                    video_id,
                    video_title or video_id,
                    amount,
                    event_period,
                    occurred_at,
                ],
            )
        conn.execute(
            f"""
            UPDATE {USAGE_TABLE}
            SET transcription_minutes_used = transcription_minutes_used + ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE user_id = ?
              AND period_start = ?
            """,
            [amount, user_id, period_start],
        )
        snapshot = _fetch_plan_and_usage(conn, user_id, period_start)
        conn.commit()
        return snapshot
    finally:
        conn.close()


def check_export_allowed(user_id: str) -> Dict[str, Any]:
    conn = get_db()
    period_start = _month_start()
    try:
        ensure_usage_metering_schema(conn)
        _ensure_usage_row(conn, user_id, period_start)
        snapshot = _fetch_plan_and_usage(conn, user_id, period_start)
        conn.commit()
    finally:
        conn.close()
    limit = snapshot["monthly_export_limit"]
    used = snapshot["exports_used"]
    remaining = None if limit is None else max(limit - used, 0)
    return {"allowed": limit is None or used < limit, "remaining": remaining}


def reserve_export(user_id: str, n: int = 1) -> Dict[str, Any]:
    amount = max(0, int(n))
    conn = get_db()
    period_start = _month_start()
    try:
        ensure_usage_metering_schema(conn)
        _ensure_usage_row(conn, user_id, period_start)
        updated_row = conn.execute(
            f"""
            UPDATE {USAGE_TABLE}
            SET exports_used = {USAGE_TABLE}.exports_used + ?,
                updated_at = CURRENT_TIMESTAMP
            FROM shorts_users u
            LEFT JOIN {PLANS_TABLE} p ON p.plan_id = u.plan_id
            WHERE {USAGE_TABLE}.user_id = u.id
              AND {USAGE_TABLE}.user_id = ?
              AND {USAGE_TABLE}.period_start = ?
              AND u.id = ?
              AND (
                    p.monthly_export_limit IS NULL
                 OR {USAGE_TABLE}.exports_used + ? <= p.monthly_export_limit
              )
            RETURNING {USAGE_TABLE}.exports_used
            """,
            [amount, user_id, period_start, user_id, amount],
        ).fetchone()
        snapshot = _fetch_plan_and_usage(conn, user_id, period_start)
        conn.commit()
    finally:
        conn.close()
    limit = snapshot["monthly_export_limit"]
    used = snapshot["exports_used"]
    remaining = None if limit is None else max(limit - used, 0)
    allowed = amount == 0 or updated_row is not None
    return {"allowed": allowed, "remaining": remaining}


def release_export(user_id: str, n: int = 1) -> Dict[str, Any]:
    amount = max(0, int(n))
    conn = get_db()
    period_start = _month_start()
    try:
        ensure_usage_metering_schema(conn)
        _ensure_usage_row(conn, user_id, period_start)
        conn.execute(
            f"""
            UPDATE {USAGE_TABLE}
            SET exports_used = CASE
                    WHEN exports_used >= ? THEN exports_used - ?
                    ELSE 0
                END,
                updated_at = CURRENT_TIMESTAMP
            WHERE user_id = ?
              AND period_start = ?
            """,
            [amount, amount, user_id, period_start],
        )
        snapshot = _fetch_plan_and_usage(conn, user_id, period_start)
        conn.commit()
        return snapshot
    finally:
        conn.close()


def finalize_export(user_id: str) -> Dict[str, Any]:
    return get_current_period(user_id)


def get_usage_snapshot(user_id: str) -> Dict[str, Any]:
    conn = get_db()
    period_start = _month_start()
    try:
        ensure_usage_metering_schema(conn)
        _ensure_usage_row(conn, user_id, period_start)
        usage = _fetch_plan_and_usage(conn, user_id, period_start)
        usage_history = _fetch_usage_history(conn, user_id, period_start)
        for item in usage_history:
            try:
                item_period_start = date.fromisoformat(str(item["period_start"]))
            except Exception:
                item_period_start = period_start
            item_events = _fetch_transcription_events(conn, user_id, item_period_start)
            if not item_events:
                item_events = _backfill_transcription_events(conn, user_id, item_period_start)
            item["transcription_events"] = item_events
        current_events = _fetch_transcription_events(conn, user_id, period_start)
        if not current_events:
            current_events = _backfill_transcription_events(conn, user_id, period_start)
        storage = _storage_snapshot(conn, user_id)
        conn.commit()
    finally:
        conn.close()

    reset_at = _next_month_start(period_start)
    return {
        "plan": {
            "id": usage["plan_id"],
            "name": usage["plan_name"],
            "price_monthly": usage["price_monthly"],
        },
        "period": {
            "start": period_start.isoformat(),
            "reset_at": reset_at.isoformat(),
        },
        "storage": {
            "used_bytes": storage["used_bytes"],
            "quota_bytes": storage["quota_bytes"],
        },
        "exports": {
            "used": usage["exports_used"],
            "limit": usage["monthly_export_limit"],
        },
        "transcription": {
            "used_minutes": _decimal_to_number(usage["transcription_minutes_used"]),
            "limit_minutes": usage["monthly_transcription_minutes"],
        },
        "transcription_events": current_events,
        "previous_months": usage_history,
    }
