from __future__ import annotations
from app.video_shorts.services import tenant_media

import json
from pathlib import Path
from typing import Any, Iterable

from app.video_shorts.services.db import table_columns
from app.video_shorts.services.storage import is_storage_reference, storage_reference_key


def _plan_entries_for_video_id(video_id: str, video_pk: int) -> list[dict[str, Any]]:
    try:
        from app.video_shorts.routes import generation

        return generation._load_plan_entries(video_id, video_pk=video_pk) or []
    except Exception:
        return []


def _short_key(filename: str) -> str:
    safe = Path(str(filename or "").strip()).name
    return tenant_media.short_key(safe) if safe else ""


def _poster_key(filename: str, poster_filename: str = "") -> str:
    safe_poster = Path(str(poster_filename or "").strip()).name
    safe_clip = Path(str(filename or "").strip()).name
    if safe_poster:
        return tenant_media.poster_key(safe_poster)
    if safe_clip:
        return tenant_media.poster_key(f'{Path(safe_clip).stem}.jpg')
    return ""


def _add_key(keys: set[str], value: Any) -> None:
    raw = str(value or "").strip()
    if not raw:
        return
    if is_storage_reference(raw):
        key = storage_reference_key(raw)
        if key:
            keys.add(key)
        return
    if raw.startswith("s3://"):
        keys.add(raw[len("s3://") :].lstrip("/"))
        return
    if raw.startswith(("http://", "https://", "/")):
        return
    if "/" in raw and not raw.startswith("."):
        keys.add(raw.lstrip("/"))


def guest_storage_keys_for_user(conn, user_id: str) -> set[str]:
    clean_user_id = str(user_id or "").strip()
    keys: set[str] = set()
    if not clean_user_id:
        return keys
    video_cols = table_columns(conn, "youtube_videos")
    if video_cols:
        rows = conn.execute(
            """
            SELECT video_id, video_url, id
            FROM youtube_videos
            WHERE CAST(owner_user_id AS VARCHAR) = ?
            """,
            [clean_user_id],
        ).fetchall()
        for video_id, video_url, video_pk in rows:
            clean_video_id = str(video_id or "").strip()
            _add_key(keys, video_url)
            for suffix in (".mp4", ".mov", ".mkv", ".webm", ".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"):
                keys.add(f"videos/{clean_video_id}{suffix}")
            for entry in _plan_entries_for_video_id(clean_video_id, video_pk):
                clip_filename = str(entry.get("clip_filename") or entry.get("output_filename") or "").strip()
                poster_filename = str(entry.get("poster_filename") or "").strip()
                _add_key(keys, _short_key(clip_filename))
                _add_key(keys, _poster_key(clip_filename, poster_filename))
    generated_cols = table_columns(conn, "shorts_generated_videos")
    if generated_cols:
        select_cols = ["clip_filename", "video_filename"]
        if "poster_filename" in generated_cols:
            select_cols.append("poster_filename")
        user_expr = "user_id" if "user_id" in generated_cols else "owner_user_id"
        if "user_id" in generated_cols and "owner_user_id" in generated_cols:
            user_expr = "COALESCE(user_id, owner_user_id)"
        rows = conn.execute(
            f"""
            SELECT {', '.join(select_cols)}
            FROM shorts_generated_videos
            WHERE CAST({user_expr} AS VARCHAR) = ?
            """,
            [clean_user_id],
        ).fetchall()
        for row in rows:
            clip_filename = str(row[0] or row[1] or "").strip()
            poster_filename = str(row[2] or "").strip() if len(row) > 2 else ""
            _add_key(keys, _short_key(clip_filename))
            _add_key(keys, _poster_key(clip_filename, poster_filename))
    job_cols = table_columns(conn, "shorts_render_jobs")
    if job_cols:
        rows = conn.execute(
            """
            SELECT payload_json, result_json
            FROM shorts_render_jobs
            WHERE CAST(user_id AS VARCHAR) = ?
            """,
            [clean_user_id],
        ).fetchall()
        for raw_payload, raw_result in rows:
            for raw in (raw_payload, raw_result):
                try:
                    payload = raw if isinstance(raw, dict) else json.loads(raw or "{}")
                except Exception:
                    payload = {}
                if not isinstance(payload, dict):
                    continue
                for key in ("source_key", "clip_filename", "output_filename", "poster_filename"):
                    value = payload.get(key)
                    if key == "source_key":
                        _add_key(keys, value)
                    elif key == "poster_filename":
                        _add_key(keys, _poster_key("", str(value or "")))
                    else:
                        _add_key(keys, _short_key(str(value or "")))
    return {key for key in keys if key and not non_guest_source_references(conn, key=key)}


def non_guest_source_references(conn, *, video_id=None, key=None) -> bool:
    """Check current customer references, including legacy keys and stored source URLs."""
    if key is not None and not str(key).startswith("videos/"):
        return False
    source_id = str(video_id or (Path(str(key)).stem if key else ""))
    stored = f"s3://{key}" if key else ""
    return conn.execute(
        """SELECT 1 FROM youtube_videos v
           LEFT JOIN shorts_users u ON CAST(u.id AS VARCHAR) = v.owner_user_id
           WHERE NOT COALESCE(u.is_guest, FALSE)
             AND (v.video_id = ? OR (? <> '' AND v.video_url = ?)) LIMIT 1""",
        [source_id, stored, stored],
    ).fetchone() is not None
