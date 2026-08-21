from __future__ import annotations

import hashlib
import html
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
PASSWORD_RESET_TOKEN_TTL_HOURS = 1
PASSWORD_RESET_COOLDOWN_SECONDS = 60
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


def password_reset_token_expiry() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=PASSWORD_RESET_TOKEN_TTL_HOURS)


def verification_resend_allowed_at(sent_at: Optional[datetime]) -> Optional[datetime]:
    if not sent_at:
        return None
    if sent_at.tzinfo is None:
        sent_at = sent_at.replace(tzinfo=timezone.utc)
    return sent_at + timedelta(seconds=VERIFY_RESEND_COOLDOWN_SECONDS)


def password_reset_allowed_at(sent_at: Optional[datetime]) -> Optional[datetime]:
    if not sent_at:
        return None
    if sent_at.tzinfo is None:
        sent_at = sent_at.replace(tzinfo=timezone.utc)
    return sent_at + timedelta(seconds=PASSWORD_RESET_COOLDOWN_SECONDS)


def can_resend_verification(sent_at: Optional[datetime]) -> tuple[bool, int]:
    allowed_at = verification_resend_allowed_at(sent_at)
    if not allowed_at:
        return True, 0
    remaining = int((allowed_at - datetime.now(timezone.utc)).total_seconds())
    if remaining <= 0:
        return True, 0
    return False, remaining


def can_send_password_reset(sent_at: Optional[datetime]) -> tuple[bool, int]:
    allowed_at = password_reset_allowed_at(sent_at)
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


def build_dashboard_url() -> str:
    base_url = ""
    if has_request_context():
        base_url = request.url_root.rstrip("/")
    if not base_url:
        base_url = (current_app.config.get("BASE_URL") or "").rstrip("/")
    if base_url:
        if base_url.startswith("http://"):
            base_url = "https://" + base_url[len("http://") :]
        return f"{base_url}{url_for('video_shorts_bp.my_videos_page')}"
    return url_for("video_shorts_bp.my_videos_page", _external=True, _scheme="https")


def _resend_headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "MintiStudio/1.0",
    }


def _resend_payload(
    *,
    to_email: str,
    subject: str,
    html: str,
    text: str,
    reply_to_email: str = "",
) -> dict[str, object]:
    _api_key, mail_from, reply_to = _resolve_mail_settings()
    payload: dict[str, object] = {
        "from": formataddr(("MintiStudio", mail_from)),
        "to": [to_email],
        "subject": subject,
        "html": html,
        "text": text,
    }
    resolved_reply_to = (reply_to_email or "").strip() or reply_to
    if resolved_reply_to:
        payload["reply_to"] = resolved_reply_to
    return payload


def send_resend_email(
    *,
    to_email: str,
    subject: str,
    html: str,
    text: str,
    reply_to_email: str = "",
    error_message: str = "Verification email could not be sent.",
) -> dict[str, object]:
    api_key, mail_from, reply_to = _resolve_mail_settings()
    resolved_reply_to = (reply_to_email or "").strip() or reply_to
    payload = _resend_payload(
        to_email=to_email,
        subject=subject,
        html=html,
        text=text,
        reply_to_email=reply_to_email,
    )
    logger.info(
        "Resend email request prepared: from=%s reply_to=%s to=%s",
        formataddr(("MintiStudio", mail_from)),
        resolved_reply_to or "(empty)",
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
            response_body = response.read().decode("utf-8", errors="replace")
            parsed_body: dict[str, object] = {}
            if response_body:
                try:
                    parsed_body = json.loads(response_body)
                except json.JSONDecodeError:
                    parsed_body = {}
            request_id = (
                response.headers.get("x-request-id")
                or response.headers.get("X-Request-Id")
                or str(parsed_body.get("id") or "").strip()
            )
            return {
                "status_code": status_code,
                "request_id": request_id,
                "response_body": response_body,
            }
    except urllib_error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        logger.exception("Resend email send failed: status=%s body=%s", exc.code, detail)
        raise RuntimeError(error_message) from exc
    except Exception as exc:
        logger.exception("Resend email send failed")
        raise RuntimeError(error_message) from exc


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


def build_password_reset_url(token: str) -> str:
    base_url = ""
    if has_request_context():
        base_url = request.url_root.rstrip("/")
    if not base_url:
        base_url = (current_app.config.get("BASE_URL") or "").rstrip("/")
    if base_url:
        if base_url.startswith("http://"):
            base_url = "https://" + base_url[len("http://") :]
        return f"{base_url}{url_for('video_shorts_bp.reset_password')}?token={token}"
    return url_for("video_shorts_bp.reset_password", token=token, _external=True, _scheme="https")


def send_password_reset_email(*, to_email: str, reset_token: str, recipient_name: str = "") -> None:
    reset_url = build_password_reset_url(reset_token)
    greeting = recipient_name.strip() or "there"
    subject = "Reset your MintiStudio password"
    html = f"""
    <div style="font-family:Arial,sans-serif;line-height:1.6;color:#0f172a;max-width:560px;margin:0 auto;">
      <div style="padding:24px;border:1px solid #dbe4f0;border-radius:16px;background:#ffffff;">
        <div style="font-size:24px;font-weight:700;margin-bottom:12px;">MintiStudio</div>
        <p style="margin:0 0 12px;">Hi {greeting},</p>
        <p style="margin:0 0 16px;">We received a request to reset your password.</p>
        <p style="margin:0 0 20px;">
          <a href="{reset_url}" style="display:inline-block;padding:12px 18px;border-radius:999px;background:#5df0d2;color:#07161d;text-decoration:none;font-weight:700;">Reset password</a>
        </p>
        <p style="margin:0 0 12px;">This link expires in 1 hour.</p>
        <p style="margin:0;color:#475569;font-size:14px;">If you did not request a password reset, you can ignore this email.</p>
      </div>
    </div>
    """.strip()
    text = (
        f"Hi {greeting},\n\n"
        "We received a request to reset your MintiStudio password.\n\n"
        f"Reset password: {reset_url}\n\n"
        "This link expires in 1 hour.\n\n"
        "If you did not request a password reset, you can ignore this email."
    )
    send_resend_email(to_email=to_email, subject=subject, html=html, text=text)


def send_onboarding_magic_link_welcome_email(
    *,
    to_email: str,
    set_password_url: str,
    recipient_name: str = "",
    language: str = "EN",
) -> dict[str, object]:
    normalized_language = (language or "").strip().upper()
    display_name = recipient_name.strip() or ("Merhaba" if normalized_language == "TR" else "there")
    if normalized_language == "TR":
        subject = "Minti Studio hesabınız hazır"
        html_body = f"""
        <div style="font-family:Arial,sans-serif;line-height:1.6;color:#0f172a;max-width:560px;margin:0 auto;">
          <div style="padding:24px;border:1px solid #dbe4f0;border-radius:16px;background:#ffffff;">
            <div style="font-size:24px;font-weight:700;margin-bottom:12px;">Minti Studio</div>
            <p style="margin:0 0 12px;">Merhaba {html.escape(display_name)},</p>
            <p style="margin:0 0 12px;">Minti Studio hesabınız hazır ({html.escape(to_email)}) ve 3 aylık ücretsiz erişiminiz aktif.</p>
            <p style="margin:0 0 12px;">Bu cihazda zaten giriş yapmış durumdasınız. İleride şifreyle giriş yapmak için şifrenizi buradan belirleyebilirsiniz:</p>
            <p style="margin:0 0 20px;">
              <a href="{html.escape(set_password_url)}" style="display:inline-block;padding:12px 18px;border-radius:999px;background:#5df0d2;color:#07161d;text-decoration:none;font-weight:700;">Şifre belirle</a>
            </p>
            <p style="margin:0 0 12px;">Bu e-posta ile Google üzerinden de giriş yapabilirsiniz.</p>
            <p style="margin:0;">Selamlar,<br>Gokhan Saltik<br>Minti Studio</p>
          </div>
        </div>
        """.strip()
        text_body = (
            f"Merhaba {display_name},\n\n"
            f"Minti Studio hesabınız hazır ({to_email}) ve 3 aylık ücretsiz erişiminiz aktif.\n"
            "Bu cihazda zaten giriş yapmış durumdasınız. İleride şifreyle giriş yapmak için "
            "şifrenizi buradan belirleyebilirsiniz:\n"
            f"{set_password_url}\n\n"
            "Bu e-posta ile Google üzerinden de giriş yapabilirsiniz.\n\n"
            "Selamlar,\n"
            "Gokhan Saltik\n"
            "Minti Studio"
        )
    else:
        subject = "Your Minti Studio account is ready"
        html_body = f"""
        <div style="font-family:Arial,sans-serif;line-height:1.6;color:#0f172a;max-width:560px;margin:0 auto;">
          <div style="padding:24px;border:1px solid #dbe4f0;border-radius:16px;background:#ffffff;">
            <div style="font-size:24px;font-weight:700;margin-bottom:12px;">Minti Studio</div>
            <p style="margin:0 0 12px;">Hi {html.escape(display_name)},</p>
            <p style="margin:0 0 12px;">Your Minti Studio account is ready ({html.escape(to_email)}), and your 3 months of complimentary access are active.</p>
            <p style="margin:0 0 12px;">You're already signed in on this device. To set a password for future logins, use this link:</p>
            <p style="margin:0 0 20px;">
              <a href="{html.escape(set_password_url)}" style="display:inline-block;padding:12px 18px;border-radius:999px;background:#5df0d2;color:#07161d;text-decoration:none;font-weight:700;">Set your password</a>
            </p>
            <p style="margin:0 0 12px;">You can also sign in with Google using this email.</p>
            <p style="margin:0;">Best,<br>Gokhan Saltik<br>Minti Studio</p>
          </div>
        </div>
        """.strip()
        text_body = (
            f"Hi {display_name},\n\n"
            f"Your Minti Studio account is ready ({to_email}), and your 3 months of complimentary access are active.\n"
            "You're already signed in on this device. To set a password for future logins, use this link:\n"
            f"{set_password_url}\n\n"
            "You can also sign in with Google using this email.\n\n"
            "Best,\n"
            "Gokhan Saltik\n"
            "Minti Studio"
        )
    return send_resend_email(
        to_email=to_email,
        subject=subject,
        html=html_body,
        text=text_body,
        error_message="Onboarding welcome email could not be sent.",
    )


def send_welcome_email(*, to_email: str, recipient_name: str = "") -> dict[str, object]:
    dashboard_url = build_dashboard_url()
    greeting = recipient_name.strip() or "there"
    subject = "Welcome to MintiStudio 🎬"
    html_body = f"""
    <div style="font-family:Arial,sans-serif;line-height:1.6;color:#0f172a;max-width:560px;margin:0 auto;">
      <div style="padding:24px;border:1px solid #dbe4f0;border-radius:16px;background:#ffffff;">
        <div style="font-size:24px;font-weight:700;margin-bottom:12px;">MintiStudio</div>
        <p style="margin:0 0 12px;">Hi {greeting},</p>
        <p style="margin:0 0 16px;">Welcome to MintiStudio. Your account is active and ready to use.</p>
        <p style="margin:0 0 20px;">
          <a href="{dashboard_url}" style="display:inline-block;padding:12px 18px;border-radius:999px;background:#5df0d2;color:#07161d;text-decoration:none;font-weight:700;">Open dashboard</a>
        </p>
        <p style="margin:0;color:#475569;font-size:14px;">You can now start turning long videos into Shorts and schedule them from your dashboard.</p>
      </div>
    </div>
    """.strip()
    text_body = (
        f"Hi {greeting},\n\n"
        "Welcome to MintiStudio. Your account is active and ready to use.\n\n"
        f"Open dashboard: {dashboard_url}\n\n"
        "You can now start turning long videos into Shorts and schedule them from your dashboard."
    )
    return send_resend_email(
        to_email=to_email,
        subject=subject,
        html=html_body,
        text=text_body,
        error_message="Welcome email could not be sent.",
    )


def send_new_member_admin_email(
    *,
    user_email: str,
    signup_method: str,
    plan: str,
    activated_at: str,
) -> dict[str, object]:
    admin_email = (os.getenv("ADMIN_NOTIFICATION_EMAIL") or "info@mintistudio.com").strip()
    subject = "New member activated"
    escaped_user_email = html.escape(user_email)
    escaped_signup_method = html.escape(signup_method)
    escaped_plan = html.escape(plan)
    escaped_activated_at = html.escape(activated_at)
    html_body = f"""
    <div style="font-family:Arial,sans-serif;line-height:1.6;color:#0f172a;max-width:560px;margin:0 auto;">
      <div style="padding:24px;border:1px solid #dbe4f0;border-radius:16px;background:#ffffff;">
        <div style="font-size:24px;font-weight:700;margin-bottom:12px;">MintiStudio</div>
        <p style="margin:0 0 16px;">A new member account was activated.</p>
        <ul style="padding-left:18px;margin:0;">
          <li>Email: {escaped_user_email}</li>
          <li>Signup method: {escaped_signup_method}</li>
          <li>Plan: {escaped_plan}</li>
          <li>Activated at (UTC): {escaped_activated_at}</li>
        </ul>
      </div>
    </div>
    """.strip()
    text_body = (
        "A new member account was activated.\n\n"
        f"Email: {user_email}\n"
        f"Signup method: {signup_method}\n"
        f"Plan: {plan}\n"
        f"Activated at (UTC): {activated_at}"
    )
    return send_resend_email(
        to_email=admin_email,
        subject=subject,
        html=html_body,
        text=text_body,
        error_message="Admin notification email could not be sent.",
    )


def send_membership_activated_emails(*, user_id: str, signup_method: str) -> None:
    from app.video_shorts.services.db import (
        ensure_auth_user_schema,
        ensure_storage_user_schema,
        get_db_readonly,
    )

    conn = get_db_readonly()
    try:
        ensure_storage_user_schema(conn)
        ensure_auth_user_schema(conn)
        row = conn.execute(
            """
            SELECT
                CAST(id AS VARCHAR),
                email,
                username,
                name,
                COALESCE(plan_id, '')
            FROM shorts_users
            WHERE CAST(id AS VARCHAR) = ?
            LIMIT 1
            """,
            [str(user_id)],
        ).fetchone()
    finally:
        conn.close()

    if not row:
        logger.warning("Membership activation email skipped: user not found user_id=%s", user_id)
        return

    user_email = (row[1] or row[2] or "").strip()
    recipient_name = (row[3] or row[2] or row[1] or "").strip()
    plan = (row[4] or "").strip() or "unknown"
    activated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    admin_email = (os.getenv("ADMIN_NOTIFICATION_EMAIL") or "info@mintistudio.com").strip()

    if user_email:
        try:
            result = send_welcome_email(to_email=user_email, recipient_name=recipient_name)
            logger.info(
                "Membership activation welcome email sent: user_id=%s to=%s status=%s request_id=%s",
                user_id,
                user_email,
                result.get("status_code"),
                result.get("request_id") or "(missing)",
            )
        except Exception:
            logger.exception("Membership activation welcome email failed: user_id=%s to=%s", user_id, user_email)
    else:
        logger.warning("Membership activation welcome email skipped: missing recipient user_id=%s", user_id)

    try:
        result = send_new_member_admin_email(
            user_email=user_email or "(missing)",
            signup_method=signup_method,
            plan=plan,
            activated_at=activated_at,
        )
        logger.info(
            "Membership activation admin email sent: user_id=%s to=%s status=%s request_id=%s signup_method=%s plan=%s",
            user_id,
            admin_email,
            result.get("status_code"),
            result.get("request_id") or "(missing)",
            signup_method,
            plan,
        )
    except Exception:
        logger.exception(
            "Membership activation admin email failed: user_id=%s to=%s signup_method=%s",
            user_id,
            admin_email,
            signup_method,
        )


def send_contact_email(*, name: str, email: str, message: str) -> None:
    sender_name = (name or "").strip()
    sender_email = (email or "").strip()
    message_body = (message or "").strip()
    subject = f"Contact form: {sender_name}"
    escaped_name = html.escape(sender_name)
    escaped_email = html.escape(sender_email)
    escaped_message = html.escape(message_body).replace("\n", "<br>")
    html_body = f"""
    <div style="font-family:Arial,sans-serif;line-height:1.6;color:#0f172a;max-width:640px;margin:0 auto;">
      <div style="padding:24px;border:1px solid #dbe4f0;border-radius:16px;background:#ffffff;">
        <div style="font-size:24px;font-weight:700;margin-bottom:16px;">New contact form message</div>
        <p style="margin:0 0 10px;"><strong>Name:</strong> {escaped_name}</p>
        <p style="margin:0 0 10px;"><strong>Email:</strong> {escaped_email}</p>
        <p style="margin:0 0 8px;"><strong>Message:</strong></p>
        <div style="white-space:normal;">{escaped_message}</div>
      </div>
    </div>
    """.strip()
    text_body = (
        "New contact form message\n\n"
        f"Name: {sender_name}\n"
        f"Email: {sender_email}\n\n"
        f"{message_body}"
    )
    send_resend_email(
        to_email="info@mintistudio.com",
        subject=subject,
        html=html_body,
        text=text_body,
        reply_to_email=sender_email,
        error_message="Contact message could not be sent.",
    )
