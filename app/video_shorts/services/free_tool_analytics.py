from __future__ import annotations

import hashlib
import ipaddress
import os
import re
from typing import Any, Mapping, Optional
from uuid import uuid4

from flask import current_app, g, has_request_context, request, session

from app.video_shorts.config import INTERNAL_IPS, SIGNUP_ALIAS_ALLOWLIST
from app.video_shorts.services.db import get_db_readonly
from app.video_shorts.services.user_events import track_event


FREE_TOOL_ANON_USER_PREFIX = "free_tool_anon"
FREE_TOOL_ATTRIBUTION_SESSION_KEY = "free_tool_attribution"
INTERNAL_TRAFFIC_COOKIE = "minti_internal_traffic"
BOT_USER_AGENT_RE = re.compile(
    r"(bot|crawl|spider|slurp|preview|headless|phantom|playwright|curl|wget|"
    r"python-requests|go-http-client|facebookexternalhit|twitterbot|slackbot|"
    r"discordbot|linkedinbot|whatsapp|skypeuripreview|proofpoint|mimecast|"
    r"barracuda|googleimageproxy|uptime|monitor|health)",
    re.IGNORECASE,
)


def _clean_value(value: object, *, max_length: int = 120) -> str:
    return str(value or "").strip()[:max_length]


def _request_ip() -> str:
    if not has_request_context():
        return ""
    cloudflare_ip = _clean_value(request.headers.get("CF-Connecting-IP"), max_length=80)
    if cloudflare_ip:
        return cloudflare_ip
    forwarded = _clean_value(request.headers.get("X-Forwarded-For"), max_length=500)
    if forwarded:
        return forwarded.split(",", 1)[0].strip()
    return _clean_value(request.remote_addr, max_length=80)


def _normalized_email(value: object) -> str:
    email = str(value or "").strip().lower()
    if "@" not in email:
        return email
    local, domain = email.rsplit("@", 1)
    local = local.split("+", 1)[0]
    if domain in {"gmail.com", "googlemail.com"}:
        local = local.replace(".", "")
        domain = "gmail.com"
    return f"{local}@{domain}"


def _ip_is_internal(value: str) -> bool:
    if not value:
        return False
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    for configured in INTERNAL_IPS:
        try:
            if "/" in configured:
                if address in ipaddress.ip_network(configured, strict=False):
                    return True
            elif address == ipaddress.ip_address(configured):
                return True
        except ValueError:
            continue
    return False


def is_obvious_bot_request() -> bool:
    if not has_request_context():
        return False
    return bool(BOT_USER_AGENT_RE.search(str(request.headers.get("User-Agent") or "")))


def _resolved_current_user(current_user: Optional[Mapping[str, Any]]) -> Optional[Mapping[str, Any]]:
    if current_user is not None:
        return current_user
    if not has_request_context():
        return None
    value = getattr(g, "vs_current_user", None)
    return value if isinstance(value, Mapping) else None


def is_internal_request(current_user: Optional[Mapping[str, Any]] = None) -> bool:
    if not has_request_context():
        return False
    if request.cookies.get(INTERNAL_TRAFFIC_COOKIE) == "true" or _ip_is_internal(_request_ip()):
        return True

    user = _resolved_current_user(current_user)
    allowlisted = {_normalized_email(email) for email in SIGNUP_ALIAS_ALLOWLIST if email}
    if user:
        if str(user.get("role") or "").strip().lower() == "admin":
            return True
        identities = (user.get("email"), user.get("username"))
        if any(_normalized_email(value) in allowlisted for value in identities if value):
            return True
        user_id = _clean_value(user.get("id"), max_length=80)
    else:
        user_id = _clean_value(session.get("vs_user_id"), max_length=80)
    if not user_id:
        return False

    conn = None
    try:
        conn = get_db_readonly()
        row = conn.execute(
            """
            SELECT role, email, username
            FROM shorts_users
            WHERE CAST(id AS VARCHAR) = ?
            LIMIT 1
            """,
            [user_id],
        ).fetchone()
    except Exception:
        current_app.logger.warning("Unable to resolve free-tool internal user", exc_info=True)
        return False
    finally:
        if conn is not None:
            conn.close()
    if not row:
        return False
    return str(row[0] or "").strip().lower() == "admin" or any(
        _normalized_email(value) in allowlisted for value in row[1:3] if value
    )


def capture_first_touch(*, lead_id: str = "") -> None:
    if not has_request_context():
        return
    existing = session.get(FREE_TOOL_ATTRIBUTION_SESSION_KEY)
    if isinstance(existing, dict) and existing:
        attribution = dict(existing)
        changed = False
    else:
        attribution = {}
        changed = True

    if not attribution.get("session_id"):
        attribution["session_id"] = str(uuid4())
        changed = True

    first_touch_pending = attribution.get("first_touch_captured") != "1"
    if first_touch_pending and not any(
        key in attribution for key in ("utm_source", "utm_medium", "utm_campaign", "lead_id")
    ):
        for key in ("utm_source", "utm_medium", "utm_campaign"):
            value = _clean_value(request.args.get(key))
            if value:
                attribution[key] = value
                changed = True
        clean_lead_id = _clean_value(lead_id, max_length=80)
        if clean_lead_id:
            attribution["lead_id"] = clean_lead_id
            changed = True

    if first_touch_pending and "referrer" not in attribution:
        referrer = _clean_value(request.referrer, max_length=500)
        if referrer:
            attribution["referrer"] = referrer
        changed = True

    if first_touch_pending:
        attribution["first_touch_captured"] = "1"
        changed = True

    if changed:
        session[FREE_TOOL_ATTRIBUTION_SESSION_KEY] = attribution
        session.modified = True


def attribution_metadata() -> dict[str, str]:
    if not has_request_context():
        return {}
    attribution = session.get(FREE_TOOL_ATTRIBUTION_SESSION_KEY)
    if not isinstance(attribution, dict) or not attribution.get("session_id"):
        capture_first_touch()
        attribution = session.get(FREE_TOOL_ATTRIBUTION_SESSION_KEY)
    if not isinstance(attribution, dict):
        return {}
    limits = {
        "utm_source": 120,
        "utm_medium": 120,
        "utm_campaign": 120,
        "lead_id": 80,
        "session_id": 80,
        "referrer": 500,
    }
    return {
        key: _clean_value(value, max_length=limits[key])
        for key, value in attribution.items()
        if key in limits and _clean_value(value, max_length=limits[key])
    }


def free_tool_event_user_id(current_user: Optional[Mapping[str, Any]] = None) -> str:
    user = _resolved_current_user(current_user)
    if user and user.get("id"):
        return str(user.get("id"))
    secret = str(current_app.secret_key or os.getenv("SECRET_KEY") or "minti-free-tool").encode("utf-8")
    digest = hashlib.sha256(secret + b":" + _request_ip().encode("utf-8")).hexdigest()
    return f"{FREE_TOOL_ANON_USER_PREFIX}:{digest[:16]}"


def track_free_tool_event(
    event_name: str,
    *,
    current_user: Optional[Mapping[str, Any]] = None,
    video_id: Any = None,
    short_id: Any = None,
    status: str = "",
    metadata: Optional[Mapping[str, Any]] = None,
) -> bool:
    if not has_request_context() or is_obvious_bot_request() or is_internal_request(current_user):
        return False
    event_metadata = {**attribution_metadata(), **dict(metadata or {})}
    track_event(
        free_tool_event_user_id(current_user),
        str(event_name or "").strip(),
        video_id=str(video_id or "").strip() or None,
        short_id=str(short_id or "").strip() or None,
        status=str(status or "").strip() or None,
        metadata=event_metadata,
    )
    return True
