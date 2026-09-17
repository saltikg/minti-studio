"""Apify contact enrichment helpers for admin-only discovery tools."""

from __future__ import annotations

import os
import re
import time
from typing import Any, Dict, List
import requests


APIFY_API_BASE = "https://api.apify.com/v2"
TRAKK_ACTOR_ID = "trakk/youtube-channel-email-sponsor-leads"
TRAKK_ACTOR_PATH = TRAKK_ACTOR_ID.replace("/", "~")
GENERIC_EMAIL_LOCAL_PARTS = {"info", "hello", "contact", "support", "admin", "team", "press", "hi", "hey", "office"}
PERSON_EMAIL_ROLES = {"creator", "owner", "founder", "personal", "person"}


def _channel_id_from_url(value: str | None) -> str:
    text = str(value or "").strip()
    match = re.search(r"/channel/([^/?#]+)", text)
    if match:
        return match.group(1).strip()
    if text.startswith("UC") and "/" not in text:
        return text
    return ""


def _first_value(row: Dict[str, Any], keys: List[str]) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, "", []):
            return value
    return None


def _list_value(row: Dict[str, Any], key: str, index: int = 0) -> Any:
    value = row.get(key)
    if isinstance(value, list) and len(value) > index:
        return value[index]
    return None


def _normalize_confidence(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed <= 1:
        parsed *= 100
    return max(0, min(100, int(round(parsed))))


def _is_generic_email(email: str | None, email_role: str | None = None) -> bool:
    role = str(email_role or "").strip().lower()
    if role:
        return role not in PERSON_EMAIL_ROLES
    local = str(email or "").split("@", 1)[0].strip().lower()
    return local in GENERIC_EMAIL_LOCAL_PARTS


def _normalize_result(row: Dict[str, Any], fallback_url: str = "") -> Dict[str, Any]:
    channel_url = str(_first_value(row, ["channelUrl", "channel_url", "channel", "url", "inputUrl", "matchedInput"]) or fallback_url or "").strip()
    channel_id = str(_first_value(row, ["channelId", "channel_id", "youtubeChannelId"]) or _channel_id_from_url(channel_url)).strip()
    email = str(_first_value(row, ["email", "primaryEmail", "primary_email", "businessEmail", "contactEmail"]) or "").strip()
    if not email:
        email = str(_list_value(row, "emails") or "").strip()
    email_role = str(_first_value(row, ["email_role", "emailRole", "primaryEmailRole"]) or _list_value(row, "emailRoles") or "").strip()
    email_validation = str(
        _first_value(row, ["email_validation", "emailValidation", "validation", "emailStatus", "primaryEmailValidation"])
        or _list_value(row, "emailValidations")
        or ""
    ).strip()
    email_validation_scope = str(
        _first_value(row, ["email_validation_scope", "emailValidationScope", "primaryEmailValidationScope"])
        or ""
    ).strip()
    email_source_type = str(
        _first_value(row, ["email_source_type", "emailSourceType", "emailSource", "source", "sourceType", "primaryEmailSourceType"])
        or _list_value(row, "emailSourceTypes")
        or ""
    ).strip()
    source_url = str(
        _first_value(row, ["email_source_url", "emailSourceUrl", "evidenceUrl", "emailEvidenceUrl", "sourceUrl", "primaryEmailSourceUrl"])
        or _list_value(row, "emailSourceUrls")
        or ""
    ).strip()
    phone = str(_first_value(row, ["phone", "primaryPhone", "primary_phone", "phoneNumber"]) or _list_value(row, "phones") or "").strip()
    return {
        "channel_id": channel_id,
        "channel_url": channel_url,
        "email": email,
        "email_confidence": _normalize_confidence(_first_value(row, ["email_confidence", "emailConfidence", "confidence", "primaryEmailConfidence"])),
        "email_validation": email_validation,
        "email_validation_scope": email_validation_scope,
        "email_role": email_role,
        "email_source_type": email_source_type,
        "email_source_url": source_url,
        "phone": phone,
        "website": str(_first_value(row, ["website", "websiteUrl", "publicWebsiteUrl"]) or "").strip(),
        "lead_tier": str(_first_value(row, ["lead_tier", "leadTier", "tier", "audienceBand"]) or "").strip(),
        "has_hidden_email": bool(row.get("hasHiddenEmail")),
        "protected_email_status": str(row.get("protectedEmailStatus") or "").strip(),
        "is_generic_email": _is_generic_email(email, email_role),
        "error": "",
    }


def _build_actor_input(channel_urls: List[str]) -> Dict[str, Any]:
    return {
        "scrapeType": "channelContacts",
        "channels": channel_urls,
        "urls": channel_urls,
        "channelUrls": channel_urls,
        "creatorChannels": channel_urls,
        "startUrls": [{"url": url} for url in channel_urls],
        "enrichmentDepth": "deep",
        "saveFilter": "all",
        "returnAllChannels": True,
        "maxCreators": len(channel_urls),
        "maxChannelsTotal": len(channel_urls),
    }


def _request_json(method: str, url: str, *, token: str, timeout: int, **kwargs) -> Any:
    params = dict(kwargs.pop("params", {}) or {})
    params["token"] = token
    response = requests.request(method, url, params=params, timeout=timeout, **kwargs)
    response.raise_for_status()
    if not response.text:
        return None
    return response.json()


def _dataset_items(dataset_id: str, *, token: str, timeout: int = 30) -> List[Dict[str, Any]]:
    url = f"{APIFY_API_BASE}/datasets/{dataset_id}/items"
    payload = _request_json("GET", url, token=token, timeout=timeout, params={"clean": "true"})
    return payload if isinstance(payload, list) else []


def _run_actor_with_poll(actor_input: Dict[str, Any], *, token: str, timeout_seconds: int) -> List[Dict[str, Any]]:
    start_url = f"{APIFY_API_BASE}/acts/{TRAKK_ACTOR_PATH}/runs"
    started = _request_json("POST", start_url, token=token, timeout=30, json=actor_input)
    run_data = (started or {}).get("data") or started or {}
    run_id = run_data.get("id")
    if not run_id:
        raise RuntimeError("Apify run did not return an id.")

    deadline = time.monotonic() + timeout_seconds
    last_status = ""
    while time.monotonic() < deadline:
        run_payload = _request_json("GET", f"{APIFY_API_BASE}/actor-runs/{run_id}", token=token, timeout=30)
        run_data = (run_payload or {}).get("data") or run_payload or {}
        last_status = str(run_data.get("status") or "").upper()
        if last_status in {"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"}:
            break
        time.sleep(8)

    if last_status != "SUCCEEDED":
        raise TimeoutError(f"Apify run did not finish successfully within timeout; status={last_status or 'UNKNOWN'}.")
    dataset_id = run_data.get("defaultDatasetId")
    if not dataset_id:
        return []
    return _dataset_items(str(dataset_id), token=token)


def apify_trakk_enrich(channel_urls: List[str], *, timeout_seconds: int = 240) -> Dict[str, Any]:
    clean_urls = []
    seen = set()
    for url in channel_urls or []:
        value = str(url or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        clean_urls.append(value)
    if not clean_urls:
        return {"results": [], "errors": [], "enriched_count": 0}

    token = (os.getenv("APIFY_TOKEN") or "").strip()
    if not token:
        return {
            "results": [_normalize_result({}, url) | {"error": "APIFY_TOKEN is not configured."} for url in clean_urls],
            "errors": [{"error": "server_config", "message": "APIFY_TOKEN is not configured."}],
            "enriched_count": 0,
        }

    actor_input = _build_actor_input(clean_urls)
    items: List[Dict[str, Any]] = []
    errors: List[Dict[str, str]] = []
    sync_url = f"{APIFY_API_BASE}/acts/{TRAKK_ACTOR_PATH}/run-sync-get-dataset-items"
    try:
        payload = _request_json("POST", sync_url, token=token, timeout=min(70, max(20, timeout_seconds)), json=actor_input)
        if isinstance(payload, list):
            items = payload
    except Exception as exc:
        errors.append({"error": "apify_sync_fallback", "message": str(exc)})
        try:
            items = _run_actor_with_poll(actor_input, token=token, timeout_seconds=timeout_seconds)
        except Exception as poll_exc:
            errors.append({"error": "apify_run_failed", "message": str(poll_exc)})

    normalized = [_normalize_result(item if isinstance(item, dict) else {}, "") for item in items]
    by_url = {str(row.get("channel_url") or "").rstrip("/"): row for row in normalized if row.get("channel_url")}
    by_id = {str(row.get("channel_id") or ""): row for row in normalized if row.get("channel_id")}
    results = []
    for url in clean_urls:
        channel_id = _channel_id_from_url(url)
        row = by_url.get(url.rstrip("/")) or (by_id.get(channel_id) if channel_id else None)
        if row:
            results.append(row)
        else:
            error_message = "; ".join(error["message"] for error in errors[-2:]) if errors else ""
            results.append(_normalize_result({}, url) | {"error": error_message})
    return {
        "results": results,
        "errors": errors,
        "enriched_count": sum(1 for row in results if row.get("email")),
    }
