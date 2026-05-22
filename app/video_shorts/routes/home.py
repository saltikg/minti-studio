from flask import render_template

from app.video_shorts import video_shorts_bp


@video_shorts_bp.route("/", methods=["GET"])
def home():
    return render_template("vs_home.html")
