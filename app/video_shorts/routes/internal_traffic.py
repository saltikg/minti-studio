from __future__ import annotations

from flask import redirect, request, url_for

from app.video_shorts import video_shorts_bp
from app.video_shorts.routes.auth import require_admin

_INTERNAL_TRAFFIC_COOKIE = "minti_internal_traffic"
_INTERNAL_TRAFFIC_COOKIE_VALUE = "true"
_INTERNAL_TRAFFIC_COOKIE_MAX_AGE = 60 * 60 * 24 * 365


def _should_set_secure_cookie() -> bool:
    forwarded_proto = (request.headers.get("X-Forwarded-Proto") or "").split(",", 1)[0].strip().lower()
    return request.is_secure or forwarded_proto == "https"


@video_shorts_bp.route("/internal-traffic/enable", methods=["GET"])
@require_admin
def enable_internal_traffic():
    response = redirect(url_for("video_shorts_bp.home"))
    response.set_cookie(
        _INTERNAL_TRAFFIC_COOKIE,
        _INTERNAL_TRAFFIC_COOKIE_VALUE,
        max_age=_INTERNAL_TRAFFIC_COOKIE_MAX_AGE,
        secure=_should_set_secure_cookie(),
        httponly=False,
        samesite="Lax",
    )
    return response


@video_shorts_bp.route("/internal-traffic/disable", methods=["GET"])
@require_admin
def disable_internal_traffic():
    response = redirect(url_for("video_shorts_bp.home"))
    response.delete_cookie(
        _INTERNAL_TRAFFIC_COOKIE,
        secure=_should_set_secure_cookie(),
        httponly=False,
        samesite="Lax",
    )
    return response
