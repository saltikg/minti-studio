# app/admin/__init__.py
from flask import Blueprint

admin_bp = Blueprint(
    "admin_bp",
    __name__,
    url_prefix="/admin",
    template_folder="templates",
    static_folder="static",
)

# Admin blueprint altındaki TÜM route modüllerini yalnızca burada import et
from . import routes            # dashboard, all, stats, brands, delete...
from . import season_routes, trends_routes
from .idea_planner import routes as idea_planner_routes
from . import youtube_captions_api    # bunu ekle

