import mimetypes

from flask import send_from_directory

from app.video_shorts import video_shorts_bp
from app.video_shorts.config import VIDEOS_DIR


@video_shorts_bp.route("/media/<path:filename>")
def serve_media(filename):
    target = (VIDEOS_DIR / filename).resolve()
    try:
        target.relative_to(VIDEOS_DIR.resolve())
    except Exception:
        return "forbidden", 403
    if not target.exists() or not target.is_file():
        return "not found", 404
    guessed_type, _ = mimetypes.guess_type(target.name)
    return send_from_directory(
        VIDEOS_DIR,
        filename,
        mimetype=guessed_type or "application/octet-stream",
    )
