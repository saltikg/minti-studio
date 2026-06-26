import json
import logging
from pathlib import Path
from uuid import uuid4

from flask import flash, jsonify, redirect, render_template, request, url_for, g
from werkzeug.utils import secure_filename

from app.video_shorts import video_shorts_bp
from app.video_shorts.config import SHORTS_DIR, STATIC_IMAGE_MAX_BYTES, STATIC_USER_IMAGES_DIR
from app.video_shorts.services.brands import current_brand_id, ensure_brand_schema
from app.video_shorts.services.db import (
    ensure_background_preferences_schema,
    ensure_categories_schema,
    ensure_prompt_settings_schema,
    ensure_static_image_categories_schema,
    ensure_static_images_schema,
    get_db,
)
from app.video_shorts.services.background_preferences import (
    load_background_preference,
    save_background_preference,
)
from app.video_shorts.services.comment_moderation import DEFAULT_COMMENT_MODERATION_PROMPT, _prompt_key
from app.video_shorts.services.storage import get_media_storage
from app.video_shorts.services.system_backgrounds import (
    is_system_background_key,
    list_system_background_paths,
    make_system_background_key,
    resolve_system_background_path,
    system_background_static_filename,
)


logger = logging.getLogger(__name__)


def _brand_prompt_key(user_id: str | None, brand_id: str | None) -> str:
    base_key = _prompt_key(user_id)
    return f"{base_key}:brand:{brand_id}" if brand_id else base_key


def _user_image_storage_key(user_id: str, filename: str) -> str:
    return f"user_images/{user_id}/{Path(filename).name}"


def _user_image_public_url(user_id: str, filename: str) -> str:
    key = _user_image_storage_key(user_id, filename)
    storage = get_media_storage()
    local_path = STATIC_USER_IMAGES_DIR / user_id / Path(filename).name
    resolved = storage.resolve_local_or_s3(key, fallback_local_paths=[local_path])
    return resolved.public_url or get_media_storage("local").public_url(key)


def _delete_user_image_asset(user_id: str, filename: str) -> None:
    key = _user_image_storage_key(user_id, filename)
    storage = get_media_storage()
    resolved = storage.resolve_local_or_s3(
        key,
        fallback_local_paths=[STATIC_USER_IMAGES_DIR / user_id / Path(filename).name],
    )
    target_storage = get_media_storage("local") if resolved.backend == "local" else storage
    target_storage.delete(key)


def _resolve_selected_background_key(user_id: str, brand_id: str | None, background_key: str | None) -> str | None:
    candidate_key = str(background_key or "").strip()
    if not candidate_key:
        return None
    if is_system_background_key(candidate_key):
        return candidate_key if resolve_system_background_path(candidate_key) else None
    if candidate_key.startswith("userbg:"):
        image_id = candidate_key.split(":", 1)[1].strip()
        if not image_id:
            return None
        conn = get_db()
        try:
            ensure_static_images_schema(conn)
            row = conn.execute(
                """
                SELECT id
                FROM shorts_static_images
                WHERE id = ? AND user_id = ? AND brand_id = ? AND COALESCE(is_active, true) = true
                """,
                [image_id, user_id, brand_id],
            ).fetchone()
            return candidate_key if row else None
        finally:
            conn.close()
    return None


def _update_plan_category_label(old_name: str, new_name: str | None, owner_id: str) -> None:
    if not old_name or not owner_id:
        return
    conn = None
    try:
        conn = get_db()
        rows = conn.execute(
            "SELECT video_id FROM youtube_videos WHERE owner_user_id = ?",
            [owner_id],
        ).fetchall()
    except Exception:
        rows = []
    finally:
        if conn:
            conn.close()
    video_ids = {row[0] for row in rows if row and row[0]}
    for video_id in video_ids:
        plan_path = SHORTS_DIR / f"{video_id}_plan.json"
        if not plan_path.exists():
            continue
        try:
            plan_data = json.loads(plan_path.read_text())
        except Exception:
            continue
        plan_entries = plan_data.get("plan") or plan_data.get("clips") or []
        changed = False
        for entry in plan_entries:
            if entry.get("category") != old_name:
                continue
            if new_name:
                entry["category"] = new_name
            else:
                entry.pop("category", None)
            changed = True
        if not changed:
            continue
        try:
            plan_path.write_text(json.dumps({"plan": plan_entries}, ensure_ascii=False, indent=2))
        except Exception:
            continue


@video_shorts_bp.route("/settings", methods=["GET"])
def settings_page():
    current_user = getattr(g, "vs_current_user", None)
    if not current_user:
        return redirect(url_for("video_shorts_bp.login", next=request.url))
    return redirect(url_for("video_shorts_bp.static_images_page"))


@video_shorts_bp.route("/settings/categories", methods=["GET", "POST"])
def categories_page():
    current_user = getattr(g, "vs_current_user", None)
    brand_id = current_brand_id()
    if not current_user:
        return redirect(url_for("video_shorts_bp.login", next=request.url))

    conn = get_db()
    ensure_brand_schema(conn)
    ensure_categories_schema(conn, current_user.get("id"))

    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        if not name:
            conn.close()
            flash("Kategori adı gerekli.", "warning")
            return redirect(url_for("video_shorts_bp.categories_page"))
        if len(name) > 60:
            conn.close()
            flash("Kategori adı çok uzun (max 60).", "warning")
            return redirect(url_for("video_shorts_bp.categories_page"))
        existing = conn.execute(
            "SELECT id FROM shorts_categories WHERE user_id = ? AND brand_id = ? AND lower(name) = lower(?)",
            [current_user.get("id"), brand_id, name],
        ).fetchone()
        if existing:
            conn.close()
            flash("Bu kategori zaten var.", "warning")
            return redirect(url_for("video_shorts_bp.categories_page"))
        conn.execute(
            "INSERT INTO shorts_categories (user_id, brand_id, name) VALUES (?, ?, ?)",
            [current_user.get("id"), brand_id, name],
        )
        conn.commit()
        conn.close()
        flash("Kategori eklendi.", "success")
        return redirect(url_for("video_shorts_bp.categories_page"))

    rows = conn.execute(
        "SELECT id, name, created_at FROM shorts_categories WHERE user_id = ? AND brand_id = ? ORDER BY lower(name)",
        [current_user.get("id"), brand_id],
    ).fetchall()
    conn.close()
    categories = [
        {"id": row[0], "name": row[1], "created_at": row[2]} for row in rows
    ]
    return render_template("shorts_categories.html", categories=categories)


@video_shorts_bp.route("/settings/categories/<category_id>/update", methods=["POST"])
def update_category(category_id):
    current_user = getattr(g, "vs_current_user", None)
    brand_id = current_brand_id()
    if not current_user:
        return redirect(url_for("video_shorts_bp.login", next=request.url))

    name = (request.form.get("name") or "").strip()
    if not name:
        flash("Kategori adı gerekli.", "warning")
        return redirect(url_for("video_shorts_bp.categories_page"))
    if len(name) > 60:
        flash("Kategori adı çok uzun (max 60).", "warning")
        return redirect(url_for("video_shorts_bp.categories_page"))

    conn = get_db()
    ensure_brand_schema(conn)
    ensure_categories_schema(conn, current_user.get("id"))
    row = conn.execute(
        "SELECT name FROM shorts_categories WHERE id = ? AND user_id = ? AND brand_id = ?",
        [category_id, current_user.get("id"), brand_id],
    ).fetchone()
    if not row:
        conn.close()
        flash("Kategori bulunamadı.", "warning")
        return redirect(url_for("video_shorts_bp.categories_page"))
    old_name = row[0]
    existing = conn.execute(
        "SELECT id FROM shorts_categories WHERE user_id = ? AND brand_id = ? AND lower(name) = lower(?) AND id <> ?",
        [current_user.get("id"), brand_id, name, category_id],
    ).fetchone()
    if existing:
        conn.close()
        flash("Bu kategori zaten var.", "warning")
        return redirect(url_for("video_shorts_bp.categories_page"))
    conn.execute(
        "UPDATE shorts_categories SET name = ?, updated_at = now() WHERE id = ? AND user_id = ? AND brand_id = ?",
        [name, category_id, current_user.get("id"), brand_id],
    )
    conn.commit()
    conn.close()

    if name != old_name:
        _update_plan_category_label(old_name, name, current_user.get("id"))

    flash("Kategori güncellendi.", "success")
    return redirect(url_for("video_shorts_bp.categories_page"))


@video_shorts_bp.route("/settings/categories/<category_id>/delete", methods=["POST"])
def delete_category(category_id):
    current_user = getattr(g, "vs_current_user", None)
    brand_id = current_brand_id()
    if not current_user:
        return redirect(url_for("video_shorts_bp.login", next=request.url))

    conn = get_db()
    ensure_brand_schema(conn)
    ensure_categories_schema(conn, current_user.get("id"))
    row = conn.execute(
        "SELECT name FROM shorts_categories WHERE id = ? AND user_id = ? AND brand_id = ?",
        [category_id, current_user.get("id"), brand_id],
    ).fetchone()
    if not row:
        conn.close()
        flash("Kategori bulunamadı.", "warning")
        return redirect(url_for("video_shorts_bp.categories_page"))
    name = row[0]
    conn.execute(
        "DELETE FROM shorts_categories WHERE id = ? AND user_id = ? AND brand_id = ?",
        [category_id, current_user.get("id"), brand_id],
    )
    conn.commit()
    conn.close()

    _update_plan_category_label(name, None, current_user.get("id"))

    flash("Kategori silindi.", "success")
    return redirect(url_for("video_shorts_bp.categories_page"))


@video_shorts_bp.route("/settings/prompts", methods=["GET", "POST"])
def prompts_page():
    current_user = getattr(g, "vs_current_user", None)
    brand_id = current_brand_id()
    if not current_user:
        return redirect(url_for("video_shorts_bp.login", next=request.url))

    conn = get_db()
    ensure_brand_schema(conn)
    ensure_prompt_settings_schema(conn)
    key = _brand_prompt_key(current_user.get("id"), brand_id)
    row = conn.execute(
        "SELECT value FROM shorts_prompt_settings WHERE key = ?",
        [key],
    ).fetchone()
    current_value = row[0] if row and row[0] else ""
    if not current_value:
        current_value = DEFAULT_COMMENT_MODERATION_PROMPT
        conn.execute(
            """
            INSERT INTO shorts_prompt_settings (key, value, updated_by, updated_at)
            VALUES (?, ?, ?, now())
            ON CONFLICT (key) DO UPDATE SET
                value = excluded.value,
                updated_by = excluded.updated_by,
                updated_at = excluded.updated_at
            """,
            [key, current_value, current_user.get("id")],
        )
        conn.commit()

    if request.method == "POST":
        value = (request.form.get("comment_moderation_prompt") or "").strip()
        if not value:
            conn.execute("DELETE FROM shorts_prompt_settings WHERE key = ?", [key])
            conn.commit()
            conn.close()
            flash("Prompt varsayilana donduruldu.", "success")
            return redirect(url_for("video_shorts_bp.prompts_page"))
        conn.execute(
            """
            INSERT INTO shorts_prompt_settings (key, value, updated_by, updated_at)
            VALUES (?, ?, ?, now())
            ON CONFLICT (key) DO UPDATE SET
                value = excluded.value,
                updated_by = excluded.updated_by,
                updated_at = excluded.updated_at
            """,
            [key, value, current_user.get("id")],
        )
        conn.commit()
        conn.close()
        flash("Prompt guncellendi.", "success")
        return redirect(url_for("video_shorts_bp.prompts_page"))

    conn.close()
    return render_template(
        "shorts_prompts.html",
        comment_moderation_prompt=current_value,
    )


@video_shorts_bp.route("/settings/static-images", methods=["GET", "POST"])
def static_images_page():
    current_user = getattr(g, "vs_current_user", None)
    brand_id = current_brand_id()
    if not current_user:
        return redirect(url_for("video_shorts_bp.login", next=request.url))

    conn = get_db()
    ensure_brand_schema(conn)
    ensure_static_images_schema(conn)
    ensure_background_preferences_schema(conn)
    ensure_static_image_categories_schema(conn, current_user.get("id"), brand_id=brand_id)

    if request.method == "POST":
        wants_json = request.headers.get("X-Requested-With") == "XMLHttpRequest"
        action = (request.form.get("action") or "upload_image").strip().lower()
        logger.info(
            "static_images_page POST received user_id=%s action=%s wants_json=%s media_backend=%s",
            current_user.get("id"),
            action,
            wants_json,
            getattr(get_media_storage(), "backend_name", "unknown"),
        )

        if action == "add_category":
            name = (request.form.get("name") or "").strip()
            if not name:
                conn.close()
                message = "Kategori adi gerekli."
                if wants_json:
                    return jsonify(success=False, message=message), 400
                flash(message, "warning")
                return redirect(url_for("video_shorts_bp.static_images_page"))
            if len(name) > 60:
                conn.close()
                message = "Kategori adi cok uzun (max 60)."
                if wants_json:
                    return jsonify(success=False, message=message), 400
                flash(message, "warning")
                return redirect(url_for("video_shorts_bp.static_images_page"))

            existing = conn.execute(
                """
                SELECT id, name
                FROM shorts_static_image_categories
                WHERE user_id = ? AND brand_id = ? AND COALESCE(is_active, true) = true AND lower(name) = lower(?)
                """,
                [current_user.get("id"), brand_id, name],
            ).fetchone()
            if existing:
                conn.close()
                message = "Bu kategori zaten var."
                if wants_json:
                    return jsonify(
                        success=True,
                        message=message,
                        category={"id": str(existing[0]), "name": existing[1]},
                    ), 200
                flash(message, "warning")
                return redirect(url_for("video_shorts_bp.static_images_page"))

            category_id = str(uuid4())
            row = conn.execute(
                """
                INSERT INTO shorts_static_image_categories (id, user_id, brand_id, name, is_active, updated_at)
                VALUES (?, ?, ?, ?, true, now())
                RETURNING id, name
                """,
                [category_id, current_user.get("id"), brand_id, name],
            ).fetchone()
            conn.commit()
            conn.close()
            if wants_json:
                return jsonify(success=True, category={"id": str(row[0]), "name": row[1]}), 200
            flash("Kategori eklendi.", "success")
            return redirect(url_for("video_shorts_bp.static_images_page"))

        upload = request.files.get("image")
        logger.info(
            "static image upload request user_id=%s file_field_present=%s form_filename=%s",
            current_user.get("id"),
            upload is not None,
            getattr(upload, "filename", "") if upload else "",
        )
        label = (request.form.get("label") or "").strip()
        category_id = (request.form.get("category_id") or "").strip()
        use_as_background = (request.form.get("use_as_background") or "").strip().lower() in {
            "1",
            "true",
            "on",
            "yes",
        }
        if not category_id:
            logger.warning(
                "static image upload validation failed user_id=%s reason=missing_category",
                current_user.get("id"),
            )
            conn.close()
            message = "Lutfen kategori secin."
            if wants_json:
                return jsonify(success=False, message=message), 400
            flash(message, "warning")
            return redirect(url_for("video_shorts_bp.static_images_page"))
        category = conn.execute(
            """
            SELECT id, name
            FROM shorts_static_image_categories
            WHERE id = ? AND user_id = ? AND COALESCE(is_active, true) = true
              AND brand_id = ?
            """,
            [category_id, current_user.get("id"), brand_id],
        ).fetchone()
        if not category:
            logger.warning(
                "static image upload validation failed user_id=%s reason=category_not_found category_id=%s",
                current_user.get("id"),
                category_id,
            )
            conn.close()
            message = "Secilen kategori bulunamadi."
            if wants_json:
                return jsonify(success=False, message=message), 400
            flash(message, "warning")
            return redirect(url_for("video_shorts_bp.static_images_page"))
        if not upload or not upload.filename:
            logger.warning(
                "static image upload validation failed user_id=%s reason=missing_file",
                current_user.get("id"),
            )
            conn.close()
            message = "Bir gorsel secin."
            if wants_json:
                return jsonify(success=False, message=message), 400
            flash(message, "warning")
            return redirect(url_for("video_shorts_bp.static_images_page"))

        filename = secure_filename(upload.filename)
        ext = Path(filename).suffix.lower()
        allowed_exts = {".png", ".jpg", ".jpeg", ".webp"}
        if ext not in allowed_exts:
            logger.warning(
                "static image upload validation failed user_id=%s reason=unsupported_ext filename=%s ext=%s",
                current_user.get("id"),
                filename,
                ext,
            )
            conn.close()
            message = "Sadece PNG, JPG veya WEBP yukleyin."
            if wants_json:
                return jsonify(success=False, message=message), 400
            flash(message, "warning")
            return redirect(url_for("video_shorts_bp.static_images_page"))

        try:
            upload.stream.seek(0, 2)
            size_bytes = upload.stream.tell()
            upload.stream.seek(0)
        except Exception:
            size_bytes = None
        if size_bytes is not None and size_bytes > STATIC_IMAGE_MAX_BYTES:
            logger.warning(
                "static image upload validation failed user_id=%s reason=file_too_large filename=%s size_bytes=%s limit_bytes=%s",
                current_user.get("id"),
                filename,
                size_bytes,
                STATIC_IMAGE_MAX_BYTES,
            )
            conn.close()
            message = "Dosya boyutu 5MB ustunde olamaz."
            if wants_json:
                return jsonify(success=False, message=message), 400
            flash(message, "warning")
            return redirect(url_for("video_shorts_bp.static_images_page"))

        stored_name = f"{uuid4().hex}{ext}"
        key = _user_image_storage_key(current_user["id"], stored_name)
        storage = get_media_storage()
        logger.info(
            "static image upload starting user_id=%s original_filename=%s stored_name=%s key=%s media_backend=%s storage_backend=%s size_bytes=%s",
            current_user.get("id"),
            filename,
            stored_name,
            key,
            getattr(storage, "backend_name", "unknown"),
            getattr(storage, "backend_name", "unknown"),
            size_bytes,
        )
        try:
            data = upload.read()
            if not data:
                raise ValueError("empty upload")
            logger.info(
                "static image upload put_bytes begin user_id=%s key=%s content_type=%s payload_size=%s",
                current_user.get("id"),
                key,
                upload.mimetype or "",
                len(data),
            )
            storage.put_bytes(
                data,
                key,
                content_type=upload.mimetype or None,
            )
            logger.info(
                "static image upload put_bytes success user_id=%s key=%s",
                current_user.get("id"),
                key,
            )
        except Exception:
            logger.exception(
                "static image upload storage failure user_id=%s original_filename=%s stored_name=%s key=%s backend=%s",
                current_user.get("id"),
                filename,
                stored_name,
                key,
                getattr(storage, "backend_name", "unknown"),
            )
            conn.close()
            message = "Gorsel kaydedilemedi."
            if wants_json:
                return jsonify(success=False, message=message), 500
            flash(message, "danger")
            return redirect(url_for("video_shorts_bp.static_images_page"))

        if not label:
            label = Path(filename).stem[:40] if filename else "Image"

        try:
            image_id = str(uuid4())
            logger.info(
                "static image upload db insert begin user_id=%s image_id=%s stored_name=%s category_id=%s use_as_background=%s",
                current_user.get("id"),
                image_id,
                stored_name,
                category_id,
                use_as_background,
            )
            conn.execute(
                """
                INSERT INTO shorts_static_images (id, user_id, brand_id, category_id, use_as_background, label, filename, file_size, file_ext, is_active, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, true, now())
                """,
                [
                    image_id,
                    current_user["id"],
                    brand_id,
                    category_id,
                    use_as_background,
                    label,
                    stored_name,
                    size_bytes,
                    ext.lstrip("."),
                ],
            )
            conn.commit()
            logger.info(
                "static image upload db insert success user_id=%s stored_name=%s",
                current_user.get("id"),
                stored_name,
            )
        except Exception:
            logger.exception(
                "static image upload db failure user_id=%s stored_name=%s key=%s",
                current_user.get("id"),
                stored_name,
                key,
            )
            conn.close()
            message = "Yukleme başarısız."
            if wants_json:
                return jsonify(success=False, message=message), 500
            flash(message, "danger")
            return redirect(url_for("video_shorts_bp.static_images_page"))
        conn.close()
        if wants_json:
            return jsonify(success=True), 200
        flash("Gorsel eklendi.", "success")
        return redirect(url_for("video_shorts_bp.static_images_page"))

    rows = conn.execute(
        """
        SELECT i.id, i.label, i.filename, i.created_at, i.file_size, c.id, c.name, COALESCE(i.use_as_background, false)
        FROM shorts_static_images i
        LEFT JOIN shorts_static_image_categories c
          ON c.id = i.category_id AND c.user_id = i.user_id
        WHERE i.user_id = ? AND i.brand_id = ? AND COALESCE(i.is_active, true) = true
        ORDER BY i.created_at DESC
        """,
        [current_user.get("id"), brand_id],
    ).fetchall()
    category_rows = conn.execute(
        """
        SELECT id, name
        FROM shorts_static_image_categories
        WHERE user_id = ? AND brand_id = ? AND COALESCE(is_active, true) = true
        ORDER BY lower(name)
        """,
        [current_user.get("id"), brand_id],
    ).fetchall()
    conn.close()
    images = []
    for row in rows:
        images.append(
            {
                "id": row[0],
                "background_key": f"userbg:{row[0]}",
                "label": row[1],
                "filename": row[2],
                "created_at": row[3],
                "file_size": row[4],
                "category_id": str(row[5]) if row[5] else "",
                "category_name": row[6] or "",
                "use_as_background": bool(row[7]) if len(row) > 7 else False,
                "image_url": _user_image_public_url(current_user.get("id"), row[2]),
            }
        )
    selected_background_key = load_background_preference(current_user.get("id"), brand_id)
    system_images = []
    for system_path in list_system_background_paths():
        system_images.append(
            {
                "id": system_path.name,
                "background_key": make_system_background_key(system_path.name),
                "label": system_path.stem.replace("_", " "),
                "filename": system_path.name,
                "image_url": url_for("video_shorts_bp.static", filename=system_background_static_filename(system_path)),
            }
        )
    categories = [{"id": str(row[0]), "name": row[1]} for row in category_rows]
    return render_template(
        "shorts_static_images.html",
        images=images,
        user_images=images,
        system_images=system_images,
        categories=categories,
        selected_background_key=selected_background_key,
    )


@video_shorts_bp.route("/settings/podcast-audios")
def podcast_audios_page():
    current_user = getattr(g, "vs_current_user", None)
    if not current_user:
        return redirect(url_for("video_shorts_bp.login", next=request.url))
    return render_template("shorts_podcast_audios.html")


@video_shorts_bp.route("/settings/static-images/<image_id>/category", methods=["POST"])
def update_static_image_category(image_id):
    current_user = getattr(g, "vs_current_user", None)
    brand_id = current_brand_id()
    if not current_user:
        return jsonify(success=False, message="Unauthorized."), 401

    conn = get_db()
    ensure_brand_schema(conn)
    ensure_static_images_schema(conn)
    ensure_static_image_categories_schema(conn, current_user.get("id"), brand_id=brand_id)

    image_row = conn.execute(
        """
        SELECT id
        FROM shorts_static_images
        WHERE id = ? AND user_id = ? AND brand_id = ? AND COALESCE(is_active, true) = true
        """,
        [image_id, current_user.get("id"), brand_id],
    ).fetchone()
    if not image_row:
        conn.close()
        return jsonify(success=False, message="Gorsel bulunamadi."), 404

    category_id = (request.form.get("category_id") or "").strip()
    if not category_id:
        conn.close()
        return jsonify(success=False, message="Lutfen kategori secin."), 400

    category_row = conn.execute(
        """
        SELECT id, name
        FROM shorts_static_image_categories
        WHERE id = ? AND user_id = ? AND brand_id = ? AND COALESCE(is_active, true) = true
        """,
        [category_id, current_user.get("id"), brand_id],
    ).fetchone()
    if not category_row:
        conn.close()
        return jsonify(success=False, message="Kategori bulunamadi."), 400

    conn.execute(
        """
        UPDATE shorts_static_images
        SET category_id = ?, updated_at = now()
        WHERE id = ? AND user_id = ? AND brand_id = ?
        """,
        [category_id, image_id, current_user.get("id"), brand_id],
    )
    conn.commit()
    conn.close()
    return jsonify(
        success=True,
        category={"id": str(category_row[0]), "name": category_row[1]},
    ), 200


@video_shorts_bp.route("/settings/static-images/<image_id>/background", methods=["POST"])
def update_static_image_background(image_id):
    current_user = getattr(g, "vs_current_user", None)
    brand_id = current_brand_id()
    if not current_user:
        return jsonify(success=False, message="Unauthorized."), 401

    conn = get_db()
    ensure_brand_schema(conn)
    ensure_static_images_schema(conn)

    image_row = conn.execute(
        """
        SELECT id
        FROM shorts_static_images
        WHERE id = ? AND user_id = ? AND brand_id = ? AND COALESCE(is_active, true) = true
        """,
        [image_id, current_user.get("id"), brand_id],
    ).fetchone()
    if not image_row:
        conn.close()
        return jsonify(success=False, message="Gorsel bulunamadi."), 404

    use_as_background = (request.form.get("use_as_background") or "").strip().lower() in {
        "1",
        "true",
        "on",
        "yes",
    }
    conn.execute(
        """
        UPDATE shorts_static_images
        SET use_as_background = ?, updated_at = now()
        WHERE id = ? AND user_id = ? AND brand_id = ?
        """,
        [use_as_background, image_id, current_user.get("id"), brand_id],
    )
    conn.commit()
    conn.close()
    return jsonify(success=True, use_as_background=use_as_background), 200


@video_shorts_bp.route("/settings/static-images/background-selection", methods=["POST"])
def update_selected_background():
    current_user = getattr(g, "vs_current_user", None)
    brand_id = current_brand_id()
    if not current_user:
        return jsonify(success=False, message="Unauthorized."), 401

    requested_key = (request.form.get("background_key") or "").strip()
    if requested_key:
        resolved_key = _resolve_selected_background_key(current_user.get("id"), brand_id, requested_key)
        if not resolved_key:
            return jsonify(success=False, message="Background bulunamadi."), 404
        save_background_preference(current_user.get("id"), brand_id, resolved_key)
        return jsonify(success=True, background_key=resolved_key), 200

    save_background_preference(current_user.get("id"), brand_id, None)
    return jsonify(success=True, background_key=""), 200


@video_shorts_bp.route("/settings/static-images/<image_id>/delete", methods=["POST"])
def delete_static_image(image_id):
    current_user = getattr(g, "vs_current_user", None)
    brand_id = current_brand_id()
    if not current_user:
        return redirect(url_for("video_shorts_bp.login", next=request.url))
    if is_system_background_key(image_id):
        flash("Sistem gorseli silinemez.", "warning")
        return redirect(url_for("video_shorts_bp.static_images_page"))

    conn = get_db()
    ensure_brand_schema(conn)
    ensure_static_images_schema(conn)
    row = conn.execute(
        """
        SELECT filename FROM shorts_static_images
        WHERE id = ? AND user_id = ? AND brand_id = ?
        """,
        [image_id, current_user.get("id"), brand_id],
    ).fetchone()
    if not row:
        conn.close()
        flash("Gorsel bulunamadi.", "warning")
        return redirect(url_for("video_shorts_bp.static_images_page"))
    conn.execute(
        """
        UPDATE shorts_static_images
        SET is_active = false, updated_at = now()
        WHERE id = ? AND user_id = ? AND brand_id = ?
        """,
        [image_id, current_user.get("id"), brand_id],
    )
    conn.commit()
    conn.close()
    selected_background_key = load_background_preference(current_user.get("id"), brand_id)
    if selected_background_key == f"userbg:{image_id}":
        save_background_preference(current_user.get("id"), brand_id, None)
    try:
        _delete_user_image_asset(current_user.get("id"), str(row[0] or ""))
    except Exception:
        pass
    flash("Gorsel kaldirildi.", "success")
    return redirect(url_for("video_shorts_bp.static_images_page"))
