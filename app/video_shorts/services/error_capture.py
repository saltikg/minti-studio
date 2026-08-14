from __future__ import annotations

import logging
from typing import Any

from flask import g, request

from app.video_shorts.services.user_events import track_event


logger = logging.getLogger(__name__)

CLIENT_ERROR_MAX_BODY_BYTES = 4096
CLIENT_ERROR_RATE_LIMITS = (
    ("burst", 10, 60),
    ("hour", 30, 3600),
)


def _truncate(value: Any, limit: int = 500) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) <= limit:
        return text
    return f"{text[: limit - 1]}…"


def current_event_user_id() -> str:
    current_user = getattr(g, "vs_current_user", None)
    if isinstance(current_user, dict):
        user_id = str(current_user.get("id") or "").strip()
        if user_id:
            return user_id
    return "anonymous"


def current_request_id() -> str | None:
    return _truncate(
        request.headers.get("X-Request-Id")
        or request.headers.get("X-Correlation-Id")
        or request.environ.get("HTTP_X_REQUEST_ID"),
        120,
    )


def wants_json_error_response() -> bool:
    path = (request.path or "").strip().lower()
    if path.startswith("/api/") or path.startswith("/video_shorts/api/"):
        return True
    requested_with = (request.headers.get("X-Requested-With") or "").strip().lower()
    if requested_with == "xmlhttprequest":
        return True
    best = request.accept_mimetypes.best_match(["application/json", "text/html"])
    if best == "application/json":
        return request.accept_mimetypes[best] >= request.accept_mimetypes["text/html"]
    return False


def _record_event(*, event_name: str, status: str, metadata: dict[str, Any]) -> None:
    try:
        track_event(
            current_event_user_id(),
            event_name,
            status=status,
            metadata=metadata,
        )
    except Exception:
        logger.exception("error capture failed event_name=%s", event_name)


def capture_server_error(
    *,
    status_code: int,
    exception_type: str | None = None,
    exception_message: str | None = None,
) -> None:
    if getattr(g, "_vs_error_capture_active", False):
        return
    if (request.path or "").startswith("/video_shorts/api/client-error"):
        return
    g._vs_error_capture_active = True
    metadata = {
        "path": _truncate(request.path, 240),
        "method": _truncate(request.method, 16),
        "status": str(status_code),
        "exception_type": _truncate(exception_type, 120),
        "exception_message": _truncate(exception_message, 500),
        "request_id": current_request_id(),
    }
    _record_event(
        event_name="server_error",
        status=str(status_code),
        metadata=metadata,
    )


def capture_client_error(
    *,
    error_type: str,
    message: str | None = None,
    source: str | None = None,
    user_agent: str | None = None,
) -> None:
    metadata = {
        "message": _truncate(message, 500),
        "source": _truncate(source or request.path, 240),
        "error_type": _truncate(error_type, 120),
        "user_agent": _truncate(user_agent or request.headers.get("User-Agent"), 300),
    }
    _record_event(
        event_name="client_error",
        status=_truncate(error_type, 120) or "unknown",
        metadata=metadata,
    )
