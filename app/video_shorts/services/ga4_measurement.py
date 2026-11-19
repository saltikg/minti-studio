from __future__ import annotations

import logging
import os
import re
from threading import Thread
from typing import Any, Mapping
from uuid import uuid4

import requests
from flask import has_request_context, request

logger = logging.getLogger(__name__)

GA4_MEASUREMENT_ID = "G-BFML4VLWMF"
GA4_COLLECT_URL = "https://www.google-analytics.com/mp/collect"
GA4_REQUEST_TIMEOUT_SECONDS = 2
INTERNAL_TRAFFIC_COOKIE = "minti_internal_traffic"
_GA_CLIENT_ID_RE = re.compile(r"(\d+\.\d+)$")
_GA4_SESSION_ID_RE = re.compile(r"(?:^|[.$])s(\d+)(?:[$.]|$)")


def ga4_client_id_from_cookie(cookie_value: object) -> str:
    match = _GA_CLIENT_ID_RE.search(str(cookie_value or "").strip())
    return match.group(1) if match else ""


def ga4_session_id_from_cookie(cookie_value: object) -> str:
    cookie = str(cookie_value or "").strip()
    match = _GA4_SESSION_ID_RE.search(cookie)
    if match:
        return match.group(1)
    parts = cookie.split(".")
    if len(parts) >= 3 and parts[0] == "GS1" and parts[2].isdigit():
        return parts[2]
    return ""


def _log_attempt(event_name: str, outcome: str, http_status: object = "none") -> None:
    log = logger.warning if outcome == "failed" else logger.info
    log(
        "GA4 Measurement Protocol event_name=%s outcome=%s http_status=%s",
        event_name,
        outcome,
        http_status,
    )


def _post_event(
    *,
    api_secret: str,
    payload: dict[str, Any],
    event_name: str,
    success_outcome: str = "sent",
) -> None:
    response = None
    try:
        response = requests.post(
            GA4_COLLECT_URL,
            params={"measurement_id": GA4_MEASUREMENT_ID, "api_secret": api_secret},
            json=payload,
            timeout=GA4_REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        _log_attempt(event_name, success_outcome, response.status_code)
    except Exception:
        _log_attempt(event_name, "failed", getattr(response, "status_code", "none"))


def send_ga4_event(event_name: str, params: Mapping[str, Any] | None = None) -> bool:
    """Queue a PII-free GA4 event, generating an anonymous client id if needed."""
    clean_event_name = str(event_name or "").strip()
    if not has_request_context():
        _log_attempt(clean_event_name, "failed")
        return False
    api_secret = str(os.getenv("GA4_API_SECRET") or "").strip()
    if not api_secret:
        _log_attempt(clean_event_name, "skipped_no_secret")
        return False
    if not clean_event_name:
        _log_attempt(clean_event_name, "failed")
        return False
    client_id = ga4_client_id_from_cookie(request.cookies.get("_ga"))
    generated_client_id = not client_id
    if generated_client_id:
        client_id = str(uuid4())
    event_params = {
        str(key): value
        for key, value in dict(params or {}).items()
        if value is not None and str(key) in {"method", "traffic_type"}
    }
    if request.cookies.get(INTERNAL_TRAFFIC_COOKIE) == "true":
        event_params["traffic_type"] = "internal"
    session_id = ga4_session_id_from_cookie(request.cookies.get("_ga_BFML4VLWMF"))
    if session_id:
        event_params["session_id"] = session_id
        event_params["engagement_time_msec"] = 1
    payload = {
        "client_id": client_id,
        "events": [{"name": clean_event_name, "params": event_params}],
    }
    Thread(
        target=_post_event,
        kwargs={
            "api_secret": api_secret,
            "payload": payload,
            "event_name": clean_event_name,
            "success_outcome": "sent_generated_client_id" if generated_client_id else "sent",
        },
        name=f"ga4-{clean_event_name}",
        daemon=True,
    ).start()
    return True
