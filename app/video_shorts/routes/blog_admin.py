from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import quote

from flask import abort, current_app, flash, jsonify, redirect, render_template, request, url_for
from werkzeug.utils import secure_filename

from app.video_shorts import video_shorts_bp
from app.video_shorts.config import CLOUDFRONT_DOMAIN, S3_BUCKET_NAME
from app.video_shorts.routes.auth import require_admin
from app.video_shorts.services.blog_articles import (
    create_blog_article,
    ensure_default_blog_articles_seeded,
    get_blog_article_by_id,
    list_admin_blog_articles,
    update_blog_article,
    update_blog_article_status,
)
from app.video_shorts.services.storage import get_media_storage

_ALLOWED_COVER_MIME_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
_MAX_COVER_IMAGE_BYTES = 5 * 1024 * 1024


def _form_article_context(article: dict | None = None) -> dict:
    article = article or {}
    published_at = article.get("published_at")
    if isinstance(published_at, str):
        published_at_value = published_at
    else:
        published_at_value = published_at.strftime("%Y-%m-%dT%H:%M") if published_at else ""
    return {
        "id": article.get("id"),
        "title": article.get("title") or "",
        "slug": article.get("slug") or "",
        "summary": article.get("summary") or "",
        "content": article.get("content") or "",
        "cover_image_url": article.get("cover_image_url") or "",
        "meta_title": article.get("meta_title") or "",
        "meta_description": article.get("meta_description") or "",
        "author_name": article.get("author_name") or "MintiStudio Team",
        "reading_time": article.get("reading_time") or "",
        "status": article.get("status") or "draft",
        "published_at": published_at_value,
    }


def _cover_upload_slug(raw_slug: str) -> str:
    cleaned = secure_filename((raw_slug or "").strip()).strip().lower()
    if cleaned:
        return cleaned
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")


def _cloudfront_cover_url(key: str) -> str:
    domain = str(CLOUDFRONT_DOMAIN or "").strip().strip("/")
    if not domain:
        raise RuntimeError("CLOUDFRONT_DOMAIN is not configured.")
    return f"https://{domain}/{quote(str(key).lstrip('/'), safe='/')}"


@video_shorts_bp.route("/admin/blog/upload-cover", methods=["POST"])
@require_admin
def admin_blog_upload_cover():
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        return jsonify({"error": "Please choose an image file to upload."}), 400
    content_type = str(upload.mimetype or "").strip().lower()
    if content_type not in _ALLOWED_COVER_MIME_TYPES:
        return jsonify({"error": "Only JPEG, PNG, and WebP images are allowed."}), 400

    filename = secure_filename(upload.filename)
    if not filename:
        return jsonify({"error": "Invalid file name."}), 400

    data = upload.read()
    if not data:
        return jsonify({"error": "Uploaded file is empty."}), 400
    if len(data) > _MAX_COVER_IMAGE_BYTES:
        return jsonify({"error": "Image must be 5 MB or smaller."}), 400

    if not S3_BUCKET_NAME:
        current_app.logger.error("Blog cover upload failed: S3_BUCKET_NAME is not configured.")
        return jsonify({"error": "S3 storage is not configured."}), 500

    slug = _cover_upload_slug(request.form.get("slug") or "")
    key = f"blog/covers/{slug}/{filename}"
    try:
        storage = get_media_storage("s3")
        storage.put_bytes(data, key, content_type=content_type)
        return jsonify({"url": _cloudfront_cover_url(key)})
    except Exception as exc:
        current_app.logger.exception("Blog cover upload failed for key=%s", key)
        return jsonify({"error": f"Upload failed: {exc}"}), 500


@video_shorts_bp.route("/admin/blog", methods=["GET"])
@require_admin
def admin_blog_articles():
    seed_result = ensure_default_blog_articles_seeded()
    return render_template(
        "shorts_admin_blog_list.html",
        admin_title="Blog",
        blog_articles=list_admin_blog_articles(),
        blog_seed_table_missing=bool(seed_result.get("table_missing")),
    )


@video_shorts_bp.route("/admin/blog/new", methods=["GET", "POST"])
@require_admin
def admin_blog_article_create():
    ensure_default_blog_articles_seeded()
    if request.method == "POST":
        try:
            created = create_blog_article(request.form)
        except Exception as exc:
            flash(f"Failed to create blog article: {exc}", "danger")
            return render_template(
                "shorts_admin_blog_form.html",
                admin_title="New blog article",
                form_article=_form_article_context(request.form),
                form_mode="create",
            ), 400
        flash("Blog article created.", "success")
        return redirect(url_for("video_shorts_bp.admin_blog_article_edit", article_id=created["id"]))
    return render_template(
        "shorts_admin_blog_form.html",
        admin_title="New blog article",
        form_article=_form_article_context(),
        form_mode="create",
    )


@video_shorts_bp.route("/admin/blog/<int:article_id>/edit", methods=["GET", "POST"])
@require_admin
def admin_blog_article_edit(article_id: int):
    ensure_default_blog_articles_seeded()
    article = get_blog_article_by_id(article_id)
    if not article:
        abort(404)
    if request.method == "POST":
        try:
            updated = update_blog_article(article_id, request.form)
        except Exception as exc:
            flash(f"Failed to update blog article: {exc}", "danger")
            return render_template(
                "shorts_admin_blog_form.html",
                admin_title="Edit blog article",
                form_article=_form_article_context({**article, **request.form}),
                form_mode="edit",
            ), 400
        if not updated:
            abort(404)
        flash("Blog article updated.", "success")
        return redirect(url_for("video_shorts_bp.admin_blog_article_edit", article_id=article_id))
    return render_template(
        "shorts_admin_blog_form.html",
        admin_title="Edit blog article",
        form_article=_form_article_context(article),
        form_mode="edit",
    )


@video_shorts_bp.route("/admin/blog/<int:article_id>/status", methods=["POST"])
@require_admin
def admin_blog_article_status(article_id: int):
    status = request.form.get("status") or "draft"
    try:
        updated = update_blog_article_status(article_id, status)
    except Exception as exc:
        flash(f"Failed to update status: {exc}", "danger")
        return redirect(url_for("video_shorts_bp.admin_blog_articles"))
    if not updated:
        abort(404)
    flash("Blog article status updated.", "success")
    return redirect(url_for("video_shorts_bp.admin_blog_articles"))
