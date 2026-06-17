import logging

from flask import current_app, g, redirect, render_template, request, session, url_for, flash
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
    SHORTS_OVERVIEW_STATS_TTL_MINUTES,
    SHORTS_OVERVIEW_STATS_MAX_VIDEOS,
    SHORTS_OVERVIEW_QUOTA_COOLDOWN_HOURS,
)
from app.video_shorts.services.db import ensure_storage_user_schema, get_db, get_db_readonly
from app.video_shorts.services.brands import (
    create_brand as create_brand_record,
    ensure_brand_for_user,
    ensure_brand_schema,
    list_user_brands,
    load_brand_context,
    set_active_brand_for_user,
    set_default_brand_for_user,
)
from app.video_shorts.services.shorts_overview_quota import get_shorts_overview_quota_state
from app.video_shorts.services.youtube_oauth import build_oauth_flow, is_reauth_required, store_refresh_token

DEFAULT_TIME_ZONE = "America/Los_Angeles"
logger = logging.getLogger(__name__)
TIMEZONE_OPTIONS = [
    ("America/Los_Angeles", "Pacific (PST/PDT)"),
    ("America/Denver", "Mountain (MST/MDT)"),
    ("America/Chicago", "Central (CST/CDT)"),
    ("America/New_York", "Eastern (EST/EDT)"),
    ("UTC", "UTC"),
    ("Europe/Istanbul", "Turkey (TRT)"),
]


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
          time_zone
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
        row = conn.execute(select_sql, [user_id]).fetchone()
    except Exception as exc:
        conn.close()
        # If schema is missing, initialize via a writable connection once.
        if "shorts_users" in str(exc).lower():
            try:
                conn = get_db()
                try:
                    ensure_storage_user_schema(conn)
                    row = conn.execute(select_sql, [user_id]).fetchone()
                finally:
                    conn.close()
            except Exception as inner_exc:
                logger.warning("Video shorts auth fallback DB unavailable while loading session user: %s", inner_exc)
                g.vs_current_user = None
                return None
        else:
            logger.warning("Video shorts auth lookup failed while loading session user: %s", exc)
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
    }
    return g.vs_current_user


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
        "video_shorts_bp.google_login",
        "video_shorts_bp.google_oauth_callback",
        "video_shorts_bp.logout",
        "video_shorts_bp.privacy_page",
        "video_shorts_bp.static",
        "video_shorts_bp.caption_tasks",
        "video_shorts_bp.caption_result",
        "video_shorts_bp.caption_status",
        "video_shorts_bp.download_status",
        "video_shorts_bp.download_tasks",
        "video_shorts_bp.serve_media",
        "video_shorts_bp.home",
        "video_shorts_bp.switch_brand",
        "video_shorts_bp.set_default_brand",
        "video_shorts_bp.create_brand",
    }
    if request.endpoint in allowed:
        return
    if request.endpoint and request.endpoint.startswith("video_shorts_bp."):
        if not _is_authenticated():
            return redirect(url_for("video_shorts_bp.login", next=request.url))


@video_shorts_bp.route("/login", methods=["GET", "POST"])
def login():
    if _is_authenticated():
        nxt = _normalize_next_url(request.args.get("next")) or url_for("video_shorts_bp.channels_page")
        return redirect(nxt)
    error = None
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        if not username or not password:
            error = "Username and password are required."
        else:
            conn = get_db()
            ensure_storage_user_schema(conn)
            row = conn.execute(
                """
                SELECT CAST(id AS VARCHAR), username, password_hash, name
                FROM shorts_users
                WHERE lower(username) = lower(?)
                """,
                [username],
            ).fetchone()
            conn.close()
            if not row:
                error = "User not found."
            elif not row[2]:
                error = "This account uses Google sign-in. Please use the Google option."
            elif not check_password_hash(row[2], password):
                error = "Incorrect password."
            else:
                session["vs_user_id"] = row[0]
                brand_conn = get_db()
                try:
                    ensure_storage_user_schema(brand_conn)
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
                flash(f"Welcome back, {row[3] or row[1]}!", "success")
                nxt = _normalize_next_url(request.args.get("next")) or url_for("video_shorts_bp.channels_page")
                return redirect(nxt)
    if error:
        flash(error, "danger")
    return render_template("vs_login.html")


@video_shorts_bp.route("/register", methods=["GET", "POST"])
def register():
    if _is_authenticated():
        return redirect(url_for("video_shorts_bp.channels_page"))
    error = None
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        password_confirm = request.form.get("password_confirm") or ""
        if not email:
            error = "E-posta gereklidir."
        elif "@" not in email:
            error = "Please enter a valid email."
        elif len(password) < 8:
            error = "Password must be at least 8 characters."
        elif password != password_confirm:
            error = "Passwords do not match."
        else:
            conn = get_db()
            ensure_storage_user_schema(conn)
            existing = conn.execute(
                "SELECT 1 FROM shorts_users WHERE lower(email) = ? OR lower(username) = ?",
                [email, email],
            ).fetchone()
            if existing:
                error = "Bu e-posta ile zaten hesap var."
                conn.close()
            else:
                username = email
                name = email.split("@")[0].replace(".", " ").title()
                user_id = str(uuid4())
                conn.execute(
                    """
                    INSERT INTO shorts_users (id, username, password_hash, name, email, role, plan_id)
                    VALUES (?, ?, ?, ?, ?, 'member', ?)
                    """,
                    [
                        user_id,
                        username,
                        generate_password_hash(password),
                        name,
                        email,
                        DEFAULT_USER_PLAN_ID,
                    ],
                )
                ensure_brand_schema(conn)
                brand = create_brand_record(
                    conn,
                    user_id=user_id,
                    name=f"{name} Workspace",
                    make_default=True,
                )
                conn.commit()
                conn.close()
                session["vs_user_id"] = user_id
                session["vs_brand_id"] = brand["id"]
                flash("Account created.", "success")
                return redirect(url_for("video_shorts_bp.channels_page"))
    if error:
        flash(error, "danger")
    return render_template("vs_register.html")


@video_shorts_bp.route("/logout")
def logout():
    session.pop("vs_user_id", None)
    session.pop("vs_brand_id", None)
    flash("You have been signed out.", "info")
    return redirect(url_for("video_shorts_bp.login"))


@video_shorts_bp.route("/profile", methods=["GET", "POST"])
def profile():
    user = _current_user()
    if not user:
        return redirect(url_for("video_shorts_bp.login", next=request.url))

    conn = get_db()
    ensure_storage_user_schema(conn)
    plan_row = None
    if user.get("plan_id"):
        plan_row = conn.execute(
            "SELECT label, quota_bytes FROM shorts_storage_plans WHERE plan_id = ?",
            [user["plan_id"]],
        ).fetchone()
    limit_bytes = (
        user.get("custom_limit_bytes")
        or (plan_row[1] if plan_row else None)
        or DEFAULT_USER_STORAGE_LIMIT
    )
    plan_label = plan_row[0] if plan_row else "No plan"
    used_bytes = conn.execute(
        """
        SELECT COALESCE(SUM(size_bytes), 0)
        FROM shorts_storage_assets
        WHERE user_id = ? AND (status = 'active' OR status IS NULL)
        """,
        [user["id"]],
    ).fetchone()[0]
    usage_percent = int(min(100, (used_bytes / limit_bytes * 100))) if limit_bytes else 0

    if request.method == "POST":
        form_type = request.form.get("form_type")
        if form_type == "profile":
            name = (request.form.get("name") or "").strip()
            email = (request.form.get("email") or "").strip()
            time_zone = (request.form.get("time_zone") or DEFAULT_TIME_ZONE).strip()
            valid_timezones = {value for value, _ in TIMEZONE_OPTIONS}
            if time_zone not in valid_timezones:
                time_zone = DEFAULT_TIME_ZONE
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
        plan_label=plan_label,
        limit_label=_format_size_bytes(limit_bytes),
        used_label=_format_size_bytes(used_bytes),
        usage_percent=usage_percent,
        timezones=TIMEZONE_OPTIONS,
        selected_timezone=user.get("time_zone") or DEFAULT_TIME_ZONE,
    )


@video_shorts_bp.context_processor
def inject_current_user():
    return {
        "vs_current_user": _current_user(),
        "vs_current_brand": _current_brand(),
        "vs_brands": getattr(g, "vs_brands", []),
        "vs_google_oauth_available": _google_oauth_enabled(),
        "vs_youtube_reauth_required": _youtube_reauth_required(),
        "vs_overview_quota": _load_overview_quota_context(),
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
    flow = _build_google_flow()
    authorization_url, state = flow.authorization_url(
        prompt="consent", include_granted_scopes="true"
    )
    session["google_oauth_state"] = state
    session["google_oauth_code_verifier"] = flow.code_verifier
    session["google_login_next"] = _normalize_next_url(request.args.get("next")) or url_for("video_shorts_bp.channels_page")
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
            "UPDATE shorts_users SET google_sub = ?, updated_at = now() WHERE id = ?",
            [google_sub, user_id],
        )
        conn.commit()
    else:
        user_id = str(uuid4())
        conn.execute(
            """
            INSERT INTO shorts_users (id, username, name, email, google_sub, role, plan_id)
            VALUES (?, ?, ?, ?, ?, 'member', ?)
            """,
            [
                user_id,
                email,
                name or email,
                email,
                google_sub,
                DEFAULT_USER_PLAN_ID,
            ],
        )
        conn.commit()
    ensure_brand_schema(conn)
    brand = ensure_brand_for_user(conn, user_id=user_id, user_name=name or email)
    conn.close()
    session["vs_user_id"] = user_id
    if brand:
        session["vs_brand_id"] = brand["id"]
    flash("Signed in with Google.", "success")
    nxt = _normalize_next_url(session.pop("google_login_next", None)) or url_for("video_shorts_bp.channels_page")
    return redirect(nxt)


@video_shorts_bp.route("/brands/switch", methods=["POST"])
def switch_brand():
    current_user = _current_user()
    if not current_user:
        return redirect(url_for("video_shorts_bp.login", next=request.url))
    brand_id = (request.form.get("brand_id") or "").strip()
    brand = set_default_brand_for_user(current_user["id"], brand_id)
    if not brand:
        flash("Brand bulunamadı.", "warning")
    else:
        session["vs_brand_id"] = brand["id"]
        flash(f"{brand['name']} aktif ve default brand yapildi.", "success")
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
        flash("Brand bulunamadı.", "warning")
    else:
        session["vs_brand_id"] = brand["id"]
        flash(f"{brand['name']} default brand yapildi.", "success")
    nxt = _normalize_next_url(request.form.get("next")) or request.referrer or url_for("video_shorts_bp.channels_page")
    return redirect(nxt)


@video_shorts_bp.route("/account")
def account_page():
    current_user = _current_user()
    if not current_user:
        return redirect(url_for("video_shorts_bp.login", next=request.url))

    account_items = [
        {
            "title": "Connected Accounts",
            "subtitle": "Manage YouTube, Instagram, Facebook and TikTok connections",
            "icon": "public",
            "href": url_for("video_shorts_bp.social_connect"),
        },
        {
            "title": "My Storage",
            "subtitle": "Browse uploaded files and storage assets",
            "icon": "cloud_download",
            "href": url_for("video_shorts_bp.shorts_storage"),
        },
        {
            "title": "Plan",
            "subtitle": "Review quota, storage limits and plan usage",
            "icon": "workspace_premium",
            "href": url_for("video_shorts_bp.shorts_storage_plans"),
        },
        {
            "title": "User Profile",
            "subtitle": "Update profile and account details",
            "icon": "account_circle",
            "href": url_for("video_shorts_bp.profile"),
        },
        {
            "title": "Brand",
            "subtitle": "Switch and manage your brands",
            "icon": "storefront",
            "href": url_for("video_shorts_bp.brands_page"),
        },
    ]
    if current_user.get("role") == "admin":
        account_items.append(
            {
                "title": "Logs",
                "subtitle": "Review social publishing and quota logs",
                "icon": "list_alt",
                "href": url_for("video_shorts_bp.shorts_social_logs"),
            }
        )
    return render_template("shorts_account.html", account_items=account_items)


@video_shorts_bp.route("/brands")
def brands_page():
    current_user = _current_user()
    if not current_user:
        return redirect(url_for("video_shorts_bp.login", next=request.url))
    conn = get_db_readonly()
    try:
        ensure_brand_schema(conn)
        brands = list_user_brands(conn, current_user["id"])
    finally:
        conn.close()
    return render_template(
        "shorts_brands.html",
        brands=brands,
        current_brand=getattr(g, "vs_current_brand", None),
    )


@video_shorts_bp.route("/brands/create", methods=["POST"])
def create_brand():
    current_user = _current_user()
    if not current_user:
        return redirect(url_for("video_shorts_bp.login", next=request.url))
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("Brand adı gerekli.", "warning")
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
            flash("Bu brand zaten var; aktif brand olarak seçildi.", "info")
        else:
            brand = create_brand_record(conn, user_id=current_user["id"], name=name, make_default=False)
            session["vs_brand_id"] = brand["id"]
            flash("Brand oluşturuldu.", "success")
    finally:
        conn.close()
    return redirect(request.referrer or url_for("video_shorts_bp.channels_page"))


@video_shorts_bp.route("/privacy")
def privacy_page():
    return render_template("vs_privacy.html")


@video_shorts_bp.route("/data-deletion")
def data_deletion_page():
    return render_template("vs_data_deletion.html")
