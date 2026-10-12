import hashlib
import os
from datetime import datetime
from typing import Any, Optional
from uuid import uuid4

from flask import abort, current_app, g, redirect, render_template, request, session, url_for

from app.video_shorts.routes.auth import (
    _client_ip,
    _current_user,
    _establish_authenticated_session,
    _verify_turnstile_or_fail,
)
from app.video_shorts.routes.quick_short import (
    _ensure_quick_schema,
    _upsert_video,
)
from app.video_shorts.services.brands import current_brand_id, ensure_brand_for_user, ensure_brand_schema
from app.video_shorts.services.db import ensure_auth_user_schema, ensure_storage_user_schema, get_db
from app.video_shorts.services.quick_short_flow import STATUS_INGESTING, create_session, update_session
from app.video_shorts.services.render_jobs import JOB_TYPE_INGEST_YOUTUBE, enqueue_job
from app.video_shorts.services.user_events import track_event
from app.video_shorts.youtube_api import YoutubeApiError, extract_video_id, fetch_video_metadata


FREE_TOOL_MAX_VIDEO_SECONDS = 30 * 60
FREE_TOOL_DAILY_GUESTS_PER_IP = 3


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


def _latest_guest_generate_url(conn, user_id: str, brand_id: Optional[str]) -> str:
    row = conn.execute(
        """
        SELECT id
        FROM youtube_videos
        WHERE owner_user_id = ?
          AND ((? IS NULL AND brand_id IS NULL) OR brand_id = ?)
        ORDER BY id DESC
        LIMIT 1
        """,
        [user_id, brand_id, brand_id],
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


def _ingest_youtube_for_user(*, user_id: str, brand_id: Optional[str], video_url: str, meta: dict) -> int:
    video_id = str(meta.get("video_id") or "").strip() or extract_video_id(video_url) or ""
    if not video_id:
        raise ValueError("Enter a valid YouTube video URL.")
    _ensure_quick_schema()
    conn = get_db()
    try:
        ensure_brand_schema(conn)
        video_pk = _upsert_video(conn, meta, None, user_id, brand_id)
        conn.commit()
    finally:
        conn.close()
    if not video_pk:
        raise RuntimeError("Video record could not be created.")

    quick_session = create_session(
        user_id=user_id,
        brand_id=brand_id,
        source_type="youtube",
        source_url=video_url,
        payload={"meta": meta, "source": "free_tool"},
    )
    enqueue_result = enqueue_job(
        user_id=user_id,
        job_type=JOB_TYPE_INGEST_YOUTUBE,
        payload={
            "quick_session_id": quick_session["id"],
            "video_pk": int(video_pk),
            "video_id": video_id,
            "video_url": video_url,
            "duration_seconds": meta.get("duration_seconds"),
            "brand_id": brand_id,
        },
        input_hash=hashlib.sha256(f"free-tool-youtube:{user_id}:{brand_id}:{video_id}".encode("utf-8")).hexdigest(),
        max_attempts=6,
    )
    job = enqueue_result.get("job") or {}
    update_session(
        quick_session["id"],
        status=STATUS_INGESTING,
        video_pk=int(video_pk),
        video_id=video_id,
        ingest_job_id=job.get("id"),
        result={"stage": "queued", "message": "Queued for ingest."},
    )
    try:
        track_event(user_id, "video_uploaded", video_id=video_id)
    except Exception:
        current_app.logger.debug("free_tool_video_uploaded_event_failed", exc_info=True)
    return int(video_pk)


def render_free_tool_landing(locale: str = "en"):
    _ensure_free_tool_visible()
    locale = "tr" if locale == "tr" else "en"
    current_user = _current_user()
    error = ""
    video_url = ""
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
                    duration = meta.get("duration_seconds")
                    if duration is not None and float(duration) > FREE_TOOL_MAX_VIDEO_SECONDS:
                        landing_next = "/tr/araclar/youtube-shorts" if locale == "tr" else "/tools/youtube-to-shorts"
                        register_url = url_for("video_shorts_bp.register", next=landing_next)
                        return render_template(
                            "free_tool_youtube_to_shorts.html",
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
                        video_pk = _ingest_youtube_for_user(
                            user_id=str(current_user["id"]),
                            brand_id=brand_id,
                            video_url=video_url,
                            meta=meta,
                        )
                        return redirect(url_for("video_shorts_bp.generate_short", video_pk=video_pk))

                    guest_ip_hash = _ip_hash()
                    conn = get_db()
                    try:
                        ensure_storage_user_schema(conn)
                        ensure_auth_user_schema(conn)
                        if current_user and current_user.get("is_guest"):
                            guest_user_id = str(current_user["id"])
                            guest_brand_id = str(session.get("vs_brand_id") or current_brand_id() or "")
                            guest_row = conn.execute(
                                "SELECT COALESCE(guest_video_count, 0) FROM shorts_users WHERE id = ?",
                                [guest_user_id],
                            ).fetchone()
                            guest_video_count = int((guest_row or [0])[0] or 0)
                            if guest_video_count >= 1:
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
                                return render_template(
                                    "free_tool_youtube_to_shorts.html",
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

                        video_pk = _ingest_youtube_for_user(
                            user_id=guest_user_id,
                            brand_id=guest_brand_id or None,
                            video_url=video_url,
                            meta=meta,
                        )
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
                        return redirect(url_for("video_shorts_bp.generate_short", video_pk=video_pk))
                    finally:
                        try:
                            conn.close()
                        except Exception:
                            pass

    return render_template(
        "free_tool_youtube_to_shorts.html",
        locale=locale,
        error=error,
        register_url=url_for("video_shorts_bp.register", next=request.path),
        video_url=video_url,
    )
