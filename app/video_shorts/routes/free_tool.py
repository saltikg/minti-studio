import hashlib
import os
from datetime import datetime
from typing import Any, Optional
from uuid import uuid4

from flask import abort, current_app, g, jsonify, redirect, render_template, request, session, url_for

from app.video_shorts import video_shorts_bp
from app.video_shorts.routes.auth import (
    _client_ip,
    _current_user,
    _establish_authenticated_session,
    _verify_turnstile_or_fail,
    turnstile_enabled,
    turnstile_site_key,
)
from app.video_shorts.routes.quick_short import (
    build_quick_short_session_status_payload,
    create_youtube_ingest_session_for_user,
)
from app.video_shorts.config import FREE_TOOL_DAILY_VIDEO_CAP, FREE_TOOL_GUEST_MIN_FREE_DISK_GB
from app.video_shorts.services.disk_guard import free_disk_below_bytes
from app.video_shorts.services.brands import current_brand_id, ensure_brand_for_user, ensure_brand_schema
from app.video_shorts.services.db import ensure_auth_user_schema, ensure_storage_user_schema, get_db
from app.video_shorts.services.quick_short_flow import (
    get_session,
)
from app.video_shorts.services.user_events import track_event
from app.video_shorts.youtube_api import YoutubeApiError, extract_video_id, fetch_video_metadata


FREE_TOOL_MAX_VIDEO_SECONDS = 30 * 60
FREE_TOOL_DAILY_GUESTS_PER_IP = 3
FREE_TOOL_ANON_USER_PREFIX = "free_tool_anon"
FREE_TOOL_ATTRIBUTION_SESSION_KEY = "free_tool_attribution"


def _render_free_tool_template(**context):
    context.setdefault("vs_turnstile_enabled", turnstile_enabled())
    context.setdefault("vs_turnstile_site_key", turnstile_site_key())
    return render_template("free_tool_youtube_to_shorts.html", **context)


def _free_tool_enabled() -> bool:
    value = str(os.getenv("FREE_TOOL_ENABLED") or "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _preview_token_matches() -> bool:
    expected = str(os.getenv("FREE_TOOL_PREVIEW_TOKEN") or "").strip()
    provided = str(request.args.get("preview") or "").strip()
    return bool(expected and provided and provided == expected)


def _ensure_free_tool_visible() -> None:
    if _preview_token_matches():
        session["free_tool_preview"] = True
    if _free_tool_enabled() or session.get("free_tool_preview"):
        return
    abort(404)


def _ip_hash() -> str:
    secret = str(current_app.secret_key or os.getenv("SECRET_KEY") or "minti-free-tool").encode("utf-8")
    ip = _client_ip().encode("utf-8")
    return hashlib.sha256(secret + b":" + ip).hexdigest()


def _free_tool_event_user_id(current_user: Optional[dict] = None) -> str:
    if current_user and current_user.get("id"):
        return str(current_user.get("id"))
    return f"{FREE_TOOL_ANON_USER_PREFIX}:{_ip_hash()[:16]}"


def _clean_attribution_value(value: object, *, max_length: int = 120) -> str:
    return str(value or "").strip()[:max_length]


def _valid_free_tool_lead_id(raw_lid: object) -> str:
    clean_lid = _clean_attribution_value(raw_lid, max_length=80)
    if not clean_lid:
        return ""
    conn = get_db()
    try:
        row = conn.execute(
            """
            SELECT CAST(id AS VARCHAR)
            FROM autopilot_leads
            WHERE CAST(id AS VARCHAR) = ?
            LIMIT 1
            """,
            [clean_lid],
        ).fetchone()
        return str(row[0] or "").strip() if row else ""
    except Exception:
        return ""
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _capture_free_tool_first_touch() -> None:
    existing = session.get(FREE_TOOL_ATTRIBUTION_SESSION_KEY)
    if isinstance(existing, dict) and existing:
        return
    attribution = {
        "utm_source": _clean_attribution_value(request.args.get("utm_source")),
        "utm_medium": _clean_attribution_value(request.args.get("utm_medium")),
        "utm_campaign": _clean_attribution_value(request.args.get("utm_campaign")),
    }
    lead_id = _valid_free_tool_lead_id(request.args.get("lid"))
    if lead_id:
        attribution["lead_id"] = lead_id
    attribution = {key: value for key, value in attribution.items() if value}
    if attribution:
        session[FREE_TOOL_ATTRIBUTION_SESSION_KEY] = attribution
        session.modified = True


def _free_tool_attribution_metadata() -> dict:
    attribution = session.get(FREE_TOOL_ATTRIBUTION_SESSION_KEY)
    if not isinstance(attribution, dict):
        return {}
    allowed = {"utm_source", "utm_medium", "utm_campaign", "lead_id"}
    return {
        key: _clean_attribution_value(value)
        for key, value in attribution.items()
        if key in allowed and _clean_attribution_value(value)
    }


def _track_free_tool_event(
    event_name: str,
    *,
    current_user: Optional[dict] = None,
    video_id: Any = None,
    status: str = "",
    metadata: Optional[dict] = None,
) -> None:
    event_metadata = {**_free_tool_attribution_metadata(), **(metadata or {})}
    track_event(
        _free_tool_event_user_id(current_user),
        event_name,
        video_id=str(video_id or "").strip() or None,
        status=str(status or "").strip() or None,
        metadata=event_metadata,
    )


def _count_recent_guest_creations(conn, guest_ip_hash: str) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*)
        FROM shorts_users
        WHERE COALESCE(is_guest, FALSE) = TRUE
          AND guest_ip_hash = ?
          AND guest_created_at >= (now() - INTERVAL '24 hours')
        """,
        [guest_ip_hash],
    ).fetchone()
    return int((row or [0])[0] or 0)


def _count_recent_guest_videos(conn) -> int:
    row = conn.execute(
        """
        SELECT COALESCE(SUM(COALESCE(guest_video_count, 0)), 0)
        FROM shorts_users
        WHERE COALESCE(is_guest, FALSE) = TRUE
          AND guest_created_at >= (now() - INTERVAL '24 hours')
        """
    ).fetchone()
    return int((row or [0])[0] or 0)


def _latest_guest_generate_url(conn, user_id: str, brand_id: Optional[str]) -> str:
    if brand_id:
        row = conn.execute(
            """
            SELECT id
            FROM youtube_videos
            WHERE owner_user_id = ?
              AND brand_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            [user_id, brand_id],
        ).fetchone()
        if row:
            return url_for("video_shorts_bp.generate_short", video_pk=int(row[0]))
        return "/tools/youtube-to-shorts"
    row = conn.execute(
        """
        SELECT id
        FROM youtube_videos
        WHERE owner_user_id = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        [user_id],
    ).fetchone()
    if row:
        return url_for("video_shorts_bp.generate_short", video_pk=int(row[0]))
    return "/tools/youtube-to-shorts"


def _duration_label(seconds: Any) -> str:
    try:
        total = int(float(seconds or 0))
    except Exception:
        total = 0
    minutes, secs = divmod(max(0, total), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _create_guest_user_and_brand(conn, guest_ip_hash: str) -> tuple[str, str]:
    ensure_storage_user_schema(conn)
    ensure_auth_user_schema(conn)
    ensure_brand_schema(conn)
    user_id = str(uuid4())
    now = datetime.utcnow()
    conn.execute(
        """
        INSERT INTO shorts_users (
            id, username, password_hash, name, email, role, plan_id,
            email_verified, created_at, is_guest, guest_created_at,
            guest_ip_hash, guest_render_count, guest_video_count
        )
        VALUES (?, NULL, NULL, 'Guest', NULL, 'member', NULL, FALSE, ?, TRUE, ?, ?, 0, 0)
        """,
        [user_id, now, now, guest_ip_hash],
    )
    brand = ensure_brand_for_user(conn, user_id=user_id, user_name="Guest")
    brand_id = str((brand or {}).get("id") or "")
    return user_id, brand_id


def _generate_url(video_pk: Any, plan_index: Any = None) -> str:
    url = url_for("video_shorts_bp.generate_short", video_pk=int(video_pk))
    clean_plan_index = str(plan_index or "").strip()
    if clean_plan_index:
        return f"{url}?tab=shorts&clip={clean_plan_index}"
    return url


def _store_free_tool_processing_state(*, video_pk: int, video_id: str, quick_session_id: str, video_url: str) -> None:
    session["free_tool_processing"] = {
        "video_pk": int(video_pk),
        "video_id": str(video_id or "").strip(),
        "quick_session_id": str(quick_session_id or "").strip(),
        "video_url": str(video_url or "").strip(),
    }
    session.modified = True


def _lookup_processing_session_from_flask_session(current_user: Optional[dict]) -> Optional[dict]:
    if not current_user or not current_user.get("is_guest"):
        return None
    state = session.get("free_tool_processing") or {}
    session_id = str((state or {}).get("quick_session_id") or "").strip()
    if not session_id:
        return None
    return get_session(session_id, user_id=str(current_user.get("id") or "")) or None


def _free_tool_video_status_payload(*, current_user: dict, video_pk: int, quick_session_id: str = "") -> tuple[dict, int]:
    from app.video_shorts.routes import generation

    quick_session = None
    if quick_session_id:
        quick_session = get_session(quick_session_id, user_id=str(current_user.get("id") or ""))
    if not quick_session:
        quick_session = _lookup_processing_session_from_flask_session(current_user)
    if not quick_session:
        return {"ok": False, "message": "Processing session not found."}, 404
    if int(quick_session.get("video_pk") or 0) != int(video_pk):
        return {"ok": False, "message": "Forbidden."}, 403

    quick_payload, quick_status = build_quick_short_session_status_payload(
        current_user,
        str(quick_session.get("id") or ""),
    )
    if quick_status != 200 or not quick_payload.get("ok"):
        return quick_payload, quick_status
    auto_payload, auto_status = generation.build_auto_suggest_render_status_payload(
        int(video_pk),
        current_user,
    )
    if auto_status != 200 or not auto_payload.get("success"):
        return {"ok": False, "message": auto_payload.get("message") or "Video not found."}, auto_status

    quick_session_payload = quick_payload.get("session") or {}
    video = quick_session_payload.get("video") or {}
    selected_plan_index = str(auto_payload.get("selected_rendered_plan_index") or "").strip()
    session_failed = str(quick_session_payload.get("status") or "").strip().lower() == "failed"
    ingest_failed = str(video.get("download_status") or "").strip().lower() in {"failed", "error"}
    transcript_failed = str(video.get("transcript_status") or "").strip().lower() in {"failed", "error"}
    status = "processing"
    message = ""
    if selected_plan_index:
        status = "ready"
        ready_key = f"free_tool_top1_ready:{video_pk}:{selected_plan_index}"
        if not session.get(ready_key):
            _track_free_tool_event(
                "free_tool_top1_ready",
                current_user=current_user,
                video_id=video.get("video_id") or video_pk,
                status="ready",
                metadata={"video_pk": int(video_pk), "plan_index": selected_plan_index},
            )
            session[ready_key] = True
            session.modified = True
    elif ingest_failed or transcript_failed or session_failed:
        status = "failed"
        message = "This video could not be processed right now. Please try again."
    elif int(auto_payload.get("clip_count") or 0) > 0:
        status = "rendering"
    elif not auto_payload.get("pending"):
        status = "finding"
    return {
        "ok": True,
        "status": status,
        "message": message,
        "video": video,
        "session": quick_session_payload,
        "auto_status": auto_payload,
        "suggestions_ready": int(auto_payload.get("clip_count") or 0) > 0,
        "render_done": bool(selected_plan_index),
        "active_render_count": int(auto_payload.get("active_render_count") or 0),
        "clip_count": int(auto_payload.get("clip_count") or 0),
        "selected_plan_index": selected_plan_index,
        "generate_url": _generate_url(video_pk, selected_plan_index),
        "redirect_url": _generate_url(video_pk, selected_plan_index) if selected_plan_index else "",
        "fallback_url": _generate_url(video_pk),
    }, 200


def _processing_context_for_guest(current_user: Optional[dict]) -> Optional[dict]:
    quick_session = _lookup_processing_session_from_flask_session(current_user)
    if not quick_session:
        return None
    video_pk = quick_session.get("video_pk") or (session.get("free_tool_processing") or {}).get("video_pk")
    if not video_pk:
        return None
    payload, status = _free_tool_video_status_payload(
        current_user=current_user or {},
        video_pk=int(video_pk),
        quick_session_id=str(quick_session.get("id") or ""),
    )
    if status != 200 or not payload.get("ok"):
        return None
    return {
        "quick_session_id": str(quick_session.get("id") or ""),
        "video_pk": int(video_pk),
        "status_url": url_for("video_shorts_bp.free_tool_processing_status", video_pk=int(video_pk), session_id=quick_session.get("id")),
        **payload,
    }


@video_shorts_bp.route("/tools/youtube-to-shorts", methods=["GET", "POST"])
def free_tool_youtube_to_shorts():
    return render_free_tool_landing("en")


@video_shorts_bp.route("/tr/araclar/youtube-shorts", methods=["GET", "POST"])
def free_tool_youtube_to_shorts_tr():
    return render_free_tool_landing("tr")


@video_shorts_bp.route("/tools/youtube-to-shorts/status/<int:video_pk>", methods=["GET"])
def free_tool_processing_status(video_pk: int):
    current_user = _current_user()
    if not current_user:
        return jsonify(ok=False, message="Authentication required."), 401
    if not current_user.get("is_guest"):
        return jsonify(ok=False, message="Only guest processing sessions use this endpoint."), 403
    quick_session_id = str(request.args.get("session_id") or "").strip()
    payload, status = _free_tool_video_status_payload(
        current_user=current_user,
        video_pk=video_pk,
        quick_session_id=quick_session_id,
    )
    return jsonify(payload), status


@video_shorts_bp.route("/tools/youtube-to-shorts/event", methods=["POST"])
def free_tool_event():
    current_user = _current_user()
    payload = request.get_json(silent=True) or request.form.to_dict() or {}
    event_name = str(payload.get("event") or "").strip()
    allowed_events = {
        "free_tool_gate_shown",
        "free_tool_gate_click",
    }
    if event_name not in allowed_events:
        return jsonify(ok=False, message="Unknown event."), 400
    metadata = {
        "action": str(payload.get("action") or "").strip(),
        "reason": str(payload.get("reason") or "").strip(),
        "choice": str(payload.get("choice") or "").strip(),
        "clip": str(payload.get("clip") or "").strip(),
        "path": str(payload.get("path") or request.path or "").strip(),
    }
    _track_free_tool_event(event_name, current_user=current_user, status=metadata.get("choice") or "", metadata=metadata)
    return jsonify(ok=True)


def render_free_tool_landing(locale: str = "en"):
    _ensure_free_tool_visible()
    _capture_free_tool_first_touch()
    locale = "tr" if locale == "tr" else "en"
    session["free_tool_lang"] = locale
    current_user = _current_user()
    error = ""
    video_url = ""
    processing_context = _processing_context_for_guest(current_user) if request.method == "GET" else None
    if request.method == "GET":
        _track_free_tool_event(
            "free_tool_landing_view",
            current_user=current_user,
            status=locale,
            metadata={"locale": locale, "path": request.path},
        )
    if request.method == "POST":
        video_url = str(request.form.get("video_url") or request.form.get("youtube_url") or "").strip()
        if not _verify_turnstile_or_fail():
            error = "Verification failed. Please try again." if locale == "en" else "Doğrulama başarısız. Lütfen tekrar deneyin."
        else:
            video_id = extract_video_id(video_url)
            if not video_id:
                error = "Enter a valid YouTube video URL." if locale == "en" else "Geçerli bir YouTube video bağlantısı girin."
            else:
                try:
                    meta = fetch_video_metadata(video_id)
                    meta["video_id"] = video_id
                except YoutubeApiError as exc:
                    error = str(exc)
                    meta = None
                except Exception:
                    current_app.logger.exception("free_tool_metadata_failed video_id=%s", video_id)
                    error = (
                        "We couldn't read that YouTube video right now."
                        if locale == "en"
                        else "Bu YouTube videosunu şu anda okuyamadık."
                    )
                    meta = None
                if not error and meta:
                    _track_free_tool_event(
                        "free_tool_submit",
                        current_user=current_user,
                        video_id=video_id,
                        status=locale,
                        metadata={
                            "locale": locale,
                            "path": request.path,
                            "video_id": video_id,
                            "video_title": str(meta.get("title") or "").strip(),
                        },
                    )
                    duration = meta.get("duration_seconds")
                    if duration is not None and float(duration) > FREE_TOOL_MAX_VIDEO_SECONDS:
                        landing_next = "/tr/araclar/youtube-shorts" if locale == "tr" else "/tools/youtube-to-shorts"
                        register_url = url_for("video_shorts_bp.register", next=landing_next)
                        return _render_free_tool_template(
                            locale=locale,
                            error=(
                                "Free tool supports videos up to 30 min — sign up free for longer videos"
                                if locale == "en"
                                else "Ücretsiz araç en fazla 30 dakikalık videoları destekler — daha uzun videolar için ücretsiz hesap oluşturun"
                            ),
                            register_url=register_url,
                            video_url=video_url,
                            duration_label=_duration_label(duration),
                        )
                    if current_user and not current_user.get("is_guest"):
                        brand_id = current_brand_id()
                        ingest_result = create_youtube_ingest_session_for_user(
                            user_id=str(current_user["id"]),
                            brand_id=brand_id,
                            video_url=video_url,
                            meta=meta,
                            source="free_tool_logged_in",
                        )
                        return redirect(url_for("video_shorts_bp.generate_short", video_pk=ingest_result["video_pk"]))

                    guest_ip_hash = _ip_hash()
                    retrying_same_guest_video = False
                    current_processing = session.get("free_tool_processing") or {}
                    if current_user and current_user.get("is_guest"):
                        retrying_same_guest_video = str(current_processing.get("video_id") or "").strip() == video_id
                    conn = get_db()
                    try:
                        ensure_storage_user_schema(conn)
                        ensure_auth_user_schema(conn)
                        if FREE_TOOL_DAILY_VIDEO_CAP and _count_recent_guest_videos(conn) >= int(FREE_TOOL_DAILY_VIDEO_CAP):
                            error = (
                                "Free tool is busy today — sign up free to continue"
                                if locale == "en"
                                else "Ücretsiz araç bugün yoğun — devam etmek için ücretsiz hesap oluşturun"
                            )
                            conn.close()
                            return _render_free_tool_template(
                                locale=locale,
                                error=error,
                                register_url=url_for("video_shorts_bp.register", next=request.path),
                                video_url=video_url,
                            )
                        if free_disk_below_bytes(int(FREE_TOOL_GUEST_MIN_FREE_DISK_GB) * 1024**3):
                            error = (
                                "Free tool is busy today — sign up free to continue"
                                if locale == "en"
                                else "Ücretsiz araç bugün yoğun — devam etmek için ücretsiz hesap oluşturun"
                            )
                            conn.close()
                            return _render_free_tool_template(
                                locale=locale,
                                error=error,
                                register_url=url_for("video_shorts_bp.register", next=request.path),
                                video_url=video_url,
                            )
                        if current_user and current_user.get("is_guest"):
                            guest_user_id = str(current_user["id"])
                            guest_brand_id = str(session.get("vs_brand_id") or current_brand_id() or "")
                            guest_row = conn.execute(
                                "SELECT COALESCE(guest_video_count, 0) FROM shorts_users WHERE id = ?",
                                [guest_user_id],
                            ).fetchone()
                            guest_video_count = int((guest_row or [0])[0] or 0)
                            if guest_video_count >= 1 and not retrying_same_guest_video:
                                generate_url = _latest_guest_generate_url(conn, guest_user_id, guest_brand_id or None)
                                conn.close()
                                return redirect(url_for("video_shorts_bp.register", next=generate_url, action="new_video"))
                        else:
                            if _count_recent_guest_creations(conn, guest_ip_hash) >= FREE_TOOL_DAILY_GUESTS_PER_IP:
                                error = (
                                    "You've used the free tool a few times today. Create a free account to continue."
                                    if locale == "en"
                                    else "Bugün ücretsiz aracı birkaç kez kullandınız. Devam etmek için ücretsiz hesap oluşturun."
                                )
                                conn.close()
                                return _render_free_tool_template(
                                    locale=locale,
                                    error=error,
                                    register_url=url_for("video_shorts_bp.register", next=request.path),
                                    video_url=video_url,
                                )
                            guest_user_id, guest_brand_id = _create_guest_user_and_brand(conn, guest_ip_hash)
                            conn.commit()
                            _establish_authenticated_session(user_id=guest_user_id, brand_id=guest_brand_id)
                            g.vs_current_user = None
                        conn.close()

                        ingest_result = create_youtube_ingest_session_for_user(
                            user_id=guest_user_id,
                            brand_id=guest_brand_id or None,
                            video_url=video_url,
                            meta=meta,
                            source="free_tool",
                        )
                        if not retrying_same_guest_video:
                            conn_update = get_db()
                            try:
                                conn_update.execute(
                                    """
                                    UPDATE shorts_users
                                    SET guest_video_count = COALESCE(guest_video_count, 0) + 1,
                                        updated_at = now()
                                    WHERE id = ? AND COALESCE(is_guest, FALSE) = TRUE
                                    """,
                                    [guest_user_id],
                                )
                                conn_update.commit()
                            finally:
                                conn_update.close()
                        _store_free_tool_processing_state(
                            video_pk=int(ingest_result["video_pk"]),
                            video_id=str(ingest_result["video_id"]),
                            quick_session_id=str(ingest_result["quick_session_id"]),
                            video_url=video_url,
                        )
                        processing_context = _processing_context_for_guest({"id": guest_user_id, "is_guest": True})
                        return _render_free_tool_template(
                            locale=locale,
                            error="",
                            register_url=url_for("video_shorts_bp.register", next=request.path),
                            video_url=video_url,
                            processing_context=processing_context,
                        )
                    finally:
                        try:
                            conn.close()
                        except Exception:
                            pass

    return _render_free_tool_template(
        locale=locale,
        error=error,
        register_url=url_for("video_shorts_bp.register", next=request.path),
        video_url=video_url,
        processing_context=processing_context,
    )
