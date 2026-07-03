from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.video_shorts.services.instagram_api import _instagram_comment_record
from app.video_shorts.services.comment_moderation import moderate_text_entries
from app.video_shorts.services.comment_store import upsert_comment_records
from app.video_shorts.services.instagram_queue import (
    get_instagram_queue_entry,
    get_instagram_queue_entry_by_media_id,
)
from app.video_shorts.services.render_jobs import (
    JOB_TYPE_INSTAGRAM_COMMENT_WEBHOOK,
    enqueue_worker_job,
)

logger = logging.getLogger(__name__)


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _normalize_comment_timestamp(value: Any) -> str:
    raw = _as_text(value)
    if not raw:
        return ""
    try:
        if raw.isdigit():
            ts = int(raw)
            if ts > 10**12:
                ts = ts / 1000
            return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        candidate = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
        parsed = datetime.fromisoformat(candidate)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        else:
            parsed = parsed.astimezone(timezone.utc)
        return parsed.isoformat()
    except Exception:
        return raw


def _extract_comment_events(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    for entry in payload.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        entry_timestamp = _normalize_comment_timestamp(entry.get("time") or entry.get("timestamp"))
        for change in entry.get("changes") or []:
            if not isinstance(change, dict):
                continue
            if _as_text(change.get("field")).lower() != "comments":
                continue
            value = change.get("value") or {}
            if not isinstance(value, dict):
                continue
            verb = _as_text(value.get("verb")).lower()
            if verb in {"remove", "delete", "deleted"}:
                continue
            item_type = _as_text(value.get("item")).lower()
            if item_type and item_type not in {"comment", "reply"}:
                continue
            source = value.get("from") if isinstance(value.get("from"), dict) else {}
            media = value.get("media") if isinstance(value.get("media"), dict) else {}
            comment_id = _as_text(value.get("comment_id") or value.get("id"))
            media_id = _as_text(value.get("media_id") or media.get("id"))
            if not comment_id or not media_id:
                continue
            events.append(
                {
                    "comment_id": comment_id,
                    "media_id": media_id,
                    "parent_id": _as_text(value.get("parent_id")),
                    "thread_id": _as_text(value.get("parent_id") or value.get("thread_id") or comment_id),
                    "text": _as_text(value.get("text") or value.get("message")),
                    "timestamp": _normalize_comment_timestamp(
                        value.get("timestamp")
                        or value.get("created_time")
                        or entry_timestamp
                    ),
                    "author_id": _as_text(source.get("id") or value.get("from_id")),
                    "author_username": _as_text(
                        source.get("username")
                        or source.get("name")
                        or value.get("username")
                        or value.get("user_name")
                    ),
                    "like_count": value.get("like_count"),
                    "raw_value": value,
                }
            )
    return events


def _build_job_hash(queue_id: str, event: Dict[str, Any]) -> str:
    canonical = {
        "queue_id": _as_text(queue_id),
        "comment_id": _as_text(event.get("comment_id")),
        "media_id": _as_text(event.get("media_id")),
        "parent_id": _as_text(event.get("parent_id")),
        "text": _as_text(event.get("text")),
        "timestamp": _as_text(event.get("timestamp")),
        "raw_value": event.get("raw_value") or {},
    }
    raw = json.dumps(canonical, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def enqueue_instagram_comment_events(payload: Dict[str, Any]) -> Dict[str, int]:
    queued = 0
    skipped = 0
    for event in _extract_comment_events(payload):
        queue_entry = get_instagram_queue_entry_by_media_id(_as_text(event.get("media_id")))
        if not queue_entry or not queue_entry.get("id") or not queue_entry.get("user_id"):
            skipped += 1
            logger.warning(
                "Instagram webhook comment skipped because no queue mapping was found for media_id=%s comment_id=%s.",
                event.get("media_id"),
                event.get("comment_id"),
            )
            continue
        result = enqueue_worker_job(
            user_id=str(queue_entry["user_id"]),
            job_type=JOB_TYPE_INSTAGRAM_COMMENT_WEBHOOK,
            payload={
                "queue_id": str(queue_entry["id"]),
                "event": event,
            },
            input_hash=_build_job_hash(str(queue_entry["id"]), event),
            max_attempts=3,
        )
        if result.get("kind") in {"queued", "existing", "cached"}:
            queued += 1
        else:
            skipped += 1
    return {"queued": queued, "skipped": skipped}


def process_instagram_comment_webhook_job(payload: Dict[str, Any]) -> Dict[str, Any]:
    queue_id = _as_text(payload.get("queue_id"))
    event = payload.get("event") or {}
    if not queue_id or not isinstance(event, dict):
        raise ValueError("Instagram webhook job payload is invalid.")
    entry = get_instagram_queue_entry(queue_id)
    if not entry:
        raise ValueError(f"Instagram queue entry not found for queue_id={queue_id}.")

    comment_id = _as_text(event.get("comment_id"))
    media_id = _as_text(event.get("media_id") or entry.get("instagram_media_id"))
    if not comment_id or not media_id:
        raise ValueError("Instagram webhook event is missing comment_id or media_id.")

    text = _as_text(event.get("text"))
    moderation_map = (
        moderate_text_entries([{"id": comment_id, "text": text}], _as_text(entry.get("user_id")))
        if text
        else {}
    )
    moderation = moderation_map.get(comment_id) or {}
    now = datetime.now(timezone.utc)
    parent_id = _as_text(event.get("parent_id")) or None
    thread_id = _as_text(event.get("thread_id")) or comment_id
    published_at = _normalize_comment_timestamp(event.get("timestamp")) or now.isoformat()

    upsert_comment_records(
        [
            _instagram_comment_record(
                entry=entry,
                media_id=media_id,
                comment_id=comment_id,
                parent_id=parent_id,
                thread_id=thread_id,
                author=_as_text(event.get("author_username")) or None,
                text=text or None,
                published_at=published_at,
                like_count=event.get("like_count"),
                moderation=moderation,
                now=now,
            )
        ]
    )
    return {
        "comment_id": comment_id,
        "queue_id": queue_id,
        "media_id": media_id,
        "moderated": bool(moderation),
    }
