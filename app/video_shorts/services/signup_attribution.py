from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.parse import urlencode, urlsplit

from flask import has_request_context, request, session


SIGNUP_ATTRIBUTION_SESSION_KEY = "signup_attribution"
UTM_KEYS = ("utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content")
MAX_ATTRIBUTION_VALUE_LENGTH = 200


def _clean(value: Any) -> str:
    return str(value or "").strip()[:MAX_ATTRIBUTION_VALUE_LENGTH]


def _referrer_host(value: Any) -> str:
    try:
        return _clean(urlsplit(str(value or "").strip()).hostname or "").lower()
    except ValueError:
        return ""


def capture_signup_attribution() -> None:
    """Capture immutable, privacy-limited first-touch signup attribution."""
    if not has_request_context() or SIGNUP_ATTRIBUTION_SESSION_KEY in session:
        return
    utm_values = {key: _clean(request.args.get(key)) for key in UTM_KEYS}
    utm_values = {key: value for key, value in utm_values.items() if value}
    landing_path = _clean(request.path or "/") or "/"
    if utm_values:
        landing_path = _clean(f"{landing_path}?{urlencode(utm_values)}")
    attribution = {
        **utm_values,
        "referrer_host": _referrer_host(request.referrer),
        "landing_path": landing_path,
        "first_seen_at": datetime.now(timezone.utc).isoformat(),
    }
    session[SIGNUP_ATTRIBUTION_SESSION_KEY] = {
        key: _clean(value)
        for key, value in attribution.items()
        if _clean(value)
    }
    session.modified = True


def signup_attribution_metadata() -> dict[str, str]:
    if not has_request_context():
        return {}
    capture_signup_attribution()
    stored = session.get(SIGNUP_ATTRIBUTION_SESSION_KEY)
    if not isinstance(stored, Mapping):
        return {}
    allowed = {*UTM_KEYS, "referrer_host", "landing_path", "first_seen_at"}
    return {
        key: _clean(value)
        for key, value in stored.items()
        if key in allowed and _clean(value)
    }


def signup_source_label(metadata: Any) -> str:
    if isinstance(metadata, str):
        import json

        try:
            metadata = json.loads(metadata)
        except (TypeError, ValueError):
            metadata = {}
    if not isinstance(metadata, Mapping):
        return ""
    return " / ".join(
        value
        for value in (
            _clean(metadata.get("utm_source")),
            _clean(metadata.get("referrer_host")),
            _clean(metadata.get("landing_path")),
        )
        if value
    )
