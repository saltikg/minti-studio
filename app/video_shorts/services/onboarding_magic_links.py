from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from flask import current_app, has_request_context, request

from app.video_shorts.services.db import (
    ensure_onboarding_magic_links_schema,
    get_db,
)
from app.video_shorts.services.trial_copy import (
    DEFAULT_SHARE_TRIAL_DAYS,
    normalize_trial_days,
)

ONBOARDING_MAGIC_LINK_TTL_DAYS = 14
ONBOARDING_MAGIC_LINK_PLAN_ID = "plan_10gb"
ONBOARDING_MAGIC_LINK_SESSION_DAYS = 60


def normalize_outreach_language(value: str | None, *, default: str = "EN") -> str:
    normalized = str(value or "").strip().upper()
    if normalized not in {"TR", "EN"}:
        return default
    return normalized


def generate_onboarding_magic_token() -> str:
    return secrets.token_urlsafe(32)


def hash_onboarding_magic_token(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def onboarding_magic_token_expiry() -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=ONBOARDING_MAGIC_LINK_TTL_DAYS)


def build_onboarding_magic_link_url(token: str) -> str:
    base_url = ""
    if has_request_context():
        base_url = request.url_root.rstrip("/")
    if not base_url:
        base_url = (current_app.config.get("BASE_URL") or "").rstrip("/")
    if base_url.startswith("http://"):
        base_url = "https://" + base_url[len("http://") :]
    return f"{base_url}/onboard/{token}"


def mint_onboarding_magic_link(
    *,
    recipient_email: str,
    recipient_name: str = "",
    share_link_id: Optional[int] = None,
    share_link_token: str = "",
    language: str | None = None,
    trial_days: Any = DEFAULT_SHARE_TRIAL_DAYS,
    conn=None,
) -> Dict[str, Any]:
    normalized_email = str(recipient_email or "").strip().lower()
    if not normalized_email:
        raise ValueError("recipient_email is required")
    normalized_language = normalize_outreach_language(language, default="EN")
    normalized_trial_days = normalize_trial_days(trial_days)
    raw_token = generate_onboarding_magic_token()
    token_hash = hash_onboarding_magic_token(raw_token)
    expires_at = onboarding_magic_token_expiry()
    own_conn = conn is None
    if own_conn:
        conn = get_db()
    try:
        ensure_onboarding_magic_links_schema(conn)
        conn.execute(
            """
            INSERT INTO onboarding_magic_links (
                token_hash,
                recipient_email,
                recipient_name,
                share_link_id,
                share_link_token,
                language,
                trial_days,
                expires_at,
                used_at,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, now())
            """,
            [
                token_hash,
                normalized_email,
                str(recipient_name or "").strip() or None,
                share_link_id,
                str(share_link_token or "").strip() or None,
                normalized_language,
                normalized_trial_days,
                expires_at,
            ],
        )
        if own_conn:
            conn.commit()
    finally:
        if own_conn and conn is not None:
            conn.close()
    return {
        "token": raw_token,
        "token_hash": token_hash,
        "url": build_onboarding_magic_link_url(raw_token),
        "expires_at": expires_at,
        "recipient_email": normalized_email,
        "recipient_name": str(recipient_name or "").strip(),
        "share_link_id": share_link_id,
        "share_link_token": str(share_link_token or "").strip() or None,
        "language": normalized_language,
        "trial_days": normalized_trial_days,
    }
