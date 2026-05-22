from flask import Blueprint

from app.video_shorts.config import VIDEO_SHORTS_DB_BACKEND

video_shorts_bp = Blueprint(
    "video_shorts_bp",
    __name__,
    url_prefix="/video_shorts",
    template_folder="templates",
    static_folder="static"
)


@video_shorts_bp.app_context_processor
def inject_video_shorts_db_backend():
    return {"video_shorts_db_backend": (VIDEO_SHORTS_DB_BACKEND or "duckdb")}

from .routes import *  # noqa: F401,F403
