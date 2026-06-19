from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, Optional
from uuid import uuid4

from app.video_shorts.services.db import get_db, table_columns


QUICK_SHORT_TABLE = "shorts_quick_sessions"
STATUS_INPUT = "input"
STATUS_INGESTING = "ingesting"
STATUS_REVIEW = "review"
STATUS_RENDERING = "rendering"
STATUS_DONE = "done"
STATUS_FAILED = "failed"


def _json_column_type(conn) -> str:
    return "JSONB" if getattr(conn, "backend_name", "") == "postgres" else "TEXT"


def _json_value_sql(conn, param_placeholder: str = "?") -> str:
    if getattr(conn, "backend_name", "") == "postgres":
        return f"CAST({param_placeholder} AS JSONB)"
    return param_placeholder


def _serialize(value: Any) -> Optional[str]:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _parse(value: Any) -> Any:
    if value in (None, ""):
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return None


def ensure_quick_short_schema(conn) -> None:
    json_type = _json_column_type(conn)
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {QUICK_SHORT_TABLE} (
            id VARCHAR PRIMARY KEY,
            user_id VARCHAR NOT NULL,
            brand_id VARCHAR,
            source_type VARCHAR NOT NULL,
            upload_kind VARCHAR,
            source_url TEXT,
            source_filename TEXT,
            video_pk INTEGER,
            video_id VARCHAR,
            ingest_job_id VARCHAR,
            render_job_id VARCHAR,
            status VARCHAR NOT NULL DEFAULT 'input',
            clip_start_seconds DOUBLE PRECISION,
            clip_end_seconds DOUBLE PRECISION,
            clip_title TEXT,
            payload_json {json_type},
            result_json {json_type},
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    cols = table_columns(conn, QUICK_SHORT_TABLE)
    extra_columns = [
        ("brand_id", "VARCHAR"),
        ("upload_kind", "VARCHAR"),
        ("source_url", "TEXT"),
        ("source_filename", "TEXT"),
        ("video_pk", "INTEGER"),
        ("video_id", "VARCHAR"),
        ("ingest_job_id", "VARCHAR"),
        ("render_job_id", "VARCHAR"),
        ("status", "VARCHAR NOT NULL DEFAULT 'input'"),
        ("clip_start_seconds", "DOUBLE PRECISION"),
        ("clip_end_seconds", "DOUBLE PRECISION"),
        ("clip_title", "TEXT"),
        ("payload_json", json_type),
        ("result_json", json_type),
        ("created_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
        ("updated_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
    ]
    for column_name, definition in extra_columns:
        if column_name not in cols:
            conn.execute(f"ALTER TABLE {QUICK_SHORT_TABLE} ADD COLUMN {column_name} {definition}")
    try:
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{QUICK_SHORT_TABLE}_user_updated ON {QUICK_SHORT_TABLE}(user_id, updated_at)")
    except Exception:
        pass
    conn.commit()


def _row_to_session(row: Any) -> Dict[str, Any]:
    return {
        "id": str(row[0]),
        "user_id": str(row[1]),
        "brand_id": row[2],
        "source_type": row[3],
        "upload_kind": row[4] or "video",
        "source_url": row[5],
        "source_filename": row[6],
        "video_pk": row[7],
        "video_id": row[8],
        "ingest_job_id": row[9],
        "render_job_id": row[10],
        "status": row[11] or STATUS_INPUT,
        "clip_start_seconds": row[12],
        "clip_end_seconds": row[13],
        "clip_title": row[14],
        "payload": _parse(row[15]) or {},
        "result": _parse(row[16]) or {},
        "created_at": row[17].isoformat() if isinstance(row[17], datetime) else (str(row[17]) if row[17] else None),
        "updated_at": row[18].isoformat() if isinstance(row[18], datetime) else (str(row[18]) if row[18] else None),
    }


def _select_sql() -> str:
    return f"""
        SELECT
            id,
            user_id,
            brand_id,
            source_type,
            upload_kind,
            source_url,
            source_filename,
            video_pk,
            video_id,
            ingest_job_id,
            render_job_id,
            status,
            clip_start_seconds,
            clip_end_seconds,
            clip_title,
            payload_json,
            result_json,
            created_at,
            updated_at
        FROM {QUICK_SHORT_TABLE}
    """


def create_session(
    *,
    user_id: str,
    brand_id: Optional[str],
    source_type: str,
    upload_kind: str = "video",
    source_url: str = "",
    source_filename: str = "",
    payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    conn = get_db()
    try:
        ensure_quick_short_schema(conn)
        session_id = str(uuid4())
        conn.execute(
            f"""
            INSERT INTO {QUICK_SHORT_TABLE} (
                id,
                user_id,
                brand_id,
                source_type,
                upload_kind,
                source_url,
                source_filename,
                status,
                payload_json,
                result_json,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, {_json_value_sql(conn)}, {_json_value_sql(conn)}, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            [
                session_id,
                user_id,
                brand_id,
                source_type,
                upload_kind or "video",
                source_url or "",
                source_filename or "",
                STATUS_INPUT,
                _serialize(payload or {}) or "{}",
                _serialize({}) or "{}",
            ],
        )
        conn.commit()
        row = conn.execute(f"{_select_sql()} WHERE id = ?", [session_id]).fetchone()
        return _row_to_session(row)
    finally:
        conn.close()


def get_session(session_id: str, *, user_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    conn = get_db()
    try:
        ensure_quick_short_schema(conn)
        sql = _select_sql() + " WHERE id = ?"
        params: list[Any] = [session_id]
        if user_id:
            sql += " AND user_id = ?"
            params.append(user_id)
        row = conn.execute(sql, params).fetchone()
        conn.commit()
        return _row_to_session(row) if row else None
    finally:
        conn.close()


def get_latest_session(user_id: str, *, brand_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    conn = get_db()
    try:
        ensure_quick_short_schema(conn)
        sql = _select_sql() + " WHERE user_id = ?"
        params: list[Any] = [user_id]
        if brand_id:
            sql += " AND brand_id = ?"
            params.append(brand_id)
        sql += " ORDER BY updated_at DESC LIMIT 1"
        row = conn.execute(sql, params).fetchone()
        conn.commit()
        return _row_to_session(row) if row else None
    finally:
        conn.close()


def update_session(session_id: str, **updates: Any) -> Optional[Dict[str, Any]]:
    conn = get_db()
    try:
        ensure_quick_short_schema(conn)
        assignments = []
        params: list[Any] = []
        mapping = {
            "brand_id": "brand_id",
            "source_type": "source_type",
            "upload_kind": "upload_kind",
            "source_url": "source_url",
            "source_filename": "source_filename",
            "video_pk": "video_pk",
            "video_id": "video_id",
            "ingest_job_id": "ingest_job_id",
            "render_job_id": "render_job_id",
            "status": "status",
            "clip_start_seconds": "clip_start_seconds",
            "clip_end_seconds": "clip_end_seconds",
            "clip_title": "clip_title",
        }
        for key, column in mapping.items():
            if key in updates:
                assignments.append(f"{column} = ?")
                params.append(updates.get(key))
        if "payload" in updates:
            assignments.append(f"payload_json = {_json_value_sql(conn)}")
            params.append(_serialize(updates.get("payload") or {}) or "{}")
        if "result" in updates:
            assignments.append(f"result_json = {_json_value_sql(conn)}")
            params.append(_serialize(updates.get("result") or {}) or "{}")
        if not assignments:
            return get_session(session_id)
        assignments.append("updated_at = CURRENT_TIMESTAMP")
        params.append(session_id)
        conn.execute(
            f"UPDATE {QUICK_SHORT_TABLE} SET {', '.join(assignments)} WHERE id = ?",
            params,
        )
        conn.commit()
        row = conn.execute(f"{_select_sql()} WHERE id = ?", [session_id]).fetchone()
        return _row_to_session(row) if row else None
    finally:
        conn.close()
