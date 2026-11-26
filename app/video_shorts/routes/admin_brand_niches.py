from __future__ import annotations

from flask import abort, current_app, flash, redirect, request, url_for

from app.video_shorts import video_shorts_bp
from app.video_shorts.routes.auth import require_admin
from app.video_shorts.services.brand_niches import (
    load_admin_brand_niche_settings,
    update_brand_niche,
)
from app.video_shorts.services.db import get_db, get_db_readonly


@video_shorts_bp.app_context_processor
def inject_admin_brand_niche_settings():
    if request.endpoint != "video_shorts_bp.admin_user_detail":
        return {}
    user_id = str((request.view_args or {}).get("user_id") or "").strip()
    if not user_id:
        return {"brand_niche_settings": {"brands": [], "niches": []}}
    conn = get_db_readonly()
    try:
        settings = load_admin_brand_niche_settings(conn, user_id)
    except Exception:
        current_app.logger.exception("Could not load admin brand niche settings user=%s", user_id)
        settings = {"brands": [], "niches": []}
    finally:
        conn.close()
    return {"brand_niche_settings": settings}


@video_shorts_bp.route("/admin/users/<user_id>/brands/<brand_id>/niche", methods=["POST"])
@require_admin
def admin_update_brand_niche(user_id: str, brand_id: str):
    target_user_id = str(user_id or "").strip()
    target_brand_id = str(brand_id or "").strip()
    if not target_user_id or not target_brand_id:
        abort(404)
    requested_niche_id = str(request.form.get("niche_id") or "").strip()
    conn = get_db()
    try:
        selected_niche_id = update_brand_niche(
            conn,
            owner_user_id=target_user_id,
            brand_id=target_brand_id,
            niche_id=requested_niche_id,
        )
        conn.commit()
    except LookupError:
        conn.rollback()
        abort(404)
    except ValueError as exc:
        conn.rollback()
        flash(str(exc), "danger")
    except Exception as exc:
        conn.rollback()
        current_app.logger.exception(
            "Admin brand niche update failed user=%s brand=%s: %s",
            target_user_id,
            target_brand_id,
            exc,
        )
        flash("Could not update brand niche.", "danger")
    else:
        flash("Brand niche updated." if selected_niche_id else "Brand niche cleared.", "success")
    finally:
        conn.close()
    return redirect(url_for("video_shorts_bp.admin_user_detail", user_id=target_user_id))
