from __future__ import annotations

import logging
import os
import re
from threading import Thread
from typing import Any, Mapping

import requests
from flask import has_request_context, request

logger = logging.getLogger(__name__)

GA4_MEASUREMENT_ID = "G-BFML4VLWMF"
GA4_COLLECT_URL = "https://www.google-analytics.com/mp/collect"
GA4_REQUEST_TIMEOUT_SECONDS = 2
INTERNAL_TRAFFIC_COOKIE = "minti_internal_traffic"
_GA_CLIENT_ID_RE = re.compile(r"(\d+\.\d+)$")


def ga4_client_id_from_cookie(cookie_value: object) -> str:
    match = _GA_CLIENT_ID_RE.search(str(cookie_value or "").strip())
    return match.group(1) if match else ""


def _post_event(*, api_secret: str, payload: dict[str, Any], event_name: str) -> None:
    try:
        response = requests.post(
            GA4_COLLECT_URL,
            params={"measurement_id": GA4_MEASUREMENT_ID, "api_secret": api_secret},
            json=payload,
            timeout=GA4_REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except Exception:
        logger.warning("GA4 Measurement Protocol event failed event_name=%s", event_name, exc_info=True)


def send_ga4_event(event_name: str, params: Mapping[str, Any] | None = None) -> bool:
    """Queue a PII-free GA4 event when the browser supplied a GA client id."""
    if not has_request_context():
        return False
    api_secret = str(os.getenv("GA4_API_SECRET") or "").strip()
    client_id = ga4_client_id_from_cookie(request.cookies.get("_ga"))
    clean_event_name = str(event_name or "").strip()
    if not api_secret or not client_id or not clean_event_name:
        return False
    event_params = {
        str(key): value
        for key, value in dict(params or {}).items()
        if value is not None and str(key) in {"method", "traffic_type"}
    }
    if request.cookies.get(INTERNAL_TRAFFIC_COOKIE) == "true":
        event_params["traffic_type"] = "internal"
    payload = {
        "client_id": client_id,
        "events": [{"name": clean_event_name, "params": event_params}],
    }
    Thread(
        target=_post_event,
        kwargs={"api_secret": api_secret, "payload": payload, "event_name": clean_event_name},
        name=f"ga4-{clean_event_name}",
        daemon=True,
    ).start()
    return True
