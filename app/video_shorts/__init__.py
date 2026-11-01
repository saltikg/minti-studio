from flask import Blueprint, redirect, request, url_for

from app.video_shorts.config import VIDEO_SHORTS_DB_BACKEND
from app.video_shorts.services.feature_flags import comments_enabled

video_shorts_bp = Blueprint(
    "video_shorts_bp",
    __name__,
    url_prefix="/video_shorts",
    template_folder="templates",
    static_folder="static"
)


@video_shorts_bp.teardown_request
def close_video_shorts_request_connections(error=None):
    from app.video_shorts.services.db import close_request_db_connections

    close_request_db_connections(error)


@video_shorts_bp.app_context_processor
def inject_video_shorts_db_backend():
    return {
        "video_shorts_db_backend": (VIDEO_SHORTS_DB_BACKEND or "duckdb"),
        "comments_enabled": comments_enabled(),
    }


@video_shorts_bp.before_request
def redirect_disabled_comment_routes():
    if comments_enabled():
        return None
    path = request.path.rstrip("/")
    comments_prefixes = (
        f"{video_shorts_bp.url_prefix}/shorts/comments",
        f"{video_shorts_bp.url_prefix}/instagram/media/",
        f"{video_shorts_bp.url_prefix}/facebook/media/",
    )
    is_comment_route = (
        path == comments_prefixes[0] or path.startswith(f"{comments_prefixes[0]}/")
    ) or (
        path.startswith(comments_prefixes[1]) and "/comments" in path
    ) or (
        path.startswith(comments_prefixes[2]) and "/comments" in path
    )
    if is_comment_route:
        return redirect(url_for("video_shorts_bp.my_videos_page"))
    return None

from .routes import *  # noqa: F401,F403
