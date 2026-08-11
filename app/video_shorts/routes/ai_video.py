from __future__ import annotations

import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from flask import current_app, flash, g, jsonify, redirect, render_template, request, url_for
from werkzeug.utils import secure_filename

from app.video_shorts import video_shorts_bp
from app.video_shorts.config import SHORTS_DIR
from app.video_shorts.services.timezones import DEFAULT_TIME_ZONE, TIMEZONE_LABELS
from app.video_shorts.routes.generation import (
    _find_latest_publish,
    _format_publish_display,
    _is_token_expired,
    _probe_media_duration_seconds,
    _short_public_url,
    _short_storage_key,
    _upsert_storage_asset,
    local_to_utc_rfc3339,
)
from app.video_shorts.services.ai_video_workspace import (
    create_ai_video,
    current_ai_video_brand_id,
    delete_background,
    delete_character,
    delete_ai_video,
    get_background,
    get_ai_video,
    get_character,
    list_ai_videos,
    list_backgrounds,
    list_characters,
    save_background,
    save_character,
    set_default_background,
    set_default_character,
    update_ai_video_content,
    update_ai_video_youtube_state,
)
from app.video_shorts.services.db import get_db, ensure_storage_user_schema, table_columns
from app.video_shorts.services.facebook_queue import enqueue_facebook_clip, load_facebook_queue_map
from app.video_shorts.services.heygen import HeyGenClient, HeyGenError, get_heygen_background_capabilities
from app.video_shorts.services.instagram_queue import enqueue_instagram_clip, load_instagram_queue_map
from app.video_shorts.services.storage import get_media_storage
from app.video_shorts.services.tiktok_queue import enqueue_tiktok_clip, load_tiktok_queue_map
from app.video_shorts.services.youtube_oauth import has_refresh_token, upload_video_with_refresh_token
from src.trends.facebook_page_tokens import get_facebook_page_data
from src.trends.instagram_tokens import get_instagram_credentials
from src.trends.tiktok_tokens import get_tiktok_data


def _load_heygen_avatar_options() -> tuple[List[Dict[str, Any]], Optional[str], bool]:
    client = HeyGenClient()
    if not client.configured:
        return [], None, False
    try:
        return client.list_avatars(), None, True
    except HeyGenError as exc:
        return [], str(exc), True


def _queue_has_published(records: List[Dict[str, Any]]) -> bool:
    valid_entries = [
        item
        for item in (records or [])
        if str(item.get("status") or "").strip().lower() not in {"canceled", "cancelled"}
    ]
    return any(str(item.get("status") or "").strip().lower() == "published" for item in valid_entries)


def _normalize_ai_video_items(
    items: List[Dict[str, Any]],
    *,
    instagram_queue_map: Dict[Any, List[Dict[str, Any]]],
    facebook_queue_map: Dict[Any, List[Dict[str, Any]]],
    tiktok_queue_map: Dict[Any, List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    for item in items:
        video_id = item.get("video_id") or ""
        queue_key = (video_id, "1")
        ig_records = instagram_queue_map.get(queue_key) or []
        fb_records = facebook_queue_map.get(queue_key) or []
        tt_records = tiktok_queue_map.get(queue_key) or []
        normalized.append(
            {
                **item,
                "clip_url": item.get("public_url") or _short_public_url(item.get("clip_filename") or ""),
                "youtube_status_label": (item.get("youtube_status") or "draft").replace("_", " ").title(),
                "instagram_published": _queue_has_published(ig_records),
                "facebook_published": _queue_has_published(fb_records),
                "tiktok_published": _queue_has_published(tt_records),
                "instagram_queued": bool(ig_records),
                "facebook_queued": bool(fb_records),
                "tiktok_queued": bool(tt_records),
            }
        )
    return normalized


def _parse_local_schedule(field_name: str, user_tz: str) -> Optional[str]:
    raw = str(request.form.get(field_name) or "").strip()
    if not raw:
        return None
    return local_to_utc_rfc3339(raw, user_tz)


@video_shorts_bp.route("/ai-video/characters", methods=["GET", "POST"])
def ai_video_characters_backgrounds():
    current_user = getattr(g, "vs_current_user", None)
    if not current_user:
        return redirect(url_for("video_shorts_bp.login", next=request.url))
    user_id = current_user.get("id")
    brand_id = current_ai_video_brand_id()

    if request.method == "POST":
        action = (request.form.get("action") or "").strip()
        try:
            if action == "save_character":
                avatar_payload = None
                avatar_id = (request.form.get("heygen_avatar_id") or "").strip()
                avatar_name = (request.form.get("heygen_avatar_name") or "").strip()
                if avatar_id:
                    avatar_payload = {
                        "avatar_id": avatar_id,
                        "avatar_name": avatar_name,
                        "gender": (request.form.get("heygen_avatar_gender") or "").strip(),
                        "preview_image_url": (request.form.get("heygen_preview_image_url") or "").strip(),
                        "preview_video_url": (request.form.get("heygen_preview_video_url") or "").strip(),
                    }
                save_character(
                    user_id=user_id,
                    brand_id=brand_id,
                    character_id=(request.form.get("character_id") or "").strip() or None,
                    name=(request.form.get("name") or "").strip(),
                    description=(request.form.get("description") or "").strip(),
                    tone_notes=(request.form.get("tone_notes") or "").strip(),
                    is_default=(request.form.get("is_default") or "").lower() in {"1", "true", "yes", "on"},
                    heygen_avatar=avatar_payload,
                )
                flash("Character saved.", "success")
            elif action == "delete_character":
                delete_character(user_id, brand_id, (request.form.get("character_id") or "").strip())
                flash("Character deleted.", "success")
            elif action == "set_default_character":
                set_default_character(user_id, brand_id, (request.form.get("character_id") or "").strip())
                flash("Default character updated.", "success")
            elif action == "save_background":
                save_background(
                    user_id=user_id,
                    brand_id=brand_id,
                    background_id=(request.form.get("background_id") or "").strip() or None,
                    name=(request.form.get("name") or "").strip(),
                    description=(request.form.get("description") or "").strip(),
                    background_type=(request.form.get("background_type") or "image").strip(),
                    is_default=(request.form.get("is_default") or "").lower() in {"1", "true", "yes", "on"},
                    color_hex=(request.form.get("color_hex") or "").strip(),
                    source_url=(request.form.get("source_url") or "").strip(),
                    heygen_asset_id=(request.form.get("heygen_asset_id") or "").strip(),
                    upload=request.files.get("background_file"),
                )
                flash("Background saved.", "success")
            elif action == "delete_background":
                delete_background(user_id, brand_id, (request.form.get("background_id") or "").strip())
                flash("Background deleted.", "success")
            elif action == "set_default_background":
                set_default_background(user_id, brand_id, (request.form.get("background_id") or "").strip())
                flash("Default background updated.", "success")
        except Exception as exc:
            flash(f"AI Video setup action failed: {exc}", "danger")
        return redirect(url_for("video_shorts_bp.ai_video_characters_backgrounds"))

    characters = list_characters(user_id, brand_id)
    backgrounds = list_backgrounds(user_id, brand_id)
    avatar_options, heygen_error, heygen_configured = _load_heygen_avatar_options()

    edit_character_id = (request.args.get("edit_character") or "").strip()
    edit_background_id = (request.args.get("edit_background") or "").strip()
    editing_character = get_character(user_id, brand_id, edit_character_id) if edit_character_id else None
    editing_background = get_background(user_id, brand_id, edit_background_id) if edit_background_id else None

    return render_template(
        "ai_video_characters.html",
        characters=characters,
        backgrounds=backgrounds,
        editing_character=editing_character,
        editing_background=editing_background,
        heygen_avatar_options=avatar_options,
        heygen_error=heygen_error,
        heygen_configured=heygen_configured,
        heygen_background_capabilities=get_heygen_background_capabilities(),
    )


@video_shorts_bp.route("/ai-video/create", methods=["GET", "POST"])
def ai_video_create():
    current_user = getattr(g, "vs_current_user", None)
    if not current_user:
        return redirect(url_for("video_shorts_bp.login", next=request.url))
    user_id = current_user.get("id")
    brand_id = current_ai_video_brand_id()
    user_tz = current_user.get("time_zone") or DEFAULT_TIME_ZONE

    if request.method == "POST":
        title = (request.form.get("title") or "").strip()
        description = (request.form.get("description") or "").strip()
        upload = request.files.get("video_file")
        if not title:
            flash("Video title is required.", "warning")
            return redirect(url_for("video_shorts_bp.ai_video_create"))
        if not upload or not upload.filename:
            flash("Video file is required.", "warning")
            return redirect(url_for("video_shorts_bp.ai_video_create"))
        ext = Path(upload.filename).suffix.lower()
        if ext not in {".mp4", ".mov", ".mkv"}:
            flash("Unsupported video format. Use MP4, MOV, or MKV.", "warning")
            return redirect(url_for("video_shorts_bp.ai_video_create"))
        safe_stem = secure_filename(Path(upload.filename).stem) or "ai_video"
        clip_filename = f"ai_{safe_stem[:40]}_{uuid.uuid4().hex[:10]}{ext}"

        temp_dir = Path(current_app.instance_path) / "tmp_ai_video_uploads"
        temp_dir.mkdir(parents=True, exist_ok=True)
        temp_clip_path = temp_dir / clip_filename
        upload.save(temp_clip_path)
        duration_seconds = _probe_media_duration_seconds(temp_clip_path) or 0
        if duration_seconds <= 0:
            try:
                temp_clip_path.unlink()
            except Exception:
                pass
            flash("Could not read video duration. Try another file.", "danger")
            return redirect(url_for("video_shorts_bp.ai_video_create"))
        storage = get_media_storage()
        clip_key = _short_storage_key(clip_filename)
        SHORTS_DIR.mkdir(parents=True, exist_ok=True)
        local_clip_path = SHORTS_DIR / clip_filename
        temp_size = temp_clip_path.stat().st_size
        shutil.copyfile(temp_clip_path, local_clip_path)
        storage.put_file(temp_clip_path, clip_key)
        clip_url = storage.public_url(clip_key)
        try:
            _upsert_storage_asset(
                f"short:{clip_filename}",
                str(local_clip_path),
                "short",
                temp_size,
                user_id,
                brand_id=brand_id,
            )
        except Exception:
            current_app.logger.exception(
                "AI video upload could not upsert storage asset file=%s brand_id=%s",
                clip_filename,
                brand_id,
            )
        create_ai_video(
            user_id=user_id,
            brand_id=brand_id,
            title=title,
            description=description,
            clip_filename=clip_filename,
            storage_key=clip_key,
            public_url=clip_url,
            duration_seconds=duration_seconds,
            provider="heygen_upload",
            source_kind="uploaded_video",
            metadata={"original_filename": upload.filename or ""},
        )

        try:
            temp_clip_path.unlink()
        except Exception:
            pass
        flash("AI video uploaded. You can schedule and publish it from this page.", "success")
        return redirect(url_for("video_shorts_bp.ai_video_create"))

    raw_items = list_ai_videos(user_id, brand_id)
    latest_schedule_iso, _ = _find_latest_publish(
        [
            {
                "publish_status": "scheduled" if str(item.get("youtube_status") or "").strip().lower() == "scheduled" else "",
                "publish_at_iso": item.get("youtube_publish_at"),
            }
            for item in raw_items
        ]
    )
    video_ids = [item["video_id"] for item in raw_items if item.get("video_id")]
    instagram_queue_map = load_instagram_queue_map(video_ids)
    facebook_queue_map = load_facebook_queue_map(video_ids)
    tiktok_queue_map = load_tiktok_queue_map(video_ids)
    items = _normalize_ai_video_items(
        raw_items,
        instagram_queue_map=instagram_queue_map,
        facebook_queue_map=facebook_queue_map,
        tiktok_queue_map=tiktok_queue_map,
    )
    instagram_connected = bool(get_instagram_credentials(user_id))
    facebook_info = get_facebook_page_data(user_id) or {}
    facebook_connected = bool(facebook_info.get("page_access_token"))
    tiktok_info = get_tiktok_data(user_id) or {}
    tiktok_connected = bool(tiktok_info.get("access_token")) and not _is_token_expired(tiktok_info.get("expires_at"))
    return render_template(
        "ai_video_create.html",
        items=items,
        instagram_connected=instagram_connected,
        facebook_connected=facebook_connected,
        tiktok_connected=tiktok_connected,
        youtube_connected=has_refresh_token(user_id, brand_id=brand_id),
        user_time_zone=user_tz,
        user_time_zone_label=TIMEZONE_LABELS.get(user_tz, user_tz),
        channel_latest_scheduled_display=_format_publish_display(latest_schedule_iso, user_tz) if latest_schedule_iso else None,
    )


@video_shorts_bp.route("/ai-video/<ai_video_id>/publish", methods=["POST"])
def ai_video_publish(ai_video_id: str):
    current_user = getattr(g, "vs_current_user", None) or {}
    user_id = current_user.get("id")
    if not user_id:
        return jsonify({"success": False, "message": "Login required."}), 403
    brand_id = current_ai_video_brand_id()
    user_tz = current_user.get("time_zone") or DEFAULT_TIME_ZONE
    item = get_ai_video(user_id, brand_id, ai_video_id)
    if not item:
        return jsonify({"success": False, "message": "AI video not found."}), 404

    youtube_enabled = str(request.form.get("youtube_enabled") or "").lower() in {"on", "1", "true", "yes"}
    instagram_reel = str(request.form.get("schedule_instagram_reel") or "").lower() in {"on", "1", "true", "yes"}
    instagram_feed = str(request.form.get("schedule_instagram_feed") or "").lower() in {"on", "1", "true", "yes"}
    facebook_reel = str(request.form.get("schedule_facebook_reel") or "").lower() in {"on", "1", "true", "yes"}
    facebook_feed = str(request.form.get("schedule_facebook_feed") or "").lower() in {"on", "1", "true", "yes"}
    tiktok_enabled = str(request.form.get("schedule_tiktok") or "").lower() in {"on", "1", "true", "yes"}
    force_requeue_instagram = str(request.form.get("force_requeue_instagram") or "").lower() in {"on", "1", "true", "yes"}
    force_requeue_tiktok = str(request.form.get("force_requeue_tiktok") or "").lower() in {"on", "1", "true", "yes"}
    if not any([youtube_enabled, instagram_reel, instagram_feed, facebook_reel, facebook_feed, tiktok_enabled]):
        return jsonify({"success": False, "message": "Select at least one platform."}), 400

    title = (request.form.get("title") or item.get("title") or "").strip()[:100]
    description = (request.form.get("description") or item.get("description") or "").strip()[:5000]
    caption_text = (description or title)[:2200]
    update_ai_video_content(
        user_id=user_id,
        brand_id=brand_id,
        ai_video_id=ai_video_id,
        title=title,
        description=description,
    )

    try:
        yt_publish_at = _parse_local_schedule("publish_at", user_tz)
        ig_publish_at = _parse_local_schedule("instagram_publish_at", user_tz)
        fb_publish_at = _parse_local_schedule("facebook_publish_at", user_tz)
        tt_publish_at = _parse_local_schedule("tiktok_publish_at", user_tz)
    except Exception:
        return jsonify({"success": False, "message": "Invalid schedule time."}), 400

    if youtube_enabled and not has_refresh_token(user_id, brand_id=brand_id):
        return jsonify({"success": False, "message": "YouTube account not connected."}), 403

    media_path = SHORTS_DIR / (item.get("clip_filename") or "")
    if not media_path.exists():
        return jsonify({"success": False, "message": "Uploaded AI video file not found."}), 404

    youtube_video_id = item.get("youtube_video_id") or ""
    if youtube_enabled:
        try:
            resp = upload_video_with_refresh_token(
                video_path=str(media_path),
                title=title,
                description=description,
                publish_at=yt_publish_at,
                privacy_status="private",
                user_id=user_id,
                brand_id=brand_id,
            )
            youtube_video_id = str((resp or {}).get("id") or "").strip()
            update_ai_video_youtube_state(
                user_id=user_id,
                brand_id=brand_id,
                ai_video_id=ai_video_id,
                youtube_status="scheduled" if yt_publish_at else "published",
                youtube_video_id=youtube_video_id,
                youtube_publish_at=yt_publish_at,
                youtube_published_at=None if yt_publish_at else (datetime.utcnow().replace(microsecond=0).isoformat() + "Z"),
            )
        except Exception as exc:
            current_app.logger.exception("AI video YouTube publish failed ai_video_id=%s", ai_video_id)
            return jsonify({"success": False, "message": f"YouTube publish failed: {exc}"}), 500

    broadcast_video_id = item.get("video_id") or ""
    plan_index = "1"
    if instagram_reel or instagram_feed:
        ig_creds = get_instagram_credentials(user_id) or {}
        if not ig_creds:
            return jsonify({"success": False, "message": "Instagram account not connected."}), 403
        publish_at_iso = ig_publish_at if ig_publish_at else yt_publish_at
        for media_type in [m for m in ["reel", "feed"] if (instagram_reel and m == "reel") or (instagram_feed and m == "feed")]:
            enqueue_instagram_clip(
                user_id=user_id,
                video_id=broadcast_video_id,
                plan_index=plan_index,
                clip_filename=item.get("clip_filename") or "",
                caption_text=caption_text,
                publish_at_iso=publish_at_iso,
                instagram_business_account_id=ig_creds.get("instagram_business_account_id"),
                instagram_username=ig_creds.get("instagram_username"),
                youtube_video_id=broadcast_video_id,
                youtube_short_id=youtube_video_id or None,
                plan_title=title,
                media_type=media_type,
                force_requeue=force_requeue_instagram,
            )

    if facebook_reel or facebook_feed:
        fb_info = get_facebook_page_data(user_id) or {}
        if not fb_info.get("page_access_token"):
            return jsonify({"success": False, "message": "Facebook account not connected."}), 403
        publish_at_iso = fb_publish_at if fb_publish_at else yt_publish_at
        for media_type in [m for m in ["reel", "feed"] if (facebook_reel and m == "reel") or (facebook_feed and m == "feed")]:
            enqueue_facebook_clip(
                user_id=user_id,
                video_id=broadcast_video_id,
                plan_index=plan_index,
                clip_filename=item.get("clip_filename") or "",
                caption_text=caption_text,
                publish_at_iso=publish_at_iso,
                page_id=fb_info.get("page_id"),
                page_name=fb_info.get("page_name"),
                plan_title=title,
                media_type=media_type,
            )

    if tiktok_enabled:
        tt_info = get_tiktok_data(user_id) or {}
        if not tt_info.get("access_token") or _is_token_expired(tt_info.get("expires_at")):
            return jsonify({"success": False, "message": "TikTok account not connected."}), 403
        publish_at_iso = tt_publish_at if tt_publish_at else yt_publish_at
        enqueue_tiktok_clip(
            user_id=user_id,
            video_id=broadcast_video_id,
            plan_index=plan_index,
            clip_filename=item.get("clip_filename") or "",
            caption_text=caption_text,
            publish_at_iso=publish_at_iso,
            tiktok_open_id=tt_info.get("open_id"),
            tiktok_username=tt_info.get("username"),
            plan_title=title,
            force_requeue=force_requeue_tiktok,
        )

    return jsonify({"success": True, "message": "Publish workflow created."})


@video_shorts_bp.route("/ai-video/<ai_video_id>/delete", methods=["POST"])
def ai_video_delete(ai_video_id: str):
    current_user = getattr(g, "vs_current_user", None) or {}
    user_id = current_user.get("id")
    if not user_id:
        return jsonify({"success": False, "message": "Login required."}), 403
    brand_id = current_ai_video_brand_id()
    item = get_ai_video(user_id, brand_id, ai_video_id)
    if not item:
        return jsonify({"success": False, "message": "AI video not found."}), 404

    clip_filename = str(item.get("clip_filename") or "").strip()
    storage_key = str(item.get("storage_key") or _short_storage_key(clip_filename)).strip()
    video_id = str(item.get("video_id") or "").strip()

    if storage_key:
        try:
            get_media_storage().delete(storage_key)
        except Exception:
            current_app.logger.exception("Failed to delete AI video from storage key=%s", storage_key)
    if clip_filename:
        try:
            clip_path = SHORTS_DIR / clip_filename
            if clip_path.exists():
                clip_path.unlink()
        except Exception:
            current_app.logger.exception("Failed to delete local AI video clip file=%s", clip_filename)

    conn = get_db()
    try:
        ensure_storage_user_schema(conn)
        asset_columns = table_columns(conn, "shorts_storage_assets")
        if "brand_id" in asset_columns:
            conn.execute(
                "DELETE FROM shorts_storage_assets WHERE file_key = ? AND user_id = ? AND (brand_id = ? OR (? IS NULL AND brand_id IS NULL))",
                [f"short:{clip_filename}", user_id, brand_id, brand_id],
            )
        else:
            conn.execute(
                "DELETE FROM shorts_storage_assets WHERE file_key = ? AND user_id = ?",
                [f"short:{clip_filename}", user_id],
            )
        conn.execute("DELETE FROM shorts_instagram_queue WHERE video_id = ?", [video_id])
        conn.execute("DELETE FROM shorts_facebook_queue WHERE video_id = ?", [video_id])
        conn.execute("DELETE FROM shorts_tiktok_queue WHERE video_id = ?", [video_id])
        conn.commit()
    finally:
        conn.close()

    delete_ai_video(user_id, brand_id, ai_video_id)
    return jsonify({"success": True, "message": "AI video deleted."})
