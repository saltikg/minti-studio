from __future__ import annotations

from flask import abort, flash, redirect, render_template, request, url_for

from app.video_shorts import video_shorts_bp
from app.video_shorts.routes.auth import require_admin
from app.video_shorts.services.blog_articles import (
    create_blog_article,
    ensure_default_blog_articles_seeded,
    get_blog_article_by_id,
    list_admin_blog_articles,
    update_blog_article,
    update_blog_article_status,
)


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
