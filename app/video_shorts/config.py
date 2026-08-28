import os
import shutil
from collections import OrderedDict
from pathlib import Path

# OpenAI is optional; if not installed, LLM-based features will be disabled.
try:
    from openai import OpenAI
except ImportError:  # pragma: no cover
    OpenAI = None


VIDEO_SHORTS_DB = os.environ.get("VIDEO_SHORTS_DB")
VIDEO_SHORTS_DATABASE_URL = (
    os.getenv("VIDEO_SHORTS_DATABASE_URL")
    or os.getenv("VIDEO_SHORTS_POSTGRES_URL")
    or os.getenv("DATABASE_URL")
    or ""
).strip()
_video_shorts_db_backend = (os.getenv("VIDEO_SHORTS_DB_BACKEND") or "").strip().lower()
if _video_shorts_db_backend:
    VIDEO_SHORTS_DB_BACKEND = _video_shorts_db_backend
elif VIDEO_SHORTS_DATABASE_URL:
    VIDEO_SHORTS_DB_BACKEND = "postgres"
else:
    VIDEO_SHORTS_DB_BACKEND = "duckdb"
VIDEO_SHORTS_POSTGRES_CONNECT_TIMEOUT = int(
    os.getenv("VIDEO_SHORTS_POSTGRES_CONNECT_TIMEOUT", "3")
)
CAPTION_API_TOKEN = os.getenv("CAPTION_API_TOKEN", "minti_caption_8273f4ac0b")
MEDIA_BACKEND = (os.getenv("MEDIA_BACKEND", "local") or "local").strip().lower()
AWS_ACCESS_KEY_ID = (os.getenv("AWS_ACCESS_KEY_ID", "") or "").strip()
AWS_SECRET_ACCESS_KEY = (os.getenv("AWS_SECRET_ACCESS_KEY", "") or "").strip()
AWS_REGION = (os.getenv("AWS_REGION", "us-east-1") or "us-east-1").strip()
S3_BUCKET_NAME = (os.getenv("S3_BUCKET_NAME", "") or "").strip()
CLOUDFRONT_DOMAIN = (os.getenv("CLOUDFRONT_DOMAIN", "") or "").strip()
CLOUDFRONT_KEY_PAIR_ID = (os.getenv("CLOUDFRONT_KEY_PAIR_ID", "") or "").strip()
CLOUDFRONT_PRIVATE_KEY_PATH = (os.getenv("CLOUDFRONT_PRIVATE_KEY_PATH", "") or "").strip()
CLOUDFRONT_DISTRIBUTION_ID = (os.getenv("CLOUDFRONT_DISTRIBUTION_ID", "") or "").strip() or None
SHORTS_DIR = Path(__file__).resolve().parent / "static" / "shorts"
VIDEOS_DIR = Path(__file__).resolve().parent / "videos"
VIDEO_SHORTS_TMP_DIR = Path(os.getenv("VIDEO_SHORTS_TMP_DIR") or (Path(__file__).resolve().parent / "tmp"))
BGCOVER_PATH = VIDEOS_DIR / "1-short_bg_8.png"
FFMPEG_BIN = os.getenv("FFMPEG_BIN") or shutil.which("ffmpeg") or "ffmpeg"


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    raw = str(raw).strip()
    if not raw:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool, *, warn_invalid: bool = False, logger=None) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = str(raw).strip()
    if not value:
        return default
    normalized = value.lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    if warn_invalid and logger is not None:
        logger.warning("Invalid boolean env var %s=%r; treating as disabled.", name, raw)
    return False


DEFAULT_STORAGE_PLANS = [
    {
        "plan_id": "plan_free",
        "label": "Free",
        "quota_bytes": 2 * 1024 ** 3,
        "price_monthly": 0,
        "price_yearly": 0,
        "monthly_export_limit": 10,
        "monthly_transcription_minutes": 60,
        "render_priority": 0,
        "max_concurrent_jobs": 1,
        "max_upload_duration_seconds": _env_int("MAX_UPLOAD_DURATION_SECONDS_FREE", 3600),
        "max_upload_size_bytes": _env_int("MAX_UPLOAD_SIZE_BYTES_FREE", 2147483648),
        "is_active": True,
        "sort_order": 0,
    },
    {
        "plan_id": "plan_2gb",
        "label": "Starter",
        "quota_bytes": 2 * 1024 ** 3,
        "price_monthly": 9,
        "price_yearly": 90,
        "monthly_export_limit": 30,
        "monthly_transcription_minutes": 180,
        "render_priority": 10,
        "max_concurrent_jobs": 2,
        "max_upload_duration_seconds": _env_int("MAX_UPLOAD_DURATION_SECONDS_PAID", 10800),
        "max_upload_size_bytes": _env_int("MAX_UPLOAD_SIZE_BYTES_PAID", 5368709120),
        "is_active": True,
        "sort_order": 1,
    },
    {
        "plan_id": "plan_10gb",
        "label": "Creator",
        "quota_bytes": 10 * 1024 ** 3,
        "price_monthly": 19,
        "price_yearly": 180,
        "monthly_export_limit": 90,
        "monthly_transcription_minutes": 540,
        "render_priority": 20,
        "max_concurrent_jobs": 2,
        "max_upload_duration_seconds": _env_int("MAX_UPLOAD_DURATION_SECONDS_PAID", 10800),
        "max_upload_size_bytes": _env_int("MAX_UPLOAD_SIZE_BYTES_PAID", 5368709120),
        "is_active": True,
        "sort_order": 2,
    },
    {
        "plan_id": "plan_100gb",
        "label": "Studio",
        "quota_bytes": 100 * 1024 ** 3,
        "price_monthly": 49,
        "price_yearly": 450,
        "monthly_export_limit": 270,
        "monthly_transcription_minutes": 1620,
        "render_priority": 30,
        "max_concurrent_jobs": 3,
        "max_upload_duration_seconds": _env_int("MAX_UPLOAD_DURATION_SECONDS_PAID", 10800),
        "max_upload_size_bytes": _env_int("MAX_UPLOAD_SIZE_BYTES_PAID", 5368709120),
        "is_active": True,
        "sort_order": 3,
    },
]
DEFAULT_USER_STORAGE_LIMIT = 3 * 1024 ** 3
DEFAULT_TITLE_TEXT_COLOR = "#000000"
DEFAULT_SUBTITLE_TEXT_COLOR = "#FFFFFF"
DEFAULT_SUBTITLE_BG_COLOR = "#000000"
SUBTITLE_HIGHLIGHT_COLOR = "#FFD84D"
SUBTITLE_PRESETS = {
    "classic_yellow": {
        "active_color": "#FFD84D",
        "inactive_color": "#FFFFFF",
        "outline_color": "#000000",
        "outline_width": 1,
        "border_style": 4,
        "box": True,
        "bold": False,
        "active_scale": 100,
        "font": "Arimo",
    },
    "pop_green": {
        "active_color": "#22C55E",
        "inactive_color": "#FFFFFF",
        "outline_color": "#000000",
        "outline_width": 4,
        "border_style": 1,
        "box": False,
        "bold": True,
        "active_scale": 115,
        "font": "Anton",
    },
    "pop_pink": {
        "active_color": "#EC4899",
        "inactive_color": "#FFFFFF",
        "outline_color": "#000000",
        "outline_width": 4,
        "border_style": 1,
        "box": False,
        "bold": True,
        "active_scale": 115,
        "font": "Bebas Neue",
    },
    "minimal_white": {
        "active_color": "#FFFFFF",
        "inactive_color": "#E5E7EB",
        "outline_color": "#111827",
        "outline_width": 3,
        "border_style": 1,
        "box": False,
        "bold": False,
        "active_scale": 100,
        "font": "Open Sans",
    },
    "deep_blue": {
        "active_color": "#93C5FD",
        "inactive_color": "#FFFFFF",
        "outline_color": "#1E3A8A",
        "outline_width": 4,
        "border_style": 1,
        "box": False,
        "bold": True,
        "active_scale": 115,
        "font": "Anton",
    },
    "clean_black": {
        "active_color": "#FFFFFF",
        "inactive_color": "#FFFFFF",
        "outline_color": "#000000",
        "outline_width": 3,
        "border_style": 4,
        "box": True,
        "bold": True,
        "active_scale": 100,
        "font": "Open Sans",
    },
    "word_highlight": {
        "active_color": "#111827",
        "inactive_color": "#FFFFFF",
        "outline_color": "#000000",
        "outline_width": 3,
        "border_style": 1,
        "box": False,
        "bold": True,
        "active_scale": 100,
        "font": "Open Sans SemiCondensed Medium",
        "active_box_color": "#FFD84D",
        "active_box_outline_color": "#FFD84D",
        "active_box_outline_width": 10,
        "active_box_border_style": 4,
        "active_box_padding_spaces": 1,
        "active_pill": True,
    },
    "word_highlight_flame": {
        "active_color": "#FFFFFF",
        "inactive_color": "#FFFFFF",
        "outline_color": "#000000",
        "outline_width": 3,
        "border_style": 1,
        "box": False,
        "bold": True,
        "active_scale": 100,
        "font": "Open Sans SemiCondensed Medium",
        "active_box_color": "#F26522",
        "active_box_outline_color": "#F26522",
        "active_box_outline_width": 10,
        "active_box_border_style": 4,
        "active_box_padding_spaces": 1,
        "active_pill": True,
    },
    "word_highlight_ocean": {
        "active_color": "#FFFFFF",
        "inactive_color": "#FFFFFF",
        "outline_color": "#000000",
        "outline_width": 3,
        "border_style": 1,
        "box": False,
        "bold": True,
        "active_scale": 100,
        "font": "Open Sans SemiCondensed Medium",
        "active_box_color": "#3B7DED",
        "active_box_outline_color": "#3B7DED",
        "active_box_outline_width": 10,
        "active_box_border_style": 4,
        "active_box_padding_spaces": 1,
        "active_pill": True,
    },
    "green_pop": {
        "active_color": "#3CE84A",
        "inactive_color": "#FFFFFF",
        "outline_color": "#000000",
        "outline_width": 0,
        "border_style": 1,
        "box": False,
        "bold": True,
        "active_scale": 100,
        "font": "Montserrat Black",
        "active_pill": False,
        "pillow_engine": True,
        "draw_pill": False,
        "shadow_color": "#000000",
        "shadow_opacity": 0.55,
        "shadow_offset_x": 0,
        "shadow_offset_y": 8,
        "shadow_blur_radius": 17,
    },
    "silver_pop": {
        "active_color": "#FFFFFF",
        "inactive_color": "#E2E4E7",
        "outline_color": "#000000",
        "outline_width": 0,
        "border_style": 1,
        "box": False,
        "bold": True,
        "active_scale": 100,
        "font": "Anton",
        "active_pill": False,
        "pillow_engine": True,
        "draw_pill": False,
        "fill_mode": "gradient",
        "active_gradient_top": "#F7F7F7",
        "active_gradient_mid": "#FFFFFF",
        "active_gradient_bottom": "#B5BBC4",
        "inactive_gradient_top": "#E2E4E7",
        "inactive_gradient_mid": "#F3F4F6",
        "inactive_gradient_bottom": "#767C85",
        "shadow_color": "#000000",
        "shadow_opacity": 0.55,
        "shadow_offset_x": 0,
        "shadow_offset_y": 8,
        "shadow_blur_radius": 17,
    },
    "amber_gold": {
        "active_color": "#FFC233",
        "inactive_color": "#FFFFFF",
        "outline_color": "#000000",
        "outline_width": 0,
        "border_style": 1,
        "box": False,
        "bold": True,
        "active_scale": 100,
        "font": "Fjalla One",
        "active_pill": False,
        "pillow_engine": True,
        "draw_pill": False,
        "fill_mode": "gradient",
        "active_fill_mode": "gradient",
        "inactive_fill_mode": "flat",
        "active_gradient_top": "#FFE07A",
        "active_gradient_mid": "#FFC233",
        "active_gradient_bottom": "#E89400",
        "shadow_color": "#000000",
        "shadow_opacity": 0.55,
        "shadow_offset_x": 0,
        "shadow_offset_y": 8,
        "shadow_blur_radius": 17,
    },
    "sky_cyan": {
        "active_color": "#38D6E0",
        "inactive_color": "#FFFFFF",
        "outline_color": "#000000",
        "outline_width": 0,
        "border_style": 1,
        "box": False,
        "bold": True,
        "active_scale": 100,
        "font": "Archivo Black",
        "active_pill": False,
        "pillow_engine": True,
        "draw_pill": False,
        "fill_mode": "gradient",
        "active_fill_mode": "gradient",
        "inactive_fill_mode": "flat",
        "active_gradient_top": "#9BF3F7",
        "active_gradient_mid": "#38D6E0",
        "active_gradient_bottom": "#1AA6B5",
        "shadow_color": "#000000",
        "shadow_opacity": 0.55,
        "shadow_offset_x": 0,
        "shadow_offset_y": 8,
        "shadow_blur_radius": 17,
    },
    "electric_lime": {
        "active_color": "#C6F24D",
        "inactive_color": "#FFFFFF",
        "outline_color": "#000000",
        "outline_width": 0,
        "border_style": 1,
        "box": False,
        "bold": True,
        "active_scale": 100,
        "font": "Teko Bold",
        "active_pill": False,
        "pillow_engine": True,
        "draw_pill": False,
        "fill_mode": "gradient",
        "active_fill_mode": "gradient",
        "inactive_fill_mode": "flat",
        "active_gradient_top": "#E8FFA8",
        "active_gradient_mid": "#C6F24D",
        "active_gradient_bottom": "#8AB800",
        "shadow_color": "#000000",
        "shadow_opacity": 0.55,
        "shadow_offset_x": 0,
        "shadow_offset_y": 8,
        "shadow_blur_radius": 17,
    },
    "serif_bordo": {
        "active_color": "#E8A9B8",
        "inactive_color": "#FFFFFF",
        "outline_color": "#000000",
        "outline_width": 0,
        "border_style": 1,
        "box": False,
        "bold": True,
        "active_scale": 100,
        "font": "Roboto Slab Black",
        "active_pill": False,
        "pillow_engine": True,
        "draw_pill": False,
        "fill_mode": "gradient",
        "active_fill_mode": "gradient",
        "inactive_fill_mode": "flat",
        "active_gradient_top": "#FBE9EC",
        "active_gradient_mid": "#E8A9B8",
        "active_gradient_bottom": "#9B2D4F",
        "shadow_color": "#000000",
        "shadow_opacity": 0.55,
        "shadow_offset_x": 0,
        "shadow_offset_y": 8,
        "shadow_blur_radius": 17,
    },
}
DEFAULT_TITLE_BG_ALPHA = 92
DEFAULT_SUBTITLE_BG_ALPHA = 85
DEFAULT_SUBTITLE_TEXT_ALPHA = 100
WORKER_CONCURRENCY = max(1, int(os.getenv("WORKER_CONCURRENCY", "1") or "1"))
JOB_TIMEOUT_SECONDS = max(60, int(os.getenv("JOB_TIMEOUT_SECONDS", "600") or "600"))
JOB_POLL_INTERVAL_SECONDS = max(1.0, float(os.getenv("JOB_POLL_INTERVAL_SECONDS", "2") or "2"))
DISK_GUARD_PCT = max(1, min(100, _env_int("DISK_GUARD_PCT", 85)))
MAX_GLOBAL_CONCURRENT_JOBS = max(1, _env_int("MAX_GLOBAL_CONCURRENT_JOBS", 2))
SIGNUPS_ENABLED = _env_bool("SIGNUPS_ENABLED", True)
_default_plan_id = next((plan["plan_id"] for plan in DEFAULT_STORAGE_PLANS if plan["plan_id"] == "plan_free"), DEFAULT_STORAGE_PLANS[0]["plan_id"])
DEFAULT_USER_PLAN_ID = os.getenv("DEFAULT_USER_PLAN_ID", _default_plan_id)
_categories_env = os.getenv("SHORTS_CATEGORY_OPTIONS", "").strip()
if _categories_env:
    SHORTS_CATEGORY_OPTIONS = [item.strip() for item in _categories_env.split(",") if item.strip()]
else:
    SHORTS_CATEGORY_OPTIONS = [
        "Hikaye",
        "Sahabe ornegi",
        "Kuran ayeti ve tefsiri",
        "Hadis ve aciklama",
        "Soru cevap (QA)",
        "Dua ve niyaz",
        "Nasihat (amel odakli)",
        "Ahlak ve kalp terbiyesi",
        "Sabir imtihan ve musibet",
        "Ihsan ihlas ve riza",
        "Tevbe istiğfar muhasebe",
        "Umut ve teselli",
        "Uyari ve ikaz",
        "Cemaat hizmet ve sorumluluk",
        "Genclik egitim aile",
        "Zaman ahir zaman fitne uyarisi",
        "Tevhid iman hakikatleri",
        "Sükur hamd ve nimet",
        "Kader tevekkul teslimiyet",
        "Kardeslik birlik uhuvvet",
    ]

# Media subprocess timeouts. Keep a backwards-compatible FFMPEG_TIMEOUT for
# older call sites, but never allow a None/empty env value to leak into int().
FFPROBE_TIMEOUT = max(1, _env_int("FFPROBE_TIMEOUT", 60))
FFMPEG_SHORT_TIMEOUT = max(1, _env_int("FFMPEG_SHORT_TIMEOUT", 300))
FFMPEG_RENDER_TIMEOUT = max(1, _env_int("FFMPEG_RENDER_TIMEOUT", 3600))
FFMPEG_TIMEOUT = max(1, _env_int("FFMPEG_TIMEOUT", FFMPEG_RENDER_TIMEOUT))
STALE_JOB_TIMEOUT_SECONDS = max(FFMPEG_RENDER_TIMEOUT + 1, _env_int("STALE_JOB_TIMEOUT_SECONDS", 5400))
MAX_CLIP_LEN = int(os.getenv("MAX_CLIP_LEN", "120"))  # safety cap for per-clip duration
SHORT_MIN_LEN = float(os.getenv("SHORT_MIN_LEN", "40"))
SHORT_MAX_LEN = float(os.getenv("SHORT_MAX_LEN", str(MAX_CLIP_LEN)))
OPENAI_MODEL = os.getenv("OPENAI_MODEL_GPT", "gpt-4.1-mini")
CLIP_PLAN_MAX_RUNS_PER_VIDEO = max(1, int(os.getenv("CLIP_PLAN_MAX_RUNS_PER_VIDEO", "2") or "2"))
CLIP_PLAN_COOLDOWN_SECONDS = max(0, int(os.getenv("CLIP_PLAN_COOLDOWN_SECONDS", "90") or "90"))

_openai_api_key = os.getenv("OPENAI_API_KEY")
_openai_client = OpenAI(api_key=_openai_api_key) if (OPENAI_MODEL and _openai_api_key and OpenAI) else None
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "whisper-1")
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")
SHORTS_OVERVIEW_STATS_TTL_MINUTES = int(os.getenv("SHORTS_OVERVIEW_STATS_TTL_MINUTES", "60"))
SHORTS_OVERVIEW_STATS_MAX_VIDEOS = int(os.getenv("SHORTS_OVERVIEW_STATS_MAX_VIDEOS", "50"))
SHORTS_OVERVIEW_QUOTA_COOLDOWN_HOURS = int(os.getenv("SHORTS_OVERVIEW_QUOTA_COOLDOWN_HOURS", "6"))
SHORTS_OVERVIEW_FIRST_FILL_MAX_VIDEOS = int(
    os.getenv("SHORTS_OVERVIEW_FIRST_FILL_MAX_VIDEOS", "5")
)
COMMENT_FETCH_MAX_PAGES = max(1, int(os.getenv("COMMENT_FETCH_MAX_PAGES", "10") or "10"))
COMMENT_LIVE_FETCH_MIN_INTERVAL_SECONDS = max(
    0, int(os.getenv("COMMENT_LIVE_FETCH_MIN_INTERVAL_SECONDS", "60") or "60")
)
_comment_auto_moderation_mode = (os.getenv("COMMENT_AUTO_MODERATION_MODE", "shadow") or "shadow").strip().lower()
if _comment_auto_moderation_mode not in {"off", "shadow", "enforce"}:
    _comment_auto_moderation_mode = "shadow"
COMMENT_AUTO_MODERATION_MODE = _comment_auto_moderation_mode
YOUTUBE_CLIENT_ID = os.getenv("YOUTUBE_CLIENT_ID")
YOUTUBE_CLIENT_SECRET = os.getenv("YOUTUBE_CLIENT_SECRET")
YOUTUBE_REDIRECT_URI = os.getenv("YOUTUBE_REDIRECT_URI")
YOUTUBE_OAUTH_SCOPES = os.getenv(
    "YOUTUBE_OAUTH_SCOPES",
    "https://www.googleapis.com/auth/youtube.upload,https://www.googleapis.com/auth/youtube.force-ssl",
)
IG_ACCESS_TOKEN = os.getenv("IG_ACCESS_TOKEN")
IG_BUSINESS_ACCOUNT_ID = os.getenv("IG_BUSINESS_ACCOUNT_ID")
IG_API_BASE = os.getenv("IG_API_BASE", "https://graph.facebook.com/v24.0")
IG_GRAPH_API_BASE = os.getenv("IG_GRAPH_API_BASE", "https://graph.instagram.com")
IG_AUTH_BASE = os.getenv("IG_AUTH_BASE", "https://www.instagram.com/oauth/authorize")
IG_APP_ID = os.getenv("IG_APP_ID")
IG_APP_SECRET = os.getenv("IG_APP_SECRET")
INSTAGRAM_APP_SECRET = os.getenv("INSTAGRAM_APP_SECRET") or IG_APP_SECRET
META_APP_SECRET = os.getenv("META_APP_SECRET") or IG_APP_SECRET or os.getenv("FB_APP_SECRET")
IG_REDIRECT_URI = os.getenv("IG_REDIRECT_URI")
IG_OAUTH_SCOPES = os.getenv(
    "IG_OAUTH_SCOPES",
    "instagram_business_basic,instagram_business_content_publish,instagram_business_manage_insights,instagram_business_manage_comments",
)
INSTAGRAM_WEBHOOK_VERIFY_TOKEN = os.getenv("INSTAGRAM_WEBHOOK_VERIFY_TOKEN")
IG_TOKEN_REFRESH_BUFFER_DAYS = int(os.getenv("IG_TOKEN_REFRESH_BUFFER_DAYS", "14"))
FB_APP_ID = os.getenv("FB_APP_ID")
FB_APP_SECRET = os.getenv("FB_APP_SECRET")
FB_REDIRECT_URI = os.getenv("FB_REDIRECT_URI")
FB_OAUTH_SCOPES = os.getenv(
    "FB_OAUTH_SCOPES",
    "pages_show_list,pages_read_engagement,pages_manage_posts,pages_read_user_content",
)
FB_API_BASE = os.getenv("FB_API_BASE", "https://graph.facebook.com/v24.0")
FB_TARGET_PAGE_ID = os.getenv("FB_TARGET_PAGE_ID", "841809229026467")
TIKTOK_CLIENT_KEY = os.getenv("TIKTOK_CLIENT_KEY")
TIKTOK_CLIENT_SECRET = os.getenv("TIKTOK_CLIENT_SECRET")
TIKTOK_REDIRECT_URI = os.getenv("TIKTOK_REDIRECT_URI")
TIKTOK_OAUTH_SCOPES = os.getenv(
    "TIKTOK_OAUTH_SCOPES",
    "user.info.basic,video.upload",
)
TIKTOK_AUTH_BASE = os.getenv("TIKTOK_AUTH_BASE", "https://www.tiktok.com/v2/auth/authorize/")
TIKTOK_API_BASE = os.getenv("TIKTOK_API_BASE", "https://open.tiktokapis.com/v2")
TIKTOK_PRIVACY_LEVEL = os.getenv("TIKTOK_PRIVACY_LEVEL", "PUBLIC")
HEYGEN_API_KEY = (os.getenv("HEYGEN_API_KEY") or "").strip()
HEYGEN_API_BASE = (os.getenv("HEYGEN_API_BASE") or "https://api.heygen.com").strip()
GOOGLE_OAUTH_CLIENT_ID = os.getenv("GOOGLE_OAUTH_CLIENT_ID")
GOOGLE_OAUTH_CLIENT_SECRET = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET")
GOOGLE_OAUTH_REDIRECT_URI = os.getenv("GOOGLE_OAUTH_REDIRECT_URI")
_default_google_scopes = "openid,https://www.googleapis.com/auth/userinfo.email,https://www.googleapis.com/auth/userinfo.profile"
GOOGLE_OAUTH_SCOPES = [
    scope.strip()
    for scope in os.getenv("GOOGLE_OAUTH_SCOPES", _default_google_scopes).split(",")
    if scope.strip()
]
if not GOOGLE_OAUTH_SCOPES:
    GOOGLE_OAUTH_SCOPES = _default_google_scopes.split(",")

# Optional path to exported YouTube cookies. We first look for an env var, then
# fall back to templates/cookies.txt so you can just drop a browser export there.
_cookies_env = os.getenv("YT_DLP_COOKIES", "").strip()
_cookies_default = Path(__file__).resolve().parent / "templates" / "cookies.txt"
if _cookies_env:
    YT_DLP_COOKIES = Path(_cookies_env)
elif _cookies_default.exists():
    YT_DLP_COOKIES = _cookies_default
else:
    YT_DLP_COOKIES = None

DEFAULT_TITLE_FONT_KEY = "oswald_regular"
DEFAULT_TITLE_FONT_SIZE = 44
DEFAULT_SUB_FONT_KEY = "arimo"
DEFAULT_SUB_FONT_SIZE = 40
DEFAULT_TITLE_MARGIN = 220
DEFAULT_TITLE_BG_COLOR = "#FFD600"
DEFAULT_VIDEO_OVERLAY_OFFSET = int(os.getenv("VIDEO_OVERLAY_OFFSET_DEFAULT", "350"))
STYLE_TEMPLATES = [
    {
        "key": "green_pop",
        "label": "Green Pop",
        "subtitle_preset": "green_pop",
        "title_engine": "pillow",
        "title_uppercase": True,
        "title_font": "Montserrat Black",
        "title_font_key": "montserrat_black",
        "title_text_color": "#FFFFFF",
        "title_bg_color": "#14532D",
        "title_bg_alpha": 0,
        "title_font_size": DEFAULT_TITLE_FONT_SIZE,
    },
    {
        "key": "silver_pop",
        "label": "Silver Pop",
        "subtitle_preset": "silver_pop",
        "title_engine": "pillow",
        "title_uppercase": True,
        "title_font": "Anton",
        "title_font_key": "anton",
        "title_text_color": "#FFFFFF",
        "title_bg_color": "#111827",
        "title_bg_alpha": 0,
        "title_font_size": DEFAULT_TITLE_FONT_SIZE,
    },
    {
        "key": "amber_gold",
        "label": "Amber Gold",
        "subtitle_preset": "amber_gold",
        "title_engine": "pillow",
        "title_uppercase": True,
        "title_font": "Fjalla One",
        "title_font_key": "fjalla_one",
        "title_text_color": "#FFFFFF",
        "title_bg_color": "#111827",
        "title_bg_alpha": 0,
        "title_font_size": DEFAULT_TITLE_FONT_SIZE,
    },
    {
        "key": "sky_cyan",
        "label": "Sky Cyan",
        "subtitle_preset": "sky_cyan",
        "title_engine": "pillow",
        "title_uppercase": True,
        "title_font": "Archivo Black",
        "title_font_key": "archivo_black",
        "title_text_color": "#FFFFFF",
        "title_bg_color": "#111827",
        "title_bg_alpha": 0,
        "title_font_size": DEFAULT_TITLE_FONT_SIZE,
    },
    {
        "key": "electric_lime",
        "label": "Electric Lime",
        "subtitle_preset": "electric_lime",
        "title_engine": "pillow",
        "title_uppercase": True,
        "title_font": "Teko Bold",
        "title_font_key": "teko_bold",
        "title_text_color": "#FFFFFF",
        "title_bg_color": "#111827",
        "title_bg_alpha": 0,
        "title_font_size": DEFAULT_TITLE_FONT_SIZE,
    },
    {
        "key": "serif_bordo",
        "label": "Serif Bordo",
        "subtitle_preset": "serif_bordo",
        "title_engine": "pillow",
        "title_uppercase": True,
        "title_font": "Roboto Slab Black",
        "title_font_key": "roboto_slab_black",
        "title_text_color": "#FFFFFF",
        "title_bg_color": "#111827",
        "title_bg_alpha": 0,
        "title_font_size": DEFAULT_TITLE_FONT_SIZE,
    },
    {
        "key": "word_highlight",
        "label": "Word Highlight",
        "subtitle_preset": "word_highlight",
        "title_engine": "drawtext",
        "title_uppercase": False,
        "title_font": "Anton",
        "title_font_key": "anton",
        "title_text_color": "#FFFFFF",
        "title_bg_color": "#111827",
        "title_font_size": DEFAULT_TITLE_FONT_SIZE,
    },
    {
        "key": "word_highlight_flame",
        "label": "Flame Highlight",
        "subtitle_preset": "word_highlight_flame",
        "title_engine": "drawtext",
        "title_uppercase": False,
        "title_font": "Anton",
        "title_font_key": "anton",
        "title_text_color": "#FFFFFF",
        "title_bg_color": "#F26522",
        "title_font_size": DEFAULT_TITLE_FONT_SIZE,
    },
    {
        "key": "word_highlight_ocean",
        "label": "Ocean Highlight",
        "subtitle_preset": "word_highlight_ocean",
        "title_engine": "drawtext",
        "title_uppercase": False,
        "title_font": "Anton",
        "title_font_key": "anton",
        "title_text_color": "#FFFFFF",
        "title_bg_color": "#3B7DED",
        "title_font_size": DEFAULT_TITLE_FONT_SIZE,
    },
    {
        "key": "classic",
        "label": "Classic",
        "subtitle_preset": "classic_yellow",
        "title_engine": "drawtext",
        "title_uppercase": False,
        "title_font": "Oswald",
        "title_font_key": DEFAULT_TITLE_FONT_KEY,
        "title_text_color": "#0F172A",
        "title_bg_color": "#FFD84D",
        "title_font_size": DEFAULT_TITLE_FONT_SIZE,
    },
    {
        "key": "bold_green",
        "label": "Bold Green",
        "subtitle_preset": "pop_green",
        "title_engine": "drawtext",
        "title_uppercase": False,
        "title_font": "Anton",
        "title_font_key": "anton",
        "title_text_color": "#FFFFFF",
        "title_bg_color": "#0F172A",
        "title_font_size": DEFAULT_TITLE_FONT_SIZE,
    },
    {
        "key": "vivid_pink",
        "label": "Vivid Pink",
        "subtitle_preset": "pop_pink",
        "title_engine": "drawtext",
        "title_uppercase": False,
        "title_font": "Bebas Neue",
        "title_font_key": "bebas_neue",
        "title_text_color": "#FFFFFF",
        "title_bg_color": "#EC4899",
        "title_font_size": DEFAULT_TITLE_FONT_SIZE,
    },
    {
        "key": "minimal_white",
        "label": "Minimal White",
        "subtitle_preset": "minimal_white",
        "title_engine": "drawtext",
        "title_uppercase": False,
        "title_font": "Oswald",
        "title_font_key": DEFAULT_TITLE_FONT_KEY,
        "title_text_color": "#0F172A",
        "title_bg_color": "#FFFFFF",
        "title_font_size": DEFAULT_TITLE_FONT_SIZE,
    },
    {
        "key": "deep_blue",
        "label": "Deep Blue",
        "subtitle_preset": "deep_blue",
        "title_engine": "drawtext",
        "title_uppercase": False,
        "title_font": "Anton",
        "title_font_key": "anton",
        "title_text_color": "#FFFFFF",
        "title_bg_color": "#1D4ED8",
        "title_font_size": DEFAULT_TITLE_FONT_SIZE,
    },
    {
        "key": "clean_black",
        "label": "Clean Black",
        "subtitle_preset": "clean_black",
        "title_engine": "drawtext",
        "title_uppercase": False,
        "title_font": "Open Sans",
        "title_font_key": "open_sans",
        "title_text_color": "#FFFFFF",
        "title_bg_color": "#111827",
        "title_font_size": DEFAULT_TITLE_FONT_SIZE,
    },
]
DEFAULT_STYLE_TEMPLATE_KEY = "amber_gold"
DEFAULT_STYLE_TEMPLATE = next(
    (template for template in STYLE_TEMPLATES if str(template.get("key") or "").strip() == DEFAULT_STYLE_TEMPLATE_KEY),
    STYLE_TEMPLATES[0] if STYLE_TEMPLATES else {},
)
DEFAULT_SUBTITLE_PRESET = str(
    DEFAULT_STYLE_TEMPLATE.get("subtitle_preset") or "classic_yellow"
).strip() or "classic_yellow"
DEFAULT_EDITOR_TITLE_FONT_KEY = str(
    DEFAULT_STYLE_TEMPLATE.get("title_font_key") or DEFAULT_TITLE_FONT_KEY
).strip() or DEFAULT_TITLE_FONT_KEY
DEFAULT_EDITOR_TITLE_TEXT_COLOR = str(
    DEFAULT_STYLE_TEMPLATE.get("title_text_color") or DEFAULT_TITLE_TEXT_COLOR
).strip() or DEFAULT_TITLE_TEXT_COLOR
DEFAULT_EDITOR_TITLE_BG_COLOR = str(
    DEFAULT_STYLE_TEMPLATE.get("title_bg_color") or DEFAULT_TITLE_BG_COLOR
).strip() or DEFAULT_TITLE_BG_COLOR
DEFAULT_EDITOR_TITLE_BG_ALPHA = int(
    DEFAULT_STYLE_TEMPLATE.get("title_bg_alpha")
    if DEFAULT_STYLE_TEMPLATE.get("title_bg_alpha") is not None
    else DEFAULT_TITLE_BG_ALPHA
)
DEFAULT_EDITOR_TITLE_FONT_SIZE = int(
    DEFAULT_STYLE_TEMPLATE.get("title_font_size") or DEFAULT_TITLE_FONT_SIZE
)

STATIC_FONTS_DIR = Path(__file__).resolve().parent / "static" / "fonts"
MINTI_BACKGROUNDS_DIR = Path(__file__).resolve().parent / "static" / "mintibackgrounds"

TITLE_FONTS = OrderedDict(
    [
        (
            "merriweather",
            {
                "label": "Merriweather",
                "css_family": "'Merriweather', serif",
                "path": Path("/usr/share/fonts/opentype/urw-base35/NimbusRoman-Bold.otf"),
            },
        ),
        (
            "anton",
            {
                "label": "Anton",
                "css_family": "'Anton', sans-serif",
                "path": STATIC_FONTS_DIR / "Anton-Regular.ttf",
            },
        ),
        (
            "fjalla_one",
            {
                "label": "Fjalla One",
                "css_family": "'Fjalla One', sans-serif",
                "path": STATIC_FONTS_DIR / "FjallaOne-Regular.ttf",
            },
        ),
        (
            "archivo_black",
            {
                "label": "Archivo Black",
                "css_family": "'Archivo Black', sans-serif",
                "path": STATIC_FONTS_DIR / "ArchivoBlack-Regular.ttf",
            },
        ),
        (
            "teko_bold",
            {
                "label": "Teko Bold",
                "css_family": "'Teko', sans-serif",
                "path": STATIC_FONTS_DIR / "Teko-Bold.ttf",
            },
        ),
        (
            "roboto_slab_black",
            {
                "label": "Roboto Slab Black",
                "css_family": "'Roboto Slab', serif",
                "path": STATIC_FONTS_DIR / "RobotoSlab-Black.ttf",
            },
        ),
        (
            "arimo",
            {
                "label": "Arimo",
                "css_family": "'Arimo', sans-serif",
                "path": STATIC_FONTS_DIR / "Arimo-Regular.ttf",
            },
        ),
        (
            "bebas_neue",
            {
                "label": "Bebas Neue",
                "css_family": "'Bebas Neue', cursive",
                "path": STATIC_FONTS_DIR / "BebasNeue-Regular.ttf",
            },
        ),
        (
            "opensans_sc",
            {
                "label": "Open Sans Semi Condensed",
                "css_family": "'Open Sans SemiCondensed', 'Open Sans', sans-serif",
                "path": STATIC_FONTS_DIR / "OpenSans_SemiCondensed-Medium.ttf",
            },
        ),
        (
            "montserrat",
            {
                "label": "Montserrat",
                "css_family": "'Montserrat', sans-serif",
                "path": STATIC_FONTS_DIR / "Montserrat-VariableFont_wght.ttf",
            },
        ),
        (
            "montserrat_black",
            {
                "label": "Montserrat Black",
                "css_family": "'Montserrat', sans-serif",
                "path": STATIC_FONTS_DIR / "Montserrat-Black.ttf",
            },
        ),
        (
            "oswald_regular",
            {
                "label": "Oswald Regular",
                "css_family": "'Oswald', sans-serif",
                "path": STATIC_FONTS_DIR / "Oswald-Regular.ttf",
            },
        ),
        (
            "oswald_bold",
            {
                "label": "Oswald Bold",
                "css_family": "'Oswald', sans-serif",
                "path": STATIC_FONTS_DIR / "Oswald-Bold.ttf",
            },
        ),
        (
            "roboto",
            {
                "label": "Roboto",
                "css_family": "'Roboto', sans-serif",
                "path": Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
            },
        ),
        (
            "sourcecode",
            {
                "label": "Source Code Pro",
                "css_family": "'Source Code Pro', monospace",
                "path": Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"),
            },
        ),
        (
            "dejavu",
            {
                "label": "DejaVu Sans",
                "css_family": "'DejaVu Sans', sans-serif",
                "path": Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
            },
        ),
    ]
)

FONT_CHOICES = [
    ("open_sans_semi", "Open Sans Semi Condensed", str(STATIC_FONTS_DIR / "OpenSans_SemiCondensed-Medium.ttf"), "'Open Sans SemiCondensed', 'Open Sans', sans-serif"),
    ("arimo", "Arimo", str(STATIC_FONTS_DIR / "Arimo-Regular.ttf"), "'Arimo', sans-serif"),
    ("roboto", "Roboto", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "'Roboto', sans-serif"),
    ("merriweather", "Merriweather", "/usr/share/fonts/opentype/urw-base35/NimbusRoman-Bold.otf", "'Merriweather', serif"),
    ("sourcecode", "Source Code Pro", "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", "'Source Code Pro', monospace"),
    ("dejavu", "DejaVu Sans", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "'DejaVu Sans', sans-serif"),
]

SUB_FONT_CHOICES = [
    ("arimo", "Arimo", str(STATIC_FONTS_DIR / "Arimo-Regular.ttf"), "Arimo"),
    ("open_sans_semi", "Open Sans Semi Condensed", str(STATIC_FONTS_DIR / "OpenSans_SemiCondensed-Medium.ttf"), "Open Sans SemiCondensed"),
    ("anton", "Anton", str(STATIC_FONTS_DIR / "Anton-Regular.ttf"), "Anton"),
    ("bebas_neue", "Bebas Neue", str(STATIC_FONTS_DIR / "BebasNeue-Regular.ttf"), "Bebas Neue"),
    ("montserrat", "Montserrat", str(STATIC_FONTS_DIR / "Montserrat-VariableFont_wght.ttf"), "Montserrat"),
    ("oswald_regular", "Oswald Regular", str(STATIC_FONTS_DIR / "Oswald-Regular.ttf"), "Oswald"),
    ("oswald_bold", "Oswald Bold", str(STATIC_FONTS_DIR / "Oswald-Bold.ttf"), "Oswald"),
    ("fjalla_one", "Fjalla One", str(STATIC_FONTS_DIR / "FjallaOne-Regular.ttf"), "Fjalla One"),
    ("archivo_black", "Archivo Black", str(STATIC_FONTS_DIR / "ArchivoBlack-Regular.ttf"), "Archivo Black"),
    ("teko_bold", "Teko Bold", str(STATIC_FONTS_DIR / "Teko-Bold.ttf"), "Teko"),
    ("roboto_slab_black", "Roboto Slab Black", str(STATIC_FONTS_DIR / "RobotoSlab-Black.ttf"), "Roboto Slab"),
]

SUBTITLE_PILL_FONT_MAP = {
    "classic_yellow": {
        "ass_font_name": "Arimo",
        "font_path": STATIC_FONTS_DIR / "Arimo-Regular.ttf",
    },
    "word_highlight": {
        "ass_font_name": "Open Sans SemiCondensed Medium",
        "font_path": STATIC_FONTS_DIR / "OpenSans_SemiCondensed-Medium.ttf",
    },
    "word_highlight_flame": {
        "ass_font_name": "Open Sans SemiCondensed Medium",
        "font_path": STATIC_FONTS_DIR / "OpenSans_SemiCondensed-Medium.ttf",
    },
    "word_highlight_ocean": {
        "ass_font_name": "Open Sans SemiCondensed Medium",
        "font_path": STATIC_FONTS_DIR / "OpenSans_SemiCondensed-Medium.ttf",
    },
    "green_pop": {
        "ass_font_name": "Montserrat Black",
        "font_path": STATIC_FONTS_DIR / "Montserrat-Black.ttf",
    },
    "silver_pop": {
        "ass_font_name": "Anton",
        "font_path": STATIC_FONTS_DIR / "Anton-Regular.ttf",
    },
    "deep_blue": {
        "ass_font_name": "Anton",
        "font_path": STATIC_FONTS_DIR / "Anton-Regular.ttf",
    },
    "amber_gold": {
        "ass_font_name": "Fjalla One",
        "font_path": STATIC_FONTS_DIR / "FjallaOne-Regular.ttf",
    },
    "sky_cyan": {
        "ass_font_name": "Archivo Black",
        "font_path": STATIC_FONTS_DIR / "ArchivoBlack-Regular.ttf",
    },
    "electric_lime": {
        "ass_font_name": "Teko Bold",
        "font_path": STATIC_FONTS_DIR / "Teko-Bold.ttf",
    },
    "serif_bordo": {
        "ass_font_name": "Roboto Slab Black",
        "font_path": STATIC_FONTS_DIR / "RobotoSlab-Black.ttf",
    },
}

TITLE_FONT_SIZES = [8, 20, 24, 28, 30, 34, 40, 44]
SUB_FONT_SIZES = [8, 9, 10, 12, 14, 16]
SUB_MARGIN_DEFAULT = 405

STATIC_IMG_DIR = Path(__file__).resolve().parent / "static" / "img"
STATIC_USER_IMAGES_DIR = Path(__file__).resolve().parent / "static" / "user_images"
STATIC_USER_AUDIO_DIR = Path(__file__).resolve().parent / "static" / "user_audio"
STATIC_USER_PODCASTS_DIR = Path(__file__).resolve().parent / "static" / "user_podcasts"
STATIC_IMAGE_MAX_BYTES = 5 * 1024 * 1024
STATIC_SUBSCRIBE_ANIMATION_MAX_BYTES = 10 * 1024 * 1024
STATIC_AUDIO_MAX_BYTES = 50 * 1024 * 1024

STATIC_VISUAL_PRESETS = [
    {
        "key": "static_1",
        "label": "1",
        "description": "Koyu mavi arka plan ve altın numara",
        "image_filename": "img/1.png",
        "bg_color": "#0f172a",
        "text_color": "#ffe600",
        "border_color": "#475569",
        "font_size": 260,
    },
    {
        "key": "static_2",
        "label": "2",
        "description": "Gece mavisi tonlarda çerçeveli kare",
        "image_filename": "img/2.png",
        "bg_color": "#1d2766",
        "text_color": "#a5f3fc",
        "border_color": "#38bdf8",
        "font_size": 260,
    },
    {
        "key": "static_3",
        "label": "3",
        "description": "Şarap kırmızısı arka plan, sıcak tonlu sayı",
        "image_filename": "img/3.png",
        "bg_color": "#3b021f",
        "text_color": "#fda4af",
        "border_color": "#fda4af",
        "font_size": 260,
    },
]

BACKGROUND_VISUAL_PRESETS = [
    {
        "key": "bg_1",
        "label": "1",
        "description": "Background 1",
        "image_filename": "1-short_bg_8.png",
    },
    {
        "key": "bg_2",
        "label": "2",
        "description": "Background 2",
        "image_filename": "2-short_bg_8.png",
    },
]

BACKGROUND_VISUAL_PRESETS = [
    {
        "key": "bg_1",
        "label": "1",
        "description": "Background 1",
        "image_filename": "1-short_bg_8.png",
    },
    {
        "key": "bg_2",
        "label": "2",
        "description": "Background 2",
        "image_filename": "2-short_bg_8.png",
    },
]

for entry in STATIC_VISUAL_PRESETS:
    filename = entry.get("image_filename")
    if filename:
        entry["image_path"] = STATIC_IMG_DIR / Path(filename).name
