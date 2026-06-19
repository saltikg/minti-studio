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
SHORTS_DIR = Path(__file__).resolve().parent / "static" / "shorts"
VIDEOS_DIR = Path(__file__).resolve().parent / "videos"
VIDEO_SHORTS_TMP_DIR = Path(os.getenv("VIDEO_SHORTS_TMP_DIR") or (Path(__file__).resolve().parent / "tmp"))
BGCOVER_PATH = VIDEOS_DIR / "1-short_bg_8.png"
FFMPEG_BIN = os.getenv("FFMPEG_BIN") or shutil.which("ffmpeg") or "ffmpeg"
DEFAULT_STORAGE_PLANS = [
    {
        "plan_id": "plan_free",
        "label": "Free",
        "quota_bytes": 2 * 1024 ** 3,
        "price_monthly": 0,
        "monthly_export_limit": 10,
        "monthly_transcription_minutes": 60,
        "render_priority": 0,
        "max_concurrent_jobs": 1,
        "is_active": True,
        "sort_order": 0,
    },
    {
        "plan_id": "plan_2gb",
        "label": "Starter",
        "quota_bytes": 2 * 1024 ** 3,
        "price_monthly": 9,
        "monthly_export_limit": 30,
        "monthly_transcription_minutes": 180,
        "render_priority": 10,
        "max_concurrent_jobs": 2,
        "is_active": True,
        "sort_order": 1,
    },
    {
        "plan_id": "plan_10gb",
        "label": "Creator",
        "quota_bytes": 10 * 1024 ** 3,
        "price_monthly": 19,
        "monthly_export_limit": 90,
        "monthly_transcription_minutes": 540,
        "render_priority": 20,
        "max_concurrent_jobs": 2,
        "is_active": True,
        "sort_order": 2,
    },
    {
        "plan_id": "plan_100gb",
        "label": "Studio",
        "quota_bytes": 100 * 1024 ** 3,
        "price_monthly": 49,
        "monthly_export_limit": 270,
        "monthly_transcription_minutes": 1620,
        "render_priority": 30,
        "max_concurrent_jobs": 3,
        "is_active": True,
        "sort_order": 3,
    },
]
DEFAULT_USER_STORAGE_LIMIT = 3 * 1024 ** 3
DEFAULT_TITLE_TEXT_COLOR = "#000000"
DEFAULT_SUBTITLE_TEXT_COLOR = "#FFFFFF"
DEFAULT_SUBTITLE_BG_COLOR = "#000000"
DEFAULT_TITLE_BG_ALPHA = 92
DEFAULT_SUBTITLE_BG_ALPHA = 85
DEFAULT_SUBTITLE_TEXT_ALPHA = 100
WORKER_CONCURRENCY = max(1, int(os.getenv("WORKER_CONCURRENCY", "1") or "1"))
JOB_TIMEOUT_SECONDS = max(60, int(os.getenv("JOB_TIMEOUT_SECONDS", "600") or "600"))
JOB_POLL_INTERVAL_SECONDS = max(1.0, float(os.getenv("JOB_POLL_INTERVAL_SECONDS", "2") or "2"))
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

# Max duration for ffmpeg calls (can override via env). Raised to handle longer clips.
# Leave timeout unset (None) unless explicitly provided
_timeout_env = os.getenv("FFMPEG_TIMEOUT")
FFMPEG_TIMEOUT = int(_timeout_env) if (_timeout_env and _timeout_env.isdigit()) else None
MAX_CLIP_LEN = int(os.getenv("MAX_CLIP_LEN", "120"))  # safety cap for per-clip duration
SHORT_MIN_LEN = float(os.getenv("SHORT_MIN_LEN", "40"))
SHORT_MAX_LEN = float(os.getenv("SHORT_MAX_LEN", str(MAX_CLIP_LEN)))
OPENAI_MODEL = os.getenv("OPENAI_MODEL_GPT", "gpt-4o-mini")
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
IG_REDIRECT_URI = os.getenv("IG_REDIRECT_URI")
IG_OAUTH_SCOPES = os.getenv(
    "IG_OAUTH_SCOPES",
    "instagram_business_basic,instagram_business_content_publish",
)
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
DEFAULT_SUB_FONT_SIZE = 9
DEFAULT_TITLE_MARGIN = 220
DEFAULT_TITLE_BG_COLOR = "#FFD600"
DEFAULT_VIDEO_OVERLAY_OFFSET = int(os.getenv("VIDEO_OVERLAY_OFFSET_DEFAULT", "400"))

STATIC_FONTS_DIR = Path(__file__).resolve().parent / "static" / "fonts"

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
]

TITLE_FONT_SIZES = [8, 20, 24, 28, 30, 34, 40, 44]
SUB_FONT_SIZES = [8, 9, 10, 12, 14, 16]
SUB_MARGIN_DEFAULT = 80

STATIC_IMG_DIR = Path(__file__).resolve().parent / "static" / "img"
STATIC_USER_IMAGES_DIR = Path(__file__).resolve().parent / "static" / "user_images"
STATIC_USER_AUDIO_DIR = Path(__file__).resolve().parent / "static" / "user_audio"
STATIC_USER_PODCASTS_DIR = Path(__file__).resolve().parent / "static" / "user_podcasts"
STATIC_IMAGE_MAX_BYTES = 5 * 1024 * 1024
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
