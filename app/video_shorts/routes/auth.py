import logging
import re
from datetime import datetime, timezone
from functools import wraps

from flask import abort, current_app, flash, g, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash
from google_auth_oauthlib.flow import Flow
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
from typing import Optional
import duckdb
from urllib.parse import urlparse
from uuid import uuid4

from app.video_shorts import video_shorts_bp
from app.video_shorts.config import (
    DEFAULT_USER_STORAGE_LIMIT,
    DEFAULT_USER_PLAN_ID,
    GOOGLE_OAUTH_CLIENT_ID,
    GOOGLE_OAUTH_CLIENT_SECRET,
    GOOGLE_OAUTH_REDIRECT_URI,
    GOOGLE_OAUTH_SCOPES,
    SIGNUPS_ENABLED,
    SHORTS_OVERVIEW_STATS_TTL_MINUTES,
    SHORTS_OVERVIEW_STATS_MAX_VIDEOS,
    SHORTS_OVERVIEW_QUOTA_COOLDOWN_HOURS,
    _env_bool,
)
from app.video_shorts.services.db import (
    ensure_auth_user_schema,
    ensure_onboarding_magic_links_schema,
    ensure_storage_user_schema,
    ensure_user_events_schema,
    get_db,
    get_db_readonly,
    table_columns,
)
from app.video_shorts.services.brands import (
    current_brand_id,
    create_brand as create_brand_record,
    ensure_brand_for_user,
    ensure_brand_schema,
    list_user_brands,
    load_brand_context,
    set_active_brand_for_user,
    set_default_brand_for_user,
)
from app.video_shorts.services.shorts_overview_quota import get_shorts_overview_quota_state
from app.video_shorts.services.usage_metering import get_usage_snapshot
from app.video_shorts.services.email_verification import (
    build_password_reset_url,
    can_resend_verification,
    can_send_password_reset,
    generate_email_verification_token,
    hash_email_verification_token,
    send_autopilot_customer_admin_email,
    password_reset_token_expiry,
    send_membership_activated_emails,
    send_onboarding_magic_link_welcome_email,
    send_verification_email,
    send_password_reset_email,
    send_contact_email,
    verification_token_expiry,
)
from app.video_shorts.services.auth_protection import (
    RateLimitRule,
    check_rate_limits,
    turnstile_enabled,
    turnstile_site_key,
    verify_turnstile_token,
)
from app.video_shorts.services.billing import (
    load_billing_user_state,
    user_has_managed_subscription,
)
from app.video_shorts.services.onboarding_magic_links import (
    ONBOARDING_MAGIC_LINK_PLAN_ID,
    hash_onboarding_magic_token,
    normalize_outreach_language,
)
from app.video_shorts.services.trial_copy import (
    DEFAULT_SHARE_TRIAL_DAYS,
    normalize_trial_days,
)
from app.video_shorts.services.user_events import track_event
from app.video_shorts.services.youtube_oauth import (
    build_oauth_flow,
    has_refresh_token,
    is_reauth_required,
    store_refresh_token,
)
from app.video_shorts.services.timezones import DEFAULT_TIME_ZONE, TIMEZONE_LABELS, TIMEZONE_OPTIONS
from src.trends.instagram_tokens import get_instagram_credentials

logger = logging.getLogger(__name__)

COMMON_WEAK_PASSWORDS = {
    "12345678",
    "123456789",
    "1234567890",
    "00000000",
    "11111111",
    "22222222",
    "33333333",
    "44444444",
    "55555555",
    "66666666",
    "77777777",
    "88888888",
    "99999999",
    "password",
    "password1",
    "passw0rd",
    "qwerty",
    "qwertyui",
    "qwerty123",
    "letmein",
    "letmein1",
    "welcome1",
    "admin123",
    "iloveyou",
    "abc12345",
    "asdfghjk",
    "abcdefgh",
}
AUTOPILOT_SERVICE_TIERS = {15, 30, 45, 60}
SELF_SERVE_PLAN_IDS = {"plan_free", "plan_2gb", "plan_10gb"}
SERVICE_MODE_VALUES = {"autopilot", "self"}

LOGIN_RATE_LIMITS = [RateLimitRule(limit=5, window_seconds=60)]
REGISTER_RATE_LIMITS = [RateLimitRule(limit=5, window_seconds=60)]
FORGOT_PASSWORD_RATE_LIMITS = [RateLimitRule(limit=1, window_seconds=60), RateLimitRule(limit=5, window_seconds=3600)]
RESEND_VERIFICATION_RATE_LIMITS = [RateLimitRule(limit=1, window_seconds=60), RateLimitRule(limit=5, window_seconds=3600)]
RESET_PASSWORD_RATE_LIMITS = [RateLimitRule(limit=5, window_seconds=3600)]
CONTACT_RATE_LIMITS = [RateLimitRule(limit=3, window_seconds=60), RateLimitRule(limit=10, window_seconds=3600)]


def _format_size_bytes(num: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024:
            return f"{num:.2f} {unit}"
        num /= 1024
    return f"{num:.2f} PB"


def _current_user():
    if hasattr(g, "vs_current_user"):
        return g.vs_current_user
    user_id = session.get("vs_user_id")
    if not user_id:
        g.vs_current_user = None
        return None
    select_sql = """
        SELECT
          CAST(id AS VARCHAR),
          username,
          name,
          email,
          plan_id,
          custom_limit_bytes,
          role,
          time_zone,
          COALESCE(email_verified, FALSE)
        FROM shorts_users
        WHERE id = ?
        """
    try:
        conn = get_db_readonly()
    except Exception as exc:
        logger.warning("Video shorts auth DB unavailable while loading session user: %s", exc)
        g.vs_current_user = None
        return None
    try:
        user_columns = table_columns(conn, "shorts_users")
        if "onboarding_dismissed" in user_columns:
            select_sql = """
                SELECT
                  CAST(id AS VARCHAR),
                  username,
                  name,
                  email,
                  plan_id,
                  custom_limit_bytes,
                  role,
                  time_zone,
                  COALESCE(email_verified, FALSE),
                  COALESCE(onboarding_dismissed, FALSE),
                  service_mode,
                  service_tier,
                  pending_service_intent,
                  pending_service_tier
                FROM shorts_users
                WHERE id = ?
            """
        row = conn.execute(select_sql, [user_id]).fetchone()
    except Exception as exc:
        conn.close()
        try:
            conn = get_db()
            try:
                ensure_storage_user_schema(conn)
                ensure_auth_user_schema(conn)
                row = conn.execute(select_sql, [user_id]).fetchone()
            finally:
                conn.close()
        except Exception as inner_exc:
            logger.warning(
                "Video shorts auth lookup failed while loading session user: %s (fallback: %s)",
                exc,
                inner_exc,
            )
            g.vs_current_user = None
            return None
    else:
        conn.close()
    if not row:
        session.pop("vs_user_id", None)
        g.vs_current_user = None
        return None
    g.vs_current_user = {
        "id": row[0],
        "username": row[1],
        "name": row[2],
        "email": row[3],
        "plan_id": row[4],
        "custom_limit_bytes": row[5],
        "role": row[6] or "member",
        "time_zone": row[7] or DEFAULT_TIME_ZONE,
        "email_verified": bool(row[8]) if len(row) > 8 else False,
        "onboarding_dismissed": bool(row[9]) if len(row) > 9 else False,
        "service_mode": str(row[10] or "").strip().lower() if len(row) > 10 and row[10] else "",
        "service_tier": int(row[11]) if len(row) > 11 and row[11] is not None else None,
        "pending_service_intent": str(row[12] or "").strip().lower() if len(row) > 12 and row[12] else "",
        "pending_service_tier": int(row[13]) if len(row) > 13 and row[13] is not None else None,
    }
    return g.vs_current_user


def _mask_email_address(email: str) -> str:
    value = (email or "").strip()
    if "@" not in value:
        return value
    local, domain = value.split("@", 1)
    if len(local) <= 2:
        local_masked = local[:1] + "•"
    else:
        local_masked = local[:2] + ("•" * max(1, len(local) - 2))
    return f"{local_masked}@{domain}"


def _normalize_auth_email(value: str) -> str:
    return (value or "").strip().lower()


def _normalize_service_intent(value: str | None) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in SERVICE_MODE_VALUES else ""


def _normalize_service_tier(value: object) -> Optional[int]:
    try:
        tier = int(value)
    except (TypeError, ValueError):
        return None
    return tier if tier in AUTOPILOT_SERVICE_TIERS else None


def _normalize_self_serve_plan(value: str | None) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in SELF_SERVE_PLAN_IDS else ""


def _stash_auth_choice(*, intent: str = "", tier: Optional[int] = None, plan_id: str = "") -> None:
    normalized_intent = _normalize_service_intent(intent)
    normalized_tier = _normalize_service_tier(tier) if normalized_intent == "autopilot" else None
    normalized_plan = _normalize_self_serve_plan(plan_id)
    if normalized_intent:
        session["vs_pending_service_intent"] = normalized_intent
    else:
        session.pop("vs_pending_service_intent", None)
    if normalized_tier is not None:
        session["vs_pending_service_tier"] = normalized_tier
    else:
        session.pop("vs_pending_service_tier", None)
    if normalized_plan:
        session["vs_pending_plan_id"] = normalized_plan
    else:
        session.pop("vs_pending_plan_id", None)


def _current_auth_choice(*, include_request_values: bool = False) -> dict[str, object]:
    request_values = request.values if include_request_values else request.args
    intent = _normalize_service_intent(request_values.get("intent"))
    if not intent:
        intent = _normalize_service_intent(session.get("vs_pending_service_intent"))
    tier = _normalize_service_tier(request_values.get("tier"))
    if tier is None:
        tier = _normalize_service_tier(session.get("vs_pending_service_tier"))
    plan_id = _normalize_self_serve_plan(request_values.get("plan"))
    if not plan_id:
        plan_id = _normalize_self_serve_plan(session.get("vs_pending_plan_id"))
    if intent != "autopilot":
        tier = None
    return {
        "intent": intent,
        "tier": tier,
        "plan_id": plan_id,
    }


def _build_auth_route_kwargs(*, include_next: bool = False) -> dict[str, object]:
    choice = _current_auth_choice()
    route_kwargs: dict[str, object] = {}
    if choice["intent"]:
        route_kwargs["intent"] = choice["intent"]
    if choice["tier"] is not None:
        route_kwargs["tier"] = choice["tier"]
    if choice["plan_id"]:
        route_kwargs["plan"] = choice["plan_id"]
    if include_next:
        nxt = _normalize_next_url(request.args.get("next"))
        if nxt:
            route_kwargs["next"] = nxt
    return route_kwargs


def _persist_pending_service_choice(
    conn,
    *,
    user_id: str,
    intent: str = "",
    tier: Optional[int] = None,
) -> None:
    normalized_intent = _normalize_service_intent(intent)
    normalized_tier = _normalize_service_tier(tier) if normalized_intent == "autopilot" else None
    if not normalized_intent:
        return
    conn.execute(
        """
        UPDATE shorts_users
        SET pending_service_intent = ?,
            pending_service_tier = ?,
            updated_at = now()
        WHERE CAST(id AS VARCHAR) = ?
          AND COALESCE(service_mode, '') = ''
        """,
        [normalized_intent, normalized_tier, str(user_id)],
    )


def _clear_pending_service_choice() -> None:
    session.pop("vs_pending_service_intent", None)
    session.pop("vs_pending_service_tier", None)
    session.pop("vs_pending_plan_id", None)


def _build_service_mode_context() -> dict[str, object]:
    user = _current_user()
    if not user:
        return {"show_modal": False}
    service_mode = _normalize_service_intent(user.get("service_mode"))
    if service_mode:
        return {"show_modal": False}
    pending_intent = _normalize_service_intent(user.get("pending_service_intent")) or _normalize_service_intent(
        session.get("vs_pending_service_intent")
    )
    pending_tier = _normalize_service_tier(user.get("pending_service_tier"))
    if pending_tier is None:
        pending_tier = _normalize_service_tier(session.get("vs_pending_service_tier"))
    selected_tier = pending_tier or 15
    return {
        "show_modal": True,
        "auto_open": True,
        "preselected_mode": pending_intent,
        "preselected_tier": selected_tier,
        "show_autopilot_confirmation": pending_intent == "autopilot",
    }


def _is_sequential_digits(value: str) -> bool:
    if not value.isdigit() or len(value) < 8:
        return False
    ascending = "01234567890"
    descending = "09876543210"
    return value in ascending or value in descending


def _is_repeated_single_char(value: str) -> bool:
    return bool(value) and len(set(value)) == 1


def _normalize_password_token(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").strip().lower())


def _validate_registration_password(email: str, password: str) -> Optional[str]:
    if len(password or "") < 8:
        return "Password must be at least 8 characters."

    lowered = (password or "").strip().lower()
    compact = _normalize_password_token(password)
    if lowered in COMMON_WEAK_PASSWORDS or compact in COMMON_WEAK_PASSWORDS:
        return "This password is too common. Please choose something harder to guess."
    if _is_repeated_single_char(compact):
        return "This password is too common. Please choose something harder to guess."
    if _is_sequential_digits(compact):
        return "This password is too common. Please choose something harder to guess."

    normalized_email = _normalize_auth_email(email)
    email_local = normalized_email.split("@", 1)[0] if "@" in normalized_email else normalized_email
    normalized_local = _normalize_password_token(email_local)
    if normalized_local:
        if compact == normalized_local:
            return "This password is too easy to guess from your email. Please choose a different one."
        if compact.startswith(normalized_local) and len(compact) - len(normalized_local) <= 3:
            remainder = compact[len(normalized_local):]
            if not remainder or remainder.isdigit():
                return "This password is too easy to guess from your email. Please choose a different one."
    return None


def _lookup_user_by_email(email: str):
    email = _normalize_auth_email(email)
    conn = get_db()
    try:
        ensure_storage_user_schema(conn)
        ensure_auth_user_schema(conn)
        row = conn.execute(
            """
            SELECT
                CAST(id AS VARCHAR),
                username,
                name,
                email,
                password_hash,
                COALESCE(email_verified, FALSE),
                email_verification_token_hash,
                email_verification_expires_at,
                email_verification_sent_at,
                password_reset_sent_at,
                google_sub
            FROM shorts_users
            WHERE lower(email) = lower(?)
               OR lower(username) = lower(?)
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            [email, email],
        ).fetchone()
        return row
    finally:
        conn.close()


def _default_name_from_email(email: str) -> str:
    normalized_email = _normalize_auth_email(email)
    local_part = normalized_email.split("@", 1)[0] if "@" in normalized_email else normalized_email
    cleaned = local_part.replace(".", " ").replace("_", " ").replace("-", " ").strip()
    return cleaned.title() or "Minti Creator"


def _establish_authenticated_session(*, user_id: str, brand_id: Optional[str]) -> None:
    session["vs_user_id"] = str(user_id)
    if brand_id:
        session["vs_brand_id"] = str(brand_id)
    else:
        session.pop("vs_brand_id", None)
    session.permanent = True


def _render_onboarding_magic_link_status_page(*, status: str, status_code: int = 200):
    normalized_status = (status or "").strip().lower() or "invalid"
    allowed_statuses = {"invalid", "expired", "used"}
    if normalized_status not in allowed_statuses:
        normalized_status = "invalid"
    return (
        render_template("vs_onboarding_magic_link_status.html", link_status=normalized_status),
        status_code,
    )


def _create_or_grant_magic_link_user(
    conn,
    *,
    recipient_email: str,
    recipient_name: str = "",
    existing_user_id: Optional[str] = None,
) -> tuple[str, Optional[str]]:
    normalized_email = _normalize_auth_email(recipient_email)
    if not normalized_email:
        raise ValueError("recipient_email is required")
    desired_name = str(recipient_name or "").strip() or _default_name_from_email(normalized_email)
    ensure_storage_user_schema(conn)
    ensure_auth_user_schema(conn)
    ensure_brand_schema(conn)
    resolved_existing_user_id = str(existing_user_id or "").strip()
    if not resolved_existing_user_id:
        existing_user = conn.execute(
            """
            SELECT CAST(id AS VARCHAR)
            FROM shorts_users
            WHERE lower(email) = lower(?)
               OR lower(username) = lower(?)
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            [normalized_email, normalized_email],
        ).fetchone()
        resolved_existing_user_id = str(existing_user[0] or "").strip() if existing_user else ""
    if resolved_existing_user_id:
        user_id = resolved_existing_user_id
        conn.execute(
            """
            UPDATE shorts_users
            SET name = COALESCE(NULLIF(name, ''), ?),
                email = COALESCE(NULLIF(email, ''), ?),
                username = COALESCE(NULLIF(username, ''), ?),
                plan_id = ?,
                email_verified = TRUE,
                email_verified_at = COALESCE(email_verified_at, now()),
                email_verification_token_hash = NULL,
                email_verification_expires_at = NULL,
                updated_at = now()
            WHERE id = ?
            """,
            [
                desired_name,
                normalized_email,
                normalized_email,
                ONBOARDING_MAGIC_LINK_PLAN_ID,
                user_id,
            ],
        )
    else:
        user_id = str(uuid4())
        conn.execute(
            """
            INSERT INTO shorts_users (
                id,
                username,
                password_hash,
                name,
                email,
                role,
                plan_id,
                email_verified,
                email_verified_at,
                created_at
            )
            VALUES (?, ?, NULL, ?, ?, 'member', ?, TRUE, now(), now())
            """,
            [
                user_id,
                normalized_email,
                desired_name,
                normalized_email,
                ONBOARDING_MAGIC_LINK_PLAN_ID,
            ],
        )
    brand = ensure_brand_for_user(conn, user_id=user_id, user_name=desired_name)
    return user_id, brand["id"] if brand else None


def _client_ip() -> str:
    forwarded_ip = (request.headers.get("CF-Connecting-IP") or "").strip()
    if forwarded_ip:
        return forwarded_ip
    xff = (request.headers.get("X-Forwarded-For") or "").strip()
    if xff:
        return xff.split(",", 1)[0].strip()
    return (request.remote_addr or "").strip()


def _turnstile_token() -> str:
    return (request.form.get("cf-turnstile-response") or "").strip()


def _verify_turnstile_or_fail() -> bool:
    if not turnstile_enabled():
        logger.error("Turnstile verification skipped: TURNSTILE_SITE_KEY is not set")
        return False
    return verify_turnstile_token(token=_turnstile_token(), remote_ip=_client_ip())


def _rate_limit_key(*parts: str) -> list[str]:
    items = [_client_ip()]
    for part in parts:
        normalized = (part or "").strip().lower()
        if normalized:
            items.append(normalized)
    return items


def _resend_verification_for_user(user_row, *, force: bool = False) -> tuple[bool, str, int]:
    user_id = user_row[0]
    email = _normalize_auth_email(user_row[3] or user_row[1] or "")
    display_name = (user_row[2] or user_row[1] or "").strip()
    sent_at = user_row[8]
    if sent_at and getattr(sent_at, "tzinfo", None) is None:
        sent_at = sent_at.replace(tzinfo=timezone.utc)
    if not force:
        allowed, retry_after = can_resend_verification(sent_at)
        if not allowed:
            return False, f"Please wait {retry_after} seconds before requesting another email.", retry_after
    verify_token = generate_email_verification_token()
    token_hash = hash_email_verification_token(verify_token)
    expires_at = verification_token_expiry()
    conn = get_db()
    try:
        ensure_storage_user_schema(conn)
        ensure_auth_user_schema(conn)
        conn.execute(
            """
            UPDATE shorts_users
            SET email_verification_token_hash = ?,
                email_verification_expires_at = ?,
                email_verification_sent_at = now(),
                updated_at = now()
            WHERE id = ?
            """,
            [token_hash, expires_at, user_id],
        )
        conn.commit()
    finally:
        conn.close()
    send_verification_email(to_email=email, verify_token=verify_token, recipient_name=display_name)
    return True, "Verification email sent.", 0


def _issue_password_reset_for_user(user_row) -> tuple[bool, int]:
    user_id = user_row[0]
    email = _normalize_auth_email(user_row[3] or user_row[1] or "")
    display_name = (user_row[2] or user_row[1] or "").strip()
    sent_at = user_row[9] if len(user_row) > 9 else None
    if sent_at and getattr(sent_at, "tzinfo", None) is None:
        sent_at = sent_at.replace(tzinfo=timezone.utc)
    allowed, retry_after = can_send_password_reset(sent_at)
    if not allowed:
        return False, retry_after

    reset_token = generate_email_verification_token()
    token_hash = hash_email_verification_token(reset_token)
    expires_at = password_reset_token_expiry()
    conn = get_db()
    try:
        ensure_storage_user_schema(conn)
        ensure_auth_user_schema(conn)
        conn.execute(
            """
            UPDATE shorts_users
            SET password_reset_token_hash = ?,
                password_reset_expires_at = ?,
                password_reset_sent_at = now(),
                updated_at = now()
            WHERE id = ?
            """,
            [token_hash, expires_at, user_id],
        )
        conn.commit()
    finally:
        conn.close()
    send_password_reset_email(to_email=email, reset_token=reset_token, recipient_name=display_name)
    return True, 0


def _create_password_reset_token_for_user(conn, *, user_id: str) -> tuple[str, datetime]:
    reset_token = generate_email_verification_token()
    token_hash = hash_email_verification_token(reset_token)
    expires_at = password_reset_token_expiry()
    ensure_storage_user_schema(conn)
    ensure_auth_user_schema(conn)
    conn.execute(
        """
        UPDATE shorts_users
        SET password_reset_token_hash = ?,
            password_reset_expires_at = ?,
            password_reset_sent_at = now(),
            updated_at = now()
        WHERE CAST(id AS VARCHAR) = ?
        """,
        [token_hash, expires_at, str(user_id)],
    )
    return reset_token, expires_at


def _render_login_page(*, resend_email: str = "", prefill_email: str = "", status_code: int = 200):
    route_kwargs = _build_auth_route_kwargs(include_next=True)
    return (
        render_template(
            "vs_login.html",
            resend_email=resend_email,
            prefill_email=prefill_email,
            auth_intent=route_kwargs.get("intent", ""),
            auth_tier=route_kwargs.get("tier"),
            auth_plan=route_kwargs.get("plan", ""),
            register_url=url_for("video_shorts_bp.register", **route_kwargs),
            google_login_url=url_for("video_shorts_bp.google_login", **route_kwargs),
        ),
        status_code,
    )


def _render_register_page(*, pending_email: str = "", status_code: int = 200):
    route_kwargs = _build_auth_route_kwargs(include_next=True)
    return (
        render_template(
            "vs_register.html",
            pending_email=pending_email,
            signups_enabled=_signups_enabled(),
            signup_disabled_message=_signup_disabled_message(),
            auth_intent=route_kwargs.get("intent", ""),
            auth_tier=route_kwargs.get("tier"),
            auth_plan=route_kwargs.get("plan", ""),
            login_url=url_for("video_shorts_bp.login", **route_kwargs),
            google_login_url=url_for("video_shorts_bp.google_login", **route_kwargs),
        ),
        status_code,
    )


def _signups_enabled() -> bool:
    return _env_bool("SIGNUPS_ENABLED", SIGNUPS_ENABLED, warn_invalid=True, logger=current_app.logger)


def _signup_disabled_message() -> str:
    return "We're at capacity right now — leave your email and we'll let you know when a spot opens."


def _log_signup_refusal(*, method: str, email: str = "", source: str = "") -> None:
    current_app.logger.warning(
        "Signup refused because signups are disabled method=%s source=%s email=%s",
        method,
        source or "unknown",
        (email or "").strip().lower(),
    )


def _render_forgot_password_page(*, prefill_email: str = "", sent: bool = False, status_code: int = 200):
    return (
        render_template("vs_forgot_password.html", prefill_email=prefill_email, sent=sent),
        status_code,
    )


def _render_check_email_page(
    *,
    email: str,
    verification_invalid: bool = False,
    verification_expired: bool = False,
    delivery_failed: bool = False,
    resend_context: str = "check_email",
    status_code: int = 200,
):
    return (
        render_template(
            "vs_check_email.html",
            email=email,
            masked_email=_mask_email_address(email),
            verification_invalid=verification_invalid,
            verification_expired=verification_expired,
            delivery_failed=delivery_failed,
            resend_email=email,
            resend_context=resend_context,
        ),
        status_code,
    )


def _current_brand():
    if hasattr(g, "vs_current_brand"):
        return g.vs_current_brand
    user = _current_user()
    if not user:
        g.vs_current_brand = None
        g.vs_brands = []
        return None
    brand, brands = load_brand_context(
        user_id=user.get("id"),
        user_name=user.get("name") or user.get("username"),
        requested_brand_id=session.get("vs_brand_id"),
    )
    g.vs_current_brand = brand
    g.vs_brands = brands
    if brand:
        session["vs_brand_id"] = brand["id"]
    else:
        session.pop("vs_brand_id", None)
    return brand


def _is_authenticated():
    return _current_user() is not None


def require_admin(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        current_user = getattr(g, "vs_current_user", None) or _current_user()
        if not current_user or (current_user.get("role") or "").strip().lower() != "admin":
            abort(404)
        return view_func(*args, **kwargs)

    return wrapped


def _count_unseen_signup_events_for_admin(admin_user_id: str) -> int:
    user_id = str(admin_user_id or "").strip()
    if not user_id:
        return 0
    conn = None
    try:
        conn = get_db()
        ensure_auth_user_schema(conn)
        ensure_user_events_schema(conn)
        last_seen_row = conn.execute(
            """
            SELECT admin_users_last_seen_at
            FROM shorts_users
            WHERE CAST(id AS VARCHAR) = ?
            LIMIT 1
            """,
            [user_id],
        ).fetchone()
        last_seen_at = last_seen_row[0] if last_seen_row else None
        # Less noisy on first visit: don't surface the full historical signup backlog.
        if last_seen_at is None:
            return 0
        row = conn.execute(
            """
            SELECT COUNT(*)
            FROM user_events
            WHERE event_name = 'signup'
              AND created_at > ?
            """,
            [last_seen_at],
        ).fetchone()
        return int(row[0] or 0) if row else 0
    except Exception:
        return 0
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _mark_admin_users_seen(admin_user_id: str) -> None:
    user_id = str(admin_user_id or "").strip()
    if not user_id:
        return
    conn = None
    try:
        conn = get_db()
        ensure_auth_user_schema(conn)
        conn.execute(
            """
            UPDATE shorts_users
            SET admin_users_last_seen_at = now()
            WHERE CAST(id AS VARCHAR) = ?
            """,
            [user_id],
        )
        conn.commit()
    except Exception:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _build_admin_nav_context() -> dict:
    current_user = getattr(g, "vs_current_user", None) or _current_user()
    if not current_user or (current_user.get("role") or "").strip().lower() != "admin":
        return {
            "vs_admin_new_users_badge_count": 0,
        }
    current_user_id = str(current_user.get("id") or "").strip()
    if request.endpoint == "video_shorts_bp.admin_users":
        _mark_admin_users_seen(current_user_id)
    return {
        "vs_admin_new_users_badge_count": _count_unseen_signup_events_for_admin(current_user_id),
    }


def _ensure_onboarding_flag_column(conn) -> None:
    cols = table_columns(conn, "shorts_users")
    if "onboarding_dismissed" in cols:
        return
    try:
        conn.execute(
            "ALTER TABLE shorts_users ADD COLUMN onboarding_dismissed BOOLEAN DEFAULT FALSE"
        )
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass


def _build_onboarding_context() -> dict:
    user = _current_user()
    if not user:
        return {"show_modal": False}

    brand_id = current_brand_id()
    youtube_connected = False
    instagram_connected = False
    source_count = 0
    first_source_channel_id = None
    first_video_channel_id = None
    first_downloadable_video_pk = None
    exports_used = 0
    published_short_count = 0

    try:
        youtube_connected = has_refresh_token(user.get("id"), brand_id=brand_id)
    except Exception:
        youtube_connected = False
    try:
        instagram_connected = bool(get_instagram_credentials(user.get("id")))
    except Exception:
        instagram_connected = False
    try:
        usage_snapshot = get_usage_snapshot(user["id"])
        exports_used = int(usage_snapshot.get("exports", {}).get("used") or 0)
    except Exception:
        exports_used = 0

    conn = None
    try:
        conn = get_db_readonly()
        if brand_id:
            source_rows = conn.execute(
                """
                SELECT channel_id, channel_url
                FROM youtube_channels
                WHERE owner_user_id = ?
                  AND brand_id = ?
                ORDER BY added_at ASC, channel_id ASC
                """,
                [user["id"], brand_id],
            ).fetchall()
        else:
            source_rows = conn.execute(
                """
                SELECT channel_id, channel_url
                FROM youtube_channels
                WHERE owner_user_id = ?
                  AND brand_id IS NULL
                ORDER BY added_at ASC, channel_id ASC
                """,
                [user["id"]],
            ).fetchall()
        real_sources = [
            row for row in source_rows
            if not str(row[1] or "").startswith("local://")
        ]
        source_count = len(real_sources)
        if real_sources:
            first_source_channel_id = real_sources[0][0]

        if brand_id:
            video_rows = conn.execute(
                """
                SELECT v.id, v.channel_id, COALESCE(lower(v.download_status), '')
                FROM youtube_videos v
                LEFT JOIN youtube_channels c ON c.channel_id = v.channel_id
                WHERE v.owner_user_id = ?
                  AND c.brand_id = ?
                ORDER BY v.published_at DESC NULLS LAST, v.id DESC
                """,
                [user["id"], brand_id],
            ).fetchall()
        else:
            video_rows = conn.execute(
                """
                SELECT v.id, v.channel_id, COALESCE(lower(v.download_status), '')
                FROM youtube_videos v
                LEFT JOIN youtube_channels c ON c.channel_id = v.channel_id
                WHERE v.owner_user_id = ?
                  AND c.brand_id IS NULL
                ORDER BY v.published_at DESC NULLS LAST, v.id DESC
                """,
                [user["id"]],
            ).fetchall()
        if video_rows:
            first_video_channel_id = video_rows[0][1]
        for row in video_rows:
            if row[2] in {"downloaded", "downloaded_deleted"}:
                first_downloadable_video_pk = row[0]
                break

        if brand_id:
            published_short_count = int(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM shorts_generated_videos
                    WHERE publish_status = 'published'
                      AND brand_id = ?
                    """,
                    [brand_id],
                ).fetchone()[0]
                or 0
            )
        else:
            published_short_count = int(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM shorts_generated_videos
                    WHERE publish_status = 'published'
                      AND brand_id IS NULL
                    """
                ).fetchone()[0]
                or 0
            )
    except Exception:
        source_count = 0
    finally:
        if conn is not None:
            conn.close()

    core_steps = [
        {
            "key": "short",
            "label": "Create your first short",
            "done": published_short_count >= 1,
            "cta_label": "Start",
            "href": url_for("video_shorts_bp.quick_short"),
        },
    ]
    optional_steps = []
    completed_core_steps = sum(1 for step in core_steps if step["done"])
    core_total_steps = len(core_steps)
    core_completed = completed_core_steps >= core_total_steps
    dismissed = bool(user.get("onboarding_dismissed"))

    progress_percent = int((completed_core_steps / core_total_steps) * 100) if core_total_steps else 0

    return {
        "show_modal": False,
        "auto_open": False,
        "core_completed": core_completed,
        "dismissed": dismissed,
        "completed_core_steps": completed_core_steps,
        "core_total_steps": core_total_steps,
        "progress_percent": progress_percent,
        "steps": core_steps + optional_steps,
        "source_count": source_count,
        "exports_used": exports_used,
        "youtube_connected": youtube_connected,
        "instagram_connected": instagram_connected,
        "first_source_channel_id": first_source_channel_id,
        "first_video_channel_id": first_video_channel_id,
        "first_downloadable_video_pk": first_downloadable_video_pk,
    }


def _allowed_netlocs():
    allowed = set()
    base_url = current_app.config.get("BASE_URL") or ""
    base_netloc = urlparse(base_url).netloc
    if base_netloc:
        allowed.add(base_netloc.lower())
    host = request.host or ""
    if host:
        allowed.add(host.lower())
        if ":" in host:
            allowed.add(host.split(":", 1)[0].lower())
    return allowed


def _normalize_next_url(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.scheme and parsed.netloc:
        if parsed.netloc.lower() not in _allowed_netlocs():
            return None
        path = parsed.path or "/"
        query = f"?{parsed.query}" if parsed.query else ""
        fragment = f"#{parsed.fragment}" if parsed.fragment else ""
        return f"{path}{query}{fragment}"
    if value.startswith("/"):
        return value
    return None


@video_shorts_bp.before_request
def _guard_video_shorts():
    # allow static and login and API endpoints
    allowed = {
        "video_shorts_bp.login",
        "video_shorts_bp.register",
        "video_shorts_bp.register_check_email",
        "video_shorts_bp.verify_email",
        "video_shorts_bp.resend_verification_email",
        "video_shorts_bp.forgot_password",
        "video_shorts_bp.reset_password",
        "video_shorts_bp.google_login",
        "video_shorts_bp.google_oauth_callback",
        "video_shorts_bp.logout",
        "video_shorts_bp.privacy_page",
        "video_shorts_bp.data_deletion_page",
        "video_shorts_bp.terms_page",
        "video_shorts_bp.contact_page",
        "video_shorts_bp.static",
        "video_shorts_bp.caption_tasks",
        "video_shorts_bp.caption_result",
        "video_shorts_bp.caption_status",
        "video_shorts_bp.download_status",
        "video_shorts_bp.download_tasks",
        "video_shorts_bp.client_error_api",
        "video_shorts_bp.public_short_watch_page",
        "video_shorts_bp.public_short_watch_event",
        "video_shorts_bp.redeem_onboarding_magic_link",
        "video_shorts_bp.serve_media",
        "video_shorts_bp.serve_instagram_media_proxy",
        "video_shorts_bp.home",
        "video_shorts_bp.blog_index",
        "video_shorts_bp.blog_article",
        "video_shorts_bp.switch_brand",
        "video_shorts_bp.set_default_brand",
        "video_shorts_bp.create_brand",
        "video_shorts_bp.create_checkout_session_route",
        "video_shorts_bp.billing_webhook",
    }
    if request.endpoint in allowed:
        return
    if request.endpoint and request.endpoint.startswith("video_shorts_bp."):
        if not _is_authenticated():
            if (request.path or "").startswith("/video_shorts/admin/"):
                abort(404)
            return redirect(url_for("video_shorts_bp.login", next=request.url))


@video_shorts_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        choice = _current_auth_choice(include_request_values=True)
        if choice["intent"] or choice["plan_id"]:
            _stash_auth_choice(
                intent=str(choice["intent"] or ""),
                tier=choice["tier"],
                plan_id=str(choice["plan_id"] or ""),
            )
    if _is_authenticated():
        nxt = _normalize_next_url(request.args.get("next")) or url_for("video_shorts_bp.my_videos_page")
        return redirect(nxt)
    error = None
    prefill_email = (request.args.get("email") or "").strip().lower()
    resend_email = ""
    if request.method == "POST":
        post_choice = {
            "intent": _normalize_service_intent(request.form.get("intent")),
            "tier": _normalize_service_tier(request.form.get("tier")),
            "plan_id": _normalize_self_serve_plan(request.form.get("plan")),
        }
        _stash_auth_choice(
            intent=post_choice["intent"],
            tier=post_choice["tier"],
            plan_id=post_choice["plan_id"],
        )
        username = _normalize_auth_email(request.form.get("username") or "")
        password = request.form.get("password") or ""
        prefill_email = username
        allowed, _retry_after = check_rate_limits("auth-login", _rate_limit_key(username), LOGIN_RATE_LIMITS)
        if not allowed:
            flash("Too many attempts. Please try again later.", "danger")
            return _render_login_page(resend_email=resend_email, prefill_email=prefill_email, status_code=429)
        if not _verify_turnstile_or_fail():
            flash("Verification failed. Please try again.", "danger")
            return _render_login_page(resend_email=resend_email, prefill_email=prefill_email, status_code=400)
        if not username or not password:
            error = "Email and password are required."
        else:
            conn = get_db()
            ensure_storage_user_schema(conn)
            ensure_auth_user_schema(conn)
            row = conn.execute(
                """
                SELECT CAST(id AS VARCHAR), username, password_hash, name, COALESCE(email_verified, FALSE)
                FROM shorts_users
                WHERE lower(username) = lower(?)
                   OR lower(email) = lower(?)
                """,
                [username, username],
            ).fetchone()
            conn.close()
            if not row:
                error = "Account not found."
            elif not row[2]:
                error = "This account uses Google sign-in. Please use the Google option."
            elif not bool(row[4]):
                error = "Please verify your email first."
                resend_email = username
            elif not check_password_hash(row[2], password):
                error = "Incorrect password."
            else:
                session["vs_user_id"] = row[0]
                brand_conn = get_db()
                try:
                    ensure_storage_user_schema(brand_conn)
                    ensure_auth_user_schema(brand_conn)
                    ensure_brand_schema(brand_conn)
                    brand = ensure_brand_for_user(
                        brand_conn,
                        user_id=row[0],
                        user_name=row[3] or row[1],
                    )
                finally:
                    brand_conn.close()
                if brand:
                    session["vs_brand_id"] = brand["id"]
                if post_choice["intent"]:
                    persist_conn = get_db()
                    try:
                        ensure_storage_user_schema(persist_conn)
                        ensure_auth_user_schema(persist_conn)
                        _persist_pending_service_choice(
                            persist_conn,
                            user_id=row[0],
                            intent=str(post_choice["intent"]),
                            tier=post_choice["tier"],
                        )
                        persist_conn.commit()
                    finally:
                        persist_conn.close()
                flash(f"Welcome back, {row[3] or row[1]}!", "success")
                nxt = _normalize_next_url(request.args.get("next")) or url_for("video_shorts_bp.my_videos_page")
                return redirect(nxt)
    if error:
        flash(error, "danger")
    return _render_login_page(resend_email=resend_email, prefill_email=prefill_email)


@video_shorts_bp.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "GET":
        choice = _current_auth_choice(include_request_values=True)
        if choice["intent"] or choice["plan_id"]:
            _stash_auth_choice(
                intent=str(choice["intent"] or ""),
                tier=choice["tier"],
                plan_id=str(choice["plan_id"] or ""),
            )
    if _is_authenticated():
        return redirect(url_for("video_shorts_bp.my_videos_page"))
    error = None
    pending_email = ""
    signups_enabled = _signups_enabled()
    if request.method == "POST":
        post_choice = {
            "intent": _normalize_service_intent(request.form.get("intent")),
            "tier": _normalize_service_tier(request.form.get("tier")),
            "plan_id": _normalize_self_serve_plan(request.form.get("plan")),
        }
        _stash_auth_choice(
            intent=post_choice["intent"],
            tier=post_choice["tier"],
            plan_id=post_choice["plan_id"],
        )
        email = _normalize_auth_email(request.form.get("email") or "")
        password = request.form.get("password") or ""
        password_confirm = request.form.get("password_confirm") or ""
        pending_email = email
        if not signups_enabled:
            _log_signup_refusal(method="password", email=email, source="register_post")
            flash(_signup_disabled_message(), "warning")
            return _render_register_page(pending_email=pending_email, status_code=403)
        allowed, _retry_after = check_rate_limits("auth-register", _rate_limit_key(email), REGISTER_RATE_LIMITS)
        if not allowed:
            flash("Too many attempts. Please try again later.", "danger")
            return _render_register_page(pending_email=pending_email, status_code=429)
        if not _verify_turnstile_or_fail():
            flash("Verification failed. Please try again.", "danger")
            return _render_register_page(pending_email=pending_email, status_code=400)
        if not email:
            error = "Email is required."
        elif "@" not in email:
            error = "Please enter a valid email."
        else:
            password_error = _validate_registration_password(email, password)
            if password_error:
                error = password_error
        if not error and password != password_confirm:
            error = "Passwords do not match."
        if not error:
            conn = get_db()
            ensure_storage_user_schema(conn)
            ensure_auth_user_schema(conn)
            existing = conn.execute(
                "SELECT 1 FROM shorts_users WHERE lower(email) = ? OR lower(username) = ?",
                [email, email],
            ).fetchone()
            if existing:
                error = "An account with this email already exists."
                conn.close()
            else:
                username = email
                name = email.split("@")[0].replace(".", " ").title()
                user_id = str(uuid4())
                verify_token = generate_email_verification_token()
                verify_token_hash = hash_email_verification_token(verify_token)
                verify_expires_at = verification_token_expiry()
                conn.execute(
                    """
                    INSERT INTO shorts_users (
                        id, username, password_hash, name, email, role, plan_id,
                        email_verified, email_verification_token_hash, email_verification_expires_at,
                        email_verification_sent_at, pending_service_intent, pending_service_tier
                    )
                    VALUES (?, ?, ?, ?, ?, 'member', ?, FALSE, ?, ?, now(), ?, ?)
                    """,
                    [
                        user_id,
                        username,
                        generate_password_hash(password),
                        name,
                        email,
                        DEFAULT_USER_PLAN_ID,
                        verify_token_hash,
                        verify_expires_at,
                        post_choice["intent"] or None,
                        post_choice["tier"],
                    ],
                )
                ensure_brand_schema(conn)
                create_brand_record(
                    conn,
                    user_id=user_id,
                    name=f"{name} Workspace",
                    make_default=True,
                )
                conn.commit()
                persisted = conn.execute(
                    """
                    SELECT CAST(id AS VARCHAR), email, COALESCE(email_verified, FALSE), email_verification_token_hash
                    FROM shorts_users
                    WHERE lower(email) = lower(?)
                    ORDER BY updated_at DESC
                    LIMIT 1
                    """,
                    [email],
                ).fetchone()
                logger.info(
                    "Register persistence check: email=%s committed=%s token_present=%s user_id=%s",
                    email,
                    bool(persisted),
                    bool(persisted and persisted[3]),
                    persisted[0] if persisted else None,
                )
                conn.close()
                track_event(user_id, "signup")
                try:
                    send_verification_email(to_email=email, verify_token=verify_token, recipient_name=name)
                except Exception:
                    logger.exception("Failed to send verification email for user=%s", user_id)
                    flash("Your account was created, but we couldn't send the verification email. Please try again.", "danger")
                    return redirect(url_for("video_shorts_bp.register_check_email", email=email, delivery_failed=1))
                return redirect(url_for("video_shorts_bp.register_check_email", email=email))
    if error:
        flash(error, "danger")
    return _render_register_page(pending_email=pending_email)


@video_shorts_bp.route("/register/check-email", methods=["GET"])
def register_check_email():
    email = _normalize_auth_email(request.args.get("email") or "")
    if not email:
        return redirect(url_for("video_shorts_bp.register"))
    delivery_failed = str(request.args.get("delivery_failed") or "").strip().lower() in {"1", "true", "yes"}
    return render_template(
        "vs_check_email.html",
        email=email,
        masked_email=_mask_email_address(email),
        verification_invalid=False,
        verification_expired=False,
        delivery_failed=delivery_failed,
        resend_email=email,
        resend_context="check_email",
    )


@video_shorts_bp.route("/verify-email", methods=["GET"])
def verify_email():
    token = (request.args.get("token") or "").strip()
    if not token:
        flash("That verification link is invalid.", "danger")
        return render_template(
            "vs_check_email.html",
            email="",
            masked_email="",
            verification_invalid=True,
            verification_expired=False,
            resend_email="",
            resend_context="verify_invalid",
        )
    token_hash = hash_email_verification_token(token)
    conn = get_db()
    try:
        ensure_storage_user_schema(conn)
        ensure_auth_user_schema(conn)
        row = conn.execute(
            """
            SELECT
                CAST(id AS VARCHAR),
                email,
                username,
                COALESCE(email_verified, FALSE),
                email_verification_expires_at
            FROM shorts_users
            WHERE email_verification_token_hash = ?
            LIMIT 1
            """,
            [token_hash],
        ).fetchone()
        logger.info("Verify email lookup: token_found=%s", bool(row))
        if not row:
            flash("That verification link is invalid.", "danger")
            return render_template(
                "vs_check_email.html",
                email="",
                masked_email="",
                verification_invalid=True,
                verification_expired=False,
                resend_email="",
                resend_context="verify_invalid",
            )
        expires_at = row[4]
        now_utc = datetime.now(timezone.utc)
        if expires_at and getattr(expires_at, "tzinfo", None) is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if bool(row[3]):
            flash("Email already verified. You can sign in.", "success")
            return redirect(url_for("video_shorts_bp.login", email=_normalize_auth_email(row[1] or row[2] or "")))
        if not expires_at or expires_at < now_utc:
            flash("That verification link has expired.", "warning")
            return render_template(
                "vs_check_email.html",
                email=_normalize_auth_email(row[1] or row[2] or ""),
                masked_email=_mask_email_address(_normalize_auth_email(row[1] or row[2] or "")),
                verification_invalid=False,
                verification_expired=True,
                resend_email=_normalize_auth_email(row[1] or row[2] or ""),
                resend_context="verify_expired",
            )
        conn.execute(
            """
            UPDATE shorts_users
            SET email_verified = TRUE,
                email_verified_at = now(),
                email_verification_token_hash = NULL,
                email_verification_expires_at = NULL,
                updated_at = now()
            WHERE id = ?
            """,
            [row[0]],
        )
        conn.commit()
    finally:
        conn.close()
    try:
        send_membership_activated_emails(user_id=row[0], signup_method="Email")
    except Exception:
        logger.exception("Membership activation email orchestration failed after verify_email for user=%s", row[0])
    flash("Email verified. You can sign in.", "success")
    return redirect(url_for("video_shorts_bp.login", email=_normalize_auth_email(row[1] or row[2] or "")))


@video_shorts_bp.route("/verification/resend", methods=["POST"])
def resend_verification_email():
    email = _normalize_auth_email(request.form.get("email") or request.args.get("email") or "")
    context = (request.form.get("context") or request.args.get("context") or "login").strip()
    allowed, _retry_after = check_rate_limits("auth-resend-verification", _rate_limit_key(email), RESEND_VERIFICATION_RATE_LIMITS)
    if not allowed:
        flash("Too many attempts. Please try again later.", "danger")
        if context != "login":
            return _render_check_email_page(email=email, resend_context=context or "check_email", status_code=429)
        return _render_login_page(resend_email=email, prefill_email=email, status_code=429)
    if not email:
        flash("Enter your email address first.", "warning")
        return redirect(url_for("video_shorts_bp.login"))
    user_row = _lookup_user_by_email(email)
    logger.info("Resend verification lookup: email=%s found=%s", email, bool(user_row))
    if not user_row:
        flash("We couldn't find an account with that email.", "warning")
        return redirect(url_for("video_shorts_bp.register"))
    if bool(user_row[5]):
        flash("This email is already verified. You can sign in.", "success")
        return redirect(url_for("video_shorts_bp.login", email=email))
    try:
        ok, message, _retry_after = _resend_verification_for_user(user_row)
    except Exception:
        logger.exception("Failed to resend verification email for %s", email)
        flash("We couldn't send the verification email right now. Please try again.", "danger")
        if context != "login":
            return redirect(url_for("video_shorts_bp.register_check_email", email=email))
        return redirect(url_for("video_shorts_bp.login", email=email))
    flash(message, "success" if ok else "warning")
    if context != "login":
        return redirect(url_for("video_shorts_bp.register_check_email", email=email))
    return redirect(url_for("video_shorts_bp.login", email=email))


@video_shorts_bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    prefill_email = _normalize_auth_email(request.args.get("email") or "")
    sent = False
    if request.method == "POST":
        email = _normalize_auth_email(request.form.get("email") or "")
        prefill_email = email
        allowed, _retry_after = check_rate_limits("auth-forgot-password", _rate_limit_key(email), FORGOT_PASSWORD_RATE_LIMITS)
        if not allowed:
            flash("Too many attempts. Please try again later.", "danger")
            return _render_forgot_password_page(prefill_email=prefill_email, sent=False, status_code=429)
        if not _verify_turnstile_or_fail():
            flash("Verification failed. Please try again.", "danger")
            return _render_forgot_password_page(prefill_email=prefill_email, sent=False, status_code=400)
        neutral_message = "If an account exists for that email, we've sent a password reset link."
        if email:
            user_row = _lookup_user_by_email(email)
            try:
                if user_row and user_row[4] and not user_row[10]:
                    _issued, _retry_after = _issue_password_reset_for_user(user_row)
            except Exception:
                logger.exception("Failed to issue password reset email for %s", email)
        flash(neutral_message, "success")
        sent = True
    return render_template("vs_forgot_password.html", prefill_email=prefill_email, sent=sent)


@video_shorts_bp.route("/reset-password", methods=["GET", "POST"])
def reset_password():
    token = (request.values.get("token") or "").strip()
    if not token:
        return render_template("vs_reset_password.html", token="", token_invalid=True, token_expired=False)
    if request.method == "POST":
        allowed, _retry_after = check_rate_limits("auth-reset-password", _rate_limit_key(token), RESET_PASSWORD_RATE_LIMITS)
        if not allowed:
            flash("Too many attempts. Please try again later.", "danger")
            return render_template("vs_reset_password.html", token=token, token_invalid=False, token_expired=False), 429
    token_hash = hash_email_verification_token(token)
    conn = get_db()
    row = None
    try:
        ensure_storage_user_schema(conn)
        ensure_auth_user_schema(conn)
        row = conn.execute(
            """
            SELECT
                CAST(id AS VARCHAR),
                email,
                username,
                password_reset_expires_at
            FROM shorts_users
            WHERE password_reset_token_hash = ?
            LIMIT 1
            """,
            [token_hash],
        ).fetchone()
        if not row:
            return render_template("vs_reset_password.html", token=token, token_invalid=True, token_expired=False)
        expires_at = row[3]
        now_utc = datetime.now(timezone.utc)
        if expires_at and getattr(expires_at, "tzinfo", None) is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if not expires_at or expires_at < now_utc:
            return render_template(
                "vs_reset_password.html",
                token=token,
                token_invalid=False,
                token_expired=True,
                email=_normalize_auth_email(row[1] or row[2] or ""),
            )
        if request.method == "POST":
            password = request.form.get("password") or ""
            password_confirm = request.form.get("password_confirm") or ""
            email = _normalize_auth_email(row[1] or row[2] or "")
            password_error = _validate_registration_password(email, password)
            if password_error:
                flash(password_error, "danger")
                return render_template("vs_reset_password.html", token=token, token_invalid=False, token_expired=False)
            if password != password_confirm:
                flash("Passwords do not match.", "danger")
                return render_template("vs_reset_password.html", token=token, token_invalid=False, token_expired=False)
            conn.execute(
                """
                UPDATE shorts_users
                SET password_hash = ?,
                    password_reset_token_hash = NULL,
                    password_reset_expires_at = NULL,
                    password_reset_sent_at = NULL,
                    updated_at = now()
                WHERE id = ?
                """,
                [generate_password_hash(password), row[0]],
            )
            conn.commit()
            flash("Your password has been reset. You can sign in.", "success")
            return redirect(url_for("video_shorts_bp.login", email=email))
    finally:
        conn.close()
    return render_template("vs_reset_password.html", token=token, token_invalid=False, token_expired=False)


@video_shorts_bp.route("/logout")
def logout():
    session.pop("vs_user_id", None)
    session.pop("vs_brand_id", None)
    flash("You have been signed out.", "info")
    return redirect(url_for("video_shorts_bp.login"))


@video_shorts_bp.route("/onboard/<token>", methods=["GET"])
def redeem_onboarding_magic_link(token: str):
    normalized_token = str(token or "").strip()
    if not normalized_token:
        return _render_onboarding_magic_link_status_page(status="invalid", status_code=400)

    token_hash = hash_onboarding_magic_token(normalized_token)
    conn = get_db()
    is_new_user = False
    outreach_language = "EN"
    welcome_trial_days = DEFAULT_SHARE_TRIAL_DAYS
    welcome_email_context: tuple[str, str, str] | None = None
    try:
        ensure_storage_user_schema(conn)
        ensure_auth_user_schema(conn)
        ensure_brand_schema(conn)
        ensure_onboarding_magic_links_schema(conn)
        row = conn.execute(
            """
            SELECT
                id,
                recipient_email,
                recipient_name,
                language,
                trial_days,
                expires_at,
                used_at
            FROM onboarding_magic_links
            WHERE token_hash = ?
            LIMIT 1
            """,
            [token_hash],
        ).fetchone()
        if not row:
            return _render_onboarding_magic_link_status_page(status="invalid", status_code=404)

        expires_at = row[5]
        if expires_at and getattr(expires_at, "tzinfo", None) is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        used_at = row[6]
        if used_at:
            return _render_onboarding_magic_link_status_page(status="used", status_code=410)
        if not expires_at or expires_at < datetime.now(timezone.utc):
            return _render_onboarding_magic_link_status_page(status="expired", status_code=410)

        recipient_email = _normalize_auth_email(row[1] or "")
        recipient_name = str(row[2] or "").strip()
        outreach_language = normalize_outreach_language(row[3], default="EN")
        welcome_trial_days = normalize_trial_days(row[4], default=DEFAULT_SHARE_TRIAL_DAYS)
        if not recipient_email:
            return _render_onboarding_magic_link_status_page(status="invalid", status_code=400)
        existing_user = _lookup_user_by_email(recipient_email)
        is_new_user = existing_user is None

        user_id, brand_id = _create_or_grant_magic_link_user(
            conn,
            recipient_email=recipient_email,
            recipient_name=recipient_name,
            existing_user_id=str(existing_user[0] or "").strip() if existing_user else None,
        )
        if is_new_user:
            reset_token, _expires_at = _create_password_reset_token_for_user(conn, user_id=user_id)
            welcome_email_context = (
                recipient_email,
                recipient_name,
                build_password_reset_url(reset_token),
            )
        updated = conn.execute(
            """
            UPDATE onboarding_magic_links
            SET used_at = now(),
                user_id = ?
            WHERE id = ?
              AND used_at IS NULL
            """,
            [str(user_id), row[0]],
        )
        if getattr(updated, "rowcount", -1) == 0:
            conn.rollback()
            return _render_onboarding_magic_link_status_page(status="used", status_code=410)
        conn.commit()
    except Exception:
        current_app.logger.exception("Failed to redeem onboarding magic link")
        try:
            conn.rollback()
        except Exception:
            pass
        return _render_onboarding_magic_link_status_page(status="invalid", status_code=400)
    finally:
        try:
            conn.close()
        except Exception:
            pass

    _establish_authenticated_session(user_id=user_id, brand_id=brand_id)
    if is_new_user and welcome_email_context:
        welcome_email, welcome_name, set_password_url = welcome_email_context
        try:
            result = send_onboarding_magic_link_welcome_email(
                to_email=welcome_email,
                set_password_url=set_password_url,
                recipient_name=welcome_name,
                language=outreach_language,
                trial_days=welcome_trial_days,
            )
            current_app.logger.info(
                "Onboarding welcome email sent: user_id=%s to=%s status=%s request_id=%s language=%s",
                user_id,
                welcome_email,
                result.get("status_code"),
                result.get("request_id") or "(missing)",
                outreach_language,
            )
        except Exception:
            current_app.logger.exception(
                "Onboarding welcome email failed: user_id=%s to=%s language=%s",
                user_id,
                welcome_email,
                outreach_language,
            )
    return redirect(url_for("video_shorts_bp.my_videos_page"))


@video_shorts_bp.route("/profile", methods=["GET", "POST"])
def profile():
    user = _current_user()
    if not user:
        return redirect(url_for("video_shorts_bp.login", next=request.url))

    conn = get_db()
    ensure_storage_user_schema(conn)
    ensure_auth_user_schema(conn)
    ensure_brand_schema(conn)
    raw_timezone_row = conn.execute(
        "SELECT time_zone FROM shorts_users WHERE id = ?",
        [user["id"]],
    ).fetchone()
    stored_time_zone = (raw_timezone_row[0] or "").strip() if raw_timezone_row and raw_timezone_row[0] else ""
    brands = list_user_brands(conn, user["id"])
    current_brand = getattr(g, "vs_current_brand", None)

    if request.method == "POST":
        form_type = request.form.get("form_type")
        is_xhr = request.headers.get("X-Requested-With") == "XMLHttpRequest"
        explicit_time_zone = (request.form.get("time_zone") or "").strip()
        valid_timezones = {value for value, _ in TIMEZONE_OPTIONS}
        timezone_only_update = bool(explicit_time_zone) and form_type in {None, "", "profile"}
        if timezone_only_update and is_xhr:
            browser_time_zone = (request.form.get("browser_time_zone") or "").strip()
            time_zone = explicit_time_zone
            if time_zone not in valid_timezones:
                time_zone = browser_time_zone if browser_time_zone in valid_timezones else DEFAULT_TIME_ZONE
            conn.execute(
                "UPDATE shorts_users SET time_zone = ?, updated_at = now() WHERE id = ?",
                [time_zone, user["id"]],
            )
            conn.commit()
            user["time_zone"] = time_zone
            g.vs_current_user = user
            return jsonify(
                {
                    "success": True,
                    "time_zone": time_zone,
                    "time_zone_label": TIMEZONE_LABELS.get(time_zone, time_zone),
                    "message": "Time zone updated.",
                }
            )
        if form_type == "profile":
            name = (request.form.get("name") or "").strip()
            email = (request.form.get("email") or "").strip()
            time_zone = (request.form.get("time_zone") or "").strip()
            browser_time_zone = (request.form.get("browser_time_zone") or "").strip()
            if time_zone not in valid_timezones:
                time_zone = browser_time_zone if browser_time_zone in valid_timezones else DEFAULT_TIME_ZONE
            if not name:
                flash("Please enter your name.", "warning")
            else:
                conn.execute(
                    "UPDATE shorts_users SET name = ?, email = ?, time_zone = ?, updated_at = now() WHERE id = ?",
                    [name, email or None, time_zone, user["id"]],
                )
                conn.commit()
                user["name"] = name
                user["email"] = email
                user["time_zone"] = time_zone
                g.vs_current_user = user
                flash("Profile updated.", "success")
            return redirect(url_for("video_shorts_bp.profile"))
        elif form_type == "password":
            current_pw = request.form.get("current_password") or ""
            new_pw = request.form.get("new_password") or ""
            confirm_pw = request.form.get("confirm_password") or ""
            db_row = conn.execute(
                "SELECT password_hash FROM shorts_users WHERE id = ?",
                [user["id"]],
            ).fetchone()
            existing_hash = db_row[0] if db_row else None
            if existing_hash and not check_password_hash(existing_hash, current_pw):
                flash("Current password is incorrect.", "danger")
            elif len(new_pw) < 8:
                flash("New password must be at least 8 characters.", "warning")
            elif new_pw != confirm_pw:
                flash("New passwords do not match.", "warning")
            else:
                conn.execute(
                    "UPDATE shorts_users SET password_hash = ?, updated_at = now() WHERE id = ?",
                    [generate_password_hash(new_pw), user["id"]],
                )
                conn.commit()
                flash("Password updated.", "success")
            conn.close()
            return redirect(url_for("video_shorts_bp.profile"))

    conn.close()
    return render_template(
        "shorts_profile.html",
        profile_user=user,
        brands=brands,
        current_brand=current_brand,
        timezones=TIMEZONE_OPTIONS,
        selected_timezone=stored_time_zone,
        has_explicit_timezone=bool(stored_time_zone),
    )


@video_shorts_bp.context_processor
def inject_current_user():
    return {
        "vs_current_user": _current_user(),
        "vs_current_brand": _current_brand(),
        "vs_brands": getattr(g, "vs_brands", []),
        "vs_google_oauth_available": _google_oauth_enabled(),
        "vs_turnstile_enabled": turnstile_enabled(),
        "vs_turnstile_site_key": turnstile_site_key(),
        "vs_youtube_reauth_required": _youtube_reauth_required(),
        "vs_overview_quota": _load_overview_quota_context(),
        "vs_onboarding": _build_onboarding_context(),
        "vs_service_mode": _build_service_mode_context(),
        **_build_admin_nav_context(),
    }


def _load_overview_quota_context() -> dict:
    context = {
        "active": False,
        "until": None,
        "until_utc": None,
        "until_pst": None,
        "last_error_code": None,
        "last_error_reason": None,
        "last_error_message": None,
        "last_error_domain": None,
        "last_error_at": None,
        "last_error_at_utc": None,
        "last_error_at_pst": None,
        "ttl_minutes": SHORTS_OVERVIEW_STATS_TTL_MINUTES,
        "max_videos": SHORTS_OVERVIEW_STATS_MAX_VIDEOS,
        "cooldown_hours": SHORTS_OVERVIEW_QUOTA_COOLDOWN_HOURS,
        "cache_last_fetched_at": None,
        "cache_last_fetched_utc": None,
        "cache_last_fetched_pst": None,
    }
    try:
        conn = get_db_readonly()
    except Exception as exc:
        logger.warning("Video shorts quota DB unavailable while building template context: %s", exc)
        return context
    try:
        state = get_shorts_overview_quota_state(conn)
        context.update(state)
    except Exception:
        pass
    finally:
        conn.close()
    return context


def _youtube_reauth_required() -> bool:
    try:
        current_user = _current_user() or {}
        return is_reauth_required(current_user.get("id"))
    except Exception:
        return False
def _google_oauth_enabled():
    return bool(GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET)

def _google_oauth_scopes():
    scopes = [scope for scope in GOOGLE_OAUTH_SCOPES if scope]
    if not scopes:
        scopes = [
            "openid",
            "https://www.googleapis.com/auth/userinfo.email",
            "https://www.googleapis.com/auth/userinfo.profile",
        ]
    return scopes

def _build_google_flow(state=None):
    if not _google_oauth_enabled():
        raise RuntimeError("Google OAuth is not configured")
    redirect_uri = GOOGLE_OAUTH_REDIRECT_URI or url_for(
        "video_shorts_bp.google_oauth_callback", _external=True
    )
    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": GOOGLE_OAUTH_CLIENT_ID,
                "client_secret": GOOGLE_OAUTH_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        },
        scopes=_google_oauth_scopes(),
        state=state,
    )
    flow.redirect_uri = redirect_uri
    return flow
@video_shorts_bp.route("/login/google")
def google_login():
    if not _google_oauth_enabled():
        flash("Google sign-in is not configured.", "warning")
        return redirect(url_for("video_shorts_bp.login"))
    choice = _current_auth_choice(include_request_values=True)
    if choice["intent"] or choice["plan_id"]:
        _stash_auth_choice(
            intent=str(choice["intent"] or ""),
            tier=choice["tier"],
            plan_id=str(choice["plan_id"] or ""),
        )
    flow = _build_google_flow()
    authorization_url, state = flow.authorization_url(prompt="consent")
    session["google_oauth_state"] = state
    session["google_oauth_code_verifier"] = flow.code_verifier
    session["google_login_next"] = _normalize_next_url(request.args.get("next")) or url_for("video_shorts_bp.my_videos_page")
    return redirect(authorization_url)


@video_shorts_bp.route("/login/google/callback")
def google_oauth_callback():
    yt_state = session.get("yt_oauth_state")
    yt_expected_state = yt_state.get("nonce") if isinstance(yt_state, dict) else yt_state
    if yt_expected_state and request.args.get("state") == yt_expected_state:
        current_user = getattr(g, "vs_current_user", None)
        yt_user_id = yt_state.get("user_id") if isinstance(yt_state, dict) else None
        effective_user_id = (current_user or {}).get("id") or yt_user_id
        if not effective_user_id:
            session.pop("yt_oauth_state", None)
            flash("YouTube bağlantısı için giriş yapın.", "danger")
            return redirect(url_for("video_shorts_bp.login", next=request.url))
        error = request.args.get("error")
        if error:
            session.pop("yt_oauth_state", None)
            flash(f"YouTube OAuth hatası: {error}", "danger")
            return redirect(url_for("video_shorts_bp.channels_page"))

        state = request.args.get("state")
        saved_state = session.pop("yt_oauth_state", None)
        saved_nonce = saved_state.get("nonce") if isinstance(saved_state, dict) else saved_state
        saved_code_verifier = saved_state.get("code_verifier") if isinstance(saved_state, dict) else None
        flow = build_oauth_flow(state=state)
        if saved_nonce and state != saved_nonce:
            current_app.logger.warning("YouTube OAuth state mismatch: %s vs %s", state, saved_nonce)
        if not saved_code_verifier:
            flash("YouTube sign-in session expired. Please try again.", "warning")
            return redirect(url_for("video_shorts_bp.social_connect"))
        flow.code_verifier = saved_code_verifier
        try:
            flow.fetch_token(authorization_response=request.url)
        except Exception as exc:
            current_app.logger.exception("Failed to fetch YouTube OAuth token: %s", exc)
            flash("YouTube OAuth sonucu alınamadı.", "danger")
            return redirect(url_for("video_shorts_bp.channels_page"))

        credentials = flow.credentials
        refresh_token = credentials.refresh_token
        if not refresh_token:
            flash("YouTube OAuth işleminden refresh token elde edilemedi.", "warning")
            return redirect(url_for("video_shorts_bp.channels_page"))

        store_refresh_token(refresh_token, user_id=effective_user_id)
        flash("YouTube connection saved; you can upload videos to YouTube later.", "success")
        return redirect(url_for("video_shorts_bp.social_connect"))

    if not _google_oauth_enabled():
        flash("Google sign-in is disabled.", "warning")
        return redirect(url_for("video_shorts_bp.login"))
    state = session.get("google_oauth_state")
    if not state or state != request.args.get("state"):
        flash("Google authentication failed.", "danger")
        return redirect(url_for("video_shorts_bp.login"))
    flow = _build_google_flow(state=state)
    code_verifier = session.get("google_oauth_code_verifier")
    if not code_verifier:
        flash("Google sign-in session expired. Please try again.", "warning")
        return redirect(url_for("video_shorts_bp.google_login", next=_normalize_next_url(session.get("google_login_next", request.args.get("next")))))
    flow.code_verifier = code_verifier
    try:
        flow.fetch_token(authorization_response=request.url)
    except Exception as exc:
        current_app.logger.exception("Failed to fetch Google token: %s", exc)
        flash("Google sign-in failed.", "danger")
        return redirect(url_for("video_shorts_bp.login"))
    session.pop("google_oauth_code_verifier", None)
    creds = flow.credentials
    try:
        idinfo = id_token.verify_oauth2_token(
            creds.id_token,
            google_requests.Request(),
            GOOGLE_OAUTH_CLIENT_ID,
        )
    except Exception as exc:
        current_app.logger.exception("Invalid Google ID token: %s", exc)
        flash("Unable to verify Google identity.", "danger")
        return redirect(url_for("video_shorts_bp.login"))
    google_sub = idinfo.get("sub")
    email = (idinfo.get("email") or "").lower()
    name = idinfo.get("name") or (email.split("@")[0] if email else "")
    if not google_sub or not email:
        flash("Could not read email from Google account.", "danger")
        return redirect(url_for("video_shorts_bp.login"))

    conn = get_db()
    ensure_storage_user_schema(conn)
    ensure_auth_user_schema(conn)
    created_new_user = False
    row = conn.execute(
        """
        SELECT CAST(id AS VARCHAR)
        FROM shorts_users
        WHERE google_sub = ?
           OR lower(email) = ?
        ORDER BY google_sub IS NULL DESC
        LIMIT 1
        """,
        [google_sub, email],
    ).fetchone()
    if row:
        user_id = row[0]
        conn.execute(
            """
            UPDATE shorts_users
            SET google_sub = ?,
                email_verified = TRUE,
                email_verified_at = COALESCE(email_verified_at, now()),
                email_verification_token_hash = NULL,
                email_verification_expires_at = NULL,
                updated_at = now()
            WHERE id = ?
            """,
            [google_sub, user_id],
        )
        persisted_choice = _current_auth_choice()
        if persisted_choice["intent"]:
            _persist_pending_service_choice(
                conn,
                user_id=user_id,
                intent=str(persisted_choice["intent"]),
                tier=persisted_choice["tier"],
            )
        conn.commit()
    else:
        if not _signups_enabled():
            conn.close()
            _log_signup_refusal(method="google_oauth", email=email, source="google_callback_create")
            flash(_signup_disabled_message(), "warning")
            return redirect(url_for("video_shorts_bp.register"))
        user_id = str(uuid4())
        created_new_user = True
        conn.execute(
            """
            INSERT INTO shorts_users (
                id, username, name, email, google_sub, role, plan_id, email_verified, email_verified_at,
                pending_service_intent, pending_service_tier
            )
            VALUES (?, ?, ?, ?, ?, 'member', ?, TRUE, now(), ?, ?)
            """,
            [
                user_id,
                email,
                name or email,
                email,
                google_sub,
                DEFAULT_USER_PLAN_ID,
                session.get("vs_pending_service_intent") or None,
                _normalize_service_tier(session.get("vs_pending_service_tier")),
            ],
        )
        conn.commit()
    ensure_brand_schema(conn)
    brand = ensure_brand_for_user(conn, user_id=user_id, user_name=name or email)
    conn.close()
    if created_new_user:
        track_event(user_id, "signup")
        try:
            send_membership_activated_emails(user_id=user_id, signup_method="Google")
        except Exception:
            logger.exception("Membership activation email orchestration failed after Google signup for user=%s", user_id)
    session["vs_user_id"] = user_id
    if brand:
        session["vs_brand_id"] = brand["id"]
    flash("Signed in with Google.", "success")
    nxt = _normalize_next_url(session.pop("google_login_next", None)) or url_for("video_shorts_bp.my_videos_page")
    return redirect(nxt)


@video_shorts_bp.route("/brands/switch", methods=["POST"])
def switch_brand():
    current_user = _current_user()
    if not current_user:
        return redirect(url_for("video_shorts_bp.login", next=request.url))
    brand_id = (request.form.get("brand_id") or "").strip()
    brand = set_default_brand_for_user(current_user["id"], brand_id)
    if not brand:
        flash("Brand not found.", "warning")
    else:
        session["vs_brand_id"] = brand["id"]
        flash(f"{brand['name']} is now active and default.", "success")
    nxt = _normalize_next_url(request.form.get("next")) or request.referrer or url_for("video_shorts_bp.channels_page")
    return redirect(nxt)


@video_shorts_bp.route("/brands/default", methods=["POST"])
def set_default_brand():
    current_user = _current_user()
    if not current_user:
        return redirect(url_for("video_shorts_bp.login", next=request.url))
    brand_id = (request.form.get("brand_id") or "").strip()
    brand = set_default_brand_for_user(current_user["id"], brand_id)
    if not brand:
        flash("Brand not found.", "warning")
    else:
        session["vs_brand_id"] = brand["id"]
        flash(f"{brand['name']} is now your default brand.", "success")
    nxt = _normalize_next_url(request.form.get("next")) or request.referrer or url_for("video_shorts_bp.channels_page")
    return redirect(nxt)


@video_shorts_bp.route("/account")
def account_page():
    current_user = _current_user()
    if not current_user:
        return redirect(url_for("video_shorts_bp.login", next=request.url))
    billing_user = load_billing_user_state(current_user["id"], refresh_live=True)
    has_managed_subscription = user_has_managed_subscription(billing_user)

    account_items = [
        {
            "title": "Profile & Brands",
            "subtitle": "Update profile and account details",
            "meta": "Your name, email, password, time zone — and the brands you switch between.",
            "icon": "manage_accounts",
            "href": url_for("video_shorts_bp.profile"),
        },
        {
            "title": "Connections",
            "subtitle": "Manage your publishing accounts",
            "meta": "Connect and manage YouTube, Instagram, TikTok and Facebook for publishing.",
            "icon": "public",
            "href": url_for("video_shorts_bp.social_connect"),
        },
        {
            "title": "Plan & Storage",
            "subtitle": "Review your usage and upgrade anytime",
            "meta": "Your plan, exports, transcription minutes and storage — upgrade anytime.",
            "icon": "workspace_premium",
            "href": url_for("video_shorts_bp.shorts_storage_plans"),
        },
    ]
    if has_managed_subscription:
        account_items.append(
            {
                "title": "Manage Subscription",
                "subtitle": "Open Stripe Billing Portal",
                "meta": "Change plans, update billing, or cancel your subscription in Stripe.",
                "icon": "credit_card",
                "href": url_for("video_shorts_bp.billing_portal"),
            }
        )
    return render_template("shorts_account.html", account_items=account_items)


@video_shorts_bp.route("/onboarding/dismiss", methods=["POST"])
def dismiss_onboarding():
    current_user = _current_user()
    if not current_user:
        return {"ok": False}, 401
    conn = get_db()
    try:
        ensure_storage_user_schema(conn)
        ensure_auth_user_schema(conn)
        _ensure_onboarding_flag_column(conn)
        conn.execute(
            "UPDATE shorts_users SET onboarding_dismissed = TRUE, updated_at = now() WHERE id = ?",
            [current_user["id"]],
        )
        conn.commit()
    finally:
        conn.close()
    current_user["onboarding_dismissed"] = True
    return {"ok": True}


@video_shorts_bp.route("/onboarding/service-mode", methods=["POST"])
def save_service_mode_choice():
    current_user = _current_user()
    if not current_user:
        return {"ok": False, "error": "auth_required"}, 401
    payload = request.get_json(silent=True) or request.form
    service_mode = _normalize_service_intent((payload or {}).get("service_mode"))
    if service_mode not in SERVICE_MODE_VALUES:
        return {"ok": False, "error": "invalid_service_mode"}, 400
    service_tier = 15 if service_mode == "autopilot" else None
    conn = get_db()
    chosen_at_label = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    try:
        ensure_storage_user_schema(conn)
        ensure_auth_user_schema(conn)
        conn.execute(
            """
            UPDATE shorts_users
            SET service_mode = ?,
                service_tier = ?,
                service_mode_chosen_at = now(),
                pending_service_intent = NULL,
                pending_service_tier = NULL,
                updated_at = now()
            WHERE CAST(id AS VARCHAR) = ?
            """,
            [service_mode, service_tier, current_user["id"]],
        )
        conn.commit()
    finally:
        conn.close()
    current_user["service_mode"] = service_mode
    current_user["service_tier"] = service_tier
    current_user["pending_service_intent"] = ""
    current_user["pending_service_tier"] = None
    _clear_pending_service_choice()
    if service_mode == "autopilot":
        try:
            send_autopilot_customer_admin_email(
                user_email=str(current_user.get("email") or current_user.get("username") or "").strip() or "(missing)",
                monthly_shorts=service_tier or 15,
                chosen_at=chosen_at_label,
            )
        except Exception:
            logger.exception(
                "Autopilot customer admin notification failed for user_id=%s",
                current_user["id"],
            )
    return {
        "ok": True,
        "service_mode": service_mode,
        "service_tier": service_tier,
    }


@video_shorts_bp.route("/brands")
def brands_page():
    return redirect(url_for("video_shorts_bp.profile"))


@video_shorts_bp.route("/brands/create", methods=["POST"])
def create_brand():
    current_user = _current_user()
    if not current_user:
        return redirect(url_for("video_shorts_bp.login", next=request.url))
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("Brand name is required.", "warning")
        return redirect(request.referrer or url_for("video_shorts_bp.channels_page"))
    conn = get_db()
    try:
        ensure_brand_schema(conn)
        existing = conn.execute(
            """
            SELECT id
            FROM shorts_brands
            WHERE owner_user_id = ? AND lower(name) = lower(?)
            LIMIT 1
            """,
            [current_user["id"], name],
        ).fetchone()
        if existing:
            session["vs_brand_id"] = existing[0]
            conn.execute(
                "UPDATE shorts_users SET last_brand_id = ?, updated_at = now() WHERE id = ?",
                [existing[0], current_user["id"]],
            )
            conn.commit()
            flash("That brand already exists, so it was made active.", "info")
        else:
            brand = create_brand_record(conn, user_id=current_user["id"], name=name, make_default=False)
            session["vs_brand_id"] = brand["id"]
            flash("Brand created.", "success")
    finally:
        conn.close()
    return redirect(request.referrer or url_for("video_shorts_bp.channels_page"))


@video_shorts_bp.route("/privacy")
def privacy_page():
    return render_template("vs_privacy.html")


@video_shorts_bp.route("/data-deletion")
def data_deletion_page():
    return render_template("vs_data_deletion.html")


@video_shorts_bp.route("/terms")
def terms_page():
    return render_template("vs_terms.html")


@video_shorts_bp.route("/contact", methods=["GET", "POST"])
def contact_page():
    form_data = {
        "name": "",
        "email": "",
        "message": "",
    }
    error = ""
    success = False
    status_code = 200

    if request.method == "POST":
        form_data = {
            "name": (request.form.get("name") or "").strip(),
            "email": _normalize_auth_email(request.form.get("email") or ""),
            "message": (request.form.get("message") or "").strip(),
        }
        allowed, _retry_after = check_rate_limits(
            "contact-form",
            _rate_limit_key(form_data["email"]),
            CONTACT_RATE_LIMITS,
        )
        if not allowed:
            error = "Too many attempts. Please try again later."
            status_code = 429
        elif not form_data["name"] or not form_data["email"] or not form_data["message"]:
            error = "Please fill in all fields."
            status_code = 400
        elif not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", form_data["email"]):
            error = "Please enter a valid email address."
            status_code = 400
        elif not _verify_turnstile_or_fail():
            error = "Verification failed. Please try again."
            status_code = 400
        else:
            try:
                send_contact_email(
                    name=form_data["name"],
                    email=form_data["email"],
                    message=form_data["message"],
                )
            except Exception:
                logger.exception("Contact form send failed for %s", form_data["email"])
                error = "We couldn't send your message right now. Please try again."
                status_code = 502
            else:
                success = True
                form_data = {"name": "", "email": "", "message": ""}

    return (
        render_template(
            "vs_contact.html",
            contact_form=form_data,
            contact_error=error,
            contact_success=success,
        ),
        status_code,
    )
