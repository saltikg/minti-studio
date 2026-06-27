from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from email.utils import formataddr
from typing import Optional
from urllib import error as urllib_error
from urllib import request as urllib_request

from flask import current_app, has_request_context, request, url_for


logger = logging.getLogger(__name__)

VERIFY_TOKEN_TTL_HOURS = 24
VERIFY_RESEND_COOLDOWN_SECONDS = 60
RESEND_API_URL = "https://api.resend.com/emails"


def _resolve_mail_settings() -> tuple[str, str, str]:
    api_key = (os.getenv("RESEND_API_KEY") or "").strip()
    mail_from = (os.getenv("MAIL_FROM") or "").strip()
    reply_to = (os.getenv("MAIL_REPLY_TO") or "").strip()
    if not api_key:
        logger.error("Resend email send aborted: RESEND_API_KEY is not set")
        raise RuntimeError("RESEND_API_KEY is not configured")
    if not mail_from:
        logger.error("Resend email send aborted: MAIL_FROM is not set")
        raise RuntimeError("MAIL_FROM is not configured")
    if not reply_to:
        logger.warning("Resend email send proceeding without MAIL_REPLY_TO")
    return api_key, mail_from, reply_to


def generate_email_verification_token() -> str:
    return secrets.token_urlsafe(32)


def hash_email_verification_token(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def verification_token_expiry() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=VERIFY_TOKEN_TTL_HOURS)


def verification_resend_allowed_at(sent_at: Optional[datetime]) -> Optional[datetime]:
    if not sent_at:
        return None
    if sent_at.tzinfo is None:
        sent_at = sent_at.replace(tzinfo=timezone.utc)
    return sent_at + timedelta(seconds=VERIFY_RESEND_COOLDOWN_SECONDS)


def can_resend_verification(sent_at: Optional[datetime]) -> tuple[bool, int]:
    allowed_at = verification_resend_allowed_at(sent_at)
    if not allowed_at:
        return True, 0
    remaining = int((allowed_at - datetime.now(timezone.utc)).total_seconds())
    if remaining <= 0:
        return True, 0
    return False, remaining


def build_verification_url(token: str) -> str:
    base_url = ""
    if has_request_context():
        base_url = request.url_root.rstrip("/")
    if not base_url:
        base_url = (current_app.config.get("BASE_URL") or "").rstrip("/")
    if base_url:
        if base_url.startswith("http://"):
            base_url = "https://" + base_url[len("http://") :]
        return f"{base_url}{url_for('video_shorts_bp.verify_email')}?token={token}"
    return url_for("video_shorts_bp.verify_email", token=token, _external=True, _scheme="https")


def _resend_headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def _resend_payload(*, to_email: str, subject: str, html: str, text: str) -> dict[str, object]:
    _api_key, mail_from, reply_to = _resolve_mail_settings()
    payload: dict[str, object] = {
        "from": formataddr(("MintiStudio", mail_from)),
        "to": [to_email],
        "subject": subject,
        "html": html,
        "text": text,
    }
    if reply_to:
        payload["reply_to"] = reply_to
    return payload


def send_resend_email(*, to_email: str, subject: str, html: str, text: str) -> None:
    api_key, mail_from, reply_to = _resolve_mail_settings()
    payload = _resend_payload(to_email=to_email, subject=subject, html=html, text=text)
    logger.info(
        "Resend email request prepared: from=%s reply_to=%s to=%s",
        formataddr(("MintiStudio", mail_from)),
        reply_to or "(empty)",
        to_email,
    )
    body = json.dumps(payload).encode("utf-8")
    req = urllib_request.Request(
        RESEND_API_URL,
        data=body,
        headers=_resend_headers(api_key),
        method="POST",
    )
    try:
        with urllib_request.urlopen(req, timeout=15) as response:
            status_code = getattr(response, "status", 200)
            if status_code >= 400:
                raise RuntimeError(f"Resend returned status {status_code}")
    except urllib_error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        logger.exception("Resend email send failed: status=%s body=%s", exc.code, detail)
        raise RuntimeError("Verification email could not be sent.") from exc
    except Exception as exc:
        logger.exception("Resend email send failed")
        raise RuntimeError("Verification email could not be sent.") from exc


def send_verification_email(*, to_email: str, verify_token: str, recipient_name: str = "") -> None:
    verify_url = build_verification_url(verify_token)
    greeting = recipient_name.strip() or "there"
    subject = "Verify your MintiStudio email"
    html = f"""
    <div style="font-family:Arial,sans-serif;line-height:1.6;color:#0f172a;max-width:560px;margin:0 auto;">
      <div style="padding:24px;border:1px solid #dbe4f0;border-radius:16px;background:#ffffff;">
        <div style="font-size:24px;font-weight:700;margin-bottom:12px;">MintiStudio</div>
        <p style="margin:0 0 12px;">Hi {greeting},</p>
        <p style="margin:0 0 16px;">Please verify your email address to finish creating your account.</p>
        <p style="margin:0 0 20px;">
          <a href="{verify_url}" style="display:inline-block;padding:12px 18px;border-radius:999px;background:#5df0d2;color:#07161d;text-decoration:none;font-weight:700;">Verify email</a>
        </p>
        <p style="margin:0 0 12px;">This link expires in 24 hours.</p>
        <p style="margin:0;color:#475569;font-size:14px;">If you did not create this account, you can ignore this email.</p>
      </div>
    </div>
    """.strip()
    text = (
        f"Hi {greeting},\n\n"
        "Please verify your email address to finish creating your MintiStudio account.\n\n"
        f"Verify email: {verify_url}\n\n"
        "This link expires in 24 hours.\n\n"
        "If you did not create this account, you can ignore this email."
    )
    send_resend_email(to_email=to_email, subject=subject, html=html, text=text)
