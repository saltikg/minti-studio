from __future__ import annotations

import base64
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional
from uuid import uuid4
from zoneinfo import ZoneInfo

try:
    import yt_dlp
except ImportError:  # pragma: no cover
    yt_dlp = None

from app.video_shorts.config import FFMPEG_BIN, OPENAI_MODEL, _openai_client
from app.video_shorts.services.db import get_db, table_columns
from app.video_shorts.services.discovery_blocklist import discovery_lead_blocked_sql
from app.video_shorts.services.storage import get_media_storage
from app.video_shorts.youtube_api import _parse_duration_iso8601, _youtube_get_json, fetch_playlist_items_batch


logger = logging.getLogger(__name__)
PACIFIC_TZ = ZoneInfo("America/Los_Angeles")
REVIEW_TABLE = "discovery_lead_frame_reviews"
DECISION_TABLE = "discovery_lead_review_decisions"
FRAME_PERCENTAGES = (10, 30, 50, 70, 90)
MIN_DURATION_SECONDS = 5 * 60
MAX_DURATION_SECONDS = 60 * 60
LEAD_TIMEOUT_SECONDS = 90
CACHE_DAYS = 14
FRAME_WIDTH = 240
FRAME_TARGET_BYTES = 15 * 1024
VISION_PROMPT = """Inspect these five real frames from one YouTube video. Is the same person talking to camera in most frames? Interviews and podcasts with visible speakers count. Slides-only, b-roll, faceless videos, gameplay, and crafts close-ups do not.

Return strict JSON only: {"label":"PROMOTE|DON'T PROMOTE","reason":"one short reason"}."""


def _safe_error(error: Any) -> str:
    message = " ".join(str(error or "Frame generation failed").split())
    message = re.sub(r"https?://\S+", "[redacted-url]", message)
    message = re.sub(r"(?i)(proxy|authorization|cookie)(?:=|:)[^\s,;]+", r"\1=[redacted]", message)
    return message[:800]


def _error_code(error: Any) -> str:
    message = str(error or "").lower()
    if "sign in to confirm" in message or "not a bot" in message:
        return "bot_check"
    if "timeout" in message or "timed out" in message or "exceeded" in message:
        return "timeout"
    return "extraction"


def _public_error(error_code: str) -> str:
    return {
        "bot_check": "Frames unavailable (bot check)",
        "timeout": "Frames unavailable (timeout)",
    }.get(str(error_code or ""), "Frames unavailable")


def ensure_frame_review_schema(conn) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {REVIEW_TABLE} (
            lead_id BIGINT PRIMARY KEY,
            status VARCHAR NOT NULL DEFAULT 'pending',
            video_id VARCHAR,
            video_title TEXT,
            duration_seconds INTEGER,
            published_at TIMESTAMP,
            frame_keys TEXT,
            frame_timestamps TEXT,
            storage_backend VARCHAR,
            size_bytes BIGINT NOT NULL DEFAULT 0,
            elapsed_seconds DOUBLE PRECISION,
            ai_label VARCHAR,
            ai_reason TEXT,
            ai_model VARCHAR,
            ai_input_tokens INTEGER NOT NULL DEFAULT 0,
            ai_output_tokens INTEGER NOT NULL DEFAULT 0,
            ai_cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0,
            attempts INTEGER NOT NULL DEFAULT 0,
            error TEXT,
            started_at TIMESTAMP,
            completed_at TIMESTAMP,
            expires_at TIMESTAMP,
            retry_after TIMESTAMP,
            error_code VARCHAR,
            frame_width INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {DECISION_TABLE} (
            id VARCHAR PRIMARY KEY,
            lead_id BIGINT NOT NULL,
            ai_label VARCHAR,
            my_action VARCHAR NOT NULL,
            decided_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def _json_value(value: Any, fallback: Any) -> Any:
    if isinstance(value, type(fallback)):
        return value
    try:
        parsed = json.loads(str(value or ""))
        return parsed if isinstance(parsed, type(fallback)) else fallback
    except Exception:
        return fallback


def _ready_predicate(alias: str = "d") -> str:
    blocked = discovery_lead_blocked_sql(alias)
    return f"""
        {alias}.icp_fit IS TRUE
        AND COALESCE({alias}.creator_email, '') <> ''
        AND COALESCE(CAST({alias}.autopilot_lead_id AS VARCHAR), '') = ''
        AND COALESCE({alias}.status, '') NOT IN ('already_lead', 'dismissed')
        AND NOT ({blocked})
        AND NOT EXISTS (
            SELECT 1 FROM autopilot_leads al
            WHERE al.youtube_channel_id = {alias}.youtube_channel_id
        )
    """


def enqueue_promotable_frame_reviews(*, limit: int = 100) -> int:
    conn = get_db()
    try:
        ensure_frame_review_schema(conn)
        conn.execute(
            f"""
            UPDATE {REVIEW_TABLE}
            SET status = 'pending', error = 'recovered_stale_processing', updated_at = CURRENT_TIMESTAMP
            WHERE status = 'processing'
              AND started_at < CURRENT_TIMESTAMP - INTERVAL '10 minutes'
            """
        )
        conn.execute(
            f"""
            UPDATE {REVIEW_TABLE} r
            SET status = 'pending', error = NULL, updated_at = CURRENT_TIMESTAMP
            FROM discovery_leads d
            WHERE d.id = r.lead_id
              AND r.status = 'expired'
              AND {_ready_predicate('d')}
            """
        )
        result = conn.execute(
            f"""
            INSERT INTO {REVIEW_TABLE} (lead_id, status, created_at, updated_at)
            SELECT d.id, 'pending', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            FROM discovery_leads d
            WHERE {_ready_predicate('d')}
              AND NOT EXISTS (SELECT 1 FROM {REVIEW_TABLE} r WHERE r.lead_id = d.id)
            ORDER BY d.last_discovered_at DESC NULLS LAST, d.id DESC
            LIMIT ?
            ON CONFLICT (lead_id) DO NOTHING
            """,
            [max(1, min(1000, int(limit or 100)))],
        )
        inserted = max(0, int(getattr(result, "rowcount", 0) or 0))
        conn.commit()
        return inserted
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def cleanup_expired_frame_reviews(*, limit: int = 50) -> int:
    conn = get_db()
    cleaned = 0
    try:
        ensure_frame_review_schema(conn)
        rows = conn.execute(
            f"""
            SELECT lead_id, frame_keys, storage_backend
            FROM {REVIEW_TABLE}
            WHERE status = 'completed' AND expires_at <= CURRENT_TIMESTAMP
            ORDER BY expires_at ASC
            LIMIT ?
            """,
            [max(1, min(500, int(limit or 50)))],
        ).fetchall()
        for lead_id, raw_keys, backend in rows:
            storage = get_media_storage(str(backend or "local"))
            for key in _json_value(raw_keys, []):
                try:
                    storage.delete(str(key))
                except Exception:
                    logger.warning("Could not delete expired discovery frame key=%s", key, exc_info=True)
            conn.execute(
                f"""
                UPDATE {REVIEW_TABLE}
                SET status = 'expired', frame_keys = NULL, frame_timestamps = NULL,
                    size_bytes = 0, updated_at = CURRENT_TIMESTAMP
                WHERE lead_id = ? AND status = 'completed'
                """,
                [lead_id],
            )
            cleaned += 1
        conn.commit()
        return cleaned
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _claim_next_review() -> Optional[Dict[str, Any]]:
    conn = get_db()
    try:
        ensure_frame_review_schema(conn)
        row = conn.execute(
            f"""
            WITH candidate AS (
                SELECT r.lead_id
                FROM {REVIEW_TABLE} r
                JOIN discovery_leads d ON d.id = r.lead_id
                WHERE (r.status = 'pending' OR (r.status = 'retry_wait' AND r.retry_after <= CURRENT_TIMESTAMP))
                  AND {_ready_predicate('d')}
                ORDER BY d.last_discovered_at DESC NULLS LAST, d.id DESC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            UPDATE {REVIEW_TABLE} r
            SET status = 'processing', attempts = attempts + 1,
                started_at = CURRENT_TIMESTAMP, error = NULL, updated_at = CURRENT_TIMESTAMP
            FROM candidate
            WHERE r.lead_id = candidate.lead_id
            RETURNING r.lead_id, r.attempts
            """
        ).fetchone()
        if not row:
            conn.rollback()
            return None
        lead = conn.execute(
            """
            SELECT id, youtube_channel_id, channel_url, raw_json
            FROM discovery_leads WHERE id = ? LIMIT 1
            """,
            [row[0]],
        ).fetchone()
        conn.commit()
        if not lead:
            return None
        return {
            "id": int(lead[0]),
            "youtube_channel_id": str(lead[1] or ""),
            "channel_url": str(lead[2] or ""),
            "raw_json": lead[3],
            "attempts": int(row[1] or 0),
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _video_details(video_ids: Iterable[str]) -> Dict[str, Dict[str, Any]]:
    clean_ids = list(dict.fromkeys(str(value or "").strip() for value in video_ids if str(value or "").strip()))
    details: Dict[str, Dict[str, Any]] = {}
    for index in range(0, len(clean_ids), 50):
        payload = _youtube_get_json(
            "videos",
            {"part": "snippet,contentDetails", "id": ",".join(clean_ids[index : index + 50])},
            timeout=10,
            require_auth=False,
            quota_feature="discovery_enrichment",
            quota_method="videos.list",
        )
        for item in payload.get("items") or []:
            video_id = str(item.get("id") or "").strip()
            snippet = item.get("snippet") or {}
            content = item.get("contentDetails") or {}
            try:
                duration = int(_parse_duration_iso8601(content.get("duration") or "") or 0)
            except Exception:
                duration = 0
            details[video_id] = {
                "video_id": video_id,
                "title": str(snippet.get("title") or "").strip(),
                "duration_seconds": duration,
                "published_at": str(snippet.get("publishedAt") or "").strip(),
            }
    return details


def _cached_candidates(lead: Dict[str, Any]) -> list[Dict[str, Any]]:
    payload = _json_value(lead.get("raw_json"), {})
    rows: list[Dict[str, Any]] = []
    for item in payload.get("face_check_video_samples") or []:
        if not isinstance(item, dict):
            continue
        video_id = str(item.get("video_id") or "").strip()
        if video_id:
            rows.append({
                "video_id": video_id,
                "title": str(item.get("title") or "").strip(),
                "duration_seconds": int(item.get("duration_seconds") or 0),
                "published_at": str(item.get("published_at") or "").strip(),
            })
    return rows


def _first_eligible(candidates: Iterable[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    for item in candidates:
        duration = int(item.get("duration_seconds") or 0)
        if MIN_DURATION_SECONDS <= duration <= MAX_DURATION_SECONDS:
            return item
    return None


def select_review_video(lead: Dict[str, Any]) -> Dict[str, Any]:
    cached = _cached_candidates(lead)
    missing_ids = [item["video_id"] for item in cached if not int(item.get("duration_seconds") or 0)]
    if missing_ids:
        fetched = _video_details(missing_ids)
        cached = [{**item, **(fetched.get(item["video_id"]) or {})} for item in cached]
    selected = _first_eligible(cached)
    if selected:
        return selected

    channel_id = str(lead.get("youtube_channel_id") or "").strip()
    if not channel_id.startswith("UC"):
        raise RuntimeError("YouTube channel id is missing")
    playlist_id = f"UU{channel_id[2:]}"
    uploads = fetch_playlist_items_batch(
        playlist_id=playlist_id,
        max_results=30,
        quota_feature="discovery_enrichment",
    ).get("videos") or []
    ordered_ids = [str(item.get("video_id") or "").strip() for item in uploads if str(item.get("video_id") or "").strip()]
    fetched = _video_details(ordered_ids)
    candidates = []
    for upload in uploads:
        video_id = str(upload.get("video_id") or "").strip()
        item = {**upload, **(fetched.get(video_id) or {}), "video_id": video_id}
        candidates.append(item)
    selected = _first_eligible(candidates)
    if not selected:
        raise RuntimeError("No recent video between 5 and 60 minutes")
    return selected


def _inject_proxy_session(proxy_url: str) -> str:
    try:
        scheme, rest = proxy_url.split("://", 1)
        credentials, host = rest.split("@", 1)
        username, password = credentials.split(":", 1)
        return f"{scheme}://{username};sessid.{secrets.token_hex(4)}:{password}@{host}"
    except Exception:
        return proxy_url


def _stream_info(video_id: str) -> tuple[str, Dict[str, str], str]:
    if yt_dlp is None:
        raise RuntimeError("yt-dlp is not installed")
    proxy_url = str(os.getenv("INGEST_PROXY_URL") or "").strip()
    proxy_url = _inject_proxy_session(proxy_url) if proxy_url else ""
    options: Dict[str, Any] = {
        "format": "bestvideo[height<=480][protocol^=http]/best[height<=480][protocol^=http]/worst[protocol^=http]",
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "socket_timeout": 10,
        "retries": 5,
        "fragment_retries": 5,
        "extractor_retries": 3,
        "legacy_server_connect": True,
        "nocheckcertificate": True,
        "extractor_args": {
            "youtube": {"player_client": ["mweb", "default"]},
            "youtubepot-bgutilhttp": {"base_url": ["http://127.0.0.1:4416"]},
        },
        "js_runtimes": {"node": {}},
    }
    cookies_path = str(os.getenv("INGEST_PROXY_COOKIES") or "").strip()
    if proxy_url:
        options["proxy"] = proxy_url
    if cookies_path:
        options["cookiefile"] = cookies_path
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
    stream_url = str((info or {}).get("url") or "").strip()
    if not stream_url:
        raise RuntimeError("yt-dlp did not return a stream URL")
    headers = {str(k): str(v) for k, v in ((info or {}).get("http_headers") or {}).items() if k and v}
    return stream_url, headers, proxy_url


def _extract_frames(video: Dict[str, Any], work_dir: Path, deadline: float) -> list[Dict[str, Any]]:
    duration = int(video.get("duration_seconds") or 0)
    stream_url, headers, proxy_url = _stream_info(str(video.get("video_id") or ""))
    header_blob = "".join(f"{key}: {value}\r\n" for key, value in headers.items())
    def extract_one(percentage: int) -> Dict[str, Any]:
        remaining = deadline - time.monotonic()
        if remaining <= 1:
            raise TimeoutError("frame extraction exceeded 90 seconds")
        timestamp = max(1, min(duration - 1, int(round(duration * percentage / 100.0))))
        output = work_dir / f"frame_{percentage}.jpg"
        command = [FFMPEG_BIN, "-hide_banner", "-loglevel", "error", "-ss", str(timestamp)]
        if proxy_url:
            command.extend(["-http_proxy", proxy_url])
        if header_blob:
            command.extend(["-headers", header_blob])
        command.extend([
            "-i", stream_url, "-frames:v", "1",
            "-vf", f"scale={FRAME_WIDTH}:-2:force_original_aspect_ratio=decrease",
            "-q:v", "7", "-y", str(output),
        ])
        try:
            completed = subprocess.run(command, capture_output=True, timeout=max(1, int(remaining)), check=False)
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(f"ffmpeg frame {percentage}% exceeded the lead timeout") from exc
        if completed.returncode != 0 or not output.exists() or output.stat().st_size <= 0:
            error = completed.stderr.decode("utf-8", errors="replace")[-300:]
            raise RuntimeError(f"ffmpeg frame {percentage}% failed: {error}")
        _shrink_jpeg_to_target(output, deadline)
        return {"percentage": percentage, "timestamp": timestamp, "path": output}

    frames: list[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=len(FRAME_PERCENTAGES), thread_name_prefix="discovery-frame") as executor:
        futures = {executor.submit(extract_one, percentage): percentage for percentage in FRAME_PERCENTAGES}
        try:
            for future in as_completed(futures, timeout=max(1, deadline - time.monotonic())):
                frames.append(future.result())
        except FuturesTimeoutError as exc:
            raise TimeoutError("frame extraction exceeded 90 seconds") from exc
    return sorted(frames, key=lambda item: FRAME_PERCENTAGES.index(int(item["percentage"])))


def _shrink_jpeg_to_target(path: Path, deadline: float) -> None:
    if path.stat().st_size <= FRAME_TARGET_BYTES:
        return
    for quality in (10, 14):
        remaining = deadline - time.monotonic()
        if remaining <= 1:
            raise TimeoutError("JPEG compression exceeded the lead timeout")
        smaller = path.with_name(f"{path.stem}_small.jpg")
        completed = subprocess.run(
            [FFMPEG_BIN, "-hide_banner", "-loglevel", "error", "-i", str(path),
             "-vf", f"scale={FRAME_WIDTH}:-2:force_original_aspect_ratio=decrease",
             "-q:v", str(quality), "-y", str(smaller)],
            capture_output=True,
            timeout=max(1, int(remaining)),
            check=False,
        )
        if completed.returncode != 0 or not smaller.exists():
            break
        smaller.replace(path)
        if path.stat().st_size <= FRAME_TARGET_BYTES:
            break


def _resize_cached_jpeg(source: Path, output: Path, deadline: float) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 1:
        raise TimeoutError("cached frame resize timed out")
    completed = subprocess.run(
        [FFMPEG_BIN, "-hide_banner", "-loglevel", "error", "-i", str(source),
         "-vf", f"scale={FRAME_WIDTH}:-2:force_original_aspect_ratio=decrease",
         "-q:v", "7", "-y", str(output)],
        capture_output=True,
        timeout=max(1, int(remaining)),
        check=False,
    )
    if completed.returncode != 0 or not output.exists() or output.stat().st_size <= 0:
        raise RuntimeError("cached frame resize failed")
    _shrink_jpeg_to_target(output, deadline)


def resize_cached_frame_reviews(*, limit: int = 100) -> Dict[str, int]:
    conn = get_db()
    try:
        rows = conn.execute(
            f"""
            SELECT lead_id, frame_keys, storage_backend
            FROM {REVIEW_TABLE}
            WHERE status = 'completed' AND COALESCE(frame_width, 0) <> ?
            ORDER BY completed_at DESC NULLS LAST
            LIMIT ?
            """,
            [FRAME_WIDTH, max(1, min(1000, int(limit or 100)))],
        ).fetchall()
    finally:
        conn.close()
    resized = 0
    failed = 0
    for lead_id, raw_keys, backend in rows:
        storage = get_media_storage(str(backend or "local"))
        keys = _json_value(raw_keys, [])
        work_dir = Path(tempfile.mkdtemp(prefix=f"resize_discovery_frames_{lead_id}_"))
        deadline = time.monotonic() + LEAD_TIMEOUT_SECONDS
        total_size = 0
        try:
            for index, key in enumerate(keys):
                source = work_dir / f"source_{index}.jpg"
                output = work_dir / f"frame_{index}.jpg"
                source.write_bytes(storage.read_bytes(str(key)))
                _resize_cached_jpeg(source, output, deadline)
                storage.put_file(output, str(key))
                total_size += output.stat().st_size
            update_conn = get_db()
            try:
                update_conn.execute(
                    f"UPDATE {REVIEW_TABLE} SET frame_width = ?, size_bytes = ?, updated_at = CURRENT_TIMESTAMP WHERE lead_id = ?",
                    [FRAME_WIDTH, total_size, lead_id],
                )
                update_conn.commit()
            finally:
                update_conn.close()
            resized += 1
        except Exception as exc:
            logger.error("Cached discovery frame resize failed lead_id=%s error=%s", lead_id, _safe_error(exc))
            failed += 1
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)
    return {"selected": len(rows), "resized": resized, "failed": failed}


def requeue_failed_frame_reviews(*, limit: int = 1000) -> int:
    conn = get_db()
    try:
        result = conn.execute(
            f"""
            UPDATE {REVIEW_TABLE} r
            SET status = 'pending', attempts = 0, retry_after = NULL, error = NULL,
                error_code = NULL, completed_at = NULL, updated_at = CURRENT_TIMESTAMP
            FROM discovery_leads d
            WHERE d.id = r.lead_id
              AND r.status = 'frames_failed'
              AND {_ready_predicate('d')}
              AND r.lead_id IN (
                  SELECT r2.lead_id FROM {REVIEW_TABLE} r2
                  WHERE r2.status = 'frames_failed'
                  ORDER BY r2.updated_at DESC
                  LIMIT ?
              )
            """,
            [max(1, min(5000, int(limit or 1000)))],
        )
        count = max(0, int(getattr(result, "rowcount", 0) or 0))
        conn.commit()
        return count
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _frame_data_url(path: Path) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def _vision_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    normalized = str(model or "").lower()
    if "gpt-4o-mini" in normalized:
        input_rate, output_rate = 0.15, 0.60
    else:
        input_rate, output_rate = 0.40, 1.60
    return round((input_tokens * input_rate + output_tokens * output_rate) / 1_000_000, 6)


def _ai_review(frames: list[Dict[str, Any]], deadline: float) -> Dict[str, Any]:
    if _openai_client is None:
        raise RuntimeError("OpenAI client is not configured")
    model = OPENAI_MODEL if str(OPENAI_MODEL or "").lower().startswith(("gpt-4o", "gpt-4.1")) else "gpt-4o-mini"
    content: list[Dict[str, Any]] = [{"type": "text", "text": VISION_PROMPT}]
    content.extend(
        {"type": "image_url", "image_url": {"url": _frame_data_url(item["path"]), "detail": "low"}}
        for item in frames
    )
    remaining = max(1.0, deadline - time.monotonic())
    response = _openai_client.with_options(timeout=remaining).chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": content}],
        response_format={"type": "json_object"},
        temperature=0,
    )
    payload = json.loads(str(response.choices[0].message.content or "").strip())
    label = str(payload.get("label") or "").strip().upper().replace("’", "'")
    if label in {"DONT PROMOTE", "DO NOT PROMOTE"}:
        label = "DON'T PROMOTE"
    if label not in {"PROMOTE", "DON'T PROMOTE"}:
        raise ValueError(f"invalid AI label: {label!r}")
    usage = getattr(response, "usage", None)
    input_tokens = int(getattr(usage, "prompt_tokens", None) or getattr(usage, "input_tokens", None) or 0)
    output_tokens = int(getattr(usage, "completion_tokens", None) or getattr(usage, "output_tokens", None) or 0)
    return {
        "label": label,
        "reason": str(payload.get("reason") or "").strip()[:300],
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": _vision_cost(model, input_tokens, output_tokens),
    }


def _store_success(lead_id: int, video: Dict[str, Any], frames: list[Dict[str, Any]], ai: Dict[str, Any], elapsed: float) -> Dict[str, Any]:
    storage = get_media_storage()
    frame_keys: list[str] = []
    timestamps: list[Dict[str, int]] = []
    size_bytes = 0
    for item in frames:
        key = f"discovery_frames/{lead_id}/{video['video_id']}/frame_{item['percentage']}.jpg"
        storage.put_file(item["path"], key)
        frame_keys.append(key)
        timestamps.append({"percentage": int(item["percentage"]), "timestamp": int(item["timestamp"])})
        size_bytes += item["path"].stat().st_size
    conn = get_db()
    try:
        conn.execute(
            f"""
            UPDATE {REVIEW_TABLE}
            SET status = 'completed', video_id = ?, video_title = ?, duration_seconds = ?,
                published_at = ?, frame_keys = ?, frame_timestamps = ?, storage_backend = ?,
                size_bytes = ?, elapsed_seconds = ?, ai_label = ?, ai_reason = ?, ai_model = ?,
                ai_input_tokens = ?, ai_output_tokens = ?, ai_cost_usd = ?, error = NULL,
                completed_at = CURRENT_TIMESTAMP, expires_at = ?, retry_after = NULL,
                error_code = NULL, frame_width = ?, updated_at = CURRENT_TIMESTAMP
            WHERE lead_id = ?
            """,
            [
                video.get("video_id"), video.get("title"), int(video.get("duration_seconds") or 0),
                video.get("published_at") or None, json.dumps(frame_keys), json.dumps(timestamps),
                storage.backend_name, size_bytes, elapsed, ai.get("label"), ai.get("reason"), ai.get("model"),
                ai.get("input_tokens", 0), ai.get("output_tokens", 0), ai.get("cost_usd", 0),
                (datetime.now(timezone.utc) + timedelta(days=CACHE_DAYS)).replace(tzinfo=None), FRAME_WIDTH, lead_id,
            ],
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "lead_id": lead_id,
        "status": "completed",
        "video_id": video.get("video_id"),
        "video_title": video.get("title"),
        "duration_seconds": int(video.get("duration_seconds") or 0),
        "size_bytes": size_bytes,
        "elapsed_seconds": elapsed,
        "ai_label": ai.get("label"),
        "ai_reason": ai.get("reason"),
        "ai_cost_usd": ai.get("cost_usd", 0),
    }


def _store_failure(lead_id: int, error: Exception, elapsed: float, *, attempts: int) -> Dict[str, Any]:
    error_code = _error_code(error)
    retry_later = error_code == "bot_check" and attempts < 2
    status = "retry_wait" if retry_later else "frames_failed"
    message = _public_error(error_code)
    conn = get_db()
    try:
        conn.execute(
            f"""
            UPDATE {REVIEW_TABLE}
            SET status = ?, error = ?, error_code = ?, elapsed_seconds = ?,
                retry_after = CASE WHEN ? THEN CURRENT_TIMESTAMP + INTERVAL '1 hour' ELSE NULL END,
                completed_at = CASE WHEN ? THEN NULL ELSE CURRENT_TIMESTAMP END,
                updated_at = CURRENT_TIMESTAMP
            WHERE lead_id = ?
            """,
            [status, message, error_code, elapsed, retry_later, retry_later, lead_id],
        )
        conn.commit()
    finally:
        conn.close()
    return {"lead_id": lead_id, "status": status, "error": message, "elapsed_seconds": elapsed}


def process_next_discovery_frame_review() -> Optional[Dict[str, Any]]:
    cleanup_expired_frame_reviews(limit=25)
    enqueue_promotable_frame_reviews(limit=200)
    lead = _claim_next_review()
    if not lead:
        return None
    started = time.monotonic()
    work_dir = Path(tempfile.mkdtemp(prefix=f"discovery_frames_{lead['id']}_"))
    try:
        deadline = started + LEAD_TIMEOUT_SECONDS
        video = select_review_video(lead)
        frames = _extract_frames(video, work_dir, deadline)
        ai = _ai_review(frames, deadline)
        elapsed = round(time.monotonic() - started, 3)
        if elapsed > LEAD_TIMEOUT_SECONDS:
            raise TimeoutError("frame review exceeded 90 seconds")
        return _store_success(int(lead["id"]), video, frames, ai, elapsed)
    except Exception as exc:
        logger.error("Discovery frame review failed lead_id=%s error=%s", lead.get("id"), _safe_error(exc))
        return _store_failure(
            int(lead["id"]), exc, round(time.monotonic() - started, 3), attempts=int(lead.get("attempts") or 1)
        )
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def load_frame_reviews(conn, lead_ids: Iterable[int]) -> Dict[int, Dict[str, Any]]:
    ids = list(dict.fromkeys(int(value) for value in lead_ids if int(value) > 0))[:100]
    if not ids or not table_columns(conn, REVIEW_TABLE):
        return {}
    placeholders = ",".join(["?"] * len(ids))
    rows = conn.execute(
        f"""
        SELECT lead_id, status, video_id, video_title, duration_seconds, frame_keys,
               frame_timestamps, storage_backend, size_bytes, elapsed_seconds,
               ai_label, ai_reason, ai_model, ai_cost_usd, error_code, expires_at, updated_at
        FROM {REVIEW_TABLE} WHERE lead_id IN ({placeholders})
        """,
        ids,
    ).fetchall()
    storage_cache: Dict[str, Any] = {}
    output: Dict[int, Dict[str, Any]] = {}
    for row in rows:
        backend = str(row[7] or "local")
        storage = storage_cache.setdefault(backend, get_media_storage(backend))
        keys = _json_value(row[5], [])
        timestamps = _json_value(row[6], [])
        frames = []
        for index, key in enumerate(keys):
            meta = timestamps[index] if index < len(timestamps) and isinstance(timestamps[index], dict) else {}
            frames.append({
                "url": storage.public_url(str(key), version=str(row[16] or row[15] or "")),
                "timestamp": int(meta.get("timestamp") or 0),
                "percentage": int(meta.get("percentage") or 0),
            })
        output[int(row[0])] = {
            "lead_id": int(row[0]), "status": str(row[1] or "pending"),
            "video_id": str(row[2] or ""), "video_title": str(row[3] or ""),
            "duration_seconds": int(row[4] or 0), "frames": frames,
            "size_bytes": int(row[8] or 0), "elapsed_seconds": float(row[9] or 0),
            "ai_label": str(row[10] or ""), "ai_reason": str(row[11] or ""),
            "ai_model": str(row[12] or ""), "ai_cost_usd": float(row[13] or 0),
            "error": _public_error(str(row[14] or "")),
        }
    if table_columns(conn, DECISION_TABLE):
        decisions = conn.execute(
            f"""
            SELECT lead_id, my_action, decided_at
            FROM {DECISION_TABLE}
            WHERE lead_id IN ({placeholders})
            ORDER BY decided_at DESC
            """,
            ids,
        ).fetchall()
        today_pt = datetime.now(PACIFIC_TZ).date()
        seen = set()
        for lead_id, action, decided_at in decisions:
            lead_id = int(lead_id)
            if lead_id in seen:
                continue
            seen.add(lead_id)
            instant = decided_at if isinstance(decided_at, datetime) else datetime.fromisoformat(str(decided_at))
            if instant.tzinfo is None:
                instant = instant.replace(tzinfo=timezone.utc)
            if str(action or "") == "skip" and instant.astimezone(PACIFIC_TZ).date() == today_pt:
                output.setdefault(lead_id, {"lead_id": lead_id, "status": "pending", "frames": []})["skipped_today"] = True
    return output


def record_review_decision(lead_id: int, action: str) -> Dict[str, Any]:
    normalized = str(action or "").strip().lower()
    if normalized not in {"promote", "skip"}:
        raise ValueError("action must be promote or skip")
    conn = get_db()
    try:
        ensure_frame_review_schema(conn)
        row = conn.execute(f"SELECT ai_label FROM {REVIEW_TABLE} WHERE lead_id = ?", [lead_id]).fetchone()
        ai_label = str((row or [""])[0] or "")
        conn.execute(
            f"INSERT INTO {DECISION_TABLE} (id, lead_id, ai_label, my_action, decided_at) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)",
            [str(uuid4()), int(lead_id), ai_label, normalized],
        )
        conn.commit()
        return {"lead_id": int(lead_id), "ai_label": ai_label, "my_action": normalized}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
