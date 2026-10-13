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
    _build_session_payload,
    _ensure_quick_schema,
    _get_or_create_channel,
    _upsert_video,
)
from app.video_shorts.config import FREE_TOOL_DAILY_VIDEO_CAP
from app.video_shorts.services.brands import current_brand_id, ensure_brand_for_user, ensure_brand_schema
from app.video_shorts.services.db import ensure_auth_user_schema, ensure_storage_user_schema, get_db
from app.video_shorts.services.quick_short_flow import (
    STATUS_FAILED,
    STATUS_INGESTING,
    create_session,
    get_session,
    update_session,
)
from app.video_shorts.services.render_jobs import JOB_TYPE_INGEST_YOUTUBE, enqueue_job
from app.video_shorts.services.user_events import track_event
from app.video_shorts.youtube_api import YoutubeApiError, extract_video_id, fetch_video_metadata


FREE_TOOL_MAX_VIDEO_SECONDS = 30 * 60
FREE_TOOL_DAILY_GUESTS_PER_IP = 3


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


def _plan_entry_index(entry: dict, fallback: int) -> str:
    for key in ("plan_index", "index", "entry_index"):
        value = entry.get(key)
        if value is not None and str(value).strip() != "":
            return str(value).strip()
    return str(fallback)


def _free_tool_video_status_payload(*, current_user: dict, video_pk: int, quick_session_id: str = "") -> tuple[dict, int]:
    from app.video_shorts.routes import generation

    conn = get_db()
    try:
        row = conn.execute(
            """
            SELECT id, video_id, title, thumbnail_url, duration_seconds, download_status,
                   transcript_status, owner_user_id, brand_id
            FROM youtube_videos
            WHERE id = ?
            """,
            [int(video_pk)],
        ).fetchone()
        if not row:
            return {"ok": False, "message": "Video not found."}, 404
        owner_user_id = str(row[7] or "")
        if owner_user_id != str(current_user.get("id") or ""):
            return {"ok": False, "message": "Forbidden."}, 403
        video = {
            "id": int(row[0]),
            "video_id": row[1],
            "title": row[2] or "YouTube video",
            "thumbnail_url": row[3],
            "duration_seconds": row[4],
            "duration_label": _duration_label(row[4]),
            "download_status": row[5],
            "transcript_status": row[6],
        }
        brand_id = row[8]
    finally:
        conn.close()

    quick_session = None
    if quick_session_id:
        quick_session = get_session(quick_session_id, user_id=str(current_user.get("id") or ""))
    if not quick_session:
        quick_session = _lookup_processing_session_from_flask_session(current_user)
    quick_payload = _build_session_payload(quick_session) if quick_session else None

    plan_entries = generation._load_plan_entries(str(video.get("video_id") or "").strip()) or []
    rendered: list[tuple[float, int, dict, str]] = []
    failed_render = False
    active_render_count = 0
    for fallback_index, raw_entry in enumerate(plan_entries):
        entry = raw_entry or {}
        plan_index = _plan_entry_index(entry, fallback_index)
        score = 0.0
        try:
            score = float(entry.get("score") or entry.get("ai_score") or 0)
        except Exception:
            score = 0.0
        filename = str(entry.get("output_filename") or entry.get("clip_filename") or entry.get("video_filename") or "").strip()
        if filename:
            rendered.append((score, fallback_index, entry, plan_index))
        status = str(entry.get("status") or entry.get("render_job_status") or "").strip().lower()
        if status in {"queued", "processing", "rendering"} or str(entry.get("render_job_id") or "").strip():
            active_render_count += 1
        if status in {"failed", "error"}:
            failed_render = True
    rendered.sort(key=lambda item: (item[0], -item[1]), reverse=True)
    selected_plan_index = rendered[0][3] if rendered else ""
    transcript_ready = str(video.get("transcript_status") or "").strip().lower() in {"done", "ready", "complete", "completed"}
    ingest_failed = str(video.get("download_status") or "").strip().lower() in {"failed", "error"}
    transcript_failed = str(video.get("transcript_status") or "").strip().lower() in {"failed", "error"}
    session_failed = str((quick_payload or {}).get("status") or "").strip().lower() == STATUS_FAILED
    status = "processing"
    message = ""
    if selected_plan_index:
        status = "ready"
    elif ingest_failed or transcript_failed or session_failed or failed_render:
        status = "failed"
        message = "This video could not be processed right now. Please try again."
    elif plan_entries and transcript_ready:
        status = "rendering"
    elif transcript_ready:
        status = "finding"
    return {
        "ok": True,
        "status": status,
        "message": message,
        "video": video,
        "session": quick_payload,
        "suggestions_ready": bool(plan_entries),
        "render_done": bool(selected_plan_index),
        "active_render_count": active_render_count,
        "clip_count": len(plan_entries),
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


def _ingest_youtube_for_user(*, user_id: str, brand_id: Optional[str], video_url: str, meta: dict) -> dict:
    video_id = str(meta.get("video_id") or "").strip() or extract_video_id(video_url) or ""
    if not video_id:
        raise ValueError("Enter a valid YouTube video URL.")
    _ensure_quick_schema()
    conn = get_db()
    try:
        ensure_brand_schema(conn)
        channel_id = _get_or_create_channel(conn, meta, user_id, brand_id)
        video_pk = _upsert_video(conn, meta, channel_id, user_id, brand_id)
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
    return {
        "video_pk": int(video_pk),
        "video_id": video_id,
        "quick_session_id": str(quick_session["id"]),
        "ingest_job_id": str(job.get("id") or ""),
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


def render_free_tool_landing(locale: str = "en"):
    _ensure_free_tool_visible()
    locale = "tr" if locale == "tr" else "en"
    session["free_tool_lang"] = locale
    current_user = _current_user()
    error = ""
    video_url = ""
    processing_context = _processing_context_for_guest(current_user) if request.method == "GET" else None
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
                        ingest_result = _ingest_youtube_for_user(
                            user_id=str(current_user["id"]),
                            brand_id=brand_id,
                            video_url=video_url,
                            meta=meta,
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

                        ingest_result = _ingest_youtube_for_user(
                            user_id=guest_user_id,
                            brand_id=guest_brand_id or None,
                            video_url=video_url,
                            meta=meta,
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
