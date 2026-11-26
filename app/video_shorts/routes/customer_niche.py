from __future__ import annotations

from flask import abort, g, render_template, request, session, url_for

from app.video_shorts import video_shorts_bp
from app.video_shorts.services.admin_operation_scope import request_admin_operation_scope
from app.video_shorts.services.brands import load_brand_context
from app.video_shorts.services.customer_niche import load_customer_niche_page
from app.video_shorts.services.db import get_db_readonly, table_columns


def _resolve_customer_niche_tenant(conn) -> tuple[str, str, bool]:
    scope = request_admin_operation_scope()
    if scope:
        if scope["workspace_kind"] != "customer":
            abort(404)
        return scope["owner_user_id"], scope["brand_id"], True

    current_user = getattr(g, "vs_current_user", None) or {}
    owner_user_id = str(current_user.get("id") or "").strip()
    if not owner_user_id:
        abort(404)
    brand, brands = load_brand_context(
        user_id=owner_user_id,
        user_name=current_user.get("name") or current_user.get("username"),
        requested_brand_id=session.get("vs_brand_id"),
    )
    if not brand:
        abort(404)
    g.vs_current_brand = brand
    g.vs_brands = brands
    session["vs_brand_id"] = brand["id"]
    return owner_user_id, str(brand["id"]), False


@video_shorts_bp.app_context_processor
def inject_customer_niche_navigation():
    scope = request_admin_operation_scope()
    brand_id = str((scope or {}).get("brand_id") or session.get("vs_brand_id") or "").strip()
    current_user = getattr(g, "vs_current_user", None) or {}
    owner_user_id = str((scope or {}).get("owner_user_id") or current_user.get("id") or "").strip()
    if not brand_id or not owner_user_id:
        return {"vs_niche_available": False, "vs_niche_url": ""}
    conn = get_db_readonly()
    try:
        if "niche_id" not in table_columns(conn, "shorts_brands"):
            return {"vs_niche_available": False, "vs_niche_url": ""}
        row = conn.execute(
            """
            SELECT 1 FROM shorts_brands
            WHERE CAST(id AS VARCHAR) = CAST(? AS VARCHAR)
              AND CAST(owner_user_id AS VARCHAR) = CAST(? AS VARCHAR)
              AND niche_id IS NOT NULL
            LIMIT 1
            """,
            [brand_id, owner_user_id],
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return {"vs_niche_available": False, "vs_niche_url": ""}
    values = {}
    if scope:
        values = {
            "admin_operation_brand": scope["brand_id"],
            "admin_operation_kind": scope["workspace_kind"],
        }
    return {
        "vs_niche_available": True,
        "vs_niche_url": url_for("video_shorts_bp.customer_niche_page", **values),
    }


@video_shorts_bp.route("/niche", methods=["GET"])
def customer_niche_page():
    conn = get_db_readonly()
    try:
        owner_user_id, brand_id, is_admin_operation = _resolve_customer_niche_tenant(conn)
        page = load_customer_niche_page(
            conn,
            owner_user_id=owner_user_id,
            brand_id=brand_id,
        )
    finally:
        conn.close()
    if not page:
        abort(404)
    return render_template(
        "customer_niche.html",
        niche_page=page,
        niche_admin_operation=is_admin_operation,
    )
