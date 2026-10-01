# app/__init__.py
import os
import json  # <- eklendi
import logging
from html import escape
from datetime import datetime, timedelta, timezone


from flask import Flask, Response, jsonify, redirect, g, render_template, request, url_for
from dotenv import load_dotenv
from werkzeug.exceptions import HTTPException, InternalServerError
from app.video_shorts.services.temp_cleanup import cleanup_video_shorts_temp_dir_on_startup
from app.video_shorts.services.error_capture import capture_server_error, wants_json_error_response

# .env yükle
load_dotenv()

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


# ENV
BASE_URL = os.getenv("BASE_URL", "https://mintistudio.com").rstrip("/")
DB_PATH  = os.getenv("DB_PATH", "/home/ubuntu/blog-factory/warehouse/blog_factory.duckdb")
ENABLE_LEGACY_BLOG = (os.getenv("ENABLE_LEGACY_BLOG", "0") or "0").strip().lower() in {"1", "true", "yes", "on"}


def from_json_safe(val):
    """list/dict ise direk döner; string ise JSON parse dener; hata olursa None."""
    if val is None:
        return None
    if isinstance(val, (list, dict)):
        return val
    try:
        return json.loads(val)
    except Exception:
        return None


def create_app():
    app = Flask(__name__)
    app.url_map.strict_slashes = False

    # Config
    app.config["DB_PATH"]  = DB_PATH
    app.config["BASE_URL"] = BASE_URL
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=60)

    app.secret_key = os.getenv("SECRET_KEY")

    logger.info(
        "Media storage backend=%s region=%s bucket=%s",
        (os.getenv("MEDIA_BACKEND", "local") or "local").strip().lower(),
        (os.getenv("AWS_REGION", "us-east-1") or "us-east-1").strip(),
        (os.getenv("S3_BUCKET_NAME", "") or "").strip() or "<unset>",
    )
    cleanup_video_shorts_temp_dir_on_startup()

    # Jinja filter
    app.jinja_env.filters['from_json_safe'] = from_json_safe
    from app.utils.datetime_format import format_datetime
    app.jinja_env.filters["fmt_dt"] = format_datetime

    if ENABLE_LEGACY_BLOG:
        @app.context_processor
        def inject_nav():
            nav_tree = get_nav_tree(app.config["DB_PATH"])
            try:
                logger.info("NAV_MENU roots: %s", [r.get("slug") for r in nav_tree])
            except Exception:
                logger.info("NAV_MENU roots: <log failed>")
            return {"NAV_MENU": nav_tree}
    else:
        @app.context_processor
        def inject_nav():
            return {"NAV_MENU": []}

    # --- Blueprints ---

    # 1) Video Shorts only
    from app.video_shorts import video_shorts_bp
    app.register_blueprint(video_shorts_bp)
    from app.video_shorts.routes.webhooks import register_instagram_webhook_routes
    from app.video_shorts.routes.auth import _current_user
    from app.video_shorts.routes.api import render_job_status_api, usage_api
    register_instagram_webhook_routes(app)

    @app.get("/api/usage")
    def video_shorts_usage_api_alias():
        _current_user()
        return usage_api()

    @app.get("/api/jobs/<job_id>")
    def video_shorts_render_job_status_api_alias(job_id):
        _current_user()
        return render_job_status_api(job_id)

    @app.get("/w/<token>")
    def public_short_watch_alias(token):
        from app.video_shorts.routes.generation import public_short_watch_page

        return public_short_watch_page(token)

    @app.post("/w/<token>/event")
    def public_short_watch_event_alias(token):
        from app.video_shorts.routes.generation import public_short_watch_event

        return public_short_watch_event(token)

    @app.get("/onboard/<token>")
    def onboarding_magic_link_alias(token):
        from app.video_shorts.routes.auth import redeem_onboarding_magic_link

        return redeem_onboarding_magic_link(token)

    if ENABLE_LEGACY_BLOG:
        from .trends import trend_bp
        app.register_blueprint(trend_bp)

        from .routes import bp as main_bp
        app.register_blueprint(main_bp)

        from app.ebay import ebay_bp
        app.register_blueprint(ebay_bp)

        from app.admin import admin_bp
        import importlib
        importlib.import_module("app.admin.season_routes")
        app.register_blueprint(admin_bp)
    else:
        @app.get("/")
        def legacy_root_redirect():
            return redirect(url_for("video_shorts_bp.home"), code=301)

    def _absolute_public_url(path: str) -> str:
        base_url = "https://mintistudio.com"
        return f"{base_url}{path}"

    @app.get("/terms")
    def terms_page_alias():
        return redirect(url_for("video_shorts_bp.terms_page"), code=301)

    @app.get("/data-deletion")
    def data_deletion_page_alias():
        return redirect(url_for("video_shorts_bp.data_deletion_page"), code=301)

    @app.get("/privacy")
    def privacy_page_alias():
        return redirect(url_for("video_shorts_bp.privacy_page"), code=301)

    def _sitemap_lastmod(value):
        if not value:
            return ""
        if not isinstance(value, datetime):
            value = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        else:
            value = value.astimezone(timezone.utc)
        return value.replace(microsecond=0).isoformat()

    @app.get("/sitemap.xml")
    def sitemap_xml():
        from app.video_shorts.services.blog_articles import ensure_default_blog_articles_seeded, list_published_blog_articles

        ensure_default_blog_articles_seeded()
        entries = [
            {
                "loc": _absolute_public_url(url_for("video_shorts_bp.home")),
                "lastmod": "",
            },
            {
                "loc": _absolute_public_url(url_for("video_shorts_bp.privacy_page")),
                "lastmod": "",
            },
            {
                "loc": _absolute_public_url(url_for("video_shorts_bp.terms_page")),
                "lastmod": "",
            },
            {
                "loc": _absolute_public_url(url_for("video_shorts_bp.data_deletion_page")),
                "lastmod": "",
            },
            {
                "loc": _absolute_public_url(url_for("video_shorts_bp.contact_page")),
                "lastmod": "",
            },
            {
                "loc": _absolute_public_url(url_for("video_shorts_bp.blog_index")),
                "lastmod": "",
            },
        ]
        for article in list_published_blog_articles():
            lastmod = article.get("content_updated_at") or article.get("published_at")
            entries.append(
                {
                    "loc": _absolute_public_url(url_for("video_shorts_bp.blog_article", slug=article["slug"])),
                    "lastmod": _sitemap_lastmod(lastmod),
                }
            )
        lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
        for entry in entries:
            lines.append("  <url>")
            lines.append(f"    <loc>{escape(entry['loc'])}</loc>")
            if entry.get("lastmod"):
                lines.append(f"    <lastmod>{escape(entry['lastmod'])}</lastmod>")
            lines.append("  </url>")
        lines.append("</urlset>")
        response = Response("\n".join(lines) + "\n", mimetype="application/xml")
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response

    @app.get("/robots.txt")
    def robots_txt():
        lines = [
            "User-agent: *",
            "Disallow: /video_shorts/admin/",
            "Disallow: /video_shorts/api/admin/",
            "Disallow: /video_shorts/login",
            "Disallow: /video_shorts/logout",
            "Allow: /video_shorts/blog/",
            f"Sitemap: {_absolute_public_url('/sitemap.xml')}",
        ]
        response = Response("\n".join(lines) + "\n", mimetype="text/plain")
        response.headers["Cache-Control"] = "public, max-age=3600"
        return response

    @app.errorhandler(Exception)
    def handle_application_exception(exc):
        status_code = 500
        if isinstance(exc, HTTPException):
            status_code = int(exc.code or 500)
        capture_server_error(
            status_code=status_code,
            exception_type=type(exc).__name__,
            exception_message=str(exc),
        )
        if isinstance(exc, HTTPException):
            return exc
        if wants_json_error_response():
            return jsonify({"ok": False, "error": "Internal server error."}), 500
        return InternalServerError()

    @app.after_request
    def capture_error_responses(response):
        try:
            status_code = int(getattr(response, "status_code", 200) or 200)
        except Exception:
            status_code = 200
        if status_code >= 400 and not getattr(g, "_vs_error_capture_active", False):
            capture_server_error(status_code=status_code)
        return response

    return app



# ---------------------
#  NAV CACHE + BUILDER
# ---------------------

# basit process-içi cache
_NAV_CACHE = {"data": None, "expires": datetime.min}

def invalidate_nav_cache():
    """Kategori/menü güncellemesi sonrası çağır (admin CRUD'da)."""
    _NAV_CACHE["data"] = None
    _NAV_CACHE["expires"] = datetime.min


def get_nav_tree(db_path: str):
    """categories_tree tablosundan görünür NAV ağacını kurar."""
    try:
        if _NAV_CACHE["data"] and _NAV_CACHE["expires"] > datetime.utcnow():
            return _NAV_CACHE["data"]

        con = connect_ro()

        sql = """
        WITH RECURSIVE tree AS (
          SELECT
            slug, name, parent_slug, sort_order, 0 AS depth,
            CAST(slug AS VARCHAR) AS path
          FROM categories_tree
          WHERE parent_slug IS NULL AND nav_visible

          UNION ALL

          SELECT
            c.slug, c.name, c.parent_slug, c.sort_order, t.depth + 1,
            t.path || '/' || c.slug
          FROM categories_tree c
          JOIN tree t ON c.parent_slug = t.slug
          WHERE c.nav_visible
        )
        SELECT slug, name, parent_slug, sort_order, depth, path
        FROM tree
        ORDER BY split_part(path,'/',1), depth, sort_order, name;
        """

        rows = con.execute(sql).fetchall()
        con.close()

    except Exception as e:
        logger.warning("NAV build failed: %s", e)
        rows = []

    by_slug = {}
    roots = []

    for slug, name, parent_slug, sort_order, depth, path in rows:
        node = by_slug.get(slug) or {
            "slug": slug,
            "name": name,
            "sort_order": sort_order or 0,
            "children": [],
        }
        node["name"] = name
        node["sort_order"] = sort_order or 0
        by_slug[slug] = node

        if parent_slug is None:
            roots.append(node)
        else:
            parent = by_slug.get(parent_slug)
            if not parent:
                parent = {"slug": parent_slug, "name": "", "sort_order": 0, "children": []}
                by_slug[parent_slug] = parent
            parent["children"].append(node)

    def sort_children(n):
        n["children"].sort(key=lambda x: (x.get("sort_order", 0), x.get("name", "")))
        for ch in n["children"]:
            sort_children(ch)

    for r in roots:
        sort_children(r)

    _NAV_CACHE["data"] = roots
    _NAV_CACHE["expires"] = datetime.utcnow() + timedelta(minutes=3)
    return roots
