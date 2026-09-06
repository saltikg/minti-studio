import json
import errno
import math
import re
import shutil
import string
import subprocess
import secrets
import tempfile
import threading
import time
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlencode, urlparse, parse_qsl, parse_qs
from uuid import uuid4

from google.auth.exceptions import RefreshError

from flask import abort, current_app, flash, g, jsonify, redirect, render_template, request, session, url_for
import requests
from werkzeug.exceptions import HTTPException
from werkzeug.utils import secure_filename

from app.video_shorts import video_shorts_bp
from app.video_shorts.services.brands import create_brand as create_brand_record, current_brand_id, ensure_brand_schema
from app.video_shorts.config import (
    BGCOVER_PATH,
    BACKGROUND_VISUAL_PRESETS,
    DEFAULT_SUB_FONT_KEY,
    DEFAULT_SUB_FONT_SIZE,
    DEFAULT_SUBTITLE_BG_ALPHA,
    DEFAULT_SUBTITLE_BG_COLOR,
    DEFAULT_SUBTITLE_PRESET,
    DEFAULT_STYLE_TEMPLATE_KEY,
    SUBTITLE_HIGHLIGHT_COLOR,
    SUBTITLE_PRESETS,
    STYLE_TEMPLATES,
    DEFAULT_TITLE_BG_COLOR,
    DEFAULT_TITLE_BG_ALPHA,
    DEFAULT_EDITOR_TITLE_BG_ALPHA,
    DEFAULT_EDITOR_TITLE_BG_COLOR,
    DEFAULT_EDITOR_TITLE_FONT_KEY,
    DEFAULT_EDITOR_TITLE_FONT_SIZE,
    DEFAULT_EDITOR_TITLE_TEXT_COLOR,
    DEFAULT_SUBTITLE_TEXT_ALPHA,
    DEFAULT_TITLE_TEXT_COLOR,
    DEFAULT_TITLE_FONT_KEY,
    DEFAULT_TITLE_FONT_SIZE,
    DEFAULT_TITLE_MARGIN,
    DEFAULT_SUBTITLE_TEXT_COLOR,
    DEFAULT_VIDEO_OVERLAY_OFFSET,
    DEFAULT_USER_STORAGE_LIMIT,
    DEFAULT_USER_PLAN_ID,
    FFMPEG_RENDER_TIMEOUT,
    FFMPEG_SHORT_TIMEOUT,
    FFPROBE_TIMEOUT,
    FB_API_BASE,
    FB_APP_ID,
    FB_APP_SECRET,
    FB_OAUTH_SCOPES,
    FB_REDIRECT_URI,
    FB_TARGET_PAGE_ID,
    IG_API_BASE,
    IG_APP_ID,
    IG_APP_SECRET,
    IG_AUTH_BASE,
    IG_GRAPH_API_BASE,
    IG_REDIRECT_URI,
    IG_OAUTH_SCOPES,
    OPENAI_MODEL,
    CLIP_PLAN_COOLDOWN_SECONDS,
    CLIP_PLAN_MAX_RUNS_PER_VIDEO,
    SHORTS_CATEGORY_OPTIONS,
    SHORTS_DIR,
    SIGNUPS_ENABLED,
    SUB_FONT_CHOICES,
    SUB_FONT_SIZES,
    SUB_MARGIN_DEFAULT,
    TITLE_FONT_SIZES,
    TITLE_FONTS,
    VIDEOS_DIR,
    YT_DLP_COOKIES,
    YOUTUBE_CLIENT_ID,
    YOUTUBE_CLIENT_SECRET,
    YOUTUBE_REDIRECT_URI,
    TIKTOK_AUTH_BASE,
    TIKTOK_API_BASE,
    TIKTOK_CLIENT_KEY,
    TIKTOK_CLIENT_SECRET,
    TIKTOK_OAUTH_SCOPES,
    TIKTOK_REDIRECT_URI,
    STATIC_VISUAL_PRESETS,
    STATIC_IMG_DIR,
    STATIC_USER_IMAGES_DIR,
    STATIC_USER_AUDIO_DIR,
    STATIC_USER_PODCASTS_DIR,
    STATIC_AUDIO_MAX_BYTES,
    _openai_client,
)
import duckdb
from app.video_shorts.services.clip_planner_agents import propose_clips_with_agents
from app.video_shorts.services.clip_planner_agents_v2 import propose_clips_with_agents_v2
from app.video_shorts.services.clip_planner_agents_v3 import propose_clips_with_agents_v3
from app.video_shorts.services.clip_planner_agents_v4 import propose_clips_with_agents_v4
from app.video_shorts.services.clip_planning import _fallback_clip_plan
from app.video_shorts.services.clip_title import generate_clip_title
from app.video_shorts.services.clip_title import _detect_title_language
from app.video_shorts.services.compositor import _build_static_visual_clip, _compose_trimmed_with_background, _cut_clip, _sanitize_text_for_overlay
from app.video_shorts.services.db import (
    _ensure_transcript_schema,
    _ensure_video_crop_schema,
    ensure_auth_user_schema,
    ensure_categories_schema,
    ensure_pricing_interest_schema,
    ensure_postgres_youtube_transcripts_id_default,
    ensure_short_share_links_schema,
    ensure_static_images_schema,
    ensure_storage_user_schema,
    ensure_channel_owner_schema,
    get_db,
    get_db_readonly,
    table_columns,
)
from app.video_shorts.services.auth_protection import RateLimitRule, check_rate_limits
from app.video_shorts.services.background_preferences import load_background_preference
from app.video_shorts.services.onboarding_magic_links import (
    mint_onboarding_magic_link,
    normalize_outreach_language,
)
from app.video_shorts.services.trial_copy import (
    DEFAULT_SHARE_TRIAL_DAYS,
    LEGACY_SHARE_TRIAL_DAYS,
    normalize_trial_days,
    trial_duration_text,
)
from app.video_shorts.services.user_preferences import (
    load_user_preference,
    load_user_bool_preference,
    save_user_preference,
    save_user_bool_preference,
)
from app.video_shorts.services.autopilot_leads import (
    _get_or_create_local_uploads_channel,
    autopilot_leads_table_ready,
    provision_discovery_lead_email,
)
from app.video_shorts.services.admin_operation_scope import (
    request_admin_operation_scope,
    resolve_admin_operation_request_scope,
    set_request_admin_operation_scope,
)

# Read-time filter for common internet scanner probes that otherwise drown the
# admin errors view. These rows remain stored in user_events; only the default
# admin listing hides 404s whose request path clearly matches one of these
# bot-scan signatures. Use ?include_noise=1 on /admin/errors to show all rows.
NOISE_404_PATTERNS = [
    r"\.php",
    r"/wp-",
    r"/wp-content",
    r"/wp-includes",
    r"/wp-admin",
    r"/cgi-bin",
    r"/adminer",
    r"/xmlrpc",
    r"phpmyadmin",
    r"/\.well-known/",
    r"/\.env",
    r"/vendor/",
]

# This is the only route family eligible to activate a target lead context.
# It covers every current and future video-bound editor endpoint without making
# a selected operation scope active on unrelated admin browsing requests.
ADMIN_OPERATION_EDITOR_ROUTE_PREFIX = "/video_shorts/generate/<int:video_pk>"

# SQL LIKE equivalents for the default admin-errors filter. Keep this list
# aligned with NOISE_404_PATTERNS above; it is intentionally conservative.
NOISE_404_PATH_LIKE_PATTERNS = [
    "%.php%",
    "%/wp-%",
    "%/wp-content%",
    "%/wp-includes%",
    "%/wp-admin%",
    "%/cgi-bin%",
    "%/adminer%",
    "%/xmlrpc%",
    "%phpmyadmin%",
    "%/.well-known/%",
    "%/.env%",
    "%/vendor/%",
]
from app.video_shorts.services.user_events import prepare_transcript_completed_transition, track_event
from app.video_shorts.services.billing import (
    STRIPE_PUBLISHABLE_KEY,
    load_billing_user_state,
    stripe_is_configured,
    user_has_managed_subscription,
)
from app.video_shorts.services.media_utils import (
    _cleanup_resolved_source_video,
    _find_source_video,
    _format_time_label,
    _resolve_ffmpeg,
    _probe_video_stream_info,
    _resolve_source_video,
    MediaSubprocessTimeoutError,
    normalize_source_video_for_streaming,
    run_media_subprocess,
    scale_media_timeout,
)
from app.video_shorts.services.disk_guard import (
    USER_FACING_DISK_GUARD_MESSAGE,
    disk_guard_triggered,
)
from app.video_shorts.services.description_prompt_settings import (
    DEFAULT_DESCRIPTION_PROMPT,
    DESCRIPTION_CONTEXT_PADDING_SECONDS,
    load_description_settings,
)
from app.video_shorts.services.system_backgrounds import (
    choose_deterministic_system_background,
    make_system_background_key,
    list_system_background_paths,
    resolve_system_background_path,
    system_background_static_filename,
)
from app.video_shorts.services.system_subscribe_animations import (
    choose_deterministic_system_subscribe,
    is_system_subscribe_key,
    list_system_subscribe_paths,
    make_system_subscribe_key,
    resolve_system_subscribe_path,
    system_subscribe_static_filename,
)
from app.video_shorts.services.storage import (
    StorageEntry,
    build_storage_reference,
    get_media_storage,
    invalidate_cloudfront_paths,
    is_storage_reference,
    public_url_for_stored_media,
    resolve_stored_media,
)
from app.video_shorts.services.transcript_service import (
    _build_ass_karaoke_for_clip,
    _build_word_highlight_caption_overlay,
    _build_srt_for_clip,
    _build_srt_from_text,
    _fetch_transcript,
    _normalize_segments_for_use,
    _transcribe_with_whisper,
    build_transcript_for_range,
)
from app.video_shorts.routes.auth import require_admin
from src.trends.token_store_db import connect_store, relation_missing
from app.video_shorts.services.non_speech_rules import load_non_speech_rules, add_non_speech_keyword

DEFAULT_VIDEO_DATE_TOP = 1006
WHISPER_USD_PER_MINUTE = 0.006
S3_USD_PER_GB_MONTH = 0.023
ADMIN_COST_APPROX_MONTH_THRESHOLD = 1.0
from app.video_shorts.services.non_speech_overrides import (
    load_non_speech_overrides,
    save_non_speech_overrides,
)
from app.video_shorts.services.clip_plan_focus_prompts import (
    ALL_FOCUS_CATEGORIES,
    get_focus_category_options,
    normalize_focus_categories,
    get_plan_focus_label,
    normalize_plan_focus,
)
from app.video_shorts.services.planner_rules_v4 import load_planner_rules_v4
from app.video_shorts.services.instagram_api import (
    InstagramActionError,
    subscribe_instagram_comment_webhooks,
)
from app.video_shorts.services.instagram_queue import enqueue_instagram_clip, load_instagram_queue_map
from app.video_shorts.services.facebook_queue import enqueue_facebook_clip, load_facebook_queue_map
from app.video_shorts.services.tiktok_queue import enqueue_tiktok_clip, load_tiktok_queue_map
from app.video_shorts.services.temp_cleanup import cleanup_video_shorts_temp_dir, ensure_video_shorts_tmp_dir
from app.video_shorts.services.generated_video_lifecycle import _first_non_empty, upsert_generated_video_record
from app.video_shorts.services.youtube_oauth import (
    build_oauth_flow,
    clear_refresh_token,
    fetch_video_statuses,
    get_connected_channel_info,
    has_refresh_token,
    is_reauth_required,
    resolve_stored_token_owner_brand,
    store_refresh_token,
    update_video_with_refresh_token,
    upload_video_with_refresh_token,
)
from app.video_shorts.services.shorts_overview_quota import get_shorts_overview_quota_state
from app.video_shorts.services.timezones import DEFAULT_TIME_ZONE, TIMEZONE_LABELS, TIMEZONE_OPTIONS
from app.video_shorts.services.render_jobs import (
    build_input_hash,
    cancel_job,
    clear_done_job_cache_for_plan,
    enqueue_render_job,
    get_job,
    invalidate_done_job_cache,
    is_render_job_cache_valid,
)
from app.video_shorts.services.usage_metering import (
    add_transcription_minutes,
    check_transcription_quota,
    load_storage_plan_catalog as load_usage_storage_plan_catalog,
    release_export,
    reserve_export,
)
from src.trends.instagram_tokens import (
    InstagramTokenStoreError,
    clear_instagram_token,
    get_instagram_data,
    get_instagram_credentials,
    refresh_instagram_token_if_needed,
    store_instagram_token,
)
from src.trends.tiktok_tokens import (
    TikTokTokenStoreError,
    clear_tiktok_token,
    get_tiktok_data,
    store_tiktok_token,
)
from src.trends.facebook_page_tokens import (
    FacebookTokenStoreError,
    clear_facebook_page_token,
    get_facebook_page_data,
    store_facebook_page_token,
)
from app.video_shorts.youtube_api import YoutubeApiError, extract_video_id, fetch_video_metadata, fetch_video_stats

PST_ZONE = ZoneInfo(DEFAULT_TIME_ZONE)
BRAND_SUBSCRIBE_OVERLAY_DIR = Path(__file__).resolve().parent.parent / "static" / "brand_subscribe_overlays"
HIDE_CLIP_COACHMARK_PREFERENCE_KEY = "hide_clip_coachmark"

_TRANSCRIBE_JOB_LOCK = threading.Lock()
_TRANSCRIBE_JOB_STATE: Dict[int, Dict[str, Any]] = {}
_TRANSCRIBE_STATUS_DIR = Path("/tmp/video_shorts_transcribe_status")
_PLAN_JOB_LOCK = threading.Lock()
_PLAN_JOB_STATE: Dict[int, Dict[str, Any]] = {}
_PLAN_STATUS_DIR = Path("/tmp/video_shorts_plan_status")


def _transcribe_status_file(video_pk: int) -> Path:
    return _TRANSCRIBE_STATUS_DIR / f"{int(video_pk)}.json"


def _plan_status_file(video_pk: int) -> Path:
    return _PLAN_STATUS_DIR / f"{int(video_pk)}.json"


def _coerce_transcribe_state_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _coerce_transcribe_state_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_coerce_transcribe_state_value(v) for v in value]
    return str(value)


def _sanitize_transcribe_state(state: Dict[str, Any]) -> Dict[str, Any]:
    return {str(key): _coerce_transcribe_state_value(value) for key, value in (state or {}).items()}


def _persist_transcribe_job_state(video_pk: int, state: Dict[str, Any]) -> None:
    try:
        _TRANSCRIBE_STATUS_DIR.mkdir(parents=True, exist_ok=True)
        target = _transcribe_status_file(video_pk)
        temp = target.with_suffix(".tmp")
        temp.write_text(json.dumps(_sanitize_transcribe_state(state), ensure_ascii=False), encoding="utf-8")
        temp.replace(target)
    except Exception:
        pass


def _load_transcribe_job_state(video_pk: int) -> Dict[str, Any]:
    try:
        target = _transcribe_status_file(video_pk)
        if not target.exists():
            return {}
        return json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _sanitize_plan_state(state: Dict[str, Any]) -> Dict[str, Any]:
    return {str(key): _coerce_transcribe_state_value(value) for key, value in (state or {}).items()}


def _persist_plan_job_state(video_pk: int, state: Dict[str, Any]) -> None:
    try:
        _PLAN_STATUS_DIR.mkdir(parents=True, exist_ok=True)
        target = _plan_status_file(video_pk)
        temp = target.with_suffix(".tmp")
        temp.write_text(json.dumps(_sanitize_plan_state(state), ensure_ascii=False), encoding="utf-8")
        temp.replace(target)
    except Exception:
        pass


def _load_plan_job_state(video_pk: int) -> Dict[str, Any]:
    try:
        target = _plan_status_file(video_pk)
        if not target.exists():
            return {}
        return json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _parse_plan_state_datetime(raw_value: Any) -> Optional[datetime]:
    text = str(raw_value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _clip_plan_cap_message() -> str:
    return (
        f"You've already generated AI suggestions for this video {CLIP_PLAN_MAX_RUNS_PER_VIDEO} times. "
        "Remove the existing ones and refine manually, or edit clips directly."
    )


def _clip_plan_cooldown_message() -> str:
    return "Suggestions were just generated — give it a moment before regenerating."


def _clip_plan_block_info(state: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    normalized = dict(state or {})
    run_count = int(normalized.get("run_count") or 0)
    if run_count >= CLIP_PLAN_MAX_RUNS_PER_VIDEO:
        return {
            "reason": "cap",
            "message": _clip_plan_cap_message(),
            "run_count": run_count,
        }
    completed_at = _parse_plan_state_datetime(normalized.get("completed_at"))
    if completed_at is not None and CLIP_PLAN_COOLDOWN_SECONDS > 0:
        elapsed = (datetime.utcnow() - completed_at).total_seconds()
        if elapsed < CLIP_PLAN_COOLDOWN_SECONDS:
            return {
                "reason": "cooldown",
                "message": _clip_plan_cooldown_message(),
                "run_count": run_count,
                "cooldown_remaining_seconds": max(0, int(CLIP_PLAN_COOLDOWN_SECONDS - elapsed)),
            }
    if normalized.get("running"):
        return {
            "reason": "running",
            "message": "Clip plan generation already running.",
            "run_count": run_count,
        }
    return None


def _current_plan_job_state(video_pk: int) -> Dict[str, Any]:
    state = _load_plan_job_state(video_pk)
    if state:
        return _sanitize_plan_state(state)
    with _PLAN_JOB_LOCK:
        return dict(_PLAN_JOB_STATE.get(video_pk) or {})


def _set_plan_job_state_locked(video_pk: int, state: Dict[str, Any]) -> Dict[str, Any]:
    normalized = _sanitize_plan_state(state)
    _PLAN_JOB_STATE[video_pk] = normalized
    _persist_plan_job_state(video_pk, normalized)
    return normalized


def local_to_utc_rfc3339(local_str: str, tz_name: Optional[str] = None) -> str:
    dt_local = datetime.fromisoformat(local_str)
    if dt_local.tzinfo is None:
        tz_label = tz_name or DEFAULT_TIME_ZONE
        try:
            tz = ZoneInfo(tz_label)
        except Exception:
            tz = PST_ZONE
        dt_local = dt_local.replace(tzinfo=tz)
    dt_utc = dt_local.astimezone(timezone.utc).replace(microsecond=0)
    return dt_utc.isoformat().replace("+00:00", "Z")


def _parse_to_utc(value):
    if not value:
        return None

    # Parse
    if isinstance(value, str):
        try:
            if value.endswith("Z"):
                dt = datetime.fromisoformat(value[:-1] + "+00:00")
            else:
                dt = datetime.fromisoformat(value)
        except Exception:
            return None
    elif isinstance(value, datetime):
        dt = value
    else:
        return None

    # Normalize timezone
    if dt.tzinfo is None:
        # IMPORTANT: naive ise bunun local mi UTC mi oldugunu bilmek lazim.
        # Bu projede naive publish_at (UI input) local PST kabul edilmeli.
        dt = dt.replace(tzinfo=PST_ZONE)

    return dt.astimezone(timezone.utc)


def _parse_iso_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        if value.endswith("Z"):
            return datetime.fromisoformat(value[:-1] + "+00:00")
        return datetime.fromisoformat(value)
    except Exception:
        return None


def _is_token_expired(expires_at: Optional[str]) -> bool:
    dt = _parse_iso_datetime(expires_at)
    if not dt:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt <= datetime.now(timezone.utc)


def _build_tiktok_scopes() -> List[str]:
    scopes_raw = TIKTOK_OAUTH_SCOPES or ""
    scopes = [s.strip() for s in scopes_raw.split(",") if s and s.strip()]
    if not scopes:
        scopes = ["user.info.basic", "video.upload"]
    allowlist = {"user.info.basic", "video.upload", "video.publish"}
    return [s for s in scopes if s in allowlist]


def _find_latest_publish(entries: List[Dict[str, Any]]) -> Tuple[Optional[str], Optional[datetime]]:
    latest_iso = None
    latest_dt = None
    if not entries:
        return None, None
    for entry in entries:
        if entry.get("publish_status") != "scheduled":
            continue
        publish_val = entry.get("publish_at_iso") or entry.get("publish_at")
        if not publish_val:
            continue
        dt = _parse_to_utc(publish_val)
        if not dt:
            continue
        if latest_dt is None or dt > latest_dt:
            latest_dt = dt
            latest_iso = publish_val
    return latest_iso, latest_dt


LEGACY_TITLE_FONT_KEY_MAP = {
    "open_sans_semi": "opensans_sc",
}


def _resolve_title_font_key(font_key: Optional[str]) -> str:
    candidate = font_key or DEFAULT_TITLE_FONT_KEY
    candidate = LEGACY_TITLE_FONT_KEY_MAP.get(candidate, candidate)
    if candidate in TITLE_FONTS:
        return candidate
    fallback = DEFAULT_TITLE_FONT_KEY if DEFAULT_TITLE_FONT_KEY in TITLE_FONTS else next(iter(TITLE_FONTS), DEFAULT_TITLE_FONT_KEY)
    return fallback


def _build_title_font_choice(font_key: Optional[str]) -> Optional[Dict[str, Any]]:
    resolved_key = _resolve_title_font_key(font_key)
    font_cfg = TITLE_FONTS.get(resolved_key)
    if not font_cfg:
        return None
    return {
        "key": resolved_key,
        "label": font_cfg["label"],
        "path": str(font_cfg["path"]),
        "css": font_cfg["css_family"],
    }


def _resolve_title_template_behavior(
    *,
    subtitle_preset: Optional[str],
    title_font_key: Optional[str],
) -> Dict[str, Any]:
    resolved_font_key = _resolve_title_font_key(title_font_key)
    preset_key = str(subtitle_preset or "").strip()
    for template in STYLE_TEMPLATES:
        if str(template.get("subtitle_preset") or "").strip() != preset_key:
            continue
        template_font_key = _resolve_title_font_key(template.get("title_font_key"))
        if template_font_key != resolved_font_key:
            continue
        return {
            "title_engine": str(template.get("title_engine") or "drawtext").strip() or "drawtext",
            "title_uppercase": bool(template.get("title_uppercase")),
        }
    return {
        "title_engine": "drawtext",
        "title_uppercase": False,
    }


def _match_style_template_key(
    *,
    subtitle_preset: Optional[str],
    title_font_key: Optional[str],
    title_text_color: Optional[str],
    title_bg_color: Optional[str],
    title_bg_alpha: Optional[int],
    title_font_size: Optional[int],
) -> Optional[str]:
    resolved_preset = str(subtitle_preset or "").strip()
    resolved_font_key = _resolve_title_font_key(title_font_key)
    resolved_title_text = _normalize_hex_color(title_text_color, DEFAULT_EDITOR_TITLE_TEXT_COLOR)
    resolved_title_bg = _normalize_hex_color(title_bg_color, DEFAULT_EDITOR_TITLE_BG_COLOR)
    resolved_title_bg_alpha = _normalize_alpha_percent(title_bg_alpha, DEFAULT_EDITOR_TITLE_BG_ALPHA)
    try:
        resolved_title_size = int(title_font_size or DEFAULT_EDITOR_TITLE_FONT_SIZE)
    except Exception:
        resolved_title_size = DEFAULT_EDITOR_TITLE_FONT_SIZE
    for template in STYLE_TEMPLATES:
        if str(template.get("subtitle_preset") or "").strip() != resolved_preset:
            continue
        if _resolve_title_font_key(template.get("title_font_key")) != resolved_font_key:
            continue
        if _normalize_hex_color(template.get("title_text_color"), DEFAULT_EDITOR_TITLE_TEXT_COLOR) != resolved_title_text:
            continue
        if _normalize_hex_color(template.get("title_bg_color"), DEFAULT_EDITOR_TITLE_BG_COLOR) != resolved_title_bg:
            continue
        if _normalize_alpha_percent(template.get("title_bg_alpha"), DEFAULT_EDITOR_TITLE_BG_ALPHA) != resolved_title_bg_alpha:
            continue
        try:
            template_title_size = int(template.get("title_font_size") or DEFAULT_EDITOR_TITLE_FONT_SIZE)
        except Exception:
            template_title_size = DEFAULT_EDITOR_TITLE_FONT_SIZE
        if template_title_size != resolved_title_size:
            continue
        return str(template.get("key") or "").strip() or None
    return None


def _format_publish_display(value, tz_name: Optional[str] = None):
    dt = _parse_to_utc(value)
    if not dt:
        return None
    zone_name = tz_name or DEFAULT_TIME_ZONE
    try:
        tz = ZoneInfo(zone_name)
    except Exception:
        tz = PST_ZONE
    local_dt = dt.astimezone(tz)
    return local_dt.strftime("%Y-%m-%d %I:%M %p %Z")


def _format_schedule_date(value: Optional[str]) -> Optional[str]:
    dt = _parse_to_utc(value)
    if not dt:
        return None
    return dt.strftime("%Y-%m-%d")


def _format_simple_datetime(value: Optional[str]) -> Optional[str]:
    dt = _parse_iso_datetime(value)
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %I:%M %p")


def _joined_transcript_tr(segments: List[Dict[str, Any]]) -> str:
    parts: List[str] = []
    for seg in (segments or []):
        txt = seg.get("tr_text") or seg.get("text") or seg.get("ar_text") or ""
        if txt:
            parts.append(str(txt))
    return " ".join(parts).strip()


def _to_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except Exception:
        return None


def _normalize_hex_color(value: Any, default: str) -> str:
    if not value:
        return default
    text = str(value).strip()
    if text.startswith("#"):
        text = text[1:]
    if len(text) != 6 or any(ch not in string.hexdigits for ch in text):
        return default
    return f"#{text.upper()}"


def _normalize_alpha_percent(value: Any, default: int = DEFAULT_TITLE_BG_ALPHA) -> int:
    try:
        alpha = int(float(value))
    except Exception:
        alpha = int(default)
    return max(0, min(100, alpha))


def _normalize_optional_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"0", "false", "no", "off", "f", ""}:
        return False
    if text in {"1", "true", "yes", "on", "t"}:
        return True
    return bool(value)


def _format_size_bytes(num: int) -> str:
    if num is None:
        return "0 B"
    size = float(num)
    units = ["B", "KB", "MB", "GB", "TB"]
    idx = 0
    while size >= 1024 and idx < len(units) - 1:
        size /= 1024
        idx += 1
    if idx == 0:
        return f"{int(size)} {units[idx]}"
    return f"{size:.2f} {units[idx]}"


def _get_user_storage_usage(conn, user_id: str) -> Dict[str, int]:
    row = conn.execute(
        """
        SELECT
            u.custom_limit_bytes,
            p.quota_bytes,
            COALESCE(SUM(a.size_bytes), 0) AS used_bytes
        FROM shorts_users u
        LEFT JOIN shorts_storage_plans p ON p.plan_id = u.plan_id
        LEFT JOIN shorts_storage_assets a
          ON a.user_id = u.id AND (a.status = 'active' OR a.status IS NULL)
        WHERE u.id = ?
        GROUP BY u.custom_limit_bytes, p.quota_bytes
        """,
        [user_id],
    ).fetchone()
    if not row:
        return {"used_bytes": 0, "limit_bytes": DEFAULT_USER_STORAGE_LIMIT, "percent": 0}
    limit_bytes = row[0] or row[1] or DEFAULT_USER_STORAGE_LIMIT
    used_bytes = int(row[2] or 0)
    percent = int(min(100, (used_bytes / limit_bytes * 100))) if limit_bytes else 0
    return {"used_bytes": used_bytes, "limit_bytes": limit_bytes, "percent": percent}


def _quota_block_message(limit_label: str, used_label: str) -> str:
    return (
        "Storage full. Please upgrade or delete files. "
        f"Used {used_label} of {limit_label}."
    )


def _duration_minutes(duration_seconds: Any) -> float:
    try:
        seconds = float(duration_seconds or 0)
    except Exception:
        seconds = 0.0
    if seconds <= 0:
        return 0.0
    return round(seconds / 60.0, 2)


def _format_transcription_minutes_label(minutes: Any) -> str:
    try:
        rounded = int(round(float(minutes or 0)))
    except Exception:
        rounded = 0
    if rounded == 1:
        return "1 minute"
    return f"{rounded} minutes"


def _transcription_quota_message(duration_seconds: Any, remaining_minutes: Any) -> str:
    needed_minutes = _duration_minutes(duration_seconds)
    return (
        f"This video is {_format_transcription_minutes_label(needed_minutes)}, "
        f"but you have {_format_transcription_minutes_label(remaining_minutes)} of transcription left this month."
    )


def _transcription_quota_error_for_user(user_id: str, duration_seconds: Any) -> str | None:
    needed_minutes = _duration_minutes(duration_seconds)
    if needed_minutes <= 0:
        return None
    quota = check_transcription_quota(user_id, needed_minutes)
    if quota.get("allowed", False):
        return None
    return _transcription_quota_message(duration_seconds, quota.get("remaining_minutes"))


def _upsert_storage_asset(
    file_key: str,
    file_path: str,
    file_type: str,
    size_bytes: int,
    user_id: Optional[str],
    brand_id: Optional[str] = None,
) -> None:
    if not file_key or not file_path:
        return
    conn = get_db()
    ensure_storage_user_schema(conn)
    try:
        asset_columns = table_columns(conn, "shorts_storage_assets")
        if "brand_id" in asset_columns:
            conn.execute(
                """
                INSERT INTO shorts_storage_assets (file_key, file_path, file_type, size_bytes, user_id, brand_id, status, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 'active', now())
                ON CONFLICT(file_key)
                DO UPDATE SET
                  file_path = excluded.file_path,
                  file_type = excluded.file_type,
                  size_bytes = excluded.size_bytes,
                  user_id = COALESCE(shorts_storage_assets.user_id, excluded.user_id),
                  brand_id = COALESCE(shorts_storage_assets.brand_id, excluded.brand_id),
                  status = COALESCE(shorts_storage_assets.status, 'active'),
                  updated_at = now()
                """,
                [file_key, file_path, file_type, size_bytes, user_id, brand_id],
            )
        else:
            conn.execute(
                """
                INSERT INTO shorts_storage_assets (file_key, file_path, file_type, size_bytes, user_id, status, updated_at)
                VALUES (?, ?, ?, ?, ?, 'active', now())
                ON CONFLICT(file_key)
                DO UPDATE SET
                  file_path = excluded.file_path,
                  file_type = excluded.file_type,
                  size_bytes = excluded.size_bytes,
                  user_id = COALESCE(shorts_storage_assets.user_id, excluded.user_id),
                  status = COALESCE(shorts_storage_assets.status, 'active'),
                  updated_at = now()
                """,
                [file_key, file_path, file_type, size_bytes, user_id],
            )
        conn.commit()
    finally:
        conn.close()


def _update_storage_asset_label(file_key: str, label: Optional[str]) -> None:
    if not file_key:
        return
    clean = (label or "").strip() or None
    conn = get_db()
    ensure_storage_user_schema(conn)
    try:
        try:
            conn.execute(
                "UPDATE shorts_storage_assets SET label = ?, updated_at = now() WHERE file_key = ?",
                [clean, file_key],
            )
            conn.commit()
        except Exception:
            # Older schema without label column or transient DB issue.
            pass
    finally:
        conn.close()


_ALLOWED_STATIC_AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}


def _user_media_storage_key(kind: str, user_id: str, filename: str) -> str:
    clean_name = Path(filename or "").name
    if kind == "image":
        return f"user_images/{user_id}/{clean_name}"
    if kind == "podcast":
        return f"user_podcasts/{user_id}/{clean_name}"
    return f"user_audio/{user_id}/{clean_name}"


def _audio_sidecar_key(kind: str, user_id: str, filename: str) -> str:
    return f"{_user_media_storage_key(kind, user_id, filename)}.meta.json"


def _audio_sidecar_path(audio_path: Path) -> Path:
    return audio_path.with_name(f"{audio_path.name}.meta.json")


def _legacy_media_path(kind: str, user_id: str, filename: str) -> Path:
    clean_name = Path(filename or "").name
    if kind == "podcast":
        return (STATIC_USER_PODCASTS_DIR / user_id / clean_name).resolve()
    if kind == "image":
        return (STATIC_USER_IMAGES_DIR / user_id / clean_name).resolve()
    return (STATIC_USER_AUDIO_DIR / user_id / clean_name).resolve()


def _user_image_public_url(user_id: str, filename: str) -> str:
    key = _user_media_storage_key("image", user_id, filename)
    storage = get_media_storage()
    local_path = STATIC_USER_IMAGES_DIR / user_id / Path(filename).name
    resolved = storage.resolve_local_or_s3(key, fallback_local_paths=[local_path])
    return resolved.public_url or get_media_storage("local").public_url(key)


def _humanize_visual_asset_label(raw_label: Any, fallback_name: Any, default_label: str) -> str:
    candidate = str(raw_label or "").strip()
    if not candidate:
        candidate = Path(str(fallback_name or "")).stem
    candidate = Path(candidate).stem
    candidate = re.sub(r"[_-]+", " ", candidate)
    candidate = re.sub(r"\s+", " ", candidate).strip()
    if not candidate:
        return default_label
    return candidate[:45].rstrip() + ("..." if len(candidate) > 45 else "")


def _subscribe_overlay_preference_key(video_pk: Any, brand_id: Optional[str]) -> str:
    clean_brand = str(brand_id or "").strip()
    return f"subscribe_overlay_image:{clean_brand}:{int(video_pk)}"


def _load_subscribe_overlay_key(
    user_id: Optional[str],
    brand_id: Optional[str],
    video_pk: Any,
) -> Optional[str]:
    if not user_id or video_pk in (None, ""):
        return None
    try:
        return load_user_preference(
            str(user_id),
            _subscribe_overlay_preference_key(video_pk, brand_id),
        )
    except Exception:
        return None


def _save_subscribe_overlay_key(
    user_id: Optional[str],
    brand_id: Optional[str],
    video_pk: Any,
    subscribe_key: Optional[str],
) -> None:
    if not user_id or video_pk in (None, ""):
        return
    save_user_preference(
        str(user_id),
        _subscribe_overlay_preference_key(video_pk, brand_id),
        subscribe_key,
    )


def _subscribe_storage_reference_for_asset(
    subscribe_key: Optional[str],
    *,
    expected_owner_user_id: Optional[str] = None,
    expected_brand_id: Optional[str] = None,
) -> Optional[str]:
    clean_key = str(subscribe_key or "").strip()
    if not clean_key:
        return None
    if is_system_subscribe_key(clean_key):
        system_path = resolve_system_subscribe_path(clean_key)
        return str(system_path) if system_path and system_path.exists() else None
    if not clean_key.startswith("usersub:"):
        return None
    image_id = clean_key.split(":", 1)[1].strip()
    if not image_id:
        return None
    conn_images = get_db_readonly()
    try:
        row = conn_images.execute(
            "SELECT user_id, filename, brand_id FROM shorts_static_images WHERE id = ? AND COALESCE(asset_kind, 'background') = 'subscribe'",
            [image_id],
        ).fetchone()
    finally:
        conn_images.close()
    if not row or not row[1]:
        return None
    owner_id, filename, owner_brand_id = row[0], row[1], row[2]
    if expected_owner_user_id and owner_id and owner_id != expected_owner_user_id:
        current_app.logger.warning(
            "Subscribe overlay owner mismatch for image_id=%s owner=%s expected_owner=%s",
            image_id,
            owner_id,
            expected_owner_user_id,
        )
        return None
    if expected_brand_id and owner_brand_id and owner_brand_id != expected_brand_id:
        current_app.logger.warning(
            "Subscribe overlay brand mismatch for image_id=%s owner_brand=%s expected_brand=%s",
            image_id,
            owner_brand_id,
            expected_brand_id,
        )
        return None
    return build_storage_reference(_user_media_storage_key("image", owner_id, filename))


def _normalize_subscribe_overlay_key(
    subscribe_key: Optional[str],
    *,
    expected_owner_user_id: Optional[str] = None,
    expected_brand_id: Optional[str] = None,
) -> Optional[str]:
    clean_key = str(subscribe_key or "").strip()
    if not clean_key:
        return None
    if is_system_subscribe_key(clean_key):
        system_path = resolve_system_subscribe_path(clean_key)
        return clean_key if system_path and system_path.exists() else None
    reference = _subscribe_storage_reference_for_asset(
        clean_key,
        expected_owner_user_id=expected_owner_user_id,
        expected_brand_id=expected_brand_id,
    )
    return clean_key if reference else None


def _resolve_user_static_image_path(
    image_id: str,
    *,
    expected_owner_user_id: Optional[str] = None,
    expected_brand_id: Optional[str] = None,
) -> Tuple[Optional[Path], bool]:
    clean_image_id = str(image_id or "").strip()
    if not clean_image_id:
        return None, False
    conn_images = get_db_readonly()
    try:
        row = conn_images.execute(
            "SELECT user_id, filename, brand_id FROM shorts_static_images WHERE id = ? AND COALESCE(asset_kind, 'background') = 'background'",
            [clean_image_id],
        ).fetchone()
    finally:
        conn_images.close()
    if not row or not row[1]:
        return None, False
    owner_id, filename, owner_brand_id = row[0], row[1], row[2]
    if expected_owner_user_id and owner_id and owner_id != expected_owner_user_id:
        current_app.logger.warning(
            "Static image owner mismatch for image_id=%s owner=%s expected_owner=%s",
            clean_image_id,
            owner_id,
            expected_owner_user_id,
        )
        return None, False
    if expected_brand_id and owner_brand_id and owner_brand_id != expected_brand_id:
        current_app.logger.warning(
            "Static image brand mismatch for image_id=%s owner_brand=%s expected_brand=%s",
            clean_image_id,
            owner_brand_id,
            expected_brand_id,
        )
        return None, False
    key = _user_media_storage_key("image", owner_id, filename)
    storage = get_media_storage()
    candidate = STATIC_USER_IMAGES_DIR / owner_id / filename
    resolved = storage.resolve_local_or_s3(key, fallback_local_paths=[candidate])
    if resolved.local_path and resolved.local_path.exists():
        return resolved.local_path, False
    if resolved.exists and resolved.backend == "s3":
        try:
            return storage.download_to_temp(key), True
        except Exception:
            current_app.logger.exception("Failed to download static image from s3 image_id=%s key=%s", clean_image_id, key)
    return None, False


def _resolve_subscribe_overlay_path(
    subscribe_key: Optional[str],
    *,
    expected_owner_user_id: Optional[str] = None,
    expected_brand_id: Optional[str] = None,
) -> Tuple[Optional[Path], bool]:
    clean_key = str(subscribe_key or "").strip()
    if not clean_key:
        return None, False
    if is_system_subscribe_key(clean_key):
        system_path = resolve_system_subscribe_path(clean_key)
        if system_path and system_path.exists():
            return system_path, False
        return None, False
    if not clean_key.startswith("usersub:"):
        return None, False
    image_id = clean_key.split(":", 1)[1].strip()
    if not image_id:
        return None, False
    conn_images = get_db_readonly()
    try:
        row = conn_images.execute(
            "SELECT user_id, filename, brand_id FROM shorts_static_images WHERE id = ? AND COALESCE(asset_kind, 'background') = 'subscribe'",
            [image_id],
        ).fetchone()
    finally:
        conn_images.close()
    if not row or not row[1]:
        return None, False
    owner_id, filename, owner_brand_id = row[0], row[1], row[2]
    if expected_owner_user_id and owner_id and owner_id != expected_owner_user_id:
        current_app.logger.warning(
            "Subscribe overlay owner mismatch for image_id=%s owner=%s expected_owner=%s",
            image_id,
            owner_id,
            expected_owner_user_id,
        )
        return None, False
    if expected_brand_id and owner_brand_id and owner_brand_id != expected_brand_id:
        current_app.logger.warning(
            "Subscribe overlay brand mismatch for image_id=%s owner_brand=%s expected_brand=%s",
            image_id,
            owner_brand_id,
            expected_brand_id,
        )
        return None, False
    key = _user_media_storage_key("image", owner_id, filename)
    storage = get_media_storage()
    candidate = STATIC_USER_IMAGES_DIR / owner_id / filename
    resolved = storage.resolve_local_or_s3(key, fallback_local_paths=[candidate])
    if resolved.local_path and resolved.local_path.exists():
        return resolved.local_path, False
    if resolved.exists and resolved.backend == "s3":
        try:
            return storage.download_to_temp(key), True
        except Exception:
            current_app.logger.exception(
                "Failed to download subscribe overlay from s3 image_id=%s key=%s",
                image_id,
                key,
            )
    return None, False


def _legacy_image_to_video_path(job_id: str) -> Path:
    safe_job_id = Path(str(job_id or "")).name
    return (VIDEOS_DIR / "image_to_video" / f"image_to_video_{safe_job_id}.mp4").resolve()


def _short_storage_key(filename: str) -> str:
    safe_name = Path(filename or "").name
    return f"shorts/{safe_name}" if safe_name else ""


def _short_local_path(filename: str) -> Path:
    safe_name = Path(filename or "").name
    return (SHORTS_DIR / safe_name).resolve()


def invalidate_short_cdn_cache(filename: str) -> None:
    safe_name = Path(filename or "").name
    if not safe_name:
        return
    key = _short_storage_key(safe_name)
    try:
        invalidation_id = invalidate_cloudfront_paths([f"/{key}"])
        if invalidation_id:
            current_app.logger.info(
                "short cloudfront invalidation requested filename=%s key=%s invalidation_id=%s",
                safe_name,
                key,
                invalidation_id,
            )
    except Exception as exc:
        current_app.logger.warning(
            "Failed to invalidate short CloudFront cache filename=%s key=%s: %s",
            safe_name,
            key,
            exc,
        )


def _short_public_url(filename: str) -> str:
    safe_name = Path(filename or "").name
    if not safe_name:
        return ""
    key = _short_storage_key(safe_name)
    storage = get_media_storage()
    if getattr(storage, "backend_name", "local") == "s3":
        try:
            if storage.exists(key):
                current_app.logger.debug("short url resolved from s3 filename=%s key=%s", safe_name, key)
                return storage.public_url(key)
        except Exception:
            current_app.logger.exception("Failed to resolve short url from s3 filename=%s key=%s", safe_name, key)
    local_storage = get_media_storage("local")
    local_path = _short_local_path(safe_name)
    if local_path.exists() and local_path.is_file():
        current_app.logger.debug("short url resolved from local filename=%s key=%s", safe_name, key)
        return local_storage.public_url(key)
    return ""


def _short_poster_storage_key(filename: str) -> str:
    safe_name = Path(filename or "").name
    if not safe_name:
        return ""
    return f"shorts/posters/{safe_name}.jpg"


def _short_poster_public_url(filename: str) -> str:
    safe_name = Path(filename or "").name
    if not safe_name:
        return ""
    key = _short_poster_storage_key(safe_name)
    storage = get_media_storage()
    if getattr(storage, "backend_name", "local") == "s3":
        try:
            if storage.exists(key):
                return storage.public_url(key)
        except Exception:
            current_app.logger.exception("Failed to resolve short poster url from s3 filename=%s key=%s", safe_name, key)
    local_storage = get_media_storage("local")
    try:
        if local_storage.exists(key):
            return local_storage.public_url(key)
    except Exception:
        current_app.logger.exception("Failed to resolve short poster url from local storage filename=%s key=%s", safe_name, key)
    return ""


def _cleanup_managed_temp_path(path_obj: Any) -> None:
    cleanup = getattr(path_obj, "cleanup", None)
    if callable(cleanup):
        try:
            cleanup()
        except Exception:
            pass


def _ensure_shared_short_poster(filename: str) -> None:
    safe_name = Path(filename or "").name
    if not safe_name:
        return
    poster_key = _short_poster_storage_key(safe_name)
    if not poster_key:
        return
    storage = get_media_storage()
    try:
        if storage.exists(poster_key):
            return
    except Exception:
        current_app.logger.exception("Failed to check short poster existence filename=%s key=%s", safe_name, poster_key)
        return

    short_key = _short_storage_key(safe_name)
    input_path_obj = None
    output_path = None
    try:
        input_path_obj = storage.download_to_temp(short_key)
        input_path = Path(str(input_path_obj))
        if not input_path.exists():
            return
        with tempfile.NamedTemporaryFile(prefix="short_poster_", suffix=".jpg", delete=False) as handle:
            output_path = Path(handle.name)
        ffmpeg_bin = _resolve_ffmpeg()
        run_media_subprocess(
            [
                ffmpeg_bin,
                "-y",
                "-ss",
                "1",
                "-i",
                str(input_path),
                "-frames:v",
                "1",
                "-q:v",
                "3",
                str(output_path),
            ],
            timeout=max(30, min(int(FFMPEG_SHORT_TIMEOUT or 120), 180)),
            operation="share_poster_extract",
            context=f"clip_filename={safe_name}",
            output_paths=[output_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if output_path.exists() and output_path.stat().st_size > 0:
            storage.put_file(output_path, poster_key)
    except Exception:
        current_app.logger.exception("Failed to generate shared short poster filename=%s", safe_name)
    finally:
        _cleanup_managed_temp_path(input_path_obj)
        if output_path is not None:
            try:
                output_path.unlink(missing_ok=True)
            except Exception:
                pass


def _short_exists(filename: str) -> bool:
    safe_name = Path(filename or "").name
    if not safe_name:
        return False
    key = _short_storage_key(safe_name)
    storage = get_media_storage()
    if getattr(storage, "backend_name", "local") == "s3":
        try:
            if storage.exists(key):
                return True
        except Exception:
            current_app.logger.exception("Failed short exists check in s3 filename=%s key=%s", safe_name, key)
    local_path = _short_local_path(safe_name)
    return local_path.exists() and local_path.is_file()


def _resolve_short_path_for_processing(filename: str) -> Optional[Path]:
    safe_name = Path(filename or "").name
    if not safe_name:
        return None
    local_path = _short_local_path(safe_name)
    if local_path.exists() and local_path.is_file():
        return local_path
    key = _short_storage_key(safe_name)
    storage = get_media_storage()
    if getattr(storage, "backend_name", "local") != "s3":
        return None
    try:
        if not storage.exists(key):
            return None
        current_app.logger.info("short source resolved from s3 filename=%s key=%s", safe_name, key)
        return storage.download_to_temp(key)
    except Exception:
        current_app.logger.exception("Failed to download short from s3 filename=%s key=%s", safe_name, key)
        return None


def _delete_short_media(filename: str) -> bool:
    safe_name = Path(filename or "").name
    if not safe_name:
        return False
    deleted = False
    key = _short_storage_key(safe_name)
    storage = get_media_storage()
    if getattr(storage, "backend_name", "local") == "s3":
        try:
            current_app.logger.info("short s3 delete begin filename=%s key=%s", safe_name, key)
            storage.delete(key)
            current_app.logger.info("short s3 delete success filename=%s key=%s", safe_name, key)
            invalidate_short_cdn_cache(safe_name)
            deleted = True
        except Exception:
            current_app.logger.exception("short s3 delete failed filename=%s key=%s", safe_name, key)
            raise
    local_path = _short_local_path(safe_name)
    if local_path.exists() and local_path.is_file():
        local_path.unlink()
        deleted = True
    return deleted


def _resolve_image_to_video_media(job_id: str, stored_output_url: str) -> Tuple[Optional[Path], Optional[str], bool]:
    fallback_local = _legacy_image_to_video_path(job_id)
    output_value = str(stored_output_url or "").strip()
    if not output_value:
        return None, None, False
    if is_storage_reference(output_value):
        resolved = resolve_stored_media(output_value, fallback_local_paths=[fallback_local])
        if not resolved.exists:
            return None, None, False
        if resolved.backend == "local" and resolved.local_path:
            return resolved.local_path, public_url_for_stored_media(output_value, fallback_local_url=""), False
        try:
            temp_path = get_media_storage().download_to_temp(resolved.key)
            return temp_path, public_url_for_stored_media(output_value), True
        except Exception:
            current_app.logger.exception(
                "Failed to download image_to_video media job_id=%s output=%s",
                job_id,
                output_value,
            )
            return None, None, False
    parsed = urlparse(output_value)
    media_rel = ""
    if "/media/" in parsed.path:
        media_rel = parsed.path.split("/media/", 1)[1].lstrip("/")
    candidate = (VIDEOS_DIR / media_rel).resolve() if media_rel else fallback_local
    try:
        candidate.relative_to(VIDEOS_DIR.resolve())
    except Exception:
        return None, None, False
    if candidate.exists():
        return candidate, output_value, False
    return None, output_value, False


def _read_audio_meta(kind: str, user_id: str, filename: str) -> Dict[str, Any]:
    storage = get_media_storage()
    clean_name = Path(filename or "").name
    local_sidecar = _audio_sidecar_path(_legacy_media_path(kind, user_id, clean_name))
    resolved = storage.resolve_local_or_s3(
        _audio_sidecar_key(kind, user_id, clean_name),
        fallback_local_paths=[local_sidecar],
    )
    if not resolved.exists:
        return {}
    try:
        if resolved.backend == "local" and resolved.local_path:
            raw = json.loads(resolved.local_path.read_text())
        else:
            raw = json.loads(storage.read_bytes(resolved.key).decode("utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _save_audio_meta(kind: str, user_id: str, filename: str, payload: Dict[str, Any]) -> None:
    try:
        get_media_storage().put_bytes(
            json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
            _audio_sidecar_key(kind, user_id, filename),
            content_type="application/json",
        )
    except Exception:
        current_app.logger.warning("Failed to write audio meta for %s/%s", kind, filename)


def _serialize_static_audio(user_id: str, audio_path: Path) -> Dict[str, Any]:
    meta = _read_audio_meta("audio", user_id, audio_path.name)
    display_name = str(meta.get("label") or audio_path.stem)
    key = _user_media_storage_key("audio", user_id, audio_path.name)
    resolved = get_media_storage().resolve_local_or_s3(key, fallback_local_paths=[audio_path])
    return {
        "id": audio_path.name,
        "filename": audio_path.name,
        "display_name": display_name,
        "size_bytes": audio_path.stat().st_size if audio_path.exists() else None,
        "url": resolved.public_url or get_media_storage("local").public_url(key),
        "created_at": meta.get("created_at"),
    }


def _resolve_user_audio_path(user_id: str, audio_filename: str) -> Optional[Path]:
    safe_name = Path(audio_filename or "").name
    if not safe_name or safe_name != (audio_filename or ""):
        return None
    key = _user_media_storage_key("audio", user_id, safe_name)
    candidate = _legacy_media_path("audio", user_id, safe_name)
    resolved = get_media_storage().resolve_local_or_s3(key, fallback_local_paths=[candidate])
    if not resolved.exists:
        return None
    if resolved.backend == "local" and resolved.local_path:
        return resolved.local_path
    try:
        return get_media_storage().download_to_temp(key)
    except Exception:
        current_app.logger.warning("Failed to download audio from storage for %s", key)
        return None


def _resolve_user_podcast_audio_path(user_id: str, audio_filename: str) -> Optional[Path]:
    safe_name = Path(audio_filename or "").name
    if not safe_name or safe_name != (audio_filename or ""):
        return None
    brand_id = current_brand_id()
    conn = get_db_readonly()
    try:
        row = conn.execute(
            """
            SELECT 1
            FROM shorts_storage_assets
            WHERE user_id = ?
              AND file_key = ?
              AND (status = 'active' OR status IS NULL)
              AND ((? IS NULL AND brand_id IS NULL) OR brand_id = ?)
            LIMIT 1
            """,
            [user_id, f"podcast_audio:{user_id}:{safe_name}", brand_id, brand_id],
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    candidates = [
        _legacy_media_path("podcast", user_id, safe_name),
        _legacy_media_path("audio", user_id, safe_name),
    ]
    storage = get_media_storage()
    keys = [
        _user_media_storage_key("podcast", user_id, safe_name),
        _user_media_storage_key("audio", user_id, safe_name),
    ]
    for key, candidate in zip(keys, candidates):
        resolved = storage.resolve_local_or_s3(key, fallback_local_paths=[candidate])
        if not resolved.exists:
            continue
        if resolved.backend == "local" and resolved.local_path:
            return resolved.local_path
        try:
            return storage.download_to_temp(key)
        except Exception:
            current_app.logger.warning("Failed to download podcast audio from storage for %s", key)
    return None


def _cleanup_video_shorts_temp_path(path: Optional[Path]) -> None:
    if not path:
        return
    try:
        resolved = Path(path).resolve()
        tmp_dir = ensure_video_shorts_tmp_dir().resolve()
        resolved.relative_to(tmp_dir)
    except Exception:
        return
    try:
        resolved.unlink(missing_ok=True)
    except Exception:
        current_app.logger.exception("Failed to cleanup video_shorts temp path: %s", resolved)


def _merge_storage_entries(*entry_groups: List[StorageEntry]) -> List[StorageEntry]:
    merged: Dict[str, StorageEntry] = {}
    for group in entry_groups:
        for entry in group:
            name = Path(entry.key).name
            if not name:
                continue
            existing = merged.get(name)
            if existing is None or entry.backend == "s3":
                merged[name] = entry
    rows = list(merged.values())
    rows.sort(
        key=lambda item: item.modified_at or datetime.fromtimestamp(0, tz=timezone.utc),
        reverse=True,
    )
    return rows


def _build_video_meta_map_for_storage(conn) -> Dict[str, Dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT yv.id, yv.video_id, yv.title, yv.channel_id, ch.channel_name, yv.download_status
        FROM youtube_videos yv
        LEFT JOIN youtube_channels ch ON yv.channel_id = ch.channel_id
        ORDER BY yv.id DESC
        """
    ).fetchall()
    video_meta: Dict[str, Dict[str, Any]] = {}
    for db_id, video_id, title, channel_id, channel_name, download_status in rows:
        plan_stats = _load_short_plan_stats(video_id)
        meta = {
            "youtube_title": title or "",
            "db_id": str(db_id),
            "channel_name": channel_name or "",
            "video_pk": str(db_id),
            "status": download_status or "",
            "video_id": video_id,
            "plan_count": plan_stats["plan_count"],
            "created_count": plan_stats["created_count"],
            "desc_ready": plan_stats["desc_ready"],
        }
        variants = {
            video_id,
            f"{video_id}.mp4",
            f"{video_id}.mov",
            f"{video_id}.mkv",
            f"{video_id}.mp3",
            f"{video_id}.wav",
            f"{video_id}.m4a",
            f"{video_id}.aac",
            f"{video_id}.ogg",
            f"{video_id}.flac",
            f"{video_id}.mp4M",
            str(db_id),
            f"{str(db_id)}.mp4",
            f"{str(db_id)}.mov",
            f"{str(db_id)}.mkv",
            f"{str(db_id)}.mp3",
            f"{str(db_id)}.wav",
            f"{str(db_id)}.m4a",
            f"{str(db_id)}.aac",
            f"{str(db_id)}.ogg",
            f"{str(db_id)}.flac",
        }
        for key in variants:
            video_meta[key] = meta
    return video_meta


def _list_user_podcast_short_clip_options(user_id: str) -> List[Dict[str, Any]]:
    conn = get_db_readonly()
    brand_id = current_brand_id()
    try:
        video_meta = _build_video_meta_map_for_storage(conn)
        short_meta = _build_short_clip_meta(video_meta)
        sql = """
            SELECT file_key, file_path, updated_at
            FROM shorts_storage_assets
            WHERE user_id = ?
              AND file_type = 'short'
              AND (status = 'active' OR status IS NULL)
        """
        params: List[Any] = [user_id]
        if brand_id is None:
            sql += "\n  AND brand_id IS NULL"
        else:
            sql += "\n  AND brand_id = ?"
            params.append(brand_id)
        sql += "\nORDER BY updated_at DESC"
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    options: List[Dict[str, Any]] = []
    for file_key, file_path, updated_at in rows:
        key = str(file_key or "").strip()
        if not key.startswith("short:"):
            continue
        filename = key.split(":", 1)[1]
        path = Path(file_path or "")
        if not path.exists() or not path.is_file():
            if not _short_exists(filename):
                continue
            path = Path(filename)
        if not path:
            continue
        meta = short_meta.get(filename) or short_meta.get(path.stem) or {}
        title = str(meta.get("youtube_title") or "").strip() or filename
        options.append(
            {
                "id": key,
                "file_key": key,
                "filename": filename,
                "title": title,
            }
        )
    return options


def _resolve_user_short_clip_source_paths(user_id: str, clip_ids: List[str], max_items: int = 2) -> List[Path]:
    cleaned: List[str] = []
    for raw in clip_ids or []:
        key = str(raw or "").strip()
        if not key or not key.startswith("short:"):
            continue
        cleaned.append(key)
    deduped = list(dict.fromkeys(cleaned))[:max_items]
    if not deduped:
        return []
    placeholders = ", ".join("?" for _ in deduped)
    brand_id = current_brand_id()
    conn = get_db_readonly()
    try:
        sql = f"""
            SELECT file_key, file_path
            FROM shorts_storage_assets
            WHERE user_id = ?
              AND file_type = 'short'
        """
        params: List[Any] = [user_id]
        if brand_id is None:
            sql += "\n  AND brand_id IS NULL"
        else:
            sql += "\n  AND brand_id = ?"
            params.append(brand_id)
        sql += f"\n  AND file_key IN ({placeholders})"
        rows = conn.execute(sql, [*params, *deduped]).fetchall()
    finally:
        conn.close()
    by_key = {str(file_key): str(file_path or "") for file_key, file_path in rows}
    resolved: List[Path] = []
    for key in deduped:
        filename = key.split(":", 1)[1]
        path = Path(by_key.get(key) or "")
        if not path.exists():
            path = _resolve_short_path_for_processing(filename) or path
        if path.exists() and path.is_file():
            resolved.append(path)
    return resolved[:max_items]


@video_shorts_bp.route("/api/static-audios", methods=["GET"])
def list_static_audios():
    current_user = getattr(g, "vs_current_user", None)
    if not current_user:
        return jsonify({"error": "auth required"}), 401
    user_id = current_user.get("id")
    storage = get_media_storage()
    prefix = _user_media_storage_key("audio", user_id, "")
    local_entries = get_media_storage("local").list_prefix(prefix)
    storage_entries = [] if storage.backend_name == "local" else storage.list_prefix(prefix)
    merged_entries = _merge_storage_entries(storage_entries, local_entries)
    audios: List[Dict[str, Any]] = []
    for entry in merged_entries:
        name = Path(entry.key).name
        if name.endswith(".meta.json"):
            continue
        if Path(name).suffix.lower() not in _ALLOWED_STATIC_AUDIO_EXTS:
            continue
        try:
            audios.append(_serialize_static_audio(user_id, entry.local_path or _legacy_media_path("audio", user_id, name)))
        except Exception:
            continue
    return jsonify({"audios": audios})


@video_shorts_bp.route("/api/podcast-audios", methods=["GET"])
def list_podcast_audios():
    current_user = getattr(g, "vs_current_user", None)
    if not current_user:
        return jsonify({"error": "auth required"}), 401
    user_id = current_user.get("id")
    brand_id = current_brand_id()
    storage = get_media_storage()
    local_entries = _merge_storage_entries(
        get_media_storage("local").list_prefix(_user_media_storage_key("podcast", user_id, "")),
        get_media_storage("local").list_prefix(_user_media_storage_key("audio", user_id, "")),
    )
    storage_entries = [] if storage.backend_name == "local" else _merge_storage_entries(
        storage.list_prefix(_user_media_storage_key("podcast", user_id, "")),
        storage.list_prefix(_user_media_storage_key("audio", user_id, "")),
    )
    merged_entries = _merge_storage_entries(storage_entries, local_entries)
    asset_labels: Dict[str, str] = {}
    allowed_file_keys: set[str] = set()
    conn = get_db()
    try:
        ensure_storage_user_schema(conn)
        ensure_channel_owner_schema(conn)
        try:
            conn.execute(
                """
                UPDATE shorts_storage_assets a
                SET brand_id = v.brand_id
                FROM youtube_videos v
                WHERE a.brand_id IS NULL
                  AND a.user_id = ?
                  AND a.file_key LIKE ?
                  AND v.owner_user_id = ?
                  AND COALESCE(v.podcast_audio_filename, '') <> ''
                  AND a.file_key = ('podcast_audio:' || CAST(v.owner_user_id AS VARCHAR) || ':' || v.podcast_audio_filename)
                  AND v.brand_id IS NOT NULL
                """,
                [user_id, "podcast_audio:%", user_id],
            )
            conn.commit()
        except Exception:
            pass
        sql = """
            SELECT file_key, label
            FROM shorts_storage_assets
            WHERE user_id = ?
              AND file_key LIKE ?
        """
        params: List[Any] = [user_id, "podcast_audio:%"]
        if brand_id is None:
            sql += "\n  AND brand_id IS NULL"
        else:
            sql += "\n  AND brand_id = ?"
            params.append(brand_id)
        rows = conn.execute(sql, params).fetchall()
        for file_key, label in rows:
            key = str(file_key or "").strip()
            if not key:
                continue
            allowed_file_keys.add(key)
            asset_labels[key] = str(label or "").strip()
    finally:
        conn.close()
    audios: List[Dict[str, Any]] = []
    for entry in merged_entries:
        name = Path(entry.key).name
        if name.endswith(".meta.json"):
            continue
        if Path(name).suffix.lower() not in _ALLOWED_STATIC_AUDIO_EXTS:
            continue
        try:
            kind = "podcast" if entry.key.startswith("user_podcasts/") else "audio"
            local_path = entry.local_path or _legacy_media_path(kind, user_id, name)
            file_key = f"podcast_audio:{user_id}:{name}"
            if file_key not in allowed_file_keys:
                continue
            meta = _read_audio_meta(kind, user_id, name)
            original_filename = str(meta.get("original_filename") or "").strip()
            original_stem = Path(original_filename).stem if original_filename else ""
            raw_label = str(meta.get("label") or "").strip()
            path_stem = Path(name).stem
            tmp_like_pattern = r"^tmp_[a-f0-9]{8,}$"
            db_label = asset_labels.get(file_key, "").strip()
            if raw_label and re.match(tmp_like_pattern, raw_label, flags=re.IGNORECASE):
                raw_label = ""
            if re.match(tmp_like_pattern, path_stem, flags=re.IGNORECASE):
                path_stem = ""
            # Prefer sidecar label when present so manual fixes can override stale DB labels.
            display_name = str(raw_label or db_label or original_stem or path_stem or "Podcast Audio")
            resolved = storage.resolve_local_or_s3(entry.key, fallback_local_paths=[local_path])
            audios.append(
                {
                    "id": name,
                    "filename": name,
                    "display_name": display_name,
                    "size_bytes": entry.size_bytes if entry.size_bytes is not None else (local_path.stat().st_size if local_path.exists() else None),
                    "duration_seconds": _probe_media_duration_seconds(local_path) if local_path.exists() else None,
                    "url": resolved.public_url or get_media_storage("local").public_url(entry.key),
                    "created_at": meta.get("created_at"),
                }
            )
        except Exception:
            continue
    return jsonify({"audios": audios})


@video_shorts_bp.route("/api/podcast-short-clips", methods=["GET"])
def list_podcast_short_clips():
    current_user = getattr(g, "vs_current_user", None)
    if not current_user:
        return jsonify({"error": "auth required"}), 401
    user_id = current_user.get("id")
    clips = _list_user_podcast_short_clip_options(user_id)
    return jsonify({"clips": clips})


@video_shorts_bp.route("/api/static-audios", methods=["POST"])
def upload_static_audio():
    current_user = getattr(g, "vs_current_user", None)
    if not current_user:
        return jsonify({"error": "auth required"}), 401
    upload = request.files.get("audio")
    if not upload or not upload.filename:
        return jsonify({"error": "audio file required"}), 400
    original_name = Path(upload.filename)
    ext = original_name.suffix.lower()
    if ext not in _ALLOWED_STATIC_AUDIO_EXTS:
        return jsonify({"error": "unsupported audio format"}), 400

    user_id = current_user.get("id")
    storage = get_media_storage()
    data = upload.read()
    size_bytes = len(data)
    if size_bytes <= 0:
        return jsonify({"error": "empty file"}), 400
    if size_bytes > STATIC_AUDIO_MAX_BYTES:
        return jsonify({"error": f"audio too large (max {STATIC_AUDIO_MAX_BYTES // (1024 * 1024)}MB)"}), 400

    conn_ro = get_db_readonly()
    try:
        usage = _get_user_storage_usage(conn_ro, user_id)
    finally:
        conn_ro.close()
    if usage["limit_bytes"] and usage["used_bytes"] + size_bytes > usage["limit_bytes"]:
        return jsonify(
            {"error": _quota_block_message(_format_size_bytes(usage["limit_bytes"]), _format_size_bytes(usage["used_bytes"]))}
        ), 403

    safe_stem = secure_filename((request.form.get("label") or "").strip()) or secure_filename(original_name.stem) or "audio"
    final_name = f"{safe_stem[:80]}{ext}"
    final_path = _legacy_media_path("audio", user_id, final_name)
    key = _user_media_storage_key("audio", user_id, final_name)
    if storage.resolve_local_or_s3(key, fallback_local_paths=[final_path]).exists:
        final_name = f"{safe_stem[:60]}_{secrets.token_hex(4)}{ext}"
        final_path = _legacy_media_path("audio", user_id, final_name)
        key = _user_media_storage_key("audio", user_id, final_name)
    storage.put_bytes(data, key, content_type=upload.mimetype or None)
    _save_audio_meta(
        "audio",
        user_id,
        final_name,
        {
            "label": (request.form.get("label") or original_name.stem or "Audio").strip(),
            "original_filename": original_name.name,
            "created_at": datetime.utcnow().isoformat() + "Z",
        },
    )
    _upsert_storage_asset(
        f"static_audio:{user_id}:{final_name}",
        str(final_path),
        "audio",
        size_bytes,
        user_id,
    )
    return jsonify({"ok": True, "audio": _serialize_static_audio(user_id, final_path)})


@video_shorts_bp.route("/api/podcast-audios", methods=["POST"])
def upload_podcast_audio():
    current_user = getattr(g, "vs_current_user", None)
    if not current_user:
        return jsonify({"error": "auth required"}), 401
    upload = request.files.get("audio")
    if not upload or not upload.filename:
        return jsonify({"error": "audio file required"}), 400
    original_name = Path(upload.filename)
    ext = original_name.suffix.lower()
    if ext not in _ALLOWED_STATIC_AUDIO_EXTS:
        return jsonify({"error": "unsupported audio format"}), 400

    user_id = current_user.get("id")
    brand_id = current_brand_id()
    storage = get_media_storage()
    data = upload.read()
    size_bytes = len(data)
    if size_bytes <= 0:
        return jsonify({"error": "empty file"}), 400
    if size_bytes > STATIC_AUDIO_MAX_BYTES:
        return jsonify({"error": f"audio too large (max {STATIC_AUDIO_MAX_BYTES // (1024 * 1024)}MB)"}), 400

    conn_ro = get_db_readonly()
    try:
        usage = _get_user_storage_usage(conn_ro, user_id)
    finally:
        conn_ro.close()
    if usage["limit_bytes"] and usage["used_bytes"] + size_bytes > usage["limit_bytes"]:
        return jsonify(
            {"error": _quota_block_message(_format_size_bytes(usage["limit_bytes"]), _format_size_bytes(usage["used_bytes"]))}
        ), 403

    safe_stem = secure_filename((request.form.get("label") or "").strip()) or secure_filename(original_name.stem) or "podcast_audio"
    final_name = f"{safe_stem[:80]}{ext}"
    final_path = _legacy_media_path("podcast", user_id, final_name)
    key = _user_media_storage_key("podcast", user_id, final_name)
    if storage.resolve_local_or_s3(key, fallback_local_paths=[final_path]).exists:
        final_name = f"{safe_stem[:60]}_{secrets.token_hex(4)}{ext}"
        final_path = _legacy_media_path("podcast", user_id, final_name)
        key = _user_media_storage_key("podcast", user_id, final_name)
    storage.put_bytes(data, key, content_type=upload.mimetype or None)
    label_value = (request.form.get("label") or original_name.stem or "Podcast Audio").strip()
    _save_audio_meta(
        "podcast",
        user_id,
        final_name,
        {
            "label": label_value,
            "original_filename": original_name.name,
            "created_at": datetime.utcnow().isoformat() + "Z",
        },
    )
    file_key = f"podcast_audio:{user_id}:{final_name}"
    _upsert_storage_asset(
        file_key,
        str(final_path),
        "audio",
        size_bytes,
        user_id,
        brand_id=brand_id,
    )
    _update_storage_asset_label(file_key, label_value)
    return jsonify(
        {
            "ok": True,
            "audio": {
                "id": final_name,
                "filename": final_name,
                "display_name": _read_audio_meta("podcast", user_id, final_name).get("label") or Path(final_name).stem,
                "size_bytes": size_bytes,
                "url": storage.resolve_local_or_s3(key, fallback_local_paths=[final_path]).public_url or get_media_storage("local").public_url(key),
            },
        }
    )


@video_shorts_bp.route("/api/static-audios/<path:audio_id>", methods=["DELETE"])
def delete_static_audio(audio_id: str):
    current_user = getattr(g, "vs_current_user", None)
    if not current_user:
        return jsonify({"error": "auth required"}), 401
    user_id = current_user.get("id")
    path = _resolve_user_audio_path(user_id, audio_id)
    if not path:
        return jsonify({"error": "audio not found"}), 404

    storage = get_media_storage()
    key = _user_media_storage_key("audio", user_id, Path(audio_id).name)
    sidecar_key = _audio_sidecar_key("audio", user_id, Path(audio_id).name)
    resolved = storage.resolve_local_or_s3(key, fallback_local_paths=[_legacy_media_path("audio", user_id, Path(audio_id).name)])
    target_storage = get_media_storage("local") if resolved.backend == "local" else storage
    try:
        target_storage.delete(sidecar_key)
    except Exception:
        current_app.logger.warning("Failed to delete audio sidecar %s", sidecar_key)
    try:
        target_storage.delete(key)
    except Exception:
        return jsonify({"error": "delete failed"}), 500

    conn = get_db()
    try:
        _ensure_video_crop_schema(conn)
        try:
            conn.execute(
                """
                UPDATE youtube_videos
                SET podcast_audio_filename = NULL
                WHERE owner_user_id = ?
                  AND podcast_audio_filename = ?
                """,
                [user_id, Path(audio_id).name],
            )
        except Exception:
            pass
        try:
            conn.execute(
                "DELETE FROM shorts_storage_assets WHERE file_key = ?",
                [f"static_audio:{user_id}:{Path(audio_id).name}"],
            )
        except Exception:
            pass
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True})


@video_shorts_bp.route("/api/podcast-audios/<path:audio_id>", methods=["DELETE"])
def delete_podcast_audio(audio_id: str):
    current_user = getattr(g, "vs_current_user", None)
    if not current_user:
        return jsonify({"error": "auth required"}), 401
    user_id = current_user.get("id")
    brand_id = current_brand_id()
    safe_name = Path(audio_id or "").name
    if not safe_name or safe_name != (audio_id or ""):
        return jsonify({"error": "audio not found"}), 404
    path = _resolve_user_podcast_audio_path(user_id, safe_name)
    if not path:
        return jsonify({"error": "audio not found"}), 404

    storage = get_media_storage()
    key = _user_media_storage_key("podcast", user_id, safe_name)
    sidecar_key = _audio_sidecar_key("podcast", user_id, safe_name)
    resolved = storage.resolve_local_or_s3(
        key,
        fallback_local_paths=[
            _legacy_media_path("podcast", user_id, safe_name),
            _legacy_media_path("audio", user_id, safe_name),
        ],
    )
    target_storage = get_media_storage("local") if resolved.backend == "local" else storage
    try:
        target_storage.delete(sidecar_key)
    except Exception:
        current_app.logger.warning("Failed to delete podcast audio sidecar %s", sidecar_key)
    try:
        target_storage.delete(key)
    except Exception:
        return jsonify({"error": "delete failed"}), 500

    conn = get_db()
    try:
        _ensure_video_crop_schema(conn)
        try:
            conn.execute(
                "UPDATE youtube_videos SET podcast_audio_filename = NULL WHERE owner_user_id = ? AND brand_id = ? AND podcast_audio_filename = ?",
                [user_id, brand_id, safe_name],
            )
        except Exception:
            pass
        try:
            conn.execute(
                "DELETE FROM shorts_storage_assets WHERE file_key = ? AND user_id = ? AND (? IS NULL OR brand_id = ?)",
                [f"podcast_audio:{user_id}:{safe_name}", user_id, brand_id, brand_id],
            )
        except Exception:
            pass
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True})


def _storage_status_label(plan_count: int, created_count: int, desc_ready: int) -> str:
    plan_count = plan_count or 0
    created_count = created_count or 0
    desc_ready = desc_ready or 0
    if plan_count == 0 and created_count == 0 and desc_ready == 0:
        return "not started to process"
    if plan_count and created_count >= plan_count:
        return "completed"
    return "processing"


def _load_short_plan_stats(video_id: str) -> Dict[str, int]:
    if not video_id:
        return {"plan_count": 0, "created_count": 0, "desc_ready": 0}
    plan_path = SHORTS_DIR / f"{video_id}_plan.json"
    plan_count = 0
    created = 0
    desc_ready = 0
    plan_entries = []
    if plan_path.exists():
        try:
            plan_data = json.loads(plan_path.read_text())
            plan_entries = plan_data.get("plan") or plan_data.get("clips") or []
        except Exception:
            plan_entries = []
    plan_count = len(plan_entries)
    for entry in plan_entries:
        status = (entry.get("status") or "").lower()
        filename = entry.get("clip_filename") or entry.get("output_filename")
        clip_created = status == "created" or _short_exists(filename)
        if clip_created:
            created += 1
        if (entry.get("yt_status") or "").lower() == "ready":
            desc_ready += 1
    return {"plan_count": plan_count, "created_count": created, "desc_ready": desc_ready}


def _build_short_clip_meta(video_meta: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    meta: Dict[str, Dict[str, Any]] = {}
    if not SHORTS_DIR.exists():
        return meta
    plan_suffix = "_plan.json"
    for plan_path in SHORTS_DIR.glob(f"*{plan_suffix}"):
        video_id = plan_path.name[: -len(plan_suffix)]
        base_meta = video_meta.get(video_id) or {}
        try:
            plan_data = json.loads(plan_path.read_text())
        except Exception:
            continue
        plan_entries = plan_data.get("plan") or plan_data.get("clips") or []
        for entry in plan_entries:
            filename = entry.get("clip_filename") or entry.get("output_filename")
            if not filename:
                continue
            meta[filename] = {
                "youtube_title": entry.get("title") or base_meta.get("youtube_title") or "",
                "db_id": base_meta.get("db_id"),
                "channel_name": base_meta.get("channel_name"),
                "video_pk": base_meta.get("video_pk"),
                "status": entry.get("status") or "",
                "video_id": video_id,
            }
    return meta


def _collect_storage_dir_entries(
    base_dir: Path,
    target_dir_key: str,
    file_type: str,
    video_meta: Dict[str, Dict[str, Any]],
    assets_map: Dict[str, Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], int]:
    entries: List[Dict[str, Any]] = []
    total = 0
    if not base_dir.exists() or not base_dir.is_dir():
        if file_type != "downloaded":
            return entries, total
    s3_entries_by_name: Dict[str, StorageEntry] = {}
    if file_type == "downloaded":
        storage = get_media_storage()
        if getattr(storage, "backend_name", "local") == "s3":
            try:
                for entry in storage.list_prefix("videos/"):
                    relative = Path(entry.key).relative_to("videos")
                    if len(relative.parts) != 1:
                        continue
                    name = relative.name
                    if name == "1-short_bg_8.png":
                        continue
                    if Path(name).suffix.lower() not in {
                        ".mp4",
                        ".mkv",
                        ".mov",
                        ".mp3",
                        ".wav",
                        ".m4a",
                        ".aac",
                        ".ogg",
                        ".flac",
                    }:
                        continue
                    s3_entries_by_name[name] = entry
            except Exception:
                current_app.logger.exception("Failed to list S3 downloaded videos for storage view")
    local_candidates: Dict[str, Path] = {}
    if base_dir.exists() and base_dir.is_dir():
        for candidate in sorted(base_dir.iterdir()):
            if not candidate.is_file():
                continue
            if candidate.name == "1-short_bg_8.png":
                continue
            if file_type == "short" and candidate.suffix.lower() not in {".mp4", ".mov", ".mkv"}:
                continue
            if file_type == "downloaded" and candidate.suffix.lower() not in {
                ".mp4",
                ".mkv",
                ".mov",
                ".mp3",
                ".wav",
                ".m4a",
                ".aac",
                ".ogg",
                ".flac",
            }:
                continue
            local_candidates[candidate.name] = candidate
    all_names = sorted(set(local_candidates.keys()) | set(s3_entries_by_name.keys()))
    for name in all_names:
        candidate = local_candidates.get(name)
        local_exists = bool(candidate and candidate.exists() and candidate.is_file())
        s3_entry = s3_entries_by_name.get(name) if file_type == "downloaded" else None
        s3_exists = bool(s3_entry and s3_entry.exists)
        if not local_exists and not s3_exists:
            continue
        size_bytes = 0
        modified = ""
        if local_exists and candidate:
            try:
                stats = candidate.stat()
                size_bytes = int(stats.st_size or 0)
                modified = datetime.fromtimestamp(stats.st_mtime).strftime("%Y-%m-%d")
            except Exception:
                size_bytes = 0
        if not modified and s3_entry and s3_entry.modified_at:
            try:
                modified = s3_entry.modified_at.strftime("%Y-%m-%d")
            except Exception:
                modified = ""
        s3_size = int((s3_entry.size_bytes or 0) if s3_entry else 0)
        display_size = s3_size or size_bytes
        total += display_size
        if local_exists and s3_exists:
            backend = "mixed"
            backend_label = "Local + S3"
        elif s3_exists:
            backend = "s3"
            backend_label = "S3"
        else:
            backend = "local"
            backend_label = "Local"
        stem = Path(name).stem
        meta = video_meta.get(name) or video_meta.get(stem) or {}
        file_key = f"{file_type}:{name}"
        asset = assets_map.get(file_key) or {}
        entries.append(
            {
                "file_key": file_key,
                "file_path": str(candidate or (base_dir / name)),
                "target_dir": target_dir_key,
                "file_type": file_type,
                "type_label": "Short clip" if file_type == "short" else "Downloaded video",
                "name": name,
                "size_label": _format_size_bytes(display_size),
                "size_bytes": display_size,
                "modified": modified,
                "video_id": meta.get("video_id") or stem,
                "youtube_title": meta.get("youtube_title") or "",
                "db_id": meta.get("db_id"),
                "channel_name": meta.get("channel_name") or "",
                "video_pk": meta.get("video_pk"),
                "status": meta.get("status") or "",
                "plan_count": meta.get("plan_count"),
                "created_count": meta.get("created_count"),
                "desc_ready": meta.get("desc_ready"),
                "status_label": _storage_status_label(
                    meta.get("plan_count") or 0, meta.get("created_count") or 0, meta.get("desc_ready") or 0
                )
                if file_type == "downloaded"
                else (meta.get("status") or "").capitalize(),
                "owner_user_id": asset.get("user_id"),
                "storage_backend": backend,
                "storage_backend_label": backend_label,
                "local_backed_bytes": size_bytes,
                "s3_backed_bytes": s3_size,
            }
        )
    return entries, total


def _collect_short_storage_entries(
    video_meta: Dict[str, Dict[str, Any]],
    assets_map: Dict[str, Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], int, int, int]:
    entries: List[Dict[str, Any]] = []
    display_total = 0
    local_total = 0
    s3_total = 0
    local_candidates: Dict[str, Path] = {}
    if SHORTS_DIR.exists() and SHORTS_DIR.is_dir():
        for candidate in SHORTS_DIR.iterdir():
            if not candidate.is_file():
                continue
            if candidate.name == "1-short_bg_8.png":
                continue
            if candidate.suffix.lower() not in {".mp4", ".mov", ".mkv"}:
                continue
            local_candidates[candidate.name] = candidate
    s3_entries_by_name: Dict[str, StorageEntry] = {}
    storage = get_media_storage()
    if getattr(storage, "backend_name", "local") == "s3":
        try:
            for entry in storage.list_prefix("shorts/"):
                name = Path(entry.key).name
                if not name or Path(name).suffix.lower() not in {".mp4", ".mov", ".mkv"}:
                    continue
                s3_entries_by_name[name] = entry
        except Exception:
            current_app.logger.exception("Failed to list S3 shorts for storage view")
    all_names = sorted(set(local_candidates.keys()) | set(s3_entries_by_name.keys()))
    for name in all_names:
        local_path = local_candidates.get(name)
        local_exists = bool(local_path and local_path.exists() and local_path.is_file())
        s3_entry = s3_entries_by_name.get(name)
        s3_exists = bool(s3_entry and s3_entry.exists)
        if not local_exists and not s3_exists:
            continue
        local_size = 0
        local_mtime = None
        if local_exists and local_path:
            try:
                stats = local_path.stat()
                local_size = int(stats.st_size or 0)
                local_mtime = stats.st_mtime
            except Exception:
                local_size = 0
                local_mtime = None
        s3_size = int((s3_entry.size_bytes or 0) if s3_entry else 0)
        display_size = s3_size or local_size
        display_total += display_size
        local_total += local_size
        s3_total += s3_size
        if local_exists and s3_exists:
            backend = "mixed"
            backend_label = "Local + S3"
        elif s3_exists:
            backend = "s3"
            backend_label = "S3"
        else:
            backend = "local"
            backend_label = "Local"
        modified_ts = local_mtime
        if modified_ts is None and s3_entry and s3_entry.modified_at:
            try:
                modified_ts = s3_entry.modified_at.timestamp()
            except Exception:
                modified_ts = None
        meta = video_meta.get(name) or video_meta.get(Path(name).stem) or {}
        file_key = f"short:{name}"
        asset = assets_map.get(file_key) or {}
        entries.append(
            {
                "file_key": file_key,
                "file_path": str(local_path or _short_local_path(name)),
                "target_dir": "shorts",
                "file_type": "short",
                "type_label": "Short clip",
                "name": name,
                "size_label": _format_size_bytes(display_size),
                "size_bytes": display_size,
                "modified": datetime.fromtimestamp(modified_ts).strftime("%Y-%m-%d") if modified_ts else "",
                "video_id": meta.get("video_id") or Path(name).stem,
                "youtube_title": meta.get("youtube_title") or "",
                "db_id": meta.get("db_id"),
                "channel_name": meta.get("channel_name") or "",
                "video_pk": meta.get("video_pk"),
                "status": meta.get("status") or "",
                "plan_count": meta.get("plan_count"),
                "created_count": meta.get("created_count"),
                "desc_ready": meta.get("desc_ready"),
                "status_label": (meta.get("status") or "").capitalize(),
                "owner_user_id": asset.get("user_id"),
                "storage_backend": backend,
                "storage_backend_label": backend_label,
                "local_backed_bytes": local_size,
                "s3_backed_bytes": s3_size,
            }
        )
    return entries, display_total, local_total, s3_total


def _collect_combined_storage_entries(
    video_meta: Dict[str, Dict[str, Any]],
    assets_map: Dict[str, Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], int, int, int, int]:
    downloaded_entries, downloaded_total = _collect_storage_dir_entries(
        VIDEOS_DIR,
        "videos",
        "downloaded",
        video_meta,
        assets_map,
    )
    short_meta = _build_short_clip_meta(video_meta)
    short_entries, short_total, short_local_total, short_s3_total = _collect_short_storage_entries(
        short_meta,
        assets_map,
    )
    combined = downloaded_entries + short_entries
    local_total = sum(int(entry.get("local_backed_bytes") or 0) for entry in downloaded_entries) + short_local_total
    s3_total = sum(int(entry.get("s3_backed_bytes") or 0) for entry in downloaded_entries) + short_s3_total
    return combined, downloaded_total, short_total, local_total, s3_total


def _sync_storage_assets(
    conn,
    entries: List[Dict[str, Any]],
    current_assets: Dict[str, Dict[str, Any]],
    default_user_id: Optional[str] = None,
):
    seen_keys = set()
    for entry in entries:
        file_key = entry["file_key"]
        seen_keys.add(file_key)
        existing_user_id = current_assets.get(file_key, {}).get("user_id")
        owner_user_id = existing_user_id or default_user_id
        entry["owner_user_id"] = owner_user_id
        conn.execute(
            """
            INSERT INTO shorts_storage_assets (file_key, file_path, file_type, size_bytes, user_id, status, updated_at)
            VALUES (?, ?, ?, ?, ?, 'active', now())
            ON CONFLICT(file_key)
            DO UPDATE SET
              file_path = excluded.file_path,
              file_type = excluded.file_type,
              size_bytes = excluded.size_bytes,
              user_id = COALESCE(shorts_storage_assets.user_id, excluded.user_id),
              status = COALESCE(shorts_storage_assets.status, 'active'),
              updated_at = now()
            """,
            [
                file_key,
                entry["file_path"],
                entry["file_type"],
                entry["size_bytes"],
                owner_user_id,
            ],
        )
    stale_keys = set(current_assets.keys()) - seen_keys
    if stale_keys:
        placeholders = ", ".join("?" for _ in stale_keys)
        conn.execute(f"DELETE FROM shorts_storage_assets WHERE file_key IN ({placeholders})", list(stale_keys))


_TIME_INPUT_PATTERN = re.compile(r"^(?:(?P<minutes>\d+):)?(?P<seconds>\d{1,2})(?:\.(?P<millis>\d+))?$")
_PREVIEW_FRAME_CACHE_VERSION = "v3"


def _parse_time_input(value: Any) -> Optional[float]:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    match = _TIME_INPUT_PATTERN.fullmatch(raw)
    if match:
        minutes = int(match.group("minutes") or 0)
        seconds = int(match.group("seconds"))
        millis = match.group("millis") or ""
        fraction = 0.0
        if millis:
            fraction = int(millis) / (10 ** len(millis))
        return minutes * 60 + seconds + fraction
    try:
        return float(raw)
    except Exception:
        return None


def _preview_frame_cache_path(video_id: str) -> Path:
    preview_dir = SHORTS_DIR / "preview_frames"
    preview_dir.mkdir(parents=True, exist_ok=True)
    return preview_dir / f"{video_id}_{_PREVIEW_FRAME_CACHE_VERSION}.jpg"


def _preview_frame_metadata_path(video_id: str) -> Path:
    preview_dir = SHORTS_DIR / "preview_frames"
    preview_dir.mkdir(parents=True, exist_ok=True)
    return preview_dir / f"{video_id}_{_PREVIEW_FRAME_CACHE_VERSION}.json"


def _load_preview_frame_metadata(video_id: str) -> dict[str, Any] | None:
    metadata_path = _preview_frame_metadata_path(video_id)
    if not metadata_path.exists():
        return None
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _preview_candidate_timestamps(duration_seconds: Optional[float]) -> list[float]:
    dur_val = _to_float(duration_seconds)
    if dur_val is None or dur_val <= 0:
        return [0.5, 1.5, 3.0, 5.0, 8.0, 12.0]
    safe_end = max(dur_val - 0.1, 0.0)
    fractions = (0.15, 0.30, 0.45, 0.60, 0.75, 0.90)
    candidates = [max(0.0, min(safe_end, dur_val * fraction)) for fraction in fractions]
    deduped: list[float] = []
    for value in candidates:
        rounded = round(value, 3)
        if not deduped or abs(deduped[-1] - rounded) > 1e-3:
            deduped.append(rounded)
    return deduped or [max(0.0, min(safe_end, dur_val / 2 if dur_val > 0 else 0.5))]


def _ensure_preview_frame(video_id: str, source_path: Optional[Path], duration_seconds: Optional[float]) -> Optional[Path]:
    if not source_path or not source_path.exists():
        return None
    preview_path = _preview_frame_cache_path(video_id)
    metadata_path = _preview_frame_metadata_path(video_id)
    if preview_path.exists():
        return preview_path

    ffmpeg_bin = _resolve_ffmpeg()
    try:
        import cv2

        probe = _probe_video_stream_info(Path(source_path))
        frame_width = int(probe.get("width") or 0)
        frame_height = int(probe.get("height") or 0)
        cascade_path = Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
        detector = cv2.CascadeClassifier(str(cascade_path))
        if detector.empty():
            raise RuntimeError(f"Face cascade could not be loaded from {cascade_path}")

        candidate_timestamps = _preview_candidate_timestamps(duration_seconds)
        best_face: tuple[int, Path, float] | None = None
        best_face_bbox: tuple[int, int, int, int] | None = None
        best_detail: tuple[float, Path, float] | None = None
        with tempfile.TemporaryDirectory(prefix=f"preview_{video_id}_") as temp_dir:
            temp_dir_path = Path(temp_dir)
            for index, timestamp in enumerate(candidate_timestamps):
                candidate_path = temp_dir_path / f"{video_id}_{index}.jpg"
                cmd = [
                    ffmpeg_bin,
                    "-y",
                    "-ss",
                    str(timestamp),
                    "-i",
                    str(source_path),
                    "-frames:v",
                    "1",
                    "-q:v",
                    "2",
                    str(candidate_path),
                ]
                run_media_subprocess(
                    cmd,
                    operation="generate_preview_frame",
                    context=f"video_id={video_id} candidate={index} ts={timestamp} output={candidate_path.name}",
                    output_paths=[candidate_path],
                    check=True,
                    timeout=FFMPEG_SHORT_TIMEOUT,
                )
                image = cv2.imread(str(candidate_path))
                if image is None:
                    continue
                gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
                faces = detector.detectMultiScale(
                    gray,
                    scaleFactor=1.1,
                    minNeighbors=4,
                    minSize=(32, 32),
                )
                if len(faces):
                    largest_face = max(faces, key=lambda item: int(item[2] * item[3]))
                    largest_face_area = int(largest_face[2] * largest_face[3])
                    if best_face is None or largest_face_area > best_face[0]:
                        best_face = (largest_face_area, candidate_path, timestamp)
                        best_face_bbox = tuple(int(value) for value in largest_face)
                laplacian_variance = float(cv2.Laplacian(gray, cv2.CV_64F).var())
                if best_detail is None or laplacian_variance > best_detail[0]:
                    best_detail = (laplacian_variance, candidate_path, timestamp)

            selected_path = None
            selected_metadata: dict[str, Any] = {
                "cache_version": _PREVIEW_FRAME_CACHE_VERSION,
                "frame_width": frame_width,
                "frame_height": frame_height,
                "selected_timestamp": None,
                "selected_by": None,
                "face_bbox": None,
                "face_cx_ratio": None,
                "face_cy_ratio": None,
                "face_w_ratio": None,
                "face_h_ratio": None,
            }
            if best_face is not None:
                selected_path = best_face[1]
                selected_metadata["selected_timestamp"] = best_face[2]
                selected_metadata["selected_by"] = "face"
                if best_face_bbox and frame_width > 0 and frame_height > 0:
                    x, y, w, h = best_face_bbox
                    selected_metadata["face_bbox"] = {
                        "x": x,
                        "y": y,
                        "w": w,
                        "h": h,
                    }
                    selected_metadata["face_cx_ratio"] = (x + (w / 2.0)) / float(frame_width)
                    selected_metadata["face_cy_ratio"] = (y + (h / 2.0)) / float(frame_height)
                    selected_metadata["face_w_ratio"] = w / float(frame_width)
                    selected_metadata["face_h_ratio"] = h / float(frame_height)
                current_app.logger.info(
                    "Preview frame selected by face video_id=%s timestamp=%.3f face_area=%s output=%s",
                    video_id,
                    best_face[2],
                    best_face[0],
                    preview_path.name,
                )
            elif best_detail is not None:
                selected_path = best_detail[1]
                selected_metadata["selected_timestamp"] = best_detail[2]
                selected_metadata["selected_by"] = "detail"
                current_app.logger.info(
                    "Preview frame selected by detail fallback video_id=%s timestamp=%.3f detail_score=%.3f output=%s",
                    video_id,
                    best_detail[2],
                    best_detail[0],
                    preview_path.name,
                )

            if not selected_path or not selected_path.exists():
                raise RuntimeError(f"No preview frame candidate could be selected for {video_id}")

            shutil.copyfile(selected_path, preview_path)
            metadata_path.write_text(json.dumps(selected_metadata, ensure_ascii=True), encoding="utf-8")
    except Exception:
        current_app.logger.exception("Preview capture failed for %s", video_id)
        try:
            preview_path.unlink()
        except Exception:
            pass
        try:
            metadata_path.unlink()
        except Exception:
            pass
        return None
    return preview_path if preview_path.exists() else None


def _maybe_apply_face_centered_default_crop(
    *,
    video_row_id: int,
    video_id: str,
    owner_user_id: Any,
    brand_id: Any,
    preview_metadata: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not preview_metadata:
        return None
    if str(preview_metadata.get("selected_by") or "") != "face":
        return None
    try:
        source_width = int(preview_metadata.get("frame_width") or 0)
        source_height = int(preview_metadata.get("frame_height") or 0)
        face_cx = float(preview_metadata.get("face_cx_ratio"))
    except Exception:
        return None
    if source_width <= 0 or source_height <= 0:
        return None
    if face_cx < 0.0 or face_cx > 1.0:
        return None

    portrait_aspect = 9.0 / 16.0
    source_aspect = float(source_width) / float(source_height)
    crop_x = 0.0
    crop_y = 0.0
    crop_w = 1.0
    crop_h = 1.0
    if source_aspect > portrait_aspect:
        crop_w = min(1.0, portrait_aspect / source_aspect)
        crop_x = max(0.0, min(1.0 - crop_w, face_cx - (crop_w / 2.0)))

    conn = get_db()
    try:
        _ensure_video_crop_schema(conn)
        video_columns = table_columns(conn, "youtube_videos")
        update_sql = """
            UPDATE youtube_videos
            SET crop_x_ratio = ?, crop_y_ratio = ?, crop_w_ratio = ?, crop_h_ratio = ?, crop_aspect = ?
            WHERE id = ?
              AND owner_user_id = ?
              AND crop_x_ratio IS NULL
              AND crop_y_ratio IS NULL
              AND crop_w_ratio IS NULL
              AND crop_h_ratio IS NULL
        """
        params: list[Any] = [
            crop_x,
            crop_y,
            crop_w,
            crop_h,
            "portrait",
            video_row_id,
            owner_user_id,
        ]
        if "brand_id" in video_columns:
            if brand_id is None:
                update_sql += "\n AND brand_id IS NULL"
            else:
                update_sql += "\n AND brand_id = ?"
                params.append(brand_id)
        cursor = conn.execute(update_sql, params)
        conn.commit()
        if cursor.rowcount <= 0:
            return None
    finally:
        conn.close()

    current_app.logger.info(
        "Applied face-centered default crop video_id=%s face_cx=%.4f crop_x=%.4f crop_w=%.4f source=%sx%s",
        video_id,
        face_cx,
        crop_x,
        crop_w,
        source_width,
        source_height,
    )
    return {
        "crop_x_ratio": crop_x,
        "crop_y_ratio": crop_y,
        "crop_w_ratio": crop_w,
        "crop_h_ratio": crop_h,
        "crop_aspect": "portrait",
        "face_cx_ratio": face_cx,
        "source_width": source_width,
        "source_height": source_height,
    }


def _probe_media_duration_seconds(path: Path) -> Optional[int]:
    if not path or not path.exists():
        return None
    ffmpeg_bin = _resolve_ffmpeg()
    ffprobe_bin = str(Path(ffmpeg_bin).with_name("ffprobe"))
    cmd = [
        ffprobe_bin,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    try:
        result = run_media_subprocess(
            cmd,
            operation="probe_media_duration",
            context=f"path={path.name}",
            capture_output=True,
            text=True,
            check=True,
            timeout=FFPROBE_TIMEOUT,
        )
        raw = (result.stdout or "").strip()
        if not raw:
            return None
        seconds = float(raw)
        if seconds <= 0:
            return None
        return int(round(seconds))
    except Exception:
        return None


def _long_comp_meta_path(output_path: Path) -> Path:
    return output_path.with_suffix(".long.meta.json")


def _format_long_created_at_pst(created_at_iso: Optional[str], fallback_ts: Optional[float]) -> str:
    dt_utc: Optional[datetime] = None
    parsed = _parse_iso_datetime(created_at_iso) if created_at_iso else None
    if parsed is not None:
        dt_utc = parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    elif fallback_ts is not None:
        try:
            dt_utc = datetime.fromtimestamp(float(fallback_ts), tz=timezone.utc)
        except Exception:
            dt_utc = None
    if not dt_utc:
        return ""
    return dt_utc.astimezone(PST_ZONE).strftime("%Y-%m-%d %I:%M %p")


def _format_datetime_pst(value: Any) -> str:
    if value in (None, ""):
        return ""
    dt_value: Optional[datetime]
    if isinstance(value, datetime):
        dt_value = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    else:
        parsed = _parse_iso_datetime(str(value))
        if parsed is None:
            return str(value)
        dt_value = parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return dt_value.astimezone(PST_ZONE).strftime("%b %-d, %Y · %H:%M:%S")


def _trial_duration_label(days: Any, language: str = "EN") -> str:
    return trial_duration_text(days, language)


def _safe_long_comp_name(name: str) -> bool:
    return bool(re.match(r"^long_from_shorts_[A-Za-z0-9_-]+_\d{8}_\d{6}\.mp4$", (name or "").strip()))


def _build_long_highlights_title(source_title: str) -> str:
    raw = str(source_title or "").strip()
    if not raw:
        return "Öne Çıkanlar"
    cleaned = raw
    # Remove explicit labels/noise from source title.
    cleaned = re.sub(r"\[?\s*özel\s*yayın\s*\]?", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bfkm\s*sohbetleri\b", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bhocaefendi\b", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bm\.?\s*fethullah\s*g[uü]len\b", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bfethullah\s*g[uü]len\b", " ", cleaned, flags=re.IGNORECASE)
    # Remove common date formats.
    cleaned = re.sub(r"\b\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?\b", " ", cleaned)
    cleaned = re.sub(r"\b\d{4}\b", " ", cleaned)
    cleaned = re.sub(
        r"\b(?:\d{1,2}\s+)?(?:ocak|subat|şubat|mart|nisan|mayis|mayıs|haziran|temmuz|agustos|ağustos|eylul|eylül|ekim|kasim|kasım|aralik|aralık)\b",
        " ",
        cleaned,
        flags=re.IGNORECASE,
    )
    # Normalize separators and spacing.
    cleaned = re.sub(r"\s*[|:/,-]\s*", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = re.sub(r"^[^A-Za-z0-9ÇĞİÖŞÜçğıöşü]+|[^A-Za-z0-9ÇĞİÖŞÜçğıöşü]+$", "", cleaned).strip()
    if not cleaned:
        return "Öne Çıkanlar"
    return f"{cleaned} | Öne Çıkanlar"


def _write_long_comp_meta(
    output_path: Path,
    video_id: str,
    source_clips: List[Dict[str, Any]],
    source_chapters: Optional[List[Dict[str, Any]]] = None,
    suggested_title: Optional[str] = None,
) -> None:
    try:
        payload = {
            "video_id": video_id,
            "clip_count": len(source_clips),
            "source_clips": source_clips,
            "source_chapters": source_chapters or [],
            "suggested_title": str(suggested_title or "").strip(),
            "created_at": datetime.utcnow().isoformat() + "Z",
        }
        _long_comp_meta_path(output_path).write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")
    except Exception:
        current_app.logger.exception("Failed to write long compilation meta")


def _update_long_comp_publish_state(filename: str, updates: Dict[str, Any]) -> None:
    if not _safe_long_comp_name(filename):
        return
    meta_path = _long_comp_meta_path(SHORTS_DIR / filename)
    if not meta_path.exists():
        return
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return
    state = meta.get("publish_state")
    if not isinstance(state, dict):
        state = {}
    for platform, payload in (updates or {}).items():
        if not isinstance(payload, dict):
            continue
        prev = state.get(platform)
        if not isinstance(prev, dict):
            prev = {}
        merged = dict(prev)
        merged.update(payload)
        state[platform] = merged
    meta["publish_state"] = state
    try:
        meta_path.write_text(json.dumps(meta, ensure_ascii=True, indent=2), encoding="utf-8")
    except Exception:
        current_app.logger.exception("Failed to update long compilation publish state for %s", filename)


def _list_generated_long_compilations(video_id: str, limit: int = 12) -> List[Dict[str, Any]]:
    safe_video_id = re.sub(r"[^A-Za-z0-9_-]+", "_", (video_id or "").strip())
    rows: List[Dict[str, Any]] = []
    if not SHORTS_DIR.exists() or not safe_video_id:
        return rows
    candidates = sorted(
        SHORTS_DIR.glob(f"long_from_shorts_{safe_video_id}_*.mp4"),
        key=lambda p: p.stat().st_mtime if p.exists() else 0,
        reverse=True,
    )
    for path in candidates[: max(1, int(limit))]:
        mtime_ts = path.stat().st_mtime
        item = {
            "filename": path.name,
            "url": url_for("video_shorts_bp.static", filename=f"shorts/{path.name}"),
            "created_at": _format_long_created_at_pst(None, mtime_ts),
            "clip_count": None,
            "source_titles": [],
            "source_chapters": [],
            "first_title": None,
            "suggested_title": None,
            "publish_state": {},
        }
        meta_path = _long_comp_meta_path(path)
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                if meta.get("created_at"):
                    item["created_at"] = _format_long_created_at_pst(str(meta.get("created_at") or ""), mtime_ts)
                if meta.get("clip_count") is not None:
                    item["clip_count"] = int(meta.get("clip_count"))
                source_clips = meta.get("source_clips")
                if isinstance(source_clips, list):
                    titles = []
                    for clip in source_clips:
                        if not isinstance(clip, dict):
                            continue
                        title = str(clip.get("title") or "").strip()
                        if title:
                            titles.append(title)
                    if titles:
                        item["source_titles"] = titles
                        item["first_title"] = titles[0]
                source_chapters = meta.get("source_chapters")
                if isinstance(source_chapters, list):
                    normalized_chapters = []
                    for chapter in source_chapters:
                        if not isinstance(chapter, dict):
                            continue
                        title = str(chapter.get("title") or "").strip()
                        try:
                            start_seconds = float(chapter.get("start_seconds") or 0.0)
                        except Exception:
                            start_seconds = 0.0
                        if title:
                            normalized_chapters.append(
                                {
                                    "title": title,
                                    "start_seconds": max(0.0, start_seconds),
                                }
                            )
                    if normalized_chapters:
                        item["source_chapters"] = normalized_chapters
                suggested_title = str(meta.get("suggested_title") or "").strip()
                if suggested_title:
                    item["suggested_title"] = suggested_title
                publish_state = meta.get("publish_state")
                if isinstance(publish_state, dict):
                    item["publish_state"] = publish_state
            except Exception:
                pass
        rows.append(item)
    return rows


def _has_audio_stream_local(source: Path) -> bool:
    ffmpeg_bin = Path(_resolve_ffmpeg())
    ffprobe_bin = ffmpeg_bin.with_name("ffprobe")
    cmd = [
        str(ffprobe_bin),
        "-v",
        "error",
        "-select_streams",
        "a",
        "-show_entries",
        "stream=index",
        "-of",
        "csv=p=0",
        str(source),
    ]
    try:
        res = run_media_subprocess(
            cmd,
            operation="has_audio_stream_local",
            context=f"source={source.name}",
            check=False,
            capture_output=True,
            text=True,
            timeout=FFPROBE_TIMEOUT,
        )
        return bool((res.stdout or "").strip())
    except Exception:
        return False


def _probe_duration_seconds_local(source: Path) -> float:
    ffmpeg_bin = Path(_resolve_ffmpeg())
    ffprobe_bin = ffmpeg_bin.with_name("ffprobe")
    cmd = [
        str(ffprobe_bin),
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(source),
    ]
    try:
        res = run_media_subprocess(
            cmd,
            operation="probe_duration_seconds_local",
            context=f"source={source.name}",
            check=False,
            capture_output=True,
            text=True,
            timeout=FFPROBE_TIMEOUT,
        )
        val = float((res.stdout or "").strip() or 0.0)
        return max(0.0, val)
    except Exception:
        return 0.0


def _long_panel_title_lines(title: str, max_total: int = 22) -> Tuple[str, str]:
    words = [w for w in " ".join((title or "").strip().split()).split(" ") if w]
    if not words:
        return ("", "")
    kept: List[str] = []
    for w in words:
        candidate = (" ".join(kept + [w])).strip()
        if len(candidate) <= max_total:
            kept.append(w)
        else:
            break
    truncated = len(kept) < len(words)
    if not kept:
        kept = [words[0][:max_total]]
        truncated = len(words[0]) > max_total or len(words) > 1
    preview = " ".join(kept).strip()
    if truncated:
        while preview and len(preview) + 3 > max_total:
            parts = preview.split()
            if len(parts) <= 1:
                preview = preview[: max(1, max_total - 3)].rstrip()
                break
            preview = " ".join(parts[:-1]).strip()
        preview = (preview or words[0][: max(1, max_total - 3)]).rstrip() + "..."
    return (preview, "")


def _escape_drawtext_local(text: str) -> str:
    return (
        (text or "")
        .replace("\\", r"\\")
        .replace(":", r"\:")
        # ffmpeg drawtext can misparse apostrophes inside single-quoted text;
        # normalize to avoid segment build failures.
        .replace("'", "")
        .replace("%", r"\%")
        .replace(",", r"\,")
        .replace("[", r"\[")
        .replace("]", r"\]")
        .replace("\n", " ")
        .replace("\r", " ")
    )


def _build_long_compilation_from_published(
    video_id: str,
    base_title: str,
    clips: List[Dict[str, Any]],
    suggested_title: Optional[str] = None,
) -> Dict[str, Any]:
    ffmpeg_bin = _resolve_ffmpeg()
    prepared_segments: List[Path] = []
    skipped: List[Dict[str, str]] = []
    used: List[Dict[str, Any]] = []
    work_dir = Path(tempfile.gettempdir()) / f"long_comp_{secrets.token_hex(6)}"
    work_dir.mkdir(parents=True, exist_ok=True)
    eligible_clips: List[Dict[str, Any]] = []
    temp_source_paths: List[Path] = []
    for clip in clips:
        clip_name = str(clip.get("clip_filename") or "").strip()
        if not clip_name:
            continue
        source = _resolve_short_path_for_processing(clip_name)
        current_app.logger.info(
            "long compilation short source resolution clip_filename=%s source=%s",
            clip_name,
            source,
        )
        if not source or not source.exists():
            skipped.append({"clip_filename": clip_name, "reason": "source_not_found"})
            continue
        prepared = dict(clip)
        prepared["_source_path"] = str(source)
        try:
            resolved_source = Path(source).resolve()
            tmp_dir = ensure_video_shorts_tmp_dir().resolve()
            resolved_source.relative_to(tmp_dir)
            temp_source_paths.append(resolved_source)
        except Exception:
            pass
        eligible_clips.append(prepared)
    panel_items: List[Tuple[int, str, str]] = []
    for i, clip in enumerate(eligible_clips, start=1):
        clip_title = str(clip.get("title") or clip.get("clip_filename") or "").strip()
        l1, l2 = _long_panel_title_lines(clip_title, max_total=22)
        panel_items.append((i, _escape_drawtext_local(_sanitize_text_for_overlay(l1)), _escape_drawtext_local(_sanitize_text_for_overlay(l2))))
    try:
        for idx, clip in enumerate(eligible_clips, start=1):
            clip_name = str(clip.get("clip_filename") or "").strip()
            if not clip_name:
                continue
            source = Path(str(clip.get("_source_path") or ""))
            if not source.exists():
                skipped.append({"clip_filename": clip_name, "reason": "source_not_found"})
                continue
            segment_out = work_dir / f"seg_{idx:02d}_{secrets.token_hex(3)}.mp4"
            panel_filters: List[str] = []
            row_h = 52
            panel_h = max(156, len(panel_items) * row_h + 24)
            for item_no, line1, line2 in panel_items:
                y_base = 118 + (item_no - 1) * row_h
                color = "yellow" if item_no == idx else "white"
                show_title = item_no <= idx
                if show_title:
                    panel_filters.append(
                        f"drawtext=text='#{item_no} - {line1}':x=42:y={y_base}:fontsize=20:fontcolor={color}"
                    )
                else:
                    panel_filters.append(
                        f"drawtext=text='#{item_no}':x=42:y={y_base}:fontsize=20:fontcolor={color}"
                    )
            filter_complex = (
                "[0:v]scale=1280:720:force_original_aspect_ratio=increase,"
                "crop=1280:720,boxblur=20:10[bg];"
                "[0:v]scale=1280:720:force_original_aspect_ratio=decrease[fg];"
                "[bg][fg]overlay=(W-w)/2:(H-h)/2,"
                "drawbox=x=0:y=0:w=iw:h=46:color=black@0.45:t=fill,"
                f"drawtext=text='{_escape_drawtext_local(_sanitize_text_for_overlay(base_title[:90]))}':x=(w-text_w)/2:y=10:fontsize=28:fontcolor=white,"
                f"drawbox=x=28:y=104:w=340:h={panel_h}:color=black@0.50:t=fill,"
                + ",".join(panel_filters)
                + "[vout]"
            )
            cmd = [ffmpeg_bin, "-y", "-i", str(source)]
            has_audio = _has_audio_stream_local(source)
            if not has_audio:
                cmd.extend(["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"])
            cmd.extend(
                [
                    "-filter_complex",
                    filter_complex,
                    "-map",
                    "[vout]",
                    "-map",
                    "0:a:0" if has_audio else "1:a:0",
                    "-r",
                    "30",
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "128k",
                    "-ar",
                    "48000",
                    "-ac",
                    "2",
                    "-shortest",
                    str(segment_out),
                ]
            )
            try:
                run_media_subprocess(
                    cmd,
                    operation="build_long_compilation_segment",
                    context=f"clip={clip_name} output={segment_out.name}",
                    output_paths=[segment_out],
                    check=True,
                    timeout=FFMPEG_RENDER_TIMEOUT,
                    capture_output=True,
                    text=True,
                )
            except Exception:
                skipped.append({"clip_filename": clip_name, "reason": "segment_failed"})
                continue
            prepared_segments.append(segment_out)
            used.append(clip)

        if not prepared_segments:
            return {"ok": False, "message": "No published local clip found.", "skipped": skipped}

        safe_video_id = re.sub(r"[^A-Za-z0-9_-]+", "_", (video_id or "").strip())[:72]
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        output_filename = f"long_from_shorts_{safe_video_id}_{ts}.mp4"
        output_path = SHORTS_DIR / output_filename

        transition_d = 0.40
        cmd_concat: List[str] = [ffmpeg_bin, "-y"]
        for seg in prepared_segments:
            cmd_concat.extend(["-i", str(seg)])
        if len(prepared_segments) == 1:
            cmd_concat.extend(
                [
                    "-map",
                    "0:v:0",
                    "-map",
                    "0:a:0",
                    "-c:v",
                    "libx264",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "128k",
                    "-ar",
                    "48000",
                    "-ac",
                    "2",
                    "-pix_fmt",
                    "yuv420p",
                    "-movflags",
                    "+faststart",
                    str(output_path),
                ]
            )
        else:
            durations = [_probe_duration_seconds_local(seg) for seg in prepared_segments]
            filters: List[str] = []
            v_label = "0:v"
            a_label = "0:a"
            composed_d = max(0.01, durations[0] if durations and durations[0] > 0 else 6.0)
            for i in range(1, len(prepared_segments)):
                out_v = f"vxf{i}"
                out_a = f"axf{i}"
                offset = max(0.0, composed_d - transition_d)
                filters.append(f"[{v_label}][{i}:v]xfade=transition=fade:duration={transition_d:.2f}:offset={offset:.3f}[{out_v}]")
                filters.append(f"[{a_label}][{i}:a]acrossfade=d={transition_d:.2f}[{out_a}]")
                v_label = out_v
                a_label = out_a
                next_d = durations[i] if i < len(durations) and durations[i] > 0 else 6.0
                composed_d = composed_d + next_d - transition_d
            cmd_concat.extend(
                [
                    "-filter_complex",
                    ";".join(filters),
                    "-map",
                    f"[{v_label}]",
                    "-map",
                    f"[{a_label}]",
                    "-c:v",
                    "libx264",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "128k",
                    "-ar",
                    "48000",
                    "-ac",
                    "2",
                    "-pix_fmt",
                    "yuv420p",
                    "-movflags",
                    "+faststart",
                    str(output_path),
                ]
            )
        run_media_subprocess(
            cmd_concat,
            operation="build_long_compilation_concat",
            context=f"video_id={video_id} output={output_path.name}",
            output_paths=[output_path],
            check=True,
            timeout=scale_media_timeout(
                FFMPEG_RENDER_TIMEOUT,
                duration_seconds=sum(d for d in durations_for_chapters if d > 0) if 'durations_for_chapters' in locals() else None,
                multiplier=2.0,
                extra_seconds=120,
            ),
            capture_output=True,
            text=True,
        )
        durations_for_chapters = [_probe_duration_seconds_local(seg) for seg in prepared_segments]
        chapter_cursor = 0.0
        source_chapters: List[Dict[str, Any]] = []
        for idx, clip in enumerate(used):
            source_chapters.append(
                {
                    "plan_index": int(clip.get("plan_index") or 0),
                    "title": str(clip.get("title") or ""),
                    "clip_filename": str(clip.get("clip_filename") or ""),
                    "start_seconds": round(max(0.0, chapter_cursor), 3),
                }
            )
            if idx < len(durations_for_chapters):
                next_delta = max(0.0, float(durations_for_chapters[idx]) - (transition_d if idx < len(used) - 1 else 0.0))
                chapter_cursor += next_delta

        _write_long_comp_meta(
            output_path,
            video_id=video_id,
            source_clips=[
                {
                    "plan_index": int(c.get("plan_index") or 0),
                    "clip_filename": str(c.get("clip_filename") or ""),
                    "title": str(c.get("title") or ""),
                }
                for c in used
            ],
            source_chapters=source_chapters,
            suggested_title=suggested_title,
        )
        return {
            "ok": True,
            "message": "Long video created.",
            "output_filename": output_filename,
            "output_url": url_for("video_shorts_bp.static", filename=f"shorts/{output_filename}"),
            "skipped": skipped,
        }
    except Exception as exc:
        current_app.logger.exception("Long compilation failed for %s", video_id)
        return {"ok": False, "message": f"Long compilation failed: {exc}", "skipped": skipped}
    finally:
        for temp_source in temp_source_paths:
            _cleanup_video_shorts_temp_path(temp_source)
        for seg in prepared_segments:
            try:
                seg.unlink()
            except Exception:
                pass
        try:
            for item in work_dir.glob("*"):
                if item.exists():
                    item.unlink()
            work_dir.rmdir()
        except Exception:
            pass


def _plan_path(video_id: str) -> Path:
    return SHORTS_DIR / f"{video_id}_plan.json"


def _plan_path_v2(video_id: str) -> Path:
    return SHORTS_DIR / f"{video_id}_plan_v2.json"


def _plan_path_v3(video_id: str) -> Path:
    return SHORTS_DIR / f"{video_id}_plan_v3.json"


def _plan_path_v4(video_id: str) -> Path:
    return SHORTS_DIR / f"{video_id}_plan_v4.json"


def _load_plan_entries(video_id: str) -> List[Dict[str, Any]]:
    path = _plan_path(video_id)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        entries = data.get("plan") or data.get("clips") or []
        if not isinstance(entries, list):
            return []
        normalized: List[Dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            item = dict(entry)
            origin = str(item.get("origin") or "").strip().lower()
            item["origin"] = origin if origin in {"manual", "ai"} else "manual"
            normalized.append(item)
        return normalized
    except Exception:
        return []


def _load_plan_entries_v2(video_id: str) -> List[Dict[str, Any]]:
    path = _plan_path_v2(video_id)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        return data.get("plan") or data.get("clips") or []
    except Exception:
        return []


def _load_plan_entries_v3(video_id: str) -> List[Dict[str, Any]]:
    path = _plan_path_v3(video_id)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        return data.get("plan") or data.get("clips") or []
    except Exception:
        return []


def _load_plan_entries_v4(video_id: str) -> List[Dict[str, Any]]:
    path = _plan_path_v4(video_id)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        return data.get("plan") or data.get("clips") or []
    except Exception:
        return []


def _write_plan_entries(video_id: str, entries: List[Dict[str, Any]]) -> None:
    path = _plan_path(video_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    prepared_entries: List[Dict[str, Any]] = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        item = dict(entry)
        origin = str(item.get("origin") or "").strip().lower()
        item["origin"] = origin if origin in {"manual", "ai"} else "manual"
        prepared_entries.append(item)
    payload = {"plan": prepared_entries}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))


def _coerce_positive_plan_index(value: Any) -> Optional[int]:
    try:
        resolved = int(value)
    except Exception:
        return None
    return resolved if resolved > 0 else None


def _extract_plan_index_from_filename(value: Any) -> Optional[int]:
    raw = str(value or "").strip()
    if not raw:
        return None
    match = re.match(r"^(\d+)_", Path(raw).name)
    if not match:
        return None
    return _coerce_positive_plan_index(match.group(1))


def _resolve_clip_plan_identity(filename: str) -> Tuple[Optional[str], Optional[int]]:
    safe_name = Path(filename or "").name
    if not safe_name:
        return None, None
    plan_index = _extract_plan_index_from_filename(safe_name)
    stem = Path(safe_name).stem
    match = re.match(r"^\d+_(.+)$", stem)
    if not match:
        return None, plan_index
    video_id = str(match.group(1) or "").strip()
    return (video_id or None), plan_index


def _reset_deleted_clip_plan_entry(video_id: str, filename: str, *, plan_index: Optional[int] = None) -> Optional[int]:
    if not video_id:
        return None
    plan_entries = _load_plan_entries(video_id)
    if not plan_entries:
        return None
    match_entry = None
    if plan_index is not None:
        for entry in plan_entries:
            try:
                if int(entry.get("plan_index") or 0) == int(plan_index):
                    match_entry = entry
                    break
            except Exception:
                continue
    if match_entry is None:
        for entry in plan_entries:
            if entry.get("clip_filename") == filename or entry.get("output_filename") == filename:
                match_entry = entry
                break
    if not match_entry:
        return None
    match_entry["status"] = "pending"
    match_entry["output_filename"] = None
    match_entry["audio_start"] = None
    match_entry["audio_end"] = None
    match_entry.pop("render_job_id", None)
    match_entry.pop("render_error", None)
    _write_plan_entries(video_id, plan_entries)
    try:
        return int(match_entry.get("plan_index") or 0)
    except Exception:
        return None


def _plan_entry_has_stable_identity(entry: Dict[str, Any]) -> bool:
    origin = str(entry.get("origin") or "").strip().lower()
    status = str(entry.get("status") or "").strip().lower()
    publish_status = str(entry.get("publish_status") or "").strip().lower()
    if origin == "manual":
        return True
    if status == "created":
        return True
    if str(entry.get("output_filename") or "").strip():
        return True
    if str(entry.get("yt_video_id") or "").strip():
        return True
    if str(entry.get("publish_at") or "").strip() or str(entry.get("publish_at_iso") or "").strip():
        return True
    return publish_status in {"scheduled", "published"}


def _is_removable_ai_suggestion(entry: Dict[str, Any]) -> bool:
    origin = str(entry.get("origin") or "").strip().lower()
    return origin == "ai" and not _plan_entry_has_stable_identity(entry)


def _choose_plan_index(preferred: Optional[int], used: set[int], next_candidate: int) -> Tuple[int, int]:
    if preferred is not None and preferred > 0 and preferred not in used:
        used.add(preferred)
        return preferred, next_candidate

    candidate = max(next_candidate, 1)
    while candidate in used:
        candidate += 1
    used.add(candidate)
    return candidate, candidate + 1


def _reindex_v1_plan_entries(video_id: str, entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    reindexed: List[Dict[str, Any]] = []
    mutable_positions: List[int] = []
    used_indexes: set[int] = set()
    next_candidate = 1

    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        item = dict(entry)
        origin = str(item.get("origin") or "").strip().lower()
        origin = origin if origin in {"manual", "ai"} else "manual"
        item["origin"] = origin
        stable_identity = _plan_entry_has_stable_identity(item)
        if stable_identity:
            preferred_index = (
                _extract_plan_index_from_filename(item.get("output_filename"))
                or _extract_plan_index_from_filename(item.get("clip_filename"))
                or _coerce_positive_plan_index(item.get("plan_index"))
            )
            assigned_index, next_candidate = _choose_plan_index(preferred_index, used_indexes, next_candidate)
            item["plan_index"] = assigned_index
            if not str(item.get("clip_filename") or "").strip():
                item["clip_filename"] = f"{assigned_index}_{video_id}.mp4"
        else:
            mutable_positions.append(len(reindexed))
        reindexed.append(item)

    for position in mutable_positions:
        item = reindexed[position]
        preferred_index = _coerce_positive_plan_index(item.get("plan_index"))
        assigned_index, next_candidate = _choose_plan_index(preferred_index, used_indexes, next_candidate)
        item["plan_index"] = assigned_index
        item["clip_filename"] = f"{assigned_index}_{video_id}.mp4"

    return reindexed


def _resolve_saved_focus_categories(entries: List[Dict[str, Any]]) -> List[str]:
    for entry in reversed(entries or []):
        if not isinstance(entry, dict):
            continue
        raw = entry.get("focus_categories")
        if raw in (None, "", []):
            continue
        resolved = normalize_focus_categories(raw, default_to_all=False)
        if resolved:
            return resolved
    return list(ALL_FOCUS_CATEGORIES)


def _write_plan_entries_v2(video_id: str, entries: List[Dict[str, Any]]) -> None:
    path = _plan_path_v2(video_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"plan": entries}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))


def _write_plan_entries_v3(video_id: str, entries: List[Dict[str, Any]]) -> None:
    path = _plan_path_v3(video_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"plan": entries}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))


def _write_plan_entries_v4(video_id: str, entries: List[Dict[str, Any]]) -> None:
    path = _plan_path_v4(video_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"plan": entries}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))


def _update_plan_entry_job_state(
    video_id: str,
    entries: List[Dict[str, Any]],
    *,
    plan_index: int,
    status: str,
    render_job_id: Optional[str] = None,
    error_message: Optional[str] = None,
) -> None:
    changed = False
    for entry in entries:
        try:
            entry_index = int(entry.get("plan_index"))
        except Exception:
            continue
        if entry_index != plan_index:
            continue
        entry["status"] = status
        if render_job_id:
            entry["render_job_id"] = render_job_id
        else:
            entry.pop("render_job_id", None)
        if error_message:
            entry["render_error"] = error_message
        elif status in {"queued", "processing", "created"}:
            entry.pop("render_error", None)
        changed = True
        break
    if changed:
        _write_plan_entries(video_id, entries)


def _build_render_job_options(
    *,
    plan_index: int,
    title: str,
    brand_id: Optional[str],
    crop_ratios: Dict[str, Any],
    crop_aspect: str,
    title_font_key: Optional[str],
    title_font_size: Optional[int],
    subtitle_font_key: Optional[str],
    subtitle_font_size: Optional[int],
    subtitle_margin: Optional[int],
    subtitle_style: Optional[str],
    subtitle_preset: Optional[str],
    title_margin: Optional[int],
    title_line_spacing: Optional[int],
    title_bg_color: Optional[str],
    title_bg_alpha: Optional[int],
    title_text_color: Optional[str],
    subtitle_text_color: Optional[str],
    subtitle_bg_color: Optional[str],
    subtitle_bg_alpha: Optional[int],
    subtitle_text_alpha: Optional[int],
    date_text: Optional[str],
    date_top: Optional[int],
    show_title: bool,
    show_subtitle: bool,
    subscribe_overlay: bool,
    subscribe_overlay_image: Optional[str],
    is_music_only: bool,
    static_visual_key: Optional[str],
    background_visual_key: Optional[str],
    visual_mode: str,
    podcast_audio_filename: str,
    podcast_overlay_short_ids: List[str],
    video_overlay_offset: Optional[int],
    subtitle_text: Optional[str],
) -> Dict[str, Any]:
    return {
        "plan_index": int(plan_index),
        "title": (title or "").strip(),
        "brand_id": brand_id,
        "crop_ratios": crop_ratios or {},
        "crop_aspect": crop_aspect or "landscape",
        "title_font_key": title_font_key,
        "title_font_size": title_font_size,
        "subtitle_font_key": subtitle_font_key,
        "subtitle_font_size": subtitle_font_size,
        "subtitle_margin": subtitle_margin,
        "subtitle_style": (subtitle_style or "plain").strip().lower(),
        "subtitle_preset": str(subtitle_preset or DEFAULT_SUBTITLE_PRESET).strip() or DEFAULT_SUBTITLE_PRESET,
        "title_margin": title_margin,
        "title_line_spacing": title_line_spacing,
        "title_bg_color": title_bg_color,
        "title_bg_alpha": title_bg_alpha,
        "title_text_color": title_text_color,
        "subtitle_text_color": subtitle_text_color,
        "subtitle_bg_color": subtitle_bg_color,
        "subtitle_bg_alpha": subtitle_bg_alpha,
        "subtitle_text_alpha": subtitle_text_alpha,
        "date_text": date_text,
        "date_top": date_top,
        "show_title": bool(show_title),
        "show_subtitle": bool(show_subtitle),
        "subscribe_overlay": bool(subscribe_overlay),
        "subscribe_overlay_image": subscribe_overlay_image,
        "is_music_only": bool(is_music_only),
        "static_visual_key": static_visual_key,
        "background_visual_key": background_visual_key,
        "visual_mode": visual_mode or "video",
        "podcast_audio_filename": podcast_audio_filename or "",
        "podcast_overlay_short_ids": podcast_overlay_short_ids or [],
        "video_overlay_offset": video_overlay_offset,
        "subtitle_text": (subtitle_text or "").strip(),
    }


def _find_plan_entry(entries: List[Dict[str, Any]], plan_index: Optional[str]) -> Optional[Dict[str, Any]]:
    if not entries:
        return None
    for entry in entries:
        entry_index = entry.get("plan_index")
        if entry_index is None:
            continue
        try:
            entry_index = str(int(entry_index))
        except Exception:
            entry_index = str(entry.get("plan_index") or "")
        if plan_index and str(plan_index) == entry_index:
            return entry
    return None


def _build_placeholder_clip_title(transcript_text: str, fallback_index: int) -> str:
    source = " ".join(str(transcript_text or "").strip().split())
    if source:
        sentence = re.split(r"(?<=[.!?])\s+|\n+", source, maxsplit=1)[0].strip()
        sentence = sentence.strip(" \"'“”‘’.,:;!?-")
        if sentence:
            words = sentence.split()
            shortened = " ".join(words[:8]).strip()
            if len(words) > 8:
                shortened = f"{shortened}…"
            if shortened:
                return shortened[:80].rstrip()
    return f"Manual clip #{fallback_index}"


def _is_placeholder_clip_title(title: str) -> bool:
    normalized = " ".join(str(title or "").strip().split())
    if not normalized:
        return True
    if re.fullmatch(r"Manual clip #\d+", normalized, flags=re.IGNORECASE):
        return True
    if len(normalized) > 80:
        return True
    return False


def _clip_title_example_from_entry(entry: Dict[str, Any]) -> Optional[Dict[str, str]]:
    title = " ".join(str(entry.get("title") or "").strip().split())
    if _is_placeholder_clip_title(title):
        return None
    excerpt = " ".join(
        str(
            entry.get("transcript_full_custom")
            or entry.get("transcript_full")
            or entry.get("excerpt")
            or ""
        ).strip().split()
    )
    if not excerpt:
        return None
    excerpt = excerpt[:280].strip()
    if len(excerpt) < 24:
        return None
    return {"excerpt": excerpt, "title": title[:80].strip()}


def _load_user_title_style_examples(
    user_id: Any,
    current_video_id: Optional[str] = None,
    max_examples: int = 5,
) -> List[Dict[str, str]]:
    if not user_id:
        return []
    conn = get_db_readonly()
    rows: List[Tuple[Any, ...]] = []
    try:
        rows = conn.execute(
            """
            SELECT video_id, id
            FROM youtube_videos
            WHERE owner_user_id = ?
            ORDER BY id DESC
            LIMIT 40
            """,
            [user_id],
        ).fetchall()
    finally:
        conn.close()

    examples: List[Dict[str, str]] = []
    for row in rows:
        video_id = str(row[0] or "").strip()
        if not video_id or video_id == str(current_video_id or "").strip():
            continue
        for entry in _load_plan_entries(video_id):
            example = _clip_title_example_from_entry(entry)
            if not example:
                continue
            title_text = str(example.get("title") or "").strip()
            if len(title_text.split()) <= 1:
                continue
            if len(title_text) > 80:
                continue
            examples.append(example)
            if len(examples) >= max_examples:
                return examples
    return examples


def _generic_short_title_examples() -> List[Dict[str, str]]:
    return _generic_short_title_examples_for_language("tr")

def _detect_title_prompt_language(excerpt: str) -> Optional[str]:
    text = " ".join(str(excerpt or "").strip().split())
    if not text:
        return None
    if re.search(r"[\u0600-\u06FF]", text):
        return "ar"
    lowered = f" {text.lower()} "
    if any(ch in text for ch in "çğıöşüÇĞİÖŞÜ"):
        return "tr"

    turkish_hints = {
        " ve ", " bir ", " bu ", " şu ", " için ", " ama ", " gibi ", " daha ",
        " çok ", " değil ", " neden ", " nasıl ", " çünkü ", " sonra ", " önce ",
    }
    english_hints = {
        " the ", " and ", " you ", " your ", " what ", " why ", " how ", " this ",
        " that ", " with ", " from ", " about ", " when ", " where ", " should ",
    }
    turkish_score = sum(1 for hint in turkish_hints if hint in lowered)
    english_score = sum(1 for hint in english_hints if hint in lowered)
    if turkish_score >= english_score + 1 and turkish_score >= 2:
        return "tr"
    if english_score >= turkish_score + 1 and english_score >= 2:
        return "en"
    return None


def _normalize_title_prompt_language(raw: Any) -> Optional[str]:
    value = str(raw or "").strip().lower()
    if not value:
        return None
    if value.startswith("en") or value == "english":
        return "en"
    if value.startswith("tr") or value in {"turkish", "turkce"}:
        return "tr"
    if value.startswith("ar") or value == "arabic":
        return "ar"
    return None


def _resolve_transcript_language(segments: List[Dict[str, Any]]) -> Optional[str]:
    language_counts: Dict[str, int] = {}
    for seg in segments or []:
        lang = _normalize_title_prompt_language(seg.get("lang") or seg.get("language"))
        if lang not in {"tr", "en"}:
            continue
        language_counts[lang] = language_counts.get(lang, 0) + 1
    if not language_counts:
        return None
    return max(language_counts.items(), key=lambda item: item[1])[0]


def _resolve_video_language(segments: List[Dict[str, Any]], transcript_text: str = "") -> Optional[str]:
    segment_language = _resolve_transcript_language(segments)
    text_language = _detect_title_language((transcript_text or "")[:2000])
    if segment_language == "en":
        return "en"
    if text_language == "en":
        return "en"
    if segment_language == "tr":
        return "tr"
    if text_language == "tr":
        return "tr"
    return segment_language or text_language


def _infer_clip_language_from_segments(
    segments: List[Dict[str, Any]],
    start: Any,
    end: Any,
    *,
    excerpt: str = "",
) -> Optional[str]:
    try:
        clip_start = float(start)
        clip_end = float(end)
    except Exception:
        return _detect_title_prompt_language(excerpt)

    language_counts: Dict[str, int] = {}
    for seg in segments or []:
        try:
            seg_start = float(seg.get("start", 0.0) or 0.0)
        except Exception:
            continue
        seg_end_val = seg.get("end")
        seg_dur_val = seg.get("duration")
        try:
            seg_end = float(seg_end_val) if seg_end_val is not None else None
        except Exception:
            seg_end = None
        if seg_end is None:
            try:
                seg_end = seg_start + max(float(seg_dur_val or 0.0), 0.0)
            except Exception:
                seg_end = seg_start
        if seg_end <= clip_start or seg_start >= clip_end:
            continue
        lang = _normalize_title_prompt_language(seg.get("lang") or seg.get("language"))
        if not lang:
            continue
        language_counts[lang] = language_counts.get(lang, 0) + 1

    if language_counts:
        return max(language_counts.items(), key=lambda item: item[1])[0]
    return _detect_title_prompt_language(excerpt)


def _example_matches_language(example: Dict[str, str], language: Optional[str]) -> bool:
    normalized = str(language or "").strip().lower()
    if not normalized:
        return True
    sample = " ".join(
        [
            str(example.get("excerpt") or "").strip(),
            str(example.get("title") or "").strip(),
        ]
    ).strip()
    if not sample:
        return False
    detected = _detect_title_prompt_language(sample)
    if not detected:
        return normalized not in {"tr", "en", "ar"}
    return detected == normalized


def _generic_short_title_examples_for_language(language: Optional[str]) -> List[Dict[str, str]]:
    normalized = str(language or "").strip().lower()
    if normalized == "tr":
        return [
            {
                "excerpt": "Bir insan sürekli aynı hatayı yapıyorsa mesele irade eksikliği değil, yanlış ortamın içinde yaşıyor olabilir.",
                "title": "Aynı Hatanın Asıl Sebebi",
            },
            {
                "excerpt": "Gençlerin en çok sorduğu şey şu: İyi bir başlangıç yapmak için önce neyi bırakmak gerekiyor?",
                "title": "İyi Başlangıç İçin Ne Bırakılmalı",
            },
        ]
    if normalized == "en":
        return [
            {
                "excerpt": "If someone keeps repeating the same mistake, the real problem may not be discipline at all but the environment they stay inside every day.",
                "title": "The Real Reason the Same Mistake Keeps Happening",
            },
            {
                "excerpt": "One of the most common questions is this: what do you need to stop doing first if you want a genuinely strong start?",
                "title": "What You Need to Stop First",
            },
        ]
    return []


def _request_short_title_suggestion(
    excerpt: str,
    *,
    user_id: Any = None,
    current_video_id: Optional[str] = None,
    language_hint: Optional[str] = None,
) -> str:
    if not _openai_client:
        raise RuntimeError("OPENAI_API_KEY missing")
    safe_excerpt = (excerpt or "").strip()[:2000]
    detected_language = _normalize_title_prompt_language(language_hint) or _detect_title_prompt_language(safe_excerpt)
    return generate_clip_title(safe_excerpt, language_hint=detected_language)


def _schedule_async_clip_title_suggestion(
    *,
    video_id: str,
    plan_index: int,
    excerpt: str,
    placeholder_title: str,
    user_id: Any = None,
    language_hint: Optional[str] = None,
    app_obj=None,
) -> None:
    if not app_obj or not _openai_client:
        return
    safe_video_id = str(video_id or "").strip()
    safe_excerpt = str(excerpt or "").strip()
    safe_placeholder = str(placeholder_title or "").strip()
    if not safe_video_id or not safe_excerpt:
        return

    def _worker() -> None:
        with app_obj.app_context():
            try:
                entries = _load_plan_entries(safe_video_id)
                plan_entry = _find_plan_entry(entries, plan_index)
                if not plan_entry:
                    return
                suggestion_excerpt = str(
                    plan_entry.get("transcript_full")
                    or plan_entry.get("excerpt")
                    or safe_excerpt
                    or ""
                ).strip()
                new_title = _request_short_title_suggestion(
                    suggestion_excerpt or safe_excerpt,
                    user_id=user_id,
                    current_video_id=safe_video_id,
                    language_hint=language_hint or _normalize_title_prompt_language(
                        plan_entry.get("language") or plan_entry.get("lang")
                    ),
                )
                if not new_title:
                    return
                existing_title = str(plan_entry.get("title") or "").strip()
                if existing_title and existing_title != safe_placeholder:
                    return
                plan_entry["title"] = new_title
                _write_plan_entries(safe_video_id, entries)
            except Exception:
                current_app.logger.exception(
                    "Async clip title suggestion failed for %s plan %s",
                    safe_video_id,
                    plan_index,
                )

    try:
        threading.Thread(
            target=_worker,
            name=f"clip-title-{safe_video_id}-{plan_index}",
            daemon=True,
        ).start()
    except Exception:
        current_app.logger.exception(
            "Failed to start async clip title suggestion for %s plan %s",
            safe_video_id,
            plan_index,
        )


def _extract_youtube_video_id(video_url: str, fallback_video_id: str = "") -> str:
    raw_url = str(video_url or "").strip()
    if not raw_url:
        return ""
    try:
        parsed = urlparse(raw_url)
    except Exception:
        parsed = None
    if not parsed:
        return ""
    host = (parsed.netloc or "").lower()
    path = (parsed.path or "").strip("/")
    if "youtu.be" in host:
        return path.split("/", 1)[0]
    if "youtube.com" in host:
        if path == "watch":
            query_video_id = (parse_qs(parsed.query or "").get("v") or [""])[0].strip()
            if query_video_id:
                return query_video_id
        if path.startswith("embed/"):
            return path.split("/", 1)[1].strip()
        if path.startswith("shorts/"):
            return path.split("/", 1)[1].strip()
    fallback = str(fallback_video_id or "").strip()
    if fallback and re.fullmatch(r"[A-Za-z0-9_-]{8,20}", fallback):
        return fallback
    return ""


def _resolve_source_video_public_url(video_id: str) -> str:
    clean_video_id = str(video_id or "").strip()
    if not clean_video_id:
        return ""
    candidates = [
        VIDEOS_DIR / f"{clean_video_id}.mp4",
        VIDEOS_DIR / f"{clean_video_id}.mov",
        VIDEOS_DIR / f"{clean_video_id}.mkv",
        VIDEOS_DIR / f"{clean_video_id}.webm",
    ]
    storage = get_media_storage()
    for candidate in candidates:
        key = f"videos/{candidate.name}"
        try:
            resolved = storage.resolve_local_or_s3(key, fallback_local_paths=[candidate])
        except Exception:
            continue
        if resolved.exists and resolved.public_url:
            return resolved.public_url
    return ""


def _build_transcript_player_source(video: Dict[str, Any]) -> Dict[str, str]:
    source_url = str(video.get("video_url") or "").strip()
    if is_storage_reference(source_url):
        direct_url = public_url_for_stored_media(source_url)
        if direct_url:
            return {
                "type": "file",
                "youtube_id": "",
                "watch_url": "",
                "src": direct_url,
            }
    youtube_video_id = _extract_youtube_video_id(source_url, fallback_video_id=str(video.get("video_id") or ""))
    if youtube_video_id:
        return {
            "type": "youtube",
            "youtube_id": youtube_video_id,
            "watch_url": source_url or f"https://www.youtube.com/watch?v={youtube_video_id}",
            "src": "",
        }
    direct_url = _resolve_source_video_public_url(str(video.get("video_id") or ""))
    if direct_url:
        return {
            "type": "file",
            "youtube_id": "",
            "watch_url": "",
            "src": direct_url,
        }
    return {
        "type": "unavailable",
        "youtube_id": "",
        "watch_url": source_url,
        "src": "",
    }


def _resolve_video_id_from_pk(video_pk: Any) -> Optional[str]:
    if not video_pk:
        return None
    try:
        pk_int = int(video_pk)
    except (TypeError, ValueError):
        return None
    editor_context = _active_editor_context()
    owner_user_id = editor_context["owner_user_id"]
    brand_id = editor_context["brand_id"]
    conn = get_db_readonly()
    sql = "SELECT video_id FROM youtube_videos WHERE id = ?"
    params: List[Any] = [pk_int]
    if owner_user_id:
        sql += " AND owner_user_id = ?"
        params.append(owner_user_id)
    if brand_id:
        sql += " AND brand_id = ?"
        params.append(brand_id)
    else:
        sql += " AND brand_id IS NULL"
    row = conn.execute(sql, params).fetchone()
    conn.close()
    if row:
        return row[0]
    return None


def _fetch_scoped_video_row(
    conn,
    video_pk: Any,
    select_clause: str,
):
    if not video_pk:
        return None
    try:
        pk_int = int(video_pk)
    except (TypeError, ValueError):
        return None
    editor_context = _active_editor_context()
    owner_user_id = editor_context["owner_user_id"]
    brand_id = editor_context["brand_id"]
    sql = f"SELECT {select_clause} FROM youtube_videos WHERE id = ?"
    params: List[Any] = [pk_int]
    if owner_user_id:
        sql += " AND owner_user_id = ?"
        params.append(owner_user_id)
    if brand_id:
        sql += " AND brand_id = ?"
        params.append(brand_id)
    else:
        sql += " AND brand_id IS NULL"
    return conn.execute(sql, params).fetchone()


def _fetch_scoped_video_row_with_scope(
    conn,
    video_pk: Any,
    select_clause: str,
    *,
    owner_user_id: Any = None,
    brand_id: Any = None,
):
    if not video_pk:
        return None
    try:
        pk_int = int(video_pk)
    except (TypeError, ValueError):
        return None
    sql = f"SELECT {select_clause} FROM youtube_videos WHERE id = ?"
    params: List[Any] = [pk_int]
    if owner_user_id:
        sql += " AND owner_user_id = ?"
        params.append(owner_user_id)
    if brand_id:
        sql += " AND brand_id = ?"
        params.append(brand_id)
    else:
        sql += " AND brand_id IS NULL"
    return conn.execute(sql, params).fetchone()


def _require_admin_operation_scope(
    *,
    brand_id: Optional[str] = None,
    workspace_kind: Optional[str] = None,
    video_pk: Optional[int] = None,
) -> Dict[str, str]:
    """Resolve an explicit request target and fail closed on every request."""
    brand_id = str(brand_id or request.args.get("admin_operation_brand") or "").strip()
    workspace_kind = str(workspace_kind or request.args.get("admin_operation_kind") or "").strip().lower()
    current_user = getattr(g, "vs_current_user", None) or {}
    conn = get_db_readonly()
    try:
        scope = resolve_admin_operation_request_scope(
            conn,
            current_user=current_user,
            brand_id=brand_id,
            workspace_kind=workspace_kind,
        )
        target_row = None
        if scope and video_pk is not None:
            target_row = conn.execute(
                """
                SELECT 1 FROM youtube_videos
                WHERE id = ? AND owner_user_id = ? AND brand_id = ?
                LIMIT 1
                """,
                [video_pk, scope["owner_user_id"], scope["brand_id"]],
            ).fetchone()
    finally:
        conn.close()
    if not scope or (video_pk is not None and not target_row):
        abort(404)
    set_request_admin_operation_scope(scope)
    return scope


@video_shorts_bp.before_request
def _activate_admin_operation_editor_context() -> None:
    """Activate a target scope only for explicitly marked editor requests.

    The target travels in the URL, not the browser session, so separate tabs
    can safely operate on different brands without changing each other.
    """
    requested_brand_id = str(request.args.get("admin_operation_brand") or "").strip()
    requested_workspace_kind = str(request.args.get("admin_operation_kind") or "").strip().lower()
    if not requested_brand_id and not requested_workspace_kind:
        return

    def _deny(branch: str, *, video_pk: Any = None, **extra: Any) -> None:
        current_user = getattr(g, "vs_current_user", None) or {}
        current_app.logger.warning(
            "admin_operation_resolver_denied branch=%s video_pk=%s route=%s "
            "current_user_id=%s role=%s requested_brand_id=%s workspace_kind=%s extra=%s",
            branch,
            video_pk,
            request.endpoint,
            current_user.get("id"),
            current_user.get("role"),
            requested_brand_id or None,
            requested_workspace_kind or None,
            extra or None,
        )
        abort(404)

    if not requested_brand_id or not requested_workspace_kind:
        _deny("incomplete_request_context")
    route_rule = str(getattr(request.url_rule, "rule", "") or "")
    if not route_rule.startswith(ADMIN_OPERATION_EDITOR_ROUTE_PREFIX):
        _deny("route_not_allowed")
    video_pk = (request.view_args or {}).get("video_pk")
    try:
        video_pk = int(video_pk)
    except (TypeError, ValueError):
        _deny("invalid_video_pk", video_pk=video_pk)

    current_user = getattr(g, "vs_current_user", None) or {}
    if str(current_user.get("role") or "").strip().lower() != "admin":
        _deny("role_not_admin", video_pk=video_pk)

    conn = get_db_readonly()
    try:
        scope = resolve_admin_operation_request_scope(
            conn,
            current_user=current_user,
            brand_id=requested_brand_id,
            workspace_kind=requested_workspace_kind,
        )
        if not scope:
            _deny("request_scope_revalidation_failed", video_pk=video_pk)
        target_row = conn.execute(
            """
            SELECT 1
            FROM youtube_videos
            WHERE id = ? AND owner_user_id = ? AND brand_id = ?
            LIMIT 1
            """,
            [video_pk, scope["owner_user_id"], scope["brand_id"]],
        ).fetchone()
    finally:
        conn.close()
    if not target_row:
        _deny(
            "target_video_owner_brand_mismatch",
            video_pk=video_pk,
            target_owner_id=scope["owner_user_id"],
            target_brand_id=scope["brand_id"],
        )
    set_request_admin_operation_scope(scope)


@video_shorts_bp.after_request
def _preserve_admin_operation_on_editor_redirect(response):
    """Keep a marked editor request in its target context after form redirects."""
    scope = request_admin_operation_scope()
    if not scope:
        return response
    if response.status_code not in {301, 302, 303, 307, 308}:
        return response
    video_pk = (request.view_args or {}).get("video_pk")
    location = response.headers.get("Location")
    if not video_pk or not location:
        return response

    parsed = urlparse(location)
    expected_path = urlparse(
        url_for("video_shorts_bp.generate_short", video_pk=video_pk)
    ).path
    if parsed.path != expected_path:
        return response
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.setdefault("admin_operation_brand", scope["brand_id"])
    query.setdefault("admin_operation_kind", scope["workspace_kind"])
    response.headers["Location"] = parsed._replace(query=urlencode(query)).geturl()
    return response


def _active_editor_context() -> Dict[str, Any]:
    """Return the only owner/brand pair editor code may use for this request."""
    operation_scope = request_admin_operation_scope()
    if operation_scope:
        return {
            "owner_user_id": operation_scope["owner_user_id"],
            "brand_id": operation_scope["brand_id"],
            "workspace_kind": operation_scope["workspace_kind"],
            "is_admin_operation": True,
        }
    current_user = getattr(g, "vs_current_user", None) or {}
    return {
        "owner_user_id": current_user.get("id"),
        "brand_id": current_brand_id(),
        "workspace_kind": "",
        "is_admin_operation": False,
    }


def _editor_url(endpoint: str, *, video_pk: Any, **values: Any) -> str:
    """Build a video-editor URL while preserving a request-local admin scope."""
    endpoint_name = endpoint if "." in endpoint else f"video_shorts_bp.{endpoint}"
    context = _active_editor_context()
    if context["is_admin_operation"]:
        values.setdefault("admin_operation_brand", context["brand_id"])
        values.setdefault("admin_operation_kind", context["workspace_kind"])
    return url_for(endpoint_name, video_pk=video_pk, **values)


def _resolve_brand_subscribe_overlay_path(brand_id: Optional[str]) -> Optional[Path]:
    candidates: List[Path] = []
    if brand_id:
        candidates.extend(
            [
                BRAND_SUBSCRIBE_OVERLAY_DIR / f"{brand_id}.gif",
                BRAND_SUBSCRIBE_OVERLAY_DIR / f"{brand_id}.mp4",
            ]
        )
    candidates.extend(
        [
            Path(__file__).resolve().parent.parent / "static" / "subscribe.gif",
            Path(__file__).resolve().parent.parent / "static" / "subscribe3.gif",
        ]
    )
    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


def _load_category_options(owner_id: Optional[str]) -> List[str]:
    if not owner_id:
        return []
    brand_id = _active_editor_context()["brand_id"]
    conn = None
    try:
        # ensure_categories_schema may write/seed; use writable connection.
        conn = get_db()
        ensure_brand_schema(conn)
        ensure_categories_schema(conn, owner_id)
        rows = conn.execute(
            "SELECT name FROM shorts_categories WHERE user_id = ? AND brand_id = ? ORDER BY lower(name)",
            [owner_id, brand_id],
        ).fetchall()
        return [row[0] for row in rows if row and row[0]]
    except Exception:
        return []
    finally:
        if conn:
            conn.close()


def _update_plan_entry_publish_state(
    video_pk: Any,
    plan_index: str,
    filename: str,
    publish_status: str,
    publish_at_local: Optional[str],
    publish_at_iso: Optional[str],
    title: Optional[str],
    description: Optional[str],
    youtube_id: Optional[str] = None,
) -> bool:
    video_id = _resolve_video_id_from_pk(video_pk)
    if not video_id:
        return False

    entries = _load_plan_entries(video_id)
    if not entries:
        return False

    target_entry = None
    plan_index_str = plan_index or ""
    for entry in entries:
        entry_index = str(entry.get("plan_index") or "")
        if plan_index_str and entry_index and entry_index == plan_index_str:
            target_entry = entry
        elif not plan_index_str and filename and entry.get("output_filename") == filename:
            target_entry = entry
        if target_entry:
            break

    if not target_entry:
        return False

    target_entry["publish_status"] = publish_status
    if publish_at_local:
        target_entry["publish_at"] = publish_at_local
    else:
        target_entry.pop("publish_at", None)
    if publish_at_iso:
        target_entry["publish_at_iso"] = publish_at_iso
    else:
        target_entry.pop("publish_at_iso", None)
    if title:
        target_entry["yt_title"] = title
    if description is not None:
        target_entry["yt_description"] = description
    if youtube_id:
        target_entry["yt_video_id"] = youtube_id
    target_entry["yt_status"] = publish_status

    try:
        _write_plan_entries(video_id, entries)
    except Exception as exc:
        current_app.logger.warning("Could not update publish state for %s: %s", video_id, exc)
        return False

    try:
        clip_name = (
            str(
                target_entry.get("clip_filename")
                or target_entry.get("output_filename")
                or filename
                or ""
            ).strip()
        )
        if clip_name:
            youtube_published_at = (
                datetime.utcnow().replace(microsecond=0).isoformat()
                if publish_status == "published"
                else None
            )
            publish_owner_user_id, publish_brand_id = resolve_stored_token_owner_brand(
                getattr(g, "vs_current_user", {}).get("id"),
                current_brand_id(),
            )
            upsert_generated_video_record(
                user_id=publish_owner_user_id,
                brand_id=publish_brand_id,
                source_video_id=video_id,
                source_channel_type="youtube",
                clip_filename=clip_name,
                output_filename=str(target_entry.get("output_filename") or clip_name),
                storage_file_key=f"short:{clip_name}",
                generation_status=str(target_entry.get("status") or "").strip().lower() or "created",
                publish_status=publish_status,
                youtube_video_id=youtube_id or target_entry.get("yt_video_id"),
                planned_publish_at=publish_at_iso,
                published_at=youtube_published_at,
                plan_run_id=target_entry.get("plan_run_id") or target_entry.get("batch_id"),
                generated_title=target_entry.get("yt_title") or target_entry.get("title"),
                generated_description=target_entry.get("yt_description") or target_entry.get("description"),
                generated_excerpt=target_entry.get("excerpt"),
                generated_transcript_full=target_entry.get("transcript_full"),
                youtube_published_at=youtube_published_at,
                primary_publish_platform="youtube" if publish_status == "published" else None,
                raw_plan_entry=target_entry,
            )
    except Exception as exc:
        current_app.logger.warning("Could not sync lifecycle row for %s: %s", video_id, exc)

    return True


def _load_generated_video_publish_guard_row(
    *,
    source_video_id: str,
    clip_filename: str,
    brand_id: Optional[str],
):
    if not source_video_id or not clip_filename or not brand_id:
        return None
    conn = get_db_readonly()
    try:
        generated_columns = table_columns(conn, "shorts_generated_videos")
        if not generated_columns:
            return None
        row = conn.execute(
            """
            SELECT
                CAST(id AS VARCHAR),
                youtube_video_id,
                publish_status,
                planned_publish_at
            FROM shorts_generated_videos
            WHERE CAST(source_video_id AS VARCHAR) = ?
              AND lower(coalesce(source_channel_type, 'youtube')) = 'youtube'
              AND clip_filename = ?
              AND brand_id = ?
            LIMIT 1
            """,
            [source_video_id, clip_filename, brand_id],
        ).fetchone()
        if not row:
            return None
        return {
            "id": str(row[0] or "").strip() or None,
            "youtube_video_id": str(row[1] or "").strip() or None,
            "publish_status": str(row[2] or "").strip().lower() or None,
            "planned_publish_at": row[3],
        }
    finally:
        conn.close()


def _sync_generated_video_from_plan_entry(
    *,
    source_video_id: str,
    clip_filename: str,
    plan_entry: Optional[Dict[str, Any]],
    generation_status: Optional[str] = None,
    publish_status: Optional[str] = None,
) -> None:
    if not source_video_id or not clip_filename:
        return
    entry = dict(plan_entry or {})
    effective_publish_status = publish_status or entry.get("publish_status")
    effective_generation_status = generation_status or entry.get("status")
    published_at = None
    youtube_published_at = None
    if str(effective_publish_status or "").strip().lower() == "published":
        published_at = datetime.utcnow().replace(microsecond=0).isoformat()
        youtube_published_at = published_at
    publish_owner_user_id = None
    publish_brand_id = None
    if youtube_published_at or entry.get("yt_video_id") or str(effective_publish_status or "").strip().lower() in {"queued", "scheduled", "uploaded", "published"}:
        publish_owner_user_id, publish_brand_id = resolve_stored_token_owner_brand(
            getattr(g, "vs_current_user", {}).get("id"),
            current_brand_id(),
        )
    upsert_generated_video_record(
        user_id=publish_owner_user_id,
        brand_id=publish_brand_id,
        source_video_id=source_video_id,
        source_channel_type="youtube",
        clip_filename=clip_filename,
        output_filename=str(entry.get("output_filename") or clip_filename),
        storage_file_key=f"short:{clip_filename}",
        generation_status=effective_generation_status,
        publish_status=effective_publish_status,
        youtube_video_id=entry.get("yt_video_id"),
        planned_publish_at=entry.get("publish_at_iso") or entry.get("publish_at"),
        published_at=published_at,
        plan_run_id=entry.get("plan_run_id") or entry.get("batch_id"),
        generated_title=entry.get("yt_title") or entry.get("title"),
        generated_description=entry.get("yt_description") or entry.get("description"),
        generated_excerpt=entry.get("excerpt"),
        generated_transcript_full=entry.get("transcript_full"),
        youtube_published_at=youtube_published_at,
        primary_publish_platform="youtube" if youtube_published_at else None,
        raw_plan_entry=entry or None,
    )


def _refresh_plan_publish_status_from_youtube_for_owner(
    video_id: str,
    entries: List[Dict[str, Any]],
    *,
    user_id: Optional[str],
    brand_id: Optional[str],
) -> Dict[str, int]:
    checked_count = 0
    updated_count = 0
    if not has_refresh_token(user_id=user_id, brand_id=brand_id):
        current_app.logger.info(
            "YouTube status refresh skipped for %s: missing refresh token user_id=%s brand_id=%s.",
            video_id,
            user_id,
            brand_id,
        )
        return {"checked_count": 0, "updated_count": 0}
    video_ids = [
        entry.get("yt_video_id")
        for entry in entries
        if entry.get("yt_video_id") and entry.get("publish_status") != "published"
    ]
    video_ids = [vid for vid in video_ids if vid]
    checked_count = len(video_ids)
    if not video_ids:
        current_app.logger.info(
            "YouTube status refresh skipped for %s: no pending video ids.",
            video_id,
        )
        return {"checked_count": 0, "updated_count": 0}
    statuses = fetch_video_statuses(video_ids, user_id=user_id, brand_id=brand_id)
    updated = False
    for entry in entries:
        vid = entry.get("yt_video_id")
        if not vid:
            current_app.logger.debug("Skipping refresh for plan entry %s because yt_video_id is missing.", entry.get("plan_index"))
            continue
        if entry.get("publish_status") == "published":
            continue
        status = statuses.get(vid) or {}
        privacy_status = status.get("privacyStatus")
        current_app.logger.info(
            "YouTube status refresh video=%s plan_index=%s privacyStatus=%s",
            vid,
            entry.get("plan_index"),
            privacy_status or "missing",
        )
        if privacy_status in {"public", "unlisted"}:
            entry["publish_status"] = "published"
            entry["yt_status"] = "published"
            updated = True
            updated_count += 1
            try:
                clip_name = str(entry.get("clip_filename") or entry.get("output_filename") or "").strip()
                if clip_name:
                    _sync_generated_video_from_plan_entry(
                        source_video_id=video_id,
                        clip_filename=clip_name,
                        plan_entry=entry,
                        publish_status="published",
                    )
            except Exception as exc:
                current_app.logger.warning(
                    "Failed to sync lifecycle publish refresh for %s plan %s: %s",
                    video_id,
                    entry.get("plan_index"),
                    exc,
                )
    if updated:
        try:
            _write_plan_entries(video_id, entries)
        except Exception as exc:
                current_app.logger.warning(
                    "Failed to refresh plan publish status from YouTube for %s: %s",
                    video_id,
                    exc,
                )
    return {"checked_count": checked_count, "updated_count": updated_count}


def _refresh_plan_publish_status_from_youtube(video_id: str, entries: List[Dict[str, Any]]) -> None:
    current_user = getattr(g, "vs_current_user", None) or {}
    _refresh_plan_publish_status_from_youtube_for_owner(
        video_id,
        entries,
        user_id=current_user.get("id"),
        brand_id=current_brand_id(),
    )


def _build_display_timing(start: Any, end: Any, plan_start: Any = None, plan_end: Any = None) -> Dict[str, Optional[str]]:
    display_start = _to_float(plan_start if plan_start is not None else start)
    display_end = _to_float(plan_end if plan_end is not None else end)
    start_label = _format_time_label(display_start) if display_start is not None else None
    end_label = _format_time_label(display_end) if display_end is not None else None
    duration = None
    if display_start is not None and display_end is not None:
        duration = max(0.0, display_end - display_start)
    duration_label = _format_time_label(duration) if duration is not None else None
    return {
        "display_start_label": start_label,
        "display_end_label": end_label,
        "display_duration_label": duration_label,
    }


def _fetch_video_with_transcript(video_pk: int) -> Optional[Tuple[str, str, Optional[float], str, List[Dict[str, Any]]]]:
    conn = get_db_readonly()
    row = _fetch_scoped_video_row(conn, video_pk, "video_id, title, duration_seconds")
    if not row:
        conn.close()
        return None
    video_id, title, duration_seconds = row
    transcript_text, segments = _fetch_transcript(conn, video_id)
    conn.close()
    return video_id, title, duration_seconds, transcript_text, segments or []


def _fetch_video_with_transcript_for_scope(
    video_pk: int,
    *,
    owner_user_id: Any = None,
    brand_id: Any = None,
) -> Optional[Tuple[str, str, Optional[float], str, List[Dict[str, Any]]]]:
    conn = get_db_readonly()
    row = _fetch_scoped_video_row_with_scope(
        conn,
        video_pk,
        "video_id, title, duration_seconds",
        owner_user_id=owner_user_id,
        brand_id=brand_id,
    )
    if not row:
        conn.close()
        return None
    video_id, title, duration_seconds = row
    transcript_text, segments = _fetch_transcript(conn, video_id)
    conn.close()
    return video_id, title, duration_seconds, transcript_text, segments or []


def _build_override_segments_for_karaoke(
    text: str,
    clip_start: float,
    clip_end: float,
) -> List[Dict[str, Any]]:
    override_text = str(text or "").strip()
    if not override_text:
        return []
    duration = max(float(clip_end) - float(clip_start), 0.01)
    return [
        {
            "start": float(clip_start),
            "end": float(clip_end),
            "duration": duration,
            "text": override_text,
            "tr_text": override_text,
            "ar_text": None,
            # No real word timings available for manual override text.
            # The karaoke renderer will synthesize proportional timings for display.
            "words": [],
            "word_tags": [],
        }
    ]


def _get_font_settings_from_session(
    video_font_key: Optional[str] = None,
    title_font_size_override: Optional[int] = None,
    video_sub_font_key: Optional[str] = None,
    subtitle_font_size_override: Optional[int] = None,
    subtitle_margin_override: Optional[int] = None,
    title_margin_override: Optional[int] = None,
    title_bg_color_override: Optional[str] = None,
    title_bg_alpha_override: Optional[int] = None,
    title_text_color_override: Optional[str] = None,
    subtitle_text_color_override: Optional[str] = None,
    subtitle_bg_color_override: Optional[str] = None,
    subtitle_bg_alpha_override: Optional[int] = None,
    subtitle_text_alpha_override: Optional[int] = None,
) -> Tuple[Optional[Dict[str, Any]], str, int, int, int, int, str, int, str, str, str, int, int]:
    font_key = video_font_key or DEFAULT_EDITOR_TITLE_FONT_KEY
    font_choice = _build_title_font_choice(font_key)
    sub_font_key = video_sub_font_key or DEFAULT_SUB_FONT_KEY
    sub_font_choice = next(
        (
            {"key": f[0], "label": f[1], "path": f[2], "fontname": f[3]}
            for f in SUB_FONT_CHOICES
            if f[0] == sub_font_key
        ),
        None,
    )
    sub_font_name = sub_font_choice["fontname"] if sub_font_choice else "DejaVu Sans"
    title_font_size = (
        title_font_size_override or DEFAULT_EDITOR_TITLE_FONT_SIZE
    )
    sub_font_size = (
        subtitle_font_size_override or DEFAULT_SUB_FONT_SIZE
    )
    if subtitle_margin_override is not None:
        sub_margin = subtitle_margin_override
    else:
        sub_margin = SUB_MARGIN_DEFAULT
    if title_margin_override is not None:
        title_margin = title_margin_override
    else:
        title_margin = DEFAULT_TITLE_MARGIN
    if title_bg_color_override is not None:
        title_bg_color = _normalize_hex_color(title_bg_color_override, DEFAULT_TITLE_BG_COLOR)
    else:
        title_bg_color = _normalize_hex_color(DEFAULT_EDITOR_TITLE_BG_COLOR, DEFAULT_TITLE_BG_COLOR)
    title_bg_alpha = _normalize_alpha_percent(
        title_bg_alpha_override if title_bg_alpha_override is not None else DEFAULT_EDITOR_TITLE_BG_ALPHA,
        DEFAULT_TITLE_BG_ALPHA,
    )
    title_text_color = _normalize_hex_color(
        title_text_color_override if title_text_color_override is not None else DEFAULT_EDITOR_TITLE_TEXT_COLOR,
        DEFAULT_TITLE_TEXT_COLOR,
    )
    subtitle_text_color = _normalize_hex_color(
        subtitle_text_color_override if subtitle_text_color_override is not None else DEFAULT_SUBTITLE_TEXT_COLOR,
        DEFAULT_SUBTITLE_TEXT_COLOR,
    )
    subtitle_bg_color = _normalize_hex_color(
        subtitle_bg_color_override if subtitle_bg_color_override is not None else DEFAULT_SUBTITLE_BG_COLOR,
        DEFAULT_SUBTITLE_BG_COLOR,
    )
    subtitle_bg_alpha = _normalize_alpha_percent(
        subtitle_bg_alpha_override if subtitle_bg_alpha_override is not None else DEFAULT_SUBTITLE_BG_ALPHA,
        DEFAULT_SUBTITLE_BG_ALPHA,
    )
    subtitle_text_alpha = _normalize_alpha_percent(
        subtitle_text_alpha_override if subtitle_text_alpha_override is not None else DEFAULT_SUBTITLE_TEXT_ALPHA,
        DEFAULT_SUBTITLE_TEXT_ALPHA,
    )
    return (
        font_choice,
        sub_font_name,
        title_font_size,
        sub_font_size,
        sub_margin,
        title_margin,
        title_bg_color,
        title_bg_alpha,
        title_text_color,
        subtitle_text_color,
        subtitle_bg_color,
        subtitle_bg_alpha,
        subtitle_text_alpha,
    )


@video_shorts_bp.route("/generate/<int:video_pk>")
def generate_short(video_pk):
    ensure_video_shorts_tmp_dir()
    cleanup_video_shorts_temp_dir()
    try:
        conn_rw = get_db()
        ensure_brand_schema(conn_rw)
        _ensure_video_crop_schema(conn_rw)
        ensure_static_images_schema(conn_rw)
    finally:
        try:
            conn_rw.close()
        except Exception:
            pass
    current_user = getattr(g, "vs_current_user", None)
    hide_clip_coachmark = load_user_bool_preference(
        current_user.get("id") if current_user else None,
        HIDE_CLIP_COACHMARK_PREFERENCE_KEY,
        default=False,
    )
    editor_context = _active_editor_context()
    brand_id = editor_context["brand_id"]
    editor_owner_user_id = editor_context["owner_user_id"]

    conn = get_db_readonly()
    video_columns = table_columns(conn, "youtube_videos")
    video_sql = """
        id, channel_id, video_id, title, video_url, thumbnail_url, duration_seconds,
        view_count, like_count, comment_count, published_at,
        split_enabled, crop_x_ratio, crop_y_ratio, crop_w_ratio, crop_h_ratio, crop2_x_ratio, crop2_y_ratio, crop2_w_ratio, crop2_h_ratio,
        crop_aspect,
        title_font_key, title_font_size, subtitle_font_key, subtitle_font_size, subtitle_margin, subtitle_style, title_margin, title_line_spacing, title_bg_color, title_bg_alpha, title_text_color, subtitle_text_color, subtitle_bg_color, subtitle_bg_alpha, subtitle_text_alpha, video_date_text, video_date_top, show_title, show_subtitle, subscribe_overlay_enabled,
        is_music_only,
        static_visual_key,
        background_visual_key,
        download_status,
        transcript_status,
        video_overlay_offset,
        podcast_audio_filename,
        visual_mode,
        podcast_overlay_short_ids,
        owner_user_id
    """
    if "subtitle_preset" in video_columns:
        video_sql += ", subtitle_preset"
    if "creator_name" in video_columns:
        video_sql += ", creator_name"
    if "creator_email" in video_columns:
        video_sql += ", creator_email"
    row = _fetch_scoped_video_row(conn, video_pk, video_sql)
    if not row:
        conn.close()
        flash("Video not found", "danger")
        return redirect(url_for("video_shorts_bp.channels_page"))

    cols = [
        "id", "channel_id", "video_id", "title", "video_url", "thumbnail_url", "duration_seconds",
        "view_count", "like_count", "comment_count", "published_at",
        "split_enabled", "crop_x_ratio", "crop_y_ratio", "crop_w_ratio", "crop_h_ratio", "crop2_x_ratio", "crop2_y_ratio", "crop2_w_ratio", "crop2_h_ratio",
        "crop_aspect",
        "title_font_key", "title_font_size", "subtitle_font_key", "subtitle_font_size", "subtitle_margin", "subtitle_style", "title_margin", "title_line_spacing", "title_bg_color", "title_bg_alpha", "title_text_color", "subtitle_text_color", "subtitle_bg_color", "subtitle_bg_alpha", "subtitle_text_alpha", "video_date_text", "video_date_top", "show_title", "show_subtitle", "subscribe_overlay_enabled",
        "is_music_only",
        "static_visual_key",
        "background_visual_key",
        "download_status",
        "transcript_status",
        "video_overlay_offset",
        "podcast_audio_filename",
        "visual_mode",
        "podcast_overlay_short_ids",
        "owner_user_id",
    ]
    if "subtitle_preset" in video_columns:
        cols.append("subtitle_preset")
    if "creator_name" in video_columns:
        cols.append("creator_name")
    if "creator_email" in video_columns:
        cols.append("creator_email")
    video = dict(zip(cols, row))
    video_duration_label = _format_time_label(video["duration_seconds"]) if video.get("duration_seconds") else None
    if video_duration_label and video_duration_label.endswith(".000"):
        video_duration_label = video_duration_label[:-4]

    transcript_text, segments = _fetch_transcript(conn, video["video_id"])
    latest_upload_session = conn.execute(
        """
        SELECT status
        FROM shorts_quick_sessions
        WHERE video_pk = ?
        ORDER BY created_at DESC
        LIMIT 1
        """,
        [video_pk],
    ).fetchone()
    has_active_upload_job = conn.execute(
        """
        SELECT 1
        FROM shorts_render_jobs
        WHERE type IN ('normalize_upload', 'transcribe_upload')
          AND status IN ('queued', 'processing', 'running')
          AND payload_json ->> 'video_pk' = ?
        LIMIT 1
        """,
        [str(video_pk)],
    ).fetchone()
    conn.close()
    upload_processing = bool(
        latest_upload_session
        and str(latest_upload_session[0] or "").strip().lower() == "ingesting"
        and has_active_upload_job
    )
    def _joined_transcript_ar(seg_list: List[Dict[str, Any]]) -> str:
        def looks_turkish(text: str) -> bool:
            if not text:
                return False
            turkish_chars = "çğıöşüÇĞİÖŞÜ"
            if any(ch in turkish_chars for ch in text):
                return True
            lt = text.lower()
            turkish_hints = [
                "dır", "dir", "dur", "dür",
                "dır.", "dir.", "dur.", "dür.",
                "siniz", "sınız", "sünüz", "sunuz",
                "olursanız", "severseniz", "yardım", "dinine",
                "sizi", "sana", "bunu", "bunları", "olarak",
                "görüyoruz", "yolunda", "terk etmeyecek",
                "ne de seni terk etti", "ne de seni", "terk etti",
                "bu mukavelelerle", "görüyoruz ki",
            ]
            return any(hint in lt for hint in turkish_hints)

        parts = []
        for seg in (seg_list or []):
            label = seg.get("label")
            lang = (seg.get("lang") or "").lower()
            tr_txt = (seg.get("tr_text") or seg.get("text") or "").strip()
            ar_txt = (seg.get("ar_text") or "").strip()

            use_ar = False
            if label == "turkish_speech":
                use_ar = False
            elif label == "arabic_prayer_or_quran" or lang == "ar":
                if ar_txt and not looks_turkish(tr_txt):
                    if len(tr_txt.split()) <= 15:
                        use_ar = True

            txt = ar_txt if use_ar else tr_txt
            if txt:
                parts.append(txt)

        return " ".join(parts).strip()

    transcript_text_tr = _joined_transcript_tr(segments) or (transcript_text or "")
    transcript_text_ar = _joined_transcript_ar(segments) or transcript_text_tr
    transcript_language = _resolve_video_language(segments, transcript_text_tr or transcript_text or "")

    non_speech_overrides = load_non_speech_overrides(video["video_id"])
    segments_view = []
    for idx, seg in enumerate(segments):
        start = seg.get("start")
        end = seg.get("end")
        # Build word-level entries combining words + tags
        words_raw = seg.get("words") or []
        tags_raw = seg.get("word_tags")
        wt_entries = []
        if isinstance(words_raw, list):
            # Ensure tags list matches length; default turkish_speech
            tag_list = []
            if isinstance(tags_raw, list) and len(tags_raw) == len(words_raw):
                tag_list = [str(t or "turkish_speech") for t in tags_raw]
            elif isinstance(tags_raw, list):
                # length mismatch: fall back to turkish_speech
                tag_list = ["turkish_speech"] * len(words_raw)
            else:
                tag_list = ["turkish_speech"] * len(words_raw)
            for w_idx, w in enumerate(words_raw):
                try:
                    word_txt = w.get("word") if isinstance(w, dict) else getattr(w, "word", None)
                except Exception:
                    word_txt = None
                try:
                    w_start = w.get("start") if isinstance(w, dict) else getattr(w, "start", None)
                except Exception:
                    w_start = None
                try:
                    w_end = w.get("end") if isinstance(w, dict) else getattr(w, "end", None)
                except Exception:
                    w_end = None
                wt_entries.append(
                    {
                        "word": word_txt,
                        "start": w_start,
                        "end": w_end,
                        "tag": tag_list[w_idx] if w_idx < len(tag_list) else "turkish_speech",
                        "lang": None,
                    }
                )

        segments_view.append(
            {
                "start": start,
                "start_label": _format_time_label(start),
                "end": end,
                "end_label": _format_time_label(end) if end is not None else None,
                "text": (seg.get("tr_text") or seg.get("text") or seg.get("ar_text") or ""),
                "tr_text": seg.get("tr_text") or "",
                "ar_text": seg.get("ar_text"),
                "lang": seg.get("lang"),
                "word_tags": wt_entries,
                "non_speech_type": non_speech_overrides.get(str(idx), ""),
            }
        )

    SHORTS_DIR.mkdir(parents=True, exist_ok=True)
    short_path = SHORTS_DIR / f"{video['video_id']}.mp4"
    short_exists = short_path.exists()
    source_path = None
    preview_url = None
    preview_path = _preview_frame_cache_path(video["video_id"])
    if not preview_path.exists():
        source_path, source_path_is_temp = _resolve_source_video(video["video_id"])
        try:
            if source_path:
                try:
                    preview_path = _ensure_preview_frame(
                        video["video_id"],
                        source_path,
                        video.get("duration_seconds"),
                    ) or preview_path
                except FileNotFoundError:
                    current_app.logger.warning(
                        "Skipping preview frame generation for %s because ffmpeg is unavailable.",
                        video["video_id"],
                    )
        finally:
            _cleanup_resolved_source_video(source_path, source_path_is_temp)
    crop_is_unset = all(
        video.get(key) is None
        for key in ("crop_x_ratio", "crop_y_ratio", "crop_w_ratio", "crop_h_ratio")
    )
    if crop_is_unset:
        applied_crop = _maybe_apply_face_centered_default_crop(
            video_row_id=int(video["id"]),
            video_id=str(video["video_id"]),
            owner_user_id=editor_owner_user_id,
            brand_id=brand_id,
            preview_metadata=_load_preview_frame_metadata(str(video["video_id"])),
        )
        if applied_crop:
            video.update(
                {
                    "crop_x_ratio": applied_crop["crop_x_ratio"],
                    "crop_y_ratio": applied_crop["crop_y_ratio"],
                    "crop_w_ratio": applied_crop["crop_w_ratio"],
                    "crop_h_ratio": applied_crop["crop_h_ratio"],
                    "crop_aspect": applied_crop["crop_aspect"],
                }
            )
    if preview_path.exists():
        try:
            rel_preview = preview_path.resolve().relative_to(SHORTS_DIR.parent.resolve())
            preview_url = url_for("video_shorts_bp.static", filename=str(rel_preview))
        except Exception:
            preview_url = None
    title_font_options = [
        {"key": key, "label": cfg["label"], "css_family": cfg["css_family"]}
        for key, cfg in TITLE_FONTS.items()
        if Path(cfg.get("path") or "").exists()
    ]
    if not title_font_options:
        default_cfg = TITLE_FONTS.get(DEFAULT_EDITOR_TITLE_FONT_KEY)
        if default_cfg:
            title_font_options = [
                {
                    "key": DEFAULT_EDITOR_TITLE_FONT_KEY,
                    "label": default_cfg["label"],
                    "css_family": default_cfg["css_family"],
                }
            ]

    # Subtitle fonts
    sub_fonts = []
    for key, label, path, fontname in SUB_FONT_CHOICES:
        exists = Path(path).exists()
        if exists:
            sub_fonts.append({"key": key, "label": label, "path": path, "fontname": fontname})
    if not sub_fonts:
        sub_fonts.append({"key": "dejavu", "label": "DejaVu Sans", "path": "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "fontname": "DejaVu Sans"})

    video_font_key = video.get("title_font_key") or DEFAULT_EDITOR_TITLE_FONT_KEY
    video_title_font_size = video.get("title_font_size") or DEFAULT_EDITOR_TITLE_FONT_SIZE
    video_sub_font_key = video.get("subtitle_font_key") or DEFAULT_SUB_FONT_KEY
    video_sub_font_size = video.get("subtitle_font_size") or DEFAULT_SUB_FONT_SIZE
    video_sub_margin = video.get("subtitle_margin") or SUB_MARGIN_DEFAULT
    video_subtitle_style = str(video.get("subtitle_style") or "plain").strip().lower()
    if video_subtitle_style not in {"plain", "karaoke"}:
        video_subtitle_style = "plain"
    video_subtitle_preset = str(video.get("subtitle_preset") or DEFAULT_SUBTITLE_PRESET).strip() or DEFAULT_SUBTITLE_PRESET
    if video_subtitle_preset not in SUBTITLE_PRESETS:
        video_subtitle_preset = DEFAULT_SUBTITLE_PRESET
    video_title_margin = video.get("title_margin") or DEFAULT_TITLE_MARGIN
    video_title_line_spacing = video.get("title_line_spacing")
    try:
        video_title_line_spacing = int(video_title_line_spacing if video_title_line_spacing is not None else -4)
    except Exception:
        video_title_line_spacing = -4
    video_title_bg_color = video.get("title_bg_color") or DEFAULT_EDITOR_TITLE_BG_COLOR
    video_title_bg_alpha = _normalize_alpha_percent(video.get("title_bg_alpha"), DEFAULT_EDITOR_TITLE_BG_ALPHA)
    video_title_text_color = video.get("title_text_color") or DEFAULT_EDITOR_TITLE_TEXT_COLOR
    video_subtitle_text_color = video.get("subtitle_text_color") or DEFAULT_SUBTITLE_TEXT_COLOR
    video_subtitle_bg_color = video.get("subtitle_bg_color") or DEFAULT_SUBTITLE_BG_COLOR
    video_subtitle_bg_alpha = _normalize_alpha_percent(video.get("subtitle_bg_alpha"), DEFAULT_SUBTITLE_BG_ALPHA)
    video_subtitle_text_alpha = _normalize_alpha_percent(video.get("subtitle_text_alpha"), DEFAULT_SUBTITLE_TEXT_ALPHA)
    video_date_text = video.get("video_date_text") or ""
    try:
        video_date_top = int(video.get("video_date_top") or DEFAULT_VIDEO_DATE_TOP)
    except Exception:
        video_date_top = DEFAULT_VIDEO_DATE_TOP
    raw_subscribe = video.get("subscribe_overlay_enabled")
    video_subscribe_overlay = _normalize_optional_bool(raw_subscribe, default=True)
    raw_show_title = video.get("show_title")
    video_show_title = _normalize_optional_bool(raw_show_title, default=True)
    raw_show_subtitle = video.get("show_subtitle")
    video_show_subtitle = _normalize_optional_bool(raw_show_subtitle, default=True)
    raw_is_music_only = video.get("is_music_only")
    video_is_music_only = bool(raw_is_music_only) if raw_is_music_only is not None else False
    selected_podcast_audio_filename = (video.get("podcast_audio_filename") or "").strip()
    selected_visual_mode = (video.get("visual_mode") or "video").strip().lower()
    if selected_visual_mode not in {"video", "static", "created", "podcast"}:
        selected_visual_mode = "video"
    selected_podcast_overlay_short_ids: List[str] = []
    try:
        raw_overlay_ids = (video.get("podcast_overlay_short_ids") or "").strip()
        if raw_overlay_ids:
            parsed_overlay_ids = json.loads(raw_overlay_ids)
            if isinstance(parsed_overlay_ids, list):
                selected_podcast_overlay_short_ids = [
                    str(item).strip()
                    for item in parsed_overlay_ids
                    if str(item or "").strip().startswith("short:")
                ][:2]
    except Exception:
        selected_podcast_overlay_short_ids = []
    try:
        video_overlay_offset = int(video.get("video_overlay_offset") or DEFAULT_VIDEO_OVERLAY_OFFSET)
    except Exception:
        video_overlay_offset = DEFAULT_VIDEO_OVERLAY_OFFSET

    video_title_bg_color = _normalize_hex_color(video_title_bg_color, DEFAULT_EDITOR_TITLE_BG_COLOR)
    video_title_text_color = _normalize_hex_color(video_title_text_color, DEFAULT_EDITOR_TITLE_TEXT_COLOR)
    video_subtitle_text_color = _normalize_hex_color(video_subtitle_text_color, DEFAULT_SUBTITLE_TEXT_COLOR)
    video_subtitle_bg_color = _normalize_hex_color(video_subtitle_bg_color, DEFAULT_SUBTITLE_BG_COLOR)
    matched_style_template_key = _match_style_template_key(
        subtitle_preset=video_subtitle_preset,
        title_font_key=video_font_key,
        title_text_color=video_title_text_color,
        title_bg_color=video_title_bg_color,
        title_bg_alpha=video_title_bg_alpha,
        title_font_size=video_title_font_size,
    )
    clip_has_saved_style_template = bool(
        matched_style_template_key
        and matched_style_template_key != DEFAULT_STYLE_TEMPLATE_KEY
    )

    static_visual_options = []
    user_background_visual_options = []
    user_subscribe_visual_options = []
    created_visual_options = []
    if editor_owner_user_id:
        conn_images = get_db_readonly()
        job_rows = []
        try:
            background_rows = conn_images.execute(
                """
                SELECT i.id, i.label, i.filename, COALESCE(i.use_as_background, false)
                FROM shorts_static_images i
                WHERE user_id = ? AND brand_id = ? AND COALESCE(is_active, true) = true
                  AND COALESCE(i.asset_kind, 'background') = 'background'
                ORDER BY i.created_at
                """,
                [editor_owner_user_id, brand_id],
            ).fetchall()
            subscribe_rows = conn_images.execute(
                """
                SELECT i.id, i.label, i.filename, COALESCE(i.file_ext, '')
                FROM shorts_static_images i
                WHERE user_id = ? AND brand_id = ? AND COALESCE(is_active, true) = true
                  AND COALESCE(i.asset_kind, 'background') = 'subscribe'
                ORDER BY i.created_at
                """,
                [editor_owner_user_id, brand_id],
            ).fetchall()
            job_rows = conn_images.execute(
                """
                SELECT job_id, output_url, created_at, payload_json
                FROM image_to_video_jobs
                WHERE user_id = ?
                  AND brand_id = ?
                  AND lower(coalesce(status, '')) = 'done'
                  AND output_url IS NOT NULL
                  AND output_url <> ''
                ORDER BY created_at DESC
                LIMIT 50
                """,
                [editor_owner_user_id, brand_id],
            ).fetchall()
        finally:
            conn_images.close()
        for idx, row in enumerate(background_rows, start=1):
            image_url = _user_image_public_url(editor_owner_user_id, row[2])
            static_visual_options.append(
                {
                    "key": f"user:{row[0]}",
                    "label": row[1] or f"U{idx}",
                    "image_url": image_url,
                }
            )
            if bool(row[3]) if len(row) > 3 else False:
                user_background_visual_options.append(
                    {
                        "key": f"userbg:{row[0]}",
                        "label": row[1] or f"BG{idx}",
                        "image_url": image_url,
                        "description": "User background",
                    }
                )
        for idx, row in enumerate(subscribe_rows, start=1):
            user_subscribe_visual_options.append(
                {
                    "key": f"usersub:{row[0]}",
                    "label": _humanize_visual_asset_label(row[1], row[2], f"Subscribe {idx}"),
                    "image_url": _user_image_public_url(editor_owner_user_id, row[2]),
                    "file_ext": str(row[3] or Path(row[2] or "").suffix.lstrip(".")).lower(),
                    "description": "Workspace subscribe animation",
                }
            )
        for idx, row in enumerate(job_rows, start=1):
            job_id = str(row[0] or "").strip()
            output_url = str(row[1] or "").strip()
            if not job_id or not output_url:
                continue
            candidate = None
            video_url = ""
            cleanup_candidate = False
            try:
                candidate, video_url, cleanup_candidate = _resolve_image_to_video_media(job_id, output_url)
                if not candidate:
                    continue
            except Exception:
                continue
            try:
                duration_seconds = _probe_media_duration_seconds(candidate)
                created_label = f"V{idx}"
                if row[2]:
                    try:
                        created_label = row[2].strftime("%m-%d-%Y")
                    except Exception:
                        created_label = f"V{idx}"
                has_music = False
                music_level_percent = None
                payload_json = row[3] if len(row) > 3 else None
                if payload_json:
                    try:
                        payload_obj = json.loads(payload_json)
                        has_music = bool((payload_obj or {}).get("music_filename"))
                        if has_music:
                            gain = float((payload_obj or {}).get("music_volume") or 0)
                            if gain > 0:
                                music_level_percent = max(0, min(100, int(round(gain * 100))))
                    except Exception:
                        has_music = False
                        music_level_percent = None
                preview_image_url = None
                try:
                    preview_path = _ensure_preview_frame(
                        f"i2v_{job_id}",
                        candidate,
                        float(duration_seconds) if duration_seconds is not None else None,
                    )
                    if preview_path:
                        rel_preview = preview_path.resolve().relative_to(SHORTS_DIR.parent.resolve())
                        preview_image_url = url_for("video_shorts_bp.static", filename=str(rel_preview))
                except Exception:
                    preview_image_url = None
                created_visual_options.append(
                    {
                        "key": f"i2v:{job_id}",
                        "label": created_label,
                        "video_url": video_url or output_url,
                        "duration_seconds": duration_seconds,
                        "preview_image_url": preview_image_url,
                        "has_music": has_music,
                        "music_level_percent": music_level_percent,
                    }
                )
            finally:
                if cleanup_candidate and candidate:
                    try:
                        candidate.unlink()
                    except Exception:
                        pass
    static_visual_map = {opt["key"]: opt for opt in static_visual_options}
    created_visual_map = {opt["key"]: opt for opt in created_visual_options}
    video_static_visual_key = video.get("static_visual_key")
    active_static_visual = static_visual_map.get(video_static_visual_key)
    active_created_visual = created_visual_map.get(video_static_visual_key)
    static_visual_label = active_static_visual["label"] if active_static_visual else None
    created_visual_label = active_created_visual["label"] if active_created_visual else None
    selected_visual_label = static_visual_label or created_visual_label
    system_background_visual_options = [
        {
            "key": make_system_background_key(system_path.name),
            "label": system_path.stem.replace("_", " "),
            "image_url": url_for("video_shorts_bp.static", filename=system_background_static_filename(system_path)),
            "description": "System background",
        }
        for system_path in list_system_background_paths()
    ]
    bg_visual_options = list(system_background_visual_options) + list(user_background_visual_options)
    bg_visual_map = {opt["key"]: opt for opt in bg_visual_options}
    system_subscribe_visual_options = [
        {
            "key": make_system_subscribe_key(system_path.name),
            "label": system_path.stem.replace("_", " "),
            "image_url": url_for("video_shorts_bp.static", filename=system_subscribe_static_filename(system_path)),
            "file_ext": system_path.suffix.lstrip(".").lower(),
            "description": "System subscribe animation",
        }
        for system_path in list_system_subscribe_paths()
    ]
    subscribe_visual_options = list(system_subscribe_visual_options) + list(user_subscribe_visual_options)
    subscribe_visual_map = {opt["key"]: opt for opt in subscribe_visual_options}
    preferred_bg_key = None
    if editor_owner_user_id:
        preferred_bg_key = load_background_preference(editor_owner_user_id, brand_id)
        if preferred_bg_key not in bg_visual_map:
            preferred_bg_key = None
    video_background_visual_key = preferred_bg_key or video.get("background_visual_key")
    if not video_background_visual_key:
        auto_bg_path = choose_deterministic_system_background(str(video.get("video_id") or ""))
        if auto_bg_path:
            candidate_key = make_system_background_key(auto_bg_path.name)
            if candidate_key in bg_visual_map:
                video_background_visual_key = candidate_key
    active_bg_visual = bg_visual_map.get(video_background_visual_key)
    background_visual_label = active_bg_visual["label"] if active_bg_visual else None
    video_subscribe_overlay_key = _load_subscribe_overlay_key(
        editor_owner_user_id,
        brand_id,
        video_pk,
    )
    if video_subscribe_overlay_key not in subscribe_visual_map:
        video_subscribe_overlay_key = None
    if not video_subscribe_overlay_key:
        auto_subscribe_path = choose_deterministic_system_subscribe(str(video.get("video_id") or ""))
        if auto_subscribe_path:
            candidate_key = make_system_subscribe_key(auto_subscribe_path.name)
            if candidate_key in subscribe_visual_map:
                video_subscribe_overlay_key = candidate_key
    active_subscribe_visual = subscribe_visual_map.get(video_subscribe_overlay_key)
    subscribe_visual_label = active_subscribe_visual["label"] if active_subscribe_visual else None

    session["vs_font"] = video_font_key
    session["vs_sub_font"] = video_sub_font_key
    session["vs_title_font_size"] = video_title_font_size or DEFAULT_TITLE_FONT_SIZE
    session["vs_sub_font_size"] = video_sub_font_size
    session["vs_sub_margin"] = video_sub_margin
    session["vs_subtitle_style"] = video_subtitle_style
    session["vs_title_margin"] = video_title_margin
    session["vs_title_line_spacing"] = video_title_line_spacing
    session["vs_title_bg_color"] = video_title_bg_color
    session["vs_title_bg_alpha"] = video_title_bg_alpha
    session["vs_title_text_color"] = video_title_text_color
    session["vs_subtitle_text_color"] = video_subtitle_text_color
    session["vs_subtitle_bg_color"] = video_subtitle_bg_color
    session["vs_subtitle_bg_alpha"] = video_subtitle_bg_alpha
    session["vs_subtitle_text_alpha"] = video_subtitle_text_alpha
    session["vs_video_date_text"] = video_date_text
    session["vs_video_date_top"] = video_date_top
    session["vs_subscribe_overlay"] = (
        video_subscribe_overlay if video_subscribe_overlay is not None else True
    )
    session["vs_show_title"] = video_show_title if video_show_title is not None else True
    session["vs_show_subtitle"] = video_show_subtitle if video_show_subtitle is not None else True
    session["vs_video_overlay_offset"] = video_overlay_offset

    selected_sub_font = video_sub_font_key
    selected_title_font_size = video_title_font_size
    selected_sub_font_size = video_sub_font_size
    selected_sub_margin = video_sub_margin
    selected_subtitle_style = video_subtitle_style
    selected_subtitle_preset = video_subtitle_preset
    selected_title_margin = video_title_margin
    selected_title_line_spacing = video_title_line_spacing
    selected_title_bg_color = video_title_bg_color
    selected_title_bg_alpha = video_title_bg_alpha
    selected_title_text_color = video_title_text_color
    selected_subtitle_text_color = video_subtitle_text_color
    selected_subtitle_bg_color = video_subtitle_bg_color
    selected_subtitle_bg_alpha = video_subtitle_bg_alpha
    selected_subtitle_text_alpha = video_subtitle_text_alpha
    available_title_font_keys = {item["key"] for item in title_font_options}
    selected_title_font_key = _resolve_title_font_key(video_font_key)
    if selected_title_font_key not in available_title_font_keys and title_font_options:
        selected_title_font_key = title_font_options[0]["key"]
    default_font_css = TITLE_FONTS.get(DEFAULT_EDITOR_TITLE_FONT_KEY, {}).get("css_family", "inherit")
    selected_title_font_css = TITLE_FONTS.get(selected_title_font_key, {}).get("css_family", default_font_css)
    selected_video_date = video_date_text
    selected_video_date_top = video_date_top
    selected_subscribe_overlay = video_subscribe_overlay if video_subscribe_overlay is not None else True
    selected_show_title = video_show_title if video_show_title is not None else True
    selected_show_subtitle = video_show_subtitle if video_show_subtitle is not None else True
    selected_video_overlay_offset = video_overlay_offset
    try:
        selected_video_overlay_offset = int(selected_video_overlay_offset)
    except Exception:
        selected_video_overlay_offset = video_overlay_offset
    plan_entries = _load_plan_entries(video["video_id"])
    selected_focus_categories = _resolve_saved_focus_categories(plan_entries)
    v2_plan_entries = _load_plan_entries_v2(video["video_id"])
    v3_plan_entries = _load_plan_entries_v3(video["video_id"])
    v2_plan_exists = bool(v2_plan_entries)
    v2_plan_clip_count = len(v2_plan_entries)
    v3_plan_exists = bool(v3_plan_entries)
    v3_plan_clip_count = len(v3_plan_entries)
    v2_plan_rows = []
    if v2_plan_entries and segments:
        v2_entries_sorted = sorted(v2_plan_entries, key=lambda c: int(c.get("plan_index") or 0))
        for entry in v2_entries_sorted:
            start = entry.get("start")
            end = entry.get("end")
            transcript_full = entry.get("transcript_full")
            if transcript_full is None and start is not None and end is not None:
                try:
                    transcript_full = build_transcript_for_range(segments, start, end, prefer_tr=True)
                except Exception:
                    transcript_full = ""
            v2_plan_rows.append(
                {
                    "start": start,
                    "end": end,
                    "start_label": _format_time_label(start) if start is not None else None,
                    "end_label": _format_time_label(end) if end is not None else None,
                    "transcript_full": transcript_full or "",
                    "excerpt": entry.get("excerpt") or "",
                }
            )
    _refresh_plan_publish_status_from_youtube(video["video_id"], plan_entries)
    if is_reauth_required((current_user or {}).get("id")):
        flash(
            "YouTube bağlantısının süresi dolmuş. Lütfen Social Connect üzerinden yeniden bağlayın.",
            "warning",
        )
    published_video_ids = [
        entry.get("yt_video_id")
        for entry in plan_entries
        if entry.get("publish_status") == "published" and entry.get("yt_video_id")
    ]
    published_video_ids = list(dict.fromkeys(published_video_ids))
    published_stats_map = fetch_video_stats(published_video_ids)
    plan_by_index = {}
    for entry in plan_entries:
        pi = entry.get("plan_index")
        try:
            pi = int(pi)
        except Exception:
            continue
        plan_by_index[pi] = entry
    plan_exists = bool(plan_by_index)
    plan_clip_count = len(plan_by_index)
    ai_suggested_clip_count = sum(1 for entry in plan_entries if _is_removable_ai_suggestion(entry))
    plan_job_state = _current_plan_job_state(video_pk)
    plan_job_block = _clip_plan_block_info(plan_job_state)
    plan_generation_block_reason = (plan_job_block or {}).get("reason") or ""
    plan_generation_block_message = (plan_job_block or {}).get("message") or ""
    plan_generation_run_count = int(plan_job_state.get("run_count") or 0)
    generated_clip_entries = []
    for entry in plan_entries:
        if entry.get("status") != "created":
            continue
        clip_filename = entry.get("clip_filename")
        if not clip_filename:
            continue
        clip_path = SHORTS_DIR / clip_filename
        if not clip_path.exists():
            continue
        generated_clip_entries.append(
            {
                "filename": clip_filename,
                "title": entry.get("title") or "",
                "subtitle": entry.get("subtitle"),
                "transcript_full": entry.get("transcript_full"),
                "start": entry.get("start"),
                "end": entry.get("end"),
                "start_label": _format_time_label(entry.get("start")) if entry.get("start") is not None else None,
                "end_label": _format_time_label(entry.get("end")) if entry.get("end") is not None else None,
                "plan_index": entry.get("plan_index"),
                "yt_description": entry.get("yt_description"),
                "yt_status": entry.get("yt_status"),
            }
        )
    debug_path = SHORTS_DIR / f"{video['video_id']}_debug.json"
    debug_info = None
    if debug_path.exists():
        try:
            debug_info = json.loads(debug_path.read_text())
        except Exception:
            debug_info = None
    clip_duration_stats = {}
    if debug_info and segments:
        for entry in (debug_info.get("window_candidates") or []):
            for clip in (entry.get("final_clips") or []):
                start = clip.get("start")
                end = clip.get("end")
                clip_text = None
                try:
                    clip_text = build_transcript_for_range(segments, start, end, prefer_tr=True)
                except Exception:
                    clip_text = None
                clip["transcript_full"] = clip_text or ""
        total = 0
        over = 0
        under = 0
        for entry in (debug_info.get("window_candidates") or []):
            for clip in (entry.get("final_clips") or []):
                total += 1
                dur = None
                try:
                    s = float(clip.get("start", 0) or 0)
                    e = float(clip.get("end", 0) or 0)
                    dur = max(e - s, 0)
                except Exception:
                    dur = None
                if dur is not None and dur > 25:
                    over += 1
                else:
                    under += 1
        clip_duration_stats = {"total": total, "over_25": over, "under_25": under}

    instagram_queue_map = {}
    tiktok_queue_map = {}
    facebook_queue_map = {}
    if video.get("video_id"):
        instagram_queue_map = load_instagram_queue_map([video["video_id"]])
        tiktok_queue_map = load_tiktok_queue_map([video["video_id"]])
        facebook_queue_map = load_facebook_queue_map([video["video_id"]])

    # Build unified clip rows (created + pending from plan)
    clip_rows = []
    current_user = getattr(g, "vs_current_user", None)
    user_tz = (current_user or {}).get("time_zone") or DEFAULT_TIME_ZONE
    user_tz_label = TIMEZONE_LABELS.get(user_tz, user_tz)
    is_admin = (current_user or {}).get("role") == "admin"
    generated_video_map: Dict[Tuple[str, str], Dict[str, Any]] = {}
    active_brand_id = brand_id
    source_video_id = str(video.get("video_id") or "").strip()
    if source_video_id and active_brand_id:
        conn_generated = get_db_readonly()
        try:
            generated_columns = table_columns(conn_generated, "shorts_generated_videos")
            if generated_columns:
                select_fields = ["id", "clip_filename", "generated_title", "publish_status", "planned_publish_at"]
                if "share_token" in generated_columns:
                    select_fields.append("share_token")
                generated_rows = conn_generated.execute(
                    f"""
                    SELECT {", ".join(select_fields)}
                    FROM shorts_generated_videos
                    WHERE CAST(source_video_id AS VARCHAR) = ?
                      AND lower(coalesce(source_channel_type, 'youtube')) = 'youtube'
                      AND brand_id = ?
                    """,
                    [source_video_id, active_brand_id],
                ).fetchall()
                generated_recipient_map: Dict[str, Dict[str, str]] = {}
                if generated_rows and _short_share_links_ready(conn_generated):
                    generated_video_ids = [
                        str(generated_row[0] or "").strip()
                        for generated_row in generated_rows
                        if str(generated_row[0] or "").strip()
                    ]
                    if generated_video_ids:
                        placeholders = ", ".join("?" for _ in generated_video_ids)
                        recipient_rows = conn_generated.execute(
                            f"""
                            SELECT
                              CAST(sl.generated_video_id AS VARCHAR) AS generated_video_id,
                              sl.recipient_name,
                              sl.recipient_email,
                              COALESCE(sl.trial_days, ?) AS trial_days
                            FROM short_share_links sl
                            JOIN (
                              SELECT generated_video_id, MAX(id) AS latest_id
                              FROM short_share_links
                              WHERE CAST(generated_video_id AS VARCHAR) IN ({placeholders})
                              GROUP BY generated_video_id
                            ) latest
                              ON CAST(latest.latest_id AS BIGINT) = CAST(sl.id AS BIGINT)
                            """,
                            [DEFAULT_SHARE_TRIAL_DAYS, *generated_video_ids],
                        ).fetchall()
                        for recipient_row in recipient_rows:
                            generated_id = str(recipient_row[0] or "").strip()
                            if not generated_id:
                                continue
                            generated_recipient_map[generated_id] = {
                                "recipient_name": str(recipient_row[1] or "").strip(),
                                "recipient_email": str(recipient_row[2] or "").strip(),
                                "trial_days": normalize_trial_days(recipient_row[3], default=DEFAULT_SHARE_TRIAL_DAYS),
                            }
                for generated_row in generated_rows:
                    clip_name = str(generated_row[1] or "").strip()
                    if not clip_name:
                        continue
                    generated_id = str(generated_row[0] or "").strip()
                    recipient_info = generated_recipient_map.get(generated_id) or {}
                    generated_video_map[(source_video_id, clip_name)] = {
                        "id": generated_row[0],
                        "generated_title": generated_row[2] if len(generated_row) > 2 else None,
                        "publish_status": str(generated_row[3] or "").strip().lower() or None,
                        "planned_publish_at": generated_row[4],
                        "share_token": generated_row[5] if len(generated_row) > 5 else None,
                        "recipient_name": str(recipient_info.get("recipient_name") or "").strip(),
                        "recipient_email": str(recipient_info.get("recipient_email") or "").strip(),
                        "trial_days": normalize_trial_days(recipient_info.get("trial_days"), default=DEFAULT_SHARE_TRIAL_DAYS),
                    }
        except Exception:
            current_app.logger.exception("Failed to load generated short rows for video %s", source_video_id)
    v2_rules = load_non_speech_rules()
    v3_rules = load_non_speech_rules()
    v4_rules = load_planner_rules_v4()

    for entry in plan_entries:
        pi = entry.get("plan_index")
        try:
            pi = int(pi)
        except Exception:
            continue
        origin = str(entry.get("origin") or "manual").strip().lower() or "manual"
        is_ai_suggestion = _is_removable_ai_suggestion(entry)
        start = entry.get("start")
        end = entry.get("end")
        try:
            duration_val = max(0.0, float(end) - float(start)) if start is not None and end is not None else None
        except Exception:
            duration_val = None
        display_timings = _build_display_timing(start, end, entry.get("start"), entry.get("end"))
        custom_transcript = entry.get("transcript_full_custom")
        transcript_full = custom_transcript or entry.get("transcript_full") or build_transcript_for_range(segments, start, end, prefer_tr=True)
        subtitle_source = entry.get("excerpt") or ""
        editable_subtitle = transcript_full or subtitle_source
        clip_filename = entry.get("clip_filename") or entry.get("output_filename")
        clip_exists = _short_exists(clip_filename) if clip_filename else False
        video_url = _short_public_url(clip_filename) if clip_exists and clip_filename else ""
        status = "created" if clip_exists else (entry.get("status") or "pending")
        video_filename = clip_filename if clip_exists else None
        yt_id = entry.get("yt_video_id")
        generated_record = generated_video_map.get((source_video_id, str(clip_filename or "").strip())) or {}
        clip_stats = published_stats_map.get(yt_id) if yt_id else {}

        publish_value = entry.get("publish_at_iso") or entry.get("publish_at")
        publish_display = _format_publish_display(publish_value, user_tz)
        youtube_schedule_date = _format_schedule_date(publish_value) if publish_value else None
        db_publish_status = (
            str(generated_record.get("publish_status") or "").strip().lower()
            or (str(entry.get("publish_status") or "").strip().lower() if entry.get("publish_status") else "")
            or ("ready" if entry.get("yt_description") else "not_ready")
        )
        db_publish_value = generated_record.get("planned_publish_at") or publish_value
        db_publish_display = _format_publish_display(db_publish_value, user_tz) if db_publish_value else publish_display

        ig_entries = instagram_queue_map.get((video.get("video_id") or "", str(pi))) or []
        ig_entries = [
            ig for ig in ig_entries
            if (str(ig.get("status") or "").strip().lower() not in {"canceled", "cancelled"})
        ]
        ig_has_reel = any((ig.get("media_type") or "reel") == "reel" for ig in ig_entries)
        ig_has_feed = any((ig.get("media_type") or "reel") == "feed" for ig in ig_entries)
        ig_publish_at = None
        ig_status = None
        if ig_entries:
            for ig in reversed(ig_entries):
                if ig.get("status") == "published":
                    ig_status = "published"
                    if ig.get("published_at"):
                        ig_publish_at = ig.get("published_at")
                    elif ig.get("publish_at"):
                        ig_publish_at = ig.get("publish_at")
                    break
            if not ig_status:
                ig_status = (ig_entries[-1].get("status") or "pending").lower()
            for ig in reversed(ig_entries):
                if ig.get("status") in {"pending", "retry", "uploading"} and ig.get("publish_at"):
                    ig_publish_at = ig.get("publish_at")
                    break
            if not ig_publish_at:
                for ig in reversed(ig_entries):
                    if ig.get("publish_at"):
                        ig_publish_at = ig.get("publish_at")
                        break
            if not ig_publish_at:
                for ig in reversed(ig_entries):
                    if ig.get("published_at"):
                        ig_publish_at = ig.get("published_at")
                        break
        ig_mode = None
        if ig_entries:
            if ig_publish_at:
                ig_publish_dt = _parse_to_utc(ig_publish_at)
                yt_publish_dt = _parse_to_utc(publish_value)
                if ig_publish_dt and yt_publish_dt and ig_publish_dt == yt_publish_dt:
                    ig_mode = "sync"
                else:
                    ig_mode = "schedule"
            else:
                ig_mode = "now"
        ig_display = _format_publish_display(ig_publish_at, user_tz) if ig_publish_at else None
        ig_schedule_date = _format_schedule_date(ig_publish_at) if ig_publish_at else None
        ig_label = None
        if ig_entries:
            if ig_status == "published":
                ig_label = "Instagram published"
            elif ig_status in {"pending", "retry", "uploading"}:
                ig_label = "Instagram scheduled" if ig_publish_at else "Instagram queued"
            elif ig_status == "failed":
                ig_label = "Instagram failed"
            else:
                ig_label = "Instagram queued"

        tt_entries = tiktok_queue_map.get((video.get("video_id") or "", str(pi))) or []
        tt_entries = [
            tt for tt in tt_entries
            if (str(tt.get("status") or "").strip().lower() not in {"canceled", "cancelled"})
        ]
        tt_publish_at = None
        tt_status = None
        if tt_entries:
            for tt in reversed(tt_entries):
                if tt.get("status") == "published":
                    tt_status = "published"
                    if tt.get("published_at"):
                        tt_publish_at = tt.get("published_at")
                    elif tt.get("publish_at"):
                        tt_publish_at = tt.get("publish_at")
                    break
            if not tt_status:
                tt_status = (tt_entries[-1].get("status") or "pending").lower()
            for tt in reversed(tt_entries):
                if tt.get("status") in {"pending", "retry", "uploading"} and tt.get("publish_at"):
                    tt_publish_at = tt.get("publish_at")
                    break
            if not tt_publish_at:
                for tt in reversed(tt_entries):
                    if tt.get("publish_at"):
                        tt_publish_at = tt.get("publish_at")
                        break
            if not tt_publish_at:
                for tt in reversed(tt_entries):
                    if tt.get("published_at"):
                        tt_publish_at = tt.get("published_at")
                        break
        tt_mode = None
        if tt_entries:
            if tt_publish_at:
                tt_publish_dt = _parse_to_utc(tt_publish_at)
                yt_publish_dt = _parse_to_utc(publish_value)
                if tt_publish_dt and yt_publish_dt and tt_publish_dt == yt_publish_dt:
                    tt_mode = "sync"
                else:
                    tt_mode = "schedule"
            else:
                tt_mode = "now"
        tt_display = _format_publish_display(tt_publish_at, user_tz) if tt_publish_at else None
        tt_schedule_date = _format_schedule_date(tt_publish_at) if tt_publish_at else None
        tt_label = None
        if tt_entries:
            if tt_status == "published":
                tt_label = "TikTok published"
            elif tt_status in {"pending", "retry", "uploading"}:
                tt_label = "TikTok scheduled" if tt_publish_at else "TikTok queued"
            elif tt_status == "failed":
                tt_label = "TikTok failed"
            else:
                tt_label = "TikTok queued"

        fb_entries = facebook_queue_map.get((video.get("video_id") or "", str(pi))) or []
        fb_entries = [
            fb for fb in fb_entries
            if (str(fb.get("status") or "").strip().lower() not in {"canceled", "cancelled"})
        ]
        fb_has_reel = any((fb.get("media_type") or "feed") == "reel" for fb in fb_entries)
        fb_has_feed = any((fb.get("media_type") or "feed") == "feed" for fb in fb_entries)
        fb_publish_at = None
        fb_status = None
        if fb_entries:
            for fb in reversed(fb_entries):
                if fb.get("status") == "published":
                    fb_status = "published"
                    if fb.get("published_at"):
                        fb_publish_at = fb.get("published_at")
                    elif fb.get("publish_at"):
                        fb_publish_at = fb.get("publish_at")
                    break
            if not fb_status:
                fb_status = (fb_entries[-1].get("status") or "pending").lower()
            for fb in reversed(fb_entries):
                if fb.get("status") in {"pending", "retry", "uploading"} and fb.get("publish_at"):
                    fb_publish_at = fb.get("publish_at")
                    break
            if not fb_publish_at:
                for fb in reversed(fb_entries):
                    if fb.get("publish_at"):
                        fb_publish_at = fb.get("publish_at")
                        break
            if not fb_publish_at:
                for fb in reversed(fb_entries):
                    if fb.get("published_at"):
                        fb_publish_at = fb.get("published_at")
                        break
        fb_mode = None
        if fb_entries:
            if fb_publish_at:
                fb_publish_dt = _parse_to_utc(fb_publish_at)
                yt_publish_dt = _parse_to_utc(publish_value)
                if fb_publish_dt and yt_publish_dt and fb_publish_dt == yt_publish_dt:
                    fb_mode = "sync"
                else:
                    fb_mode = "schedule"
            else:
                fb_mode = "now"
        fb_display = _format_publish_display(fb_publish_at, user_tz) if fb_publish_at else None
        fb_schedule_date = _format_schedule_date(fb_publish_at) if fb_publish_at else None
        fb_label = None
        if fb_entries:
            if fb_status == "published":
                fb_label = "Facebook published"
            elif fb_status in {"pending", "retry", "uploading"}:
                fb_label = "Facebook scheduled" if fb_publish_at else "Facebook queued"
            elif fb_status == "failed":
                fb_label = "Facebook failed"
            else:
                fb_label = "Facebook queued"

        yt_published = str(entry.get("publish_status") or "").lower() == "published"
        ig_published = ig_status == "published"
        tt_published = tt_status == "published"
        fb_published = fb_status == "published"
        any_platform_published = yt_published or ig_published or tt_published or fb_published

        clip_rows.append({
            "plan_index": pi,
            "origin": origin,
            "is_ai_suggestion": is_ai_suggestion,
            "generated_video_id": generated_record.get("id"),
            "share_token": generated_record.get("share_token"),
            "recipient_name": str(generated_record.get("recipient_name") or "").strip(),
            "recipient_email": str(generated_record.get("recipient_email") or "").strip(),
            "title": entry.get("title") or "",
            "start": start,
            "end": end,
            "start_label": _format_time_label(start) if start is not None else None,
            "end_label": _format_time_label(end) if end is not None else None,
            "duration": duration_val,
            "duration_label": _format_time_label(duration_val) if duration_val is not None else None,
            "transcript_full": transcript_full,
            "video_filename": video_filename,
            "video_url": video_url,
            "subtitle": subtitle_source,
            "status": status,
            "render_job_id": entry.get("render_job_id") or "",
            "render_error": entry.get("render_error") or "",
            "publish_status": entry.get("publish_status") or ("ready" if entry.get("yt_description") else "not_ready"),
            "publish_at": entry.get("publish_at"),
            "publish_at_iso": entry.get("publish_at_iso"),
            "publish_display": publish_display,
            "db_publish_status": db_publish_status,
            "db_publish_display": db_publish_display,
            "youtube_schedule_date": youtube_schedule_date,
            "yt_description": entry.get("yt_description"),
            "category": entry.get("category") or "",
            "display_start_label": display_timings["display_start_label"],
            "display_end_label": display_timings["display_end_label"],
            "display_duration_label": display_timings["display_duration_label"],
            "subtitle_edit": editable_subtitle or "",
            "published_stats": clip_stats or {},
            "yt_video_id": yt_id,
            "instagram_reel": ig_has_reel,
            "instagram_feed": ig_has_feed,
            "instagram_mode": ig_mode,
            "instagram_publish_at": ig_publish_at,
            "instagram_label": ig_label,
            "instagram_display": ig_display,
            "instagram_schedule_date": ig_schedule_date,
            "tiktok_enabled": bool(tt_entries),
            "tiktok_mode": tt_mode,
            "tiktok_publish_at": tt_publish_at,
            "tiktok_label": tt_label,
            "tiktok_display": tt_display,
            "tiktok_schedule_date": tt_schedule_date,
            "facebook_reel": fb_has_reel,
            "facebook_feed": fb_has_feed,
            "facebook_mode": fb_mode,
            "facebook_publish_at": fb_publish_at,
            "facebook_label": fb_label,
            "facebook_display": fb_display,
            "facebook_schedule_date": fb_schedule_date,
            "any_platform_published": any_platform_published,
        })
    latest_schedule_iso, latest_schedule_dt = _find_latest_publish(plan_entries)
    latest_scheduled_display = (
        _format_publish_display(latest_schedule_iso, user_tz) if latest_schedule_iso else None
    )
    allowed_source_ids: Optional[set[str]] = None
    if editor_owner_user_id:
        conn_sources = get_db_readonly()
        try:
            source_sql = "SELECT video_id FROM youtube_videos WHERE owner_user_id = ?"
            source_params: List[Any] = [editor_owner_user_id]
            if brand_id:
                source_sql += " AND brand_id = ?"
                source_params.append(brand_id)
            else:
                source_sql += " AND brand_id IS NULL"
            allowed_source_ids = {
                str(source_row[0])
                for source_row in conn_sources.execute(source_sql, source_params).fetchall()
                if source_row and source_row[0]
            }
        finally:
            conn_sources.close()
    channel_latest_schedule_iso = None
    channel_latest_schedule_dt = None
    for plan_path in SHORTS_DIR.glob("*_plan.json"):
        try:
            source_video_id = plan_path.name[: -len("_plan.json")]
            if allowed_source_ids is not None and source_video_id not in allowed_source_ids:
                continue
            data = json.loads(plan_path.read_text())
        except Exception:
            continue
        entries = data.get("plan") or data.get("clips") or []
        iso, dt = _find_latest_publish(entries)
        if not iso or not dt:
            continue
        if channel_latest_schedule_dt is None or dt > channel_latest_schedule_dt:
            channel_latest_schedule_dt = dt
            channel_latest_schedule_iso = iso
    channel_latest_scheduled_display = (
        _format_publish_display(channel_latest_schedule_iso, user_tz)
        if channel_latest_schedule_iso
        else None
    )
    transcript_override = None
    for entry in generated_clip_entries:
        transcript_override = entry.get("transcript_full")
        if transcript_override:
            break
    if not transcript_override:
        for row in clip_rows:
            edit = row.get("subtitle_edit")
            if edit:
                transcript_override = edit
                break
    category_owner_id = editor_owner_user_id
    category_options = _load_category_options(category_owner_id)
    transcript_player_source = _build_transcript_player_source(video)

    youtube_connected = has_refresh_token(editor_owner_user_id, brand_id=brand_id)
    instagram_connected = False
    tiktok_connected = False
    facebook_connected = False
    if editor_owner_user_id:
        try:
            instagram_info = get_instagram_data(editor_owner_user_id)
        except InstagramTokenStoreError as exc:
            current_app.logger.warning("Instagram info unavailable on generate_short: %s", exc)
            instagram_info = None
        if instagram_info:
            instagram_connected = _validate_instagram_connection(instagram_info)
        try:
            tiktok_info = get_tiktok_data(editor_owner_user_id)
        except TikTokTokenStoreError as exc:
            current_app.logger.warning("TikTok info unavailable on generate_short: %s", exc)
            tiktok_info = None
        if tiktok_info and tiktok_info.get("access_token"):
            if not _is_token_expired(tiktok_info.get("expires_at")):
                tiktok_connected = True
        try:
            facebook_info = get_facebook_page_data(editor_owner_user_id)
        except FacebookTokenStoreError as exc:
            current_app.logger.warning("Facebook info unavailable on generate_short: %s", exc)
            facebook_info = None
        if facebook_info:
            facebook_connected = _validate_facebook_page_connection(facebook_info)
    else:
        instagram_connected = False
    published_created_clip_count = len(
        [
            r
            for r in clip_rows
            if (r.get("status") == "created" and r.get("any_platform_published"))
        ]
    )
    current_plan_id = str((current_user or {}).get("plan_id") or "").strip().lower()
    llm_description_enabled = current_plan_id not in {"", "free"}
    suggested_long_video_title = _build_long_highlights_title(video.get("title") or "")
    long_compilation_videos = _list_generated_long_compilations(video.get("video_id") or "", limit=12)
    brand_subscribe_overlay_available = bool(subscribe_visual_options)
    if not brand_subscribe_overlay_available:
        selected_subscribe_overlay = False

    def _summarize_queue_status(entries: List[Dict[str, Any]]) -> Tuple[Optional[str], Optional[str]]:
        if not entries:
            return None, None
        status = None
        publish_at = None
        for item in reversed(entries):
            item_status = str(item.get("status") or "").strip().lower()
            if item_status == "published":
                status = "published"
                publish_at = item.get("published_at") or item.get("publish_at")
                break
        if not status:
            status = str(entries[-1].get("status") or "pending").strip().lower()
            for item in reversed(entries):
                item_status = str(item.get("status") or "").strip().lower()
                if item_status in {"pending", "retry", "uploading"} and item.get("publish_at"):
                    publish_at = item.get("publish_at")
                    break
            if not publish_at:
                for item in reversed(entries):
                    if item.get("publish_at"):
                        publish_at = item.get("publish_at")
                        break
        return status, publish_at

    for item in long_compilation_videos:
        filename = str(item.get("filename") or "").strip()
        if not filename:
            continue
        key = (video.get("video_id") or "", "longcomp")
        ig_entries_all = instagram_queue_map.get(key) or []
        tt_entries_all = tiktok_queue_map.get(key) or []
        fb_entries_all = facebook_queue_map.get(key) or []

        ig_entries = [
            e
            for e in ig_entries_all
            if str(e.get("clip_filename") or "").strip() == filename
            and str(e.get("status") or "").strip().lower() not in {"canceled", "cancelled"}
        ]
        tt_entries = [
            e
            for e in tt_entries_all
            if str(e.get("clip_filename") or "").strip() == filename
            and str(e.get("status") or "").strip().lower() not in {"canceled", "cancelled"}
        ]
        fb_entries = [
            e
            for e in fb_entries_all
            if str(e.get("clip_filename") or "").strip() == filename
            and str(e.get("status") or "").strip().lower() not in {"canceled", "cancelled"}
        ]

        publish_state = item.get("publish_state") if isinstance(item.get("publish_state"), dict) else {}
        yt_state = publish_state.get("youtube") if isinstance(publish_state.get("youtube"), dict) else {}
        yt_status = str(yt_state.get("status") or "").strip().lower()
        yt_publish_val = yt_state.get("published_at") if yt_status == "published" else (
            yt_state.get("publish_at") or yt_state.get("published_at")
        )
        if yt_status in {"pending", "retry", "uploading"}:
            yt_label = "YouTube scheduled" if yt_publish_val else "YouTube queued"
            yt_icon = "text-warning"
        elif yt_status == "published":
            yt_label = "YouTube published"
            yt_icon = "text-success"
        elif yt_status == "failed":
            yt_label = "YouTube failed"
            yt_icon = "text-danger"
        elif yt_status:
            yt_label = "YouTube queued"
            yt_icon = "text-warning"
        else:
            yt_label = None
            yt_icon = "text-warning"
        item["youtube_label"] = yt_label
        item["youtube_display"] = _format_publish_display(yt_publish_val, user_tz) if yt_publish_val else None
        item["youtube_icon_class"] = yt_icon

        ig_status, ig_publish_val = _summarize_queue_status(ig_entries)
        if ig_status == "published":
            ig_label = "Instagram published"
            ig_icon = "text-success"
        elif ig_status in {"pending", "retry", "uploading"}:
            ig_label = "Instagram scheduled" if ig_publish_val else "Instagram queued"
            ig_icon = "text-warning"
        elif ig_status == "failed":
            ig_label = "Instagram failed"
            ig_icon = "text-danger"
        elif ig_status:
            ig_label = "Instagram queued"
            ig_icon = "text-warning"
        else:
            ig_label = None
            ig_icon = "text-warning"
        item["instagram_label"] = ig_label
        item["instagram_display"] = _format_publish_display(ig_publish_val, user_tz) if ig_publish_val else None
        item["instagram_icon_class"] = ig_icon

        tt_status, tt_publish_val = _summarize_queue_status(tt_entries)
        if tt_status == "published":
            tt_label = "TikTok published"
            tt_icon = "text-success"
        elif tt_status in {"pending", "retry", "uploading"}:
            tt_label = "TikTok scheduled" if tt_publish_val else "TikTok queued"
            tt_icon = "text-warning"
        elif tt_status == "failed":
            tt_label = "TikTok failed"
            tt_icon = "text-danger"
        elif tt_status:
            tt_label = "TikTok queued"
            tt_icon = "text-warning"
        else:
            tt_label = None
            tt_icon = "text-warning"
        item["tiktok_label"] = tt_label
        item["tiktok_display"] = _format_publish_display(tt_publish_val, user_tz) if tt_publish_val else None
        item["tiktok_icon_class"] = tt_icon

        fb_status, fb_publish_val = _summarize_queue_status(fb_entries)
        if fb_status == "published":
            fb_label = "Facebook published"
            fb_icon = "text-success"
        elif fb_status in {"pending", "retry", "uploading"}:
            fb_label = "Facebook scheduled" if fb_publish_val else "Facebook queued"
            fb_icon = "text-warning"
        elif fb_status == "failed":
            fb_label = "Facebook failed"
            fb_icon = "text-danger"
        elif fb_status:
            fb_label = "Facebook queued"
            fb_icon = "text-warning"
        else:
            fb_label = None
            fb_icon = "text-warning"
        item["facebook_label"] = fb_label
        item["facebook_display"] = _format_publish_display(fb_publish_val, user_tz) if fb_publish_val else None
        item["facebook_icon_class"] = fb_icon

    return render_template(
        "generate_short.html",
        video=video,
        upload_processing=upload_processing,
        transcript_text=transcript_text,
        transcript_text_tr=transcript_text_tr,
        transcript_text_ar=transcript_text_ar,
        transcript_language=transcript_language,
        segments=segments,
        segments_view=segments_view,
        short_exists=short_exists,
        short_filename=short_path.name,
        source_path=source_path,
        preview_url=preview_url,
        generated_clip_entries=generated_clip_entries,
        title_font_options=title_font_options,
        selected_title_font_key=selected_title_font_key,
        selected_title_font_css=selected_title_font_css,
        sub_fonts=sub_fonts,
        selected_sub_font=selected_sub_font,
        title_font_sizes=TITLE_FONT_SIZES,
        selected_title_font_size=selected_title_font_size,
        sub_font_sizes=SUB_FONT_SIZES,
        selected_sub_font_size=selected_sub_font_size,
        selected_sub_margin=selected_sub_margin,
        selected_subtitle_style=selected_subtitle_style,
        selected_subtitle_preset=selected_subtitle_preset,
        selected_title_margin=selected_title_margin,
        selected_title_line_spacing=selected_title_line_spacing,
        selected_title_bg_color=selected_title_bg_color,
        selected_title_bg_alpha=selected_title_bg_alpha,
        selected_title_text_color=selected_title_text_color,
        selected_subtitle_text_color=selected_subtitle_text_color,
        selected_subtitle_bg_color=selected_subtitle_bg_color,
        selected_subtitle_bg_alpha=selected_subtitle_bg_alpha,
        selected_subtitle_text_alpha=selected_subtitle_text_alpha,
        style_templates=STYLE_TEMPLATES,
        default_style_template_key=DEFAULT_STYLE_TEMPLATE_KEY,
        clip_has_saved_style_template=clip_has_saved_style_template,
        selected_video_date=selected_video_date,
        selected_video_date_top=selected_video_date_top,
        selected_show_title=selected_show_title,
        selected_show_subtitle=selected_show_subtitle,
        selected_subscribe_overlay=selected_subscribe_overlay,
        selected_is_music_only=video_is_music_only,
        selected_podcast_audio_filename=selected_podcast_audio_filename,
        selected_podcast_overlay_short_ids=selected_podcast_overlay_short_ids,
        selected_visual_mode=selected_visual_mode,
        selected_video_overlay_offset=selected_video_overlay_offset,
        debug_info=debug_info,
        clip_rows=clip_rows,
        ai_suggested_clip_count=ai_suggested_clip_count,
        plan_generation_block_reason=plan_generation_block_reason,
        plan_generation_block_message=plan_generation_block_message,
        plan_generation_run_count=plan_generation_run_count,
        focus_category_options=get_focus_category_options(transcript_language or "tr"),
        selected_focus_categories=selected_focus_categories,
        transcript_player_source=transcript_player_source,
        youtube_connected=youtube_connected,
        video_duration_label=video_duration_label,
        clip_duration_stats=clip_duration_stats,
        transcript_override=transcript_override,
        category_options=category_options,
        plan_exists=plan_exists,
        plan_clip_count=plan_clip_count,
        v2_plan_exists=v2_plan_exists,
        v2_plan_clip_count=v2_plan_clip_count,
        v2_plan_rows=v2_plan_rows,
        v3_plan_exists=v3_plan_exists,
        v3_plan_clip_count=v3_plan_clip_count,
        v2_rules=v2_rules,
        v3_rules=v3_rules,
        v4_rules=v4_rules,
        is_admin=is_admin,
        editor_is_admin_operation=editor_context["is_admin_operation"],
        editor_is_customer_operation=bool(getattr(g, "vs_admin_operation_workspace_kind", "") == "customer"),
        latest_scheduled_display=latest_scheduled_display,
        channel_latest_scheduled_display=channel_latest_scheduled_display,
        static_visual_options=static_visual_options,
        created_visual_options=created_visual_options,
        video_static_visual_key=video_static_visual_key,
        static_visual_label=static_visual_label,
        created_visual_label=created_visual_label,
        selected_visual_label=selected_visual_label,
        bg_visual_options=bg_visual_options,
        video_background_visual_key=video_background_visual_key,
        background_visual_label=background_visual_label,
        subscribe_visual_options=subscribe_visual_options,
        video_subscribe_overlay_key=video_subscribe_overlay_key,
        subscribe_visual_label=subscribe_visual_label,
        background_preference_update_url=url_for("video_shorts_bp.update_selected_background"),
        clip_coachmark_preference_url=url_for("video_shorts_bp.update_clip_coachmark_preference"),
        hide_clip_coachmark=hide_clip_coachmark,
        video_crop_aspect=video.get("crop_aspect") if video else None,
        instagram_connected=instagram_connected,
        tiktok_connected=tiktok_connected,
        facebook_connected=facebook_connected,
        user_time_zone_label=user_tz_label,
        user_time_zone=user_tz,
        timezone_options=TIMEZONE_OPTIONS,
        timezone_label_map=TIMEZONE_LABELS,
        timezone_update_url=url_for("video_shorts_bp.profile"),
        published_created_clip_count=published_created_clip_count,
        suggested_long_video_title=suggested_long_video_title,
        long_compilation_videos=long_compilation_videos,
        brand_subscribe_overlay_available=brand_subscribe_overlay_available,
        llm_description_enabled=llm_description_enabled,
        editor_url=_editor_url,
    )


SHARE_EVENT_MAX_BODY_BYTES = 512
SHARE_EVENT_RATE_LIMITS = [
    RateLimitRule(limit=20, window_seconds=60),
    RateLimitRule(limit=120, window_seconds=3600),
]


def _share_base_url() -> str:
    return str(current_app.config.get("BASE_URL") or "https://mintistudio.com").rstrip("/")


def _share_public_url(token: str) -> str:
    return f"{_share_base_url()}/w/{token}"


def _share_preview_url(token: str) -> str:
    return f"{_share_public_url(token)}?preview=1"


def _short_share_links_ready(conn) -> bool:
    ensure_short_share_links_schema(conn)
    return bool(table_columns(conn, "short_share_links"))


def _load_shared_short_row(conn, token: str) -> dict[str, Any] | None:
    normalized_token = str(token or "").strip()
    if not normalized_token:
        return None

    if _short_share_links_ready(conn):
        row = conn.execute(
            """
            SELECT
              gv.id,
              gv.clip_filename,
              gv.generated_title,
              yv.title,
              COALESCE(NULLIF(CAST(gv.user_id AS VARCHAR), ''), CAST(b.owner_user_id AS VARCHAR)) AS owner_user_id,
              sl.id AS share_link_id,
              sl.token AS resolved_token,
              sl.recipient_name,
              sl.recipient_email,
              sl.language,
              COALESCE(sl.trial_days, ?) AS trial_days,
              'recipient' AS token_source
            FROM short_share_links sl
            JOIN shorts_generated_videos gv
              ON CAST(gv.id AS BIGINT) = CAST(sl.generated_video_id AS BIGINT)
            LEFT JOIN shorts_brands b
              ON CAST(b.id AS VARCHAR) = CAST(gv.brand_id AS VARCHAR)
            LEFT JOIN youtube_videos yv
              ON CAST(yv.video_id AS VARCHAR) = CAST(gv.source_video_id AS VARCHAR)
            WHERE sl.token = ?
            LIMIT 1
            """,
            [DEFAULT_SHARE_TRIAL_DAYS, normalized_token],
        ).fetchone()
        if row:
            return {
                "generated_video_id": str(row[0] or "").strip(),
                "clip_filename": str(row[1] or "").strip(),
                "clip_title": str(row[2] or row[3] or row[1] or "Short unavailable").strip() or "Short unavailable",
                "owner_user_id": str(row[4] or "").strip(),
                "share_link_id": str(row[5] or "").strip() or None,
                "token": str(row[6] or normalized_token).strip(),
                "recipient_name": str(row[7] or "").strip(),
                "recipient_email": str(row[8] or "").strip().lower(),
                "language": str(row[9] or "").strip().upper(),
                "trial_days": normalize_trial_days(row[10], default=DEFAULT_SHARE_TRIAL_DAYS),
                "token_source": str(row[11] or "recipient").strip() or "recipient",
            }

    generated_columns = table_columns(conn, "shorts_generated_videos")
    if "share_token" not in generated_columns:
        return None
    row = conn.execute(
        """
        SELECT
          gv.id,
          gv.clip_filename,
          gv.generated_title,
          yv.title,
          COALESCE(NULLIF(CAST(gv.user_id AS VARCHAR), ''), CAST(b.owner_user_id AS VARCHAR)) AS owner_user_id
        FROM shorts_generated_videos gv
        LEFT JOIN shorts_brands b
          ON CAST(b.id AS VARCHAR) = CAST(gv.brand_id AS VARCHAR)
        LEFT JOIN youtube_videos yv
          ON CAST(yv.video_id AS VARCHAR) = CAST(gv.source_video_id AS VARCHAR)
        WHERE gv.share_token = ?
        LIMIT 1
        """,
        [normalized_token],
    ).fetchone()
    if not row:
        return None
    return {
        "generated_video_id": str(row[0] or "").strip(),
        "clip_filename": str(row[1] or "").strip(),
        "clip_title": str(row[2] or row[3] or row[1] or "Short unavailable").strip() or "Short unavailable",
        "owner_user_id": str(row[4] or "").strip(),
        "share_link_id": None,
        "recipient_name": "",
        "recipient_email": "",
        "language": "",
        "trial_days": DEFAULT_SHARE_TRIAL_DAYS,
        "token": normalized_token,
        "token_source": "legacy",
    }


@video_shorts_bp.route("/api/generated-shorts/share-link", methods=["POST"])
@require_admin
def create_generated_short_share_link():
    payload = request.get_json(silent=True) or request.form or {}
    generated_video_id = str(payload.get("generated_video_id") or "").strip()
    recipient_name = str(payload.get("recipient_name") or "").strip()
    recipient_email = str(payload.get("recipient_email") or "").strip()
    trial_days = normalize_trial_days(payload.get("trial_days"), default=DEFAULT_SHARE_TRIAL_DAYS)
    normalized_recipient_email = recipient_email.lower()
    if not generated_video_id:
        return jsonify({"success": False, "message": "Generated short id is required."}), 400

    conn = get_db()
    try:
        if not _short_share_links_ready(conn):
            return jsonify({"success": False, "message": "Share links are not available until the database migration is applied."}), 503

        row = conn.execute(
            """
            SELECT id, clip_filename
            FROM shorts_generated_videos
            WHERE CAST(id AS VARCHAR) = ?
            LIMIT 1
            """,
            [generated_video_id],
        ).fetchone()
        if not row:
            return jsonify({"success": False, "message": "Generated short not found."}), 404

        clip_filename = str(row[1] or "").strip()
        if not clip_filename:
            return jsonify({"success": False, "message": "Rendered short is not available for sharing yet."}), 404

        existing_share_row = None
        if normalized_recipient_email:
            existing_share_row = conn.execute(
                """
                SELECT id, token
                FROM short_share_links
                WHERE CAST(generated_video_id AS VARCHAR) = ?
                  AND lower(coalesce(recipient_email, '')) = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                [str(row[0] or generated_video_id), normalized_recipient_email],
            ).fetchone()
            if not existing_share_row:
                existing_share_row = conn.execute(
                    """
                    SELECT id, token
                    FROM short_share_links
                    WHERE CAST(generated_video_id AS VARCHAR) = ?
                      AND coalesce(nullif(trim(recipient_email), ''), '') = ''
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    [str(row[0] or generated_video_id)],
                ).fetchone()
                if existing_share_row:
                    conn.execute(
                        """
                        UPDATE short_share_links
                           SET recipient_name = ?,
                               recipient_email = ?,
                               trial_days = ?
                         WHERE id = ?
                        """,
                        [recipient_name or None, recipient_email or None, trial_days, existing_share_row[0]],
                    )
        else:
            existing_share_row = conn.execute(
                """
                SELECT id, token
                FROM short_share_links
                WHERE CAST(generated_video_id AS VARCHAR) = ?
                  AND coalesce(nullif(trim(recipient_email), ''), '') = ''
                ORDER BY id DESC
                LIMIT 1
                """,
                [str(row[0] or generated_video_id)],
            ).fetchone()

        if existing_share_row:
            conn.execute(
                """
                UPDATE short_share_links
                   SET recipient_name = ?,
                       recipient_email = ?,
                       trial_days = ?
                 WHERE id = ?
                """,
                [recipient_name or None, recipient_email or None, trial_days, existing_share_row[0]],
            )
            share_token = str(existing_share_row[1] or "").strip()
            conn.commit()
            _ensure_shared_short_poster(clip_filename)
            return jsonify(
                {
                    "success": True,
                    "share_link_id": int(existing_share_row[0]),
                    "generated_video_id": str(row[0] or generated_video_id),
                    "share_token": share_token,
                    "share_url": _share_public_url(share_token),
                    "reused": True,
                    "trial_days": trial_days,
                }
            )

        share_token = ""
        for _ in range(5):
            candidate = secrets.token_urlsafe(16)
            exists = conn.execute(
                "SELECT 1 FROM short_share_links WHERE token = ? LIMIT 1",
                [candidate],
            ).fetchone()
            if exists:
                continue
            conn.execute(
                """
                INSERT INTO short_share_links (
                    generated_video_id,
                    token,
                    recipient_name,
                    recipient_email,
                    trial_days,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, now())
                """,
                [
                    str(row[0] or generated_video_id),
                    candidate,
                    recipient_name or None,
                    recipient_email or None,
                    trial_days,
                ],
            )
            share_token = candidate
            break
        if not share_token:
            return jsonify({"success": False, "message": "Share link could not be created."}), 500
        share_link_row = conn.execute(
            """
            SELECT id
            FROM short_share_links
            WHERE token = ?
            LIMIT 1
            """,
            [share_token],
        ).fetchone()
        conn.commit()
        _ensure_shared_short_poster(clip_filename)
        return jsonify(
            {
                "success": True,
                "share_link_id": int(share_link_row[0]) if share_link_row and share_link_row[0] is not None else None,
                "generated_video_id": str(row[0] or generated_video_id),
                "share_token": share_token,
                "share_url": _share_public_url(share_token),
                "trial_days": trial_days,
            }
        )
    except Exception:
        current_app.logger.exception("Failed to create share link for generated short %s", generated_video_id)
        try:
            conn.rollback()
        except Exception:
            pass
        return jsonify({"success": False, "message": "Share link could not be created."}), 500
    finally:
        conn.close()


@video_shorts_bp.route("/generate/<int:video_pk>/lead-share-link", methods=["POST"])
@require_admin
def admin_operation_create_lead_share_link(video_pk: int):
    """Create one share link bound to the active pre-conversion lead."""
    scope = request_admin_operation_scope()
    if not scope:
        abort(404)
    lead = _require_active_lead_workspace(scope)
    payload = request.get_json(silent=True) or request.form or {}
    generated_video_id = str(payload.get("generated_video_id") or "").strip()
    trial_days = normalize_trial_days(payload.get("trial_days"), default=DEFAULT_SHARE_TRIAL_DAYS)
    if not generated_video_id:
        return jsonify({"success": False, "message": "Generated short id is required."}), 400
    if not lead["creator_email"]:
        return jsonify({"success": False, "message": "This lead needs an email before a share link can be created."}), 400

    conn = get_db()
    try:
        if not _short_share_links_ready(conn) or "autopilot_lead_id" not in table_columns(conn, "short_share_links"):
            return jsonify({"success": False, "message": "Lead share links are not available until the database migration is applied."}), 503

        generated_row = conn.execute(
            """
            SELECT gv.id, gv.clip_filename
            FROM shorts_generated_videos gv
            JOIN youtube_videos yv
              ON CAST(yv.video_id AS VARCHAR) = CAST(gv.source_video_id AS VARCHAR)
            JOIN autopilot_leads l
              ON CAST(l.id AS VARCHAR) = CAST(? AS VARCHAR)
            WHERE CAST(gv.id AS VARCHAR) = ?
              AND CAST(gv.brand_id AS VARCHAR) = CAST(? AS VARCHAR)
              AND yv.id = ?
              AND CAST(yv.owner_user_id AS VARCHAR) = CAST(? AS VARCHAR)
              AND CAST(yv.brand_id AS VARCHAR) = CAST(? AS VARCHAR)
              AND CAST(l.user_id AS VARCHAR) = CAST(? AS VARCHAR)
              AND CAST(l.brand_id AS VARCHAR) = CAST(? AS VARCHAR)
              AND l.first_video_id = yv.id
              AND (l.channel_id IS NULL OR l.channel_id = yv.channel_id)
              AND l.converted_at IS NULL
            LIMIT 1
            """,
            [
                lead["id"],
                generated_video_id,
                scope["brand_id"],
                video_pk,
                scope["owner_user_id"],
                scope["brand_id"],
                scope["owner_user_id"],
                scope["brand_id"],
            ],
        ).fetchone()
        if not generated_row:
            abort(404)
        clip_filename = str(generated_row[1] or "").strip()
        if not clip_filename:
            return jsonify({"success": False, "message": "Rendered short is not available for sharing yet."}), 404

        existing_row = conn.execute(
            """
            SELECT id, token
            FROM short_share_links
            WHERE CAST(generated_video_id AS VARCHAR) = ?
              AND CAST(autopilot_lead_id AS VARCHAR) = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            [str(generated_row[0]), lead["id"]],
        ).fetchone()
        if not existing_row:
            # Bind a legacy unaddressed link for this exact recipient instead of creating a duplicate.
            existing_row = conn.execute(
                """
                SELECT id, token
                FROM short_share_links
                WHERE CAST(generated_video_id AS VARCHAR) = ?
                  AND lower(coalesce(recipient_email, '')) = ?
                  AND autopilot_lead_id IS NULL
                ORDER BY id DESC
                LIMIT 1
                """,
                [str(generated_row[0]), lead["creator_email"].lower()],
            ).fetchone()

        if existing_row:
            conn.execute(
                """
                UPDATE short_share_links
                SET recipient_name = ?,
                    recipient_email = ?,
                    autopilot_lead_id = ?,
                    trial_days = ?
                WHERE id = ?
                """,
                [lead["creator_name"], lead["creator_email"], lead["id"], trial_days, existing_row[0]],
            )
            share_link_id = int(existing_row[0])
            share_token = str(existing_row[1] or "").strip()
            reused = True
        else:
            share_token = ""
            for _ in range(5):
                candidate = secrets.token_urlsafe(16)
                if conn.execute("SELECT 1 FROM short_share_links WHERE token = ? LIMIT 1", [candidate]).fetchone():
                    continue
                conn.execute(
                    """
                    INSERT INTO short_share_links (
                        generated_video_id, token, recipient_name, recipient_email,
                        autopilot_lead_id, trial_days, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, now())
                    """,
                    [str(generated_row[0]), candidate, lead["creator_name"], lead["creator_email"], lead["id"], trial_days],
                )
                share_token = candidate
                break
            if not share_token:
                return jsonify({"success": False, "message": "Share link could not be created."}), 500
            share_link_row = conn.execute("SELECT id FROM short_share_links WHERE token = ? LIMIT 1", [share_token]).fetchone()
            share_link_id = int(share_link_row[0])
            reused = False

        conn.commit()
        _ensure_shared_short_poster(clip_filename)
    except Exception:
        conn.rollback()
        current_app.logger.exception("Failed to create lead share link for generated short %s", generated_video_id)
        raise
    finally:
        conn.close()

    track_event(
        scope["acting_admin_id"],
        "admin_operation_lead_share_link_created",
        video_id=str(video_pk),
        short_id=str(generated_row[0]),
        metadata={
            "acting_admin_id": scope["acting_admin_id"],
            "target_owner_user_id": scope["owner_user_id"],
            "target_brand_id": scope["brand_id"],
            "autopilot_lead_id": lead["id"],
            "share_link_id": share_link_id,
        },
    )
    return jsonify(
        {
            "success": True,
            "share_link_id": share_link_id,
            "generated_video_id": str(generated_row[0]),
            "share_token": share_token,
            "share_url": _share_public_url(share_token),
            "trial_days": trial_days,
            "reused": reused,
        }
    )


def _render_public_short_watch_page(token: str):
    normalized_token = str(token or "").strip()
    if not normalized_token:
        abort(404)
    preview_mode = (request.args.get("preview") or "").strip().lower() in {"1", "true", "yes", "on"}

    conn = get_db_readonly()
    try:
        row = _load_shared_short_row(conn, normalized_token)
    finally:
        conn.close()

    if not row:
        abort(404)

    clip_title = row["clip_title"]
    clip_filename = row["clip_filename"]
    if not clip_filename:
        return render_template("short_share_unavailable.html", clip_title=clip_title), 404

    playback_url = _short_public_url(clip_filename)
    if not playback_url:
        return render_template("short_share_unavailable.html", clip_title=clip_title), 404

    poster_url = _short_poster_public_url(clip_filename) or None
    onboarding_url = ""
    resolved_language = normalize_outreach_language(row.get("language"), default="EN")
    recipient_email = str(row.get("recipient_email") or "").strip().lower()
    if recipient_email:
        try:
            minted = mint_onboarding_magic_link(
                recipient_email=recipient_email,
                recipient_name=str(row.get("recipient_name") or "").strip(),
                share_link_id=int(row["share_link_id"]) if row.get("share_link_id") else None,
                share_link_token=str(row.get("token") or "").strip(),
                language=resolved_language,
                trial_days=row.get("trial_days"),
            )
            onboarding_url = str(minted.get("url") or "").strip()
        except Exception:
            current_app.logger.exception(
                "Failed to mint onboarding magic link for share token=%s share_link_id=%s",
                normalized_token,
                row.get("share_link_id"),
            )
    return render_template(
        "short_share_watch.html",
        clip_title=clip_title,
        playback_url=f"{playback_url}#t=1",
        poster_url=poster_url,
        event_url=None if preview_mode else url_for("public_short_watch_event_alias", token=normalized_token),
        onboarding_url=onboarding_url or None,
        language=resolved_language,
        trial_duration_label=_trial_duration_label(row.get("trial_days"), resolved_language),
        preview_mode=preview_mode,
    )


@video_shorts_bp.route("/w/<token>")
def public_short_watch_page(token: str):
    return _render_public_short_watch_page(token)


def _handle_public_short_watch_event(token: str):
    if int(request.content_length or 0) > SHARE_EVENT_MAX_BODY_BYTES:
        return ("", 204)

    key_parts = [
        str(token or "").strip(),
        request.headers.get("X-Forwarded-For", ""),
        request.remote_addr or "",
    ]
    allowed, _retry_after = check_rate_limits("share-watch-event", key_parts, SHARE_EVENT_RATE_LIMITS)
    if not allowed:
        return ("", 204)

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return ("", 204)

    event_type = str(payload.get("type") or "").strip().lower()
    if event_type not in {"view", "play", "cta_click", "watch_progress"}:
        return ("", 204)

    conn = get_db_readonly()
    try:
        row = _load_shared_short_row(conn, token)
    finally:
        conn.close()

    if not row or not row.get("generated_video_id") or not row.get("owner_user_id"):
        return ("", 204)

    metadata = {
        "token": row["token"],
        "generated_video_id": row["generated_video_id"],
        "share_link_id": row["share_link_id"],
        "token_source": row["token_source"],
    }
    device_type = _normalize_share_watch_device_type(payload.get("device_type"))
    if device_type:
        metadata["device_type"] = device_type
    if event_type == "cta_click":
        cta_value = _normalize_share_watch_cta(payload.get("cta"))
        if not cta_value:
            return ("", 204)
        metadata["cta"] = cta_value
    elif event_type == "watch_progress":
        seconds_watched = _normalize_share_watch_seconds(payload.get("seconds_watched"))
        percent_watched = _normalize_share_watch_percent(payload.get("percent_watched"))
        if seconds_watched is None and percent_watched is None:
            return ("", 204)
        if seconds_watched is not None:
            metadata["seconds_watched"] = seconds_watched
        if percent_watched is not None:
            metadata["percent_watched"] = percent_watched

    track_event(
        row["owner_user_id"],
        f"share_{event_type}",
        short_id=row["generated_video_id"],
        metadata=metadata,
    )
    return ("", 204)


@video_shorts_bp.route("/w/<token>/event", methods=["POST"])
def public_short_watch_event(token: str):
    return _handle_public_short_watch_event(token)


@video_shorts_bp.route("/admin/share-links/<int:share_link_id>/emailed", methods=["POST"])
@require_admin
def admin_share_link_set_emailed(share_link_id: int):
    next_url = (request.form.get("next") or request.args.get("next") or "").strip()
    checked_value = (request.form.get("emailed") or "").strip().lower()
    mark_emailed = checked_value in {"1", "true", "yes", "on"}

    conn = get_db()
    try:
        if not _short_share_links_ready(conn):
            flash("Share links are not available until the database migration is applied.", "warning")
            return redirect(next_url or url_for("video_shorts_bp.admin_share_links"))
        share_link_columns = table_columns(conn, "short_share_links")
        if "emailed_at" not in share_link_columns:
            flash("Email sent tracking is not available until the database migration is applied.", "warning")
            return redirect(next_url or url_for("video_shorts_bp.admin_share_links"))
        exists = conn.execute(
            "SELECT 1 FROM short_share_links WHERE id = ? LIMIT 1",
            [share_link_id],
        ).fetchone()
        if not exists:
            abort(404)
        conn.execute(
            """
            UPDATE short_share_links
               SET emailed_at = CURRENT_TIMESTAMP
             WHERE id = ?
            """
            if mark_emailed
            else """
            UPDATE short_share_links
               SET emailed_at = NULL
             WHERE id = ?
            """,
            [share_link_id],
        )
        conn.commit()
    except HTTPException:
        raise
    except Exception:
        current_app.logger.exception("Failed to update emailed_at for short_share_link id=%s", share_link_id)
        try:
            conn.rollback()
        except Exception:
            pass
        flash("Email sent state could not be updated.", "danger")
    finally:
        conn.close()
    return redirect(next_url or url_for("video_shorts_bp.admin_share_links"))


@video_shorts_bp.route("/admin/share-links/<int:share_link_id>/archived", methods=["POST"])
@require_admin
def admin_share_link_set_archived(share_link_id: int):
    next_url = (request.form.get("next") or request.args.get("next") or "").strip()
    checked_value = (request.form.get("archived") or "").strip().lower()
    mark_archived = checked_value in {"1", "true", "yes", "on"}
    wants_json = (request.headers.get("X-Requested-With") or "").strip().lower() == "xmlhttprequest"

    conn = get_db()
    try:
        if not _short_share_links_ready(conn):
            if wants_json:
                return jsonify({"ok": False, "error": "share_links_unavailable"}), 503
            flash("Share links are not available until the database migration is applied.", "warning")
            return redirect(next_url or url_for("video_shorts_bp.admin_share_links"))
        share_link_columns = table_columns(conn, "short_share_links")
        if "archived" not in share_link_columns:
            if wants_json:
                return jsonify({"ok": False, "error": "archive_tracking_unavailable"}), 503
            flash("Archive tracking is not available until the database migration is applied.", "warning")
            return redirect(next_url or url_for("video_shorts_bp.admin_share_links"))
        exists = conn.execute(
            "SELECT 1 FROM short_share_links WHERE id = ? LIMIT 1",
            [share_link_id],
        ).fetchone()
        if not exists:
            if wants_json:
                return jsonify({"ok": False, "error": "not_found"}), 404
            abort(404)
        conn.execute(
            """
            UPDATE short_share_links
               SET archived = TRUE
             WHERE id = ?
            """
            if mark_archived
            else """
            UPDATE short_share_links
               SET archived = FALSE
             WHERE id = ?
            """,
            [share_link_id],
        )
        conn.commit()
        if wants_json:
            return jsonify({"ok": True, "archived": mark_archived})
    except HTTPException:
        raise
    except Exception:
        current_app.logger.exception("Failed to update archived for short_share_link id=%s", share_link_id)
        try:
            conn.rollback()
        except Exception:
            pass
        if wants_json:
            return jsonify({"ok": False, "error": "update_failed"}), 500
        flash("Archive state could not be updated.", "danger")
    finally:
        conn.close()
    return redirect(next_url or url_for("video_shorts_bp.admin_share_links"))


@video_shorts_bp.route("/api/admin/share-links/<int:share_link_id>/followup-sent", methods=["POST"])
def admin_share_link_set_followup_sent(share_link_id: int):
    current_user = getattr(g, "vs_current_user", None) or {}
    if not current_user:
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    if (current_user.get("role") or "").strip().lower() != "admin":
        return jsonify({"ok": False, "error": "forbidden"}), 403

    conn = get_db()
    try:
        if not _short_share_links_ready(conn):
            return jsonify({"ok": False, "error": "share_links_unavailable"}), 503
        share_link_columns = table_columns(conn, "short_share_links")
        if "followup_sent" not in share_link_columns or "followup_sent_at" not in share_link_columns:
            return jsonify({"ok": False, "error": "followup_tracking_unavailable"}), 503
        row = conn.execute(
            """
            SELECT followup_sent, followup_sent_at
            FROM short_share_links
            WHERE id = ?
            LIMIT 1
            """,
            [share_link_id],
        ).fetchone()
        if not row:
            return jsonify({"ok": False, "error": "not_found"}), 404
        if bool(row[0]) and row[1]:
            return jsonify(
                {
                    "ok": True,
                    "followup_sent_at": _format_datetime_pst(row[1]),
                    "followup_sent_at_iso": row[1].isoformat() if hasattr(row[1], "isoformat") else str(row[1] or ""),
                }
            )

        conn.execute(
            """
            UPDATE short_share_links
               SET followup_sent = TRUE,
                   followup_sent_at = CURRENT_TIMESTAMP
             WHERE id = ?
            """,
            [share_link_id],
        )
        conn.commit()
        updated_row = conn.execute(
            """
            SELECT followup_sent_at
            FROM short_share_links
            WHERE id = ?
            LIMIT 1
            """,
            [share_link_id],
        ).fetchone()
        followup_sent_at = updated_row[0] if updated_row else None
        return jsonify(
            {
                "ok": True,
                "followup_sent_at": _format_datetime_pst(followup_sent_at),
                "followup_sent_at_iso": (
                    followup_sent_at.isoformat() if hasattr(followup_sent_at, "isoformat") else str(followup_sent_at or "")
                ),
            }
        )
    except Exception:
        current_app.logger.exception("Failed to mark follow-up sent for short_share_link id=%s", share_link_id)
        try:
            conn.rollback()
        except Exception:
            pass
        return jsonify({"ok": False, "error": "update_failed"}), 500
    finally:
        conn.close()


@video_shorts_bp.route("/api/generated-shorts/share-link/<int:share_link_id>/language", methods=["POST"])
@require_admin
def admin_generated_short_share_link_set_language(share_link_id: int):
    payload = request.get_json(silent=True) or request.form or {}
    resolved_language = normalize_outreach_language(payload.get("language"), default="")
    if resolved_language not in {"TR", "EN"}:
        return jsonify({"success": False, "message": "Language must be TR or EN."}), 400

    conn = get_db()
    try:
        if not _short_share_links_ready(conn):
            return jsonify({"success": False, "message": "Share links are not available until the database migration is applied."}), 503
        share_link_columns = table_columns(conn, "short_share_links")
        if "language" not in share_link_columns:
            return jsonify({"success": False, "message": "Share-link language tracking is not available until the database migration is applied."}), 503
        exists = conn.execute(
            "SELECT 1 FROM short_share_links WHERE id = ? LIMIT 1",
            [share_link_id],
        ).fetchone()
        if not exists:
            return jsonify({"success": False, "message": "Share link not found."}), 404
        conn.execute(
            """
            UPDATE short_share_links
            SET language = ?
            WHERE id = ?
            """,
            [resolved_language, share_link_id],
        )
        conn.commit()
        return jsonify({"success": True, "share_link_id": share_link_id, "language": resolved_language})
    except Exception:
        current_app.logger.exception("Failed to update language for short_share_link id=%s", share_link_id)
        try:
            conn.rollback()
        except Exception:
            pass
        return jsonify({"success": False, "message": "Share-link language could not be updated."}), 500
    finally:
        conn.close()


@video_shorts_bp.route("/api/generated-shorts/share-link/<int:share_link_id>/trial-days", methods=["POST"])
@require_admin
def admin_generated_short_share_link_set_trial_days(share_link_id: int):
    payload = request.get_json(silent=True) or request.form or {}
    trial_days = normalize_trial_days(payload.get("trial_days"), default=DEFAULT_SHARE_TRIAL_DAYS)

    conn = get_db()
    try:
        if not _short_share_links_ready(conn):
            return jsonify({"success": False, "message": "Share links are not available until the database migration is applied."}), 503
        share_link_columns = table_columns(conn, "short_share_links")
        if "trial_days" not in share_link_columns:
            return jsonify({"success": False, "message": "Share-link trial days are not available until the database migration is applied."}), 503
        exists = conn.execute(
            "SELECT 1 FROM short_share_links WHERE id = ? LIMIT 1",
            [share_link_id],
        ).fetchone()
        if not exists:
            return jsonify({"success": False, "message": "Share link not found."}), 404
        conn.execute(
            """
            UPDATE short_share_links
            SET trial_days = ?
            WHERE id = ?
            """,
            [trial_days, share_link_id],
        )
        conn.commit()
        return jsonify(
            {
                "success": True,
                "share_link_id": share_link_id,
                "trial_days": trial_days,
                "trial_duration_label_tr": _trial_duration_label(trial_days, "TR"),
                "trial_duration_label_en": _trial_duration_label(trial_days, "EN"),
            }
        )
    except Exception:
        current_app.logger.exception("Failed to update trial_days for short_share_link id=%s", share_link_id)
        try:
            conn.rollback()
        except Exception:
            pass
        return jsonify({"success": False, "message": "Share-link trial days could not be updated."}), 500
    finally:
        conn.close()


@video_shorts_bp.route("/generate/preferences/clip-coachmark", methods=["POST"])
def update_clip_coachmark_preference():
    current_user = getattr(g, "vs_current_user", None)
    user_id = str((current_user or {}).get("id") or "").strip()
    if not user_id:
        return jsonify({"success": False, "message": "Authentication required."}), 401
    payload = request.get_json(silent=True) or {}
    hide_value = bool(payload.get("hide"))
    try:
        save_user_bool_preference(user_id, HIDE_CLIP_COACHMARK_PREFERENCE_KEY, hide_value)
    except Exception as exc:
        current_app.logger.warning("Failed to save clip coachmark preference for %s: %s", user_id, exc)
        return jsonify({"success": False, "message": "Preference could not be saved."}), 500
    return jsonify({"success": True, "hide": hide_value})


@video_shorts_bp.route("/generate/<int:video_pk>/create_long_from_shorts", methods=["POST"])
def create_long_from_shorts(video_pk: int):
    cleanup_video_shorts_temp_dir()
    conn = get_db_readonly()
    row = _fetch_scoped_video_row(conn, video_pk, "video_id, title")
    conn.close()
    if not row:
        return jsonify({"ok": False, "message": "Video not found."}), 404
    video_id = str(row[0] or "").strip()
    video_title = str(row[1] or "").strip() or "Long Compilation"
    suggested_title = _build_long_highlights_title(video_title)
    if not video_id:
        return jsonify({"ok": False, "message": "Video id not found."}), 400

    entries = _load_plan_entries(video_id)
    instagram_queue_map = load_instagram_queue_map([video_id])
    tiktok_queue_map = load_tiktok_queue_map([video_id])
    facebook_queue_map = load_facebook_queue_map([video_id])

    def _queue_has_published(queue_entries: List[Dict[str, Any]]) -> bool:
        if not queue_entries:
            return False
        valid_entries = [
            item
            for item in queue_entries
            if str(item.get("status") or "").strip().lower() not in {"canceled", "cancelled"}
        ]
        return any(str(item.get("status") or "").strip().lower() == "published" for item in valid_entries)

    published_clips: List[Dict[str, Any]] = []
    for entry in sorted(entries, key=lambda e: int(e.get("plan_index") or 0)):
        plan_index = int(entry.get("plan_index") or 0)
        status = str(entry.get("publish_status") or "").lower()
        ig_published = _queue_has_published(instagram_queue_map.get((video_id, str(plan_index))) or [])
        tt_published = _queue_has_published(tiktok_queue_map.get((video_id, str(plan_index))) or [])
        fb_published = _queue_has_published(facebook_queue_map.get((video_id, str(plan_index))) or [])
        any_platform_published = status == "published" or ig_published or tt_published or fb_published
        clip_filename = str(entry.get("clip_filename") or entry.get("output_filename") or "").strip()
        if any_platform_published and clip_filename and _short_exists(clip_filename):
            published_clips.append(
                {
                    "plan_index": plan_index,
                    "clip_filename": clip_filename,
                    "title": str(entry.get("title") or ""),
                }
            )
    if not published_clips:
        return jsonify({"ok": False, "message": "No published local short clip found."}), 400

    build = _build_long_compilation_from_published(
        video_id=video_id,
        base_title=suggested_title,
        clips=published_clips,
        suggested_title=suggested_title,
    )
    if not build.get("ok"):
        return jsonify({"ok": False, "message": build.get("message") or "Long video could not be created."}), 500

    skipped_items = build.get("skipped") or []
    used_count = max(0, len(published_clips) - len(skipped_items))
    msg = build.get("message") or "Long video created."
    msg = f"{msg} Included: {used_count}, skipped: {len(skipped_items)}."
    return jsonify(
        {
            "ok": True,
            "message": msg,
            "output_video_url": build.get("output_url"),
            "output_video_filename": build.get("output_filename"),
            "skipped": skipped_items,
            "long_videos": _list_generated_long_compilations(video_id, limit=12),
        }
    )


@video_shorts_bp.route("/generate/<int:video_pk>/publish_long_video", methods=["POST"])
def publish_long_video_from_generate(video_pk: int):
    current_user = getattr(g, "vs_current_user", None) or {}
    editor_context = _active_editor_context()
    user_id = editor_context["owner_user_id"]
    user_tz = current_user.get("time_zone") or DEFAULT_TIME_ZONE
    brand_id = editor_context["brand_id"]

    conn = get_db_readonly()
    row = conn.execute(
        """
        SELECT video_id, title
        FROM youtube_videos
        WHERE id = ?
          AND owner_user_id = ?
          AND ((? IS NULL AND brand_id IS NULL) OR brand_id = ?)
        """,
        [video_pk, user_id, brand_id, brand_id],
    ).fetchone()
    conn.close()
    if not row:
        return jsonify({"success": False, "message": "Video not found."}), 404
    video_id = str(row[0] or "").strip()
    source_title = str(row[1] or "").strip()

    filename = str(request.form.get("filename") or "").strip()
    if not _safe_long_comp_name(filename):
        return jsonify({"success": False, "message": "Invalid long video filename."}), 400
    media_path = SHORTS_DIR / filename
    if not media_path.exists():
        return jsonify({"success": False, "message": "Long video file not found."}), 404

    title = str(request.form.get("title") or source_title or filename.rsplit(".", 1)[0]).strip()[:100]
    description = str(request.form.get("description") or "").strip()[:5000]
    caption_text = (description or title)[:2200]

    youtube_enabled = str(request.form.get("youtube_enabled") or "").lower() in {"on", "1", "true", "yes"}
    instagram_reel = str(request.form.get("schedule_instagram_reel") or "").lower() in {"on", "1", "true", "yes"}
    instagram_feed = str(request.form.get("schedule_instagram_feed") or "").lower() in {"on", "1", "true", "yes"}
    facebook_reel = str(request.form.get("schedule_facebook_reel") or "").lower() in {"on", "1", "true", "yes"}
    facebook_feed = str(request.form.get("schedule_facebook_feed") or "").lower() in {"on", "1", "true", "yes"}
    tiktok_enabled = str(request.form.get("schedule_tiktok") or "").lower() in {"on", "1", "true", "yes"}
    force_requeue_instagram = str(request.form.get("force_requeue_instagram") or "").lower() in {"on", "1", "true", "yes"}
    force_requeue_tiktok = str(request.form.get("force_requeue_tiktok") or "").lower() in {"on", "1", "true", "yes"}

    if not any([youtube_enabled, instagram_reel, instagram_feed, facebook_reel, facebook_feed, tiktok_enabled]):
        return jsonify({"success": False, "message": "Select at least one platform."}), 400

    def _local_schedule(field_name: str) -> Optional[str]:
        raw = str(request.form.get(field_name) or "").strip()
        if not raw:
            return None
        return local_to_utc_rfc3339(raw, user_tz)

    try:
        yt_publish_at = _local_schedule("publish_at")
        ig_publish_at = _local_schedule("instagram_publish_at")
        fb_publish_at = _local_schedule("facebook_publish_at")
        tt_publish_at = _local_schedule("tiktok_publish_at")
    except Exception:
        return jsonify({"success": False, "message": "Invalid schedule date format."}), 400

    ig_mode = str(request.form.get("instagram_mode") or "sync").strip().lower()
    fb_mode = str(request.form.get("facebook_mode") or "sync").strip().lower()
    tt_mode = str(request.form.get("tiktok_mode") or "sync").strip().lower()
    if ig_mode == "sync":
        ig_publish_at = yt_publish_at
    elif ig_mode == "now":
        ig_publish_at = None
    if fb_mode == "sync":
        fb_publish_at = yt_publish_at
    elif fb_mode == "now":
        fb_publish_at = None
    if tt_mode == "sync":
        tt_publish_at = yt_publish_at
    elif tt_mode == "now":
        tt_publish_at = None

    results: Dict[str, Any] = {"youtube": None, "instagram": [], "facebook": [], "tiktok": None}
    youtube_video_id = None

    if youtube_enabled:
        if not has_refresh_token(user_id, brand_id=brand_id):
            return jsonify({"success": False, "message": "YouTube account not connected."}), 403
        try:
            resp = upload_video_with_refresh_token(
                video_path=str(media_path),
                title=title,
                description=description,
                publish_at=yt_publish_at,
                privacy_status="private",
                user_id=user_id,
                brand_id=brand_id,
            )
            youtube_video_id = (resp or {}).get("id")
            results["youtube"] = {"id": youtube_video_id, "scheduled_at": yt_publish_at}
        except Exception as exc:
            return jsonify({"success": False, "message": f"YouTube publish failed: {exc}"}), 500

    plan_index = "longcomp"
    if instagram_reel or instagram_feed:
        if not user_id:
            return jsonify({"success": False, "message": "Login required for Instagram queue."}), 403
        try:
            ig_creds = get_instagram_credentials(user_id)
        except InstagramTokenStoreError:
            ig_creds = None
        if not ig_creds:
            return jsonify({"success": False, "message": "Instagram account not connected."}), 403
        for media_type in [m for m in ["reel", "feed"] if (instagram_reel and m == "reel") or (instagram_feed and m == "feed")]:
            qid = enqueue_instagram_clip(
                user_id=user_id,
                video_id=video_id,
                plan_index=plan_index,
                clip_filename=filename,
                caption_text=caption_text,
                publish_at_iso=ig_publish_at,
                instagram_business_account_id=ig_creds.get("instagram_business_account_id"),
                instagram_username=ig_creds.get("instagram_username"),
                youtube_video_id=None,
                youtube_short_id=youtube_video_id,
                plan_title=title,
                media_type=media_type,
                force_requeue=force_requeue_instagram,
            )
            results["instagram"].append({"media_type": media_type, "queue_id": qid})

    if facebook_reel or facebook_feed:
        if not user_id:
            return jsonify({"success": False, "message": "Login required for Facebook queue."}), 403
        try:
            fb_info = get_facebook_page_data(user_id)
        except FacebookTokenStoreError:
            fb_info = None
        if not fb_info or not fb_info.get("page_access_token"):
            return jsonify({"success": False, "message": "Facebook account not connected."}), 403
        for media_type in [m for m in ["reel", "feed"] if (facebook_reel and m == "reel") or (facebook_feed and m == "feed")]:
            qid = enqueue_facebook_clip(
                user_id=user_id,
                video_id=video_id,
                plan_index=plan_index,
                clip_filename=filename,
                caption_text=caption_text,
                publish_at_iso=fb_publish_at,
                page_id=fb_info.get("page_id"),
                page_name=fb_info.get("page_name"),
                plan_title=title,
                media_type=media_type,
            )
            results["facebook"].append({"media_type": media_type, "queue_id": qid})

    if tiktok_enabled:
        if not user_id:
            return jsonify({"success": False, "message": "Login required for TikTok queue."}), 403
        try:
            tt_info = get_tiktok_data(user_id)
        except TikTokTokenStoreError:
            tt_info = None
        if not tt_info or not tt_info.get("access_token") or _is_token_expired(tt_info.get("expires_at")):
            return jsonify({"success": False, "message": "TikTok account not connected."}), 403
        qid = enqueue_tiktok_clip(
            user_id=user_id,
            video_id=video_id,
            plan_index=plan_index,
            clip_filename=filename,
            caption_text=caption_text,
            publish_at_iso=tt_publish_at,
            tiktok_open_id=tt_info.get("open_id"),
            tiktok_username=tt_info.get("username"),
            plan_title=title,
            force_requeue=force_requeue_tiktok,
        )
        results["tiktok"] = {"queue_id": qid}

    state_updates: Dict[str, Any] = {}
    if youtube_enabled:
        yt_status = "scheduled" if yt_publish_at else "published"
        state_updates["youtube"] = {
            "status": yt_status,
            "publish_at": yt_publish_at,
            "published_at": None if yt_publish_at else (datetime.utcnow().isoformat() + "Z"),
            "yt_video_id": youtube_video_id,
            "updated_at": datetime.utcnow().isoformat() + "Z",
        }
    if instagram_reel or instagram_feed:
        state_updates["instagram"] = {
            "status": "scheduled" if ig_publish_at else "queued",
            "publish_at": ig_publish_at,
            "updated_at": datetime.utcnow().isoformat() + "Z",
        }
    if facebook_reel or facebook_feed:
        state_updates["facebook"] = {
            "status": "scheduled" if fb_publish_at else "queued",
            "publish_at": fb_publish_at,
            "updated_at": datetime.utcnow().isoformat() + "Z",
        }
    if tiktok_enabled:
        state_updates["tiktok"] = {
            "status": "scheduled" if tt_publish_at else "queued",
            "publish_at": tt_publish_at,
            "updated_at": datetime.utcnow().isoformat() + "Z",
        }
    if state_updates:
        _update_long_comp_publish_state(filename, state_updates)

    return jsonify({"success": True, "message": "Publish/queue created.", "results": results})


@video_shorts_bp.route("/generate/<int:video_pk>/delete_long_video", methods=["POST"])
def delete_long_video_from_generate(video_pk: int):
    editor_context = _active_editor_context()
    owner_user_id = editor_context["owner_user_id"]
    brand_id = editor_context["brand_id"]
    conn = get_db_readonly()
    row = conn.execute(
        """
        SELECT video_id
        FROM youtube_videos
        WHERE id = ?
          AND owner_user_id = ?
          AND ((? IS NULL AND brand_id IS NULL) OR brand_id = ?)
        """,
        [video_pk, owner_user_id, brand_id, brand_id],
    ).fetchone()
    conn.close()
    if not row:
        return jsonify({"ok": False, "message": "Video not found."}), 404
    video_id = str(row[0] or "").strip()

    payload = request.get_json(silent=True) or {}
    filename = str(payload.get("filename") or request.form.get("filename") or "").strip()
    if not _safe_long_comp_name(filename):
        return jsonify({"ok": False, "message": "Invalid filename."}), 400

    target = SHORTS_DIR / filename
    meta = _long_comp_meta_path(target)
    removed = False
    try:
        if target.exists() and target.is_file():
            target.unlink()
            removed = True
        if meta.exists() and meta.is_file():
            meta.unlink()
            removed = True or removed
    except Exception:
        current_app.logger.exception("Long video delete failed for %s", filename)
        return jsonify({"ok": False, "message": "Delete failed."}), 500

    if not removed:
        return jsonify({"ok": False, "message": "File not found."}), 404

    return jsonify(
        {
            "ok": True,
            "message": "Long video deleted.",
            "long_videos": _list_generated_long_compilations(video_id, limit=12),
        }
    )


@video_shorts_bp.route("/generate/<int:video_pk>/save_crop", methods=["POST"])
def save_crop_area(video_pk):
    current_user = getattr(g, "vs_current_user", None)
    editor_context = _active_editor_context()
    brand_id = editor_context["brand_id"]
    editor_owner_user_id = editor_context["owner_user_id"]
    def _parse_ratio(name: str):
        value = request.form.get(name)
        if value is None or value == "":
            return None
        try:
            return float(value)
        except Exception:
            return None

    static_visual_key = (request.form.get("static_visual_key") or "").strip()
    if static_visual_key == "":
        static_visual_key = None
    background_visual_key = (request.form.get("background_visual_key") or "").strip()
    if background_visual_key == "":
        background_visual_key = None
    subscribe_overlay_key = _normalize_subscribe_overlay_key(
        request.form.get("subscribe_overlay_image"),
        expected_owner_user_id=editor_owner_user_id,
        expected_brand_id=brand_id,
    )
    subscribe_overlay_value = request.form.get("enable_subscribe_overlay")
    subscribe_overlay_enabled_raw = (subscribe_overlay_value or "").strip().lower()
    subscribe_overlay_enabled = subscribe_overlay_enabled_raw in {"1", "true", "yes", "on"}
    crop_aspect = (request.form.get("crop_aspect") or "").strip().lower()
    if crop_aspect not in {"landscape", "portrait"}:
        crop_aspect = "landscape"
    visual_mode = (request.form.get("visual_mode") or "").strip().lower()

    ratios = {
        "crop_x_ratio": _parse_ratio("crop_x_ratio"),
        "crop_y_ratio": _parse_ratio("crop_y_ratio"),
        "crop_w_ratio": _parse_ratio("crop_w_ratio"),
        "crop_h_ratio": _parse_ratio("crop_h_ratio"),
    }
    if any(val is None for val in ratios.values()):
        return jsonify(success=False, message="Invalid crop values."), 400
    split_enabled = (request.form.get("split_enabled") or "").strip().lower() in {"1", "true", "yes", "on"}
    crop2_ratios = {
        "crop2_x_ratio": _parse_ratio("crop2_x_ratio"),
        "crop2_y_ratio": _parse_ratio("crop2_y_ratio"),
        "crop2_w_ratio": _parse_ratio("crop2_w_ratio"),
        "crop2_h_ratio": _parse_ratio("crop2_h_ratio"),
    }
    has_crop2_values = any(val is not None for val in crop2_ratios.values())
    if split_enabled and any(val is None for val in crop2_ratios.values()):
        return jsonify(success=False, message="Invalid split crop values."), 400

    def _clamp(value: float, minimum: float = 0.0, maximum: float = 1.0) -> float:
        return max(minimum, min(maximum, value))

    x_val = _clamp(ratios["crop_x_ratio"])
    y_val = _clamp(ratios["crop_y_ratio"])
    w_val = max(0.01, min(1.0 - x_val, _clamp(ratios["crop_w_ratio"])))
    h_val = max(0.01, min(1.0 - y_val, _clamp(ratios["crop_h_ratio"])))
    crop2_values = {
        "crop2_x_ratio": None,
        "crop2_y_ratio": None,
        "crop2_w_ratio": None,
        "crop2_h_ratio": None,
    }
    if has_crop2_values:
        crop2_x = _clamp(crop2_ratios["crop2_x_ratio"] or 0.0)
        crop2_y = _clamp(crop2_ratios["crop2_y_ratio"] or 0.0)
        crop2_w = max(0.01, min(1.0 - crop2_x, _clamp(crop2_ratios["crop2_w_ratio"] or 0.0)))
        crop2_h = max(0.01, min(1.0 - crop2_y, _clamp(crop2_ratios["crop2_h_ratio"] or 0.0)))
        crop2_values = {
            "crop2_x_ratio": crop2_x,
            "crop2_y_ratio": crop2_y,
            "crop2_w_ratio": crop2_w,
            "crop2_h_ratio": crop2_h,
        }

    conn = get_db()
    try:
        _ensure_video_crop_schema(conn)
        video_columns = table_columns(conn, "youtube_videos")
        existing_row = None
        try:
            existing_sql = """
                SELECT podcast_audio_filename, subscribe_overlay_enabled
                FROM youtube_videos
                WHERE id = ?
                  AND owner_user_id = ?
            """
            existing_params: List[Any] = [video_pk, editor_owner_user_id]
            if "brand_id" in video_columns:
                if brand_id is None:
                    existing_sql += "\n AND brand_id IS NULL"
                else:
                    existing_sql += "\n AND brand_id = ?"
                    existing_params.append(brand_id)
            existing_row = conn.execute(existing_sql, existing_params).fetchone()
        except Exception:
            existing_row = None
        if subscribe_overlay_value is None and existing_row is not None and len(existing_row) > 1:
            subscribe_overlay_enabled = bool(existing_row[1]) if existing_row[1] is not None else True
        if visual_mode == "podcast":
            try:
                audio_row = existing_row
                if audio_row and str(audio_row[0] or "").strip():
                    crop_aspect = "landscape"
                if subscribe_overlay_value is None and audio_row is not None and len(audio_row) > 1:
                    subscribe_overlay_enabled = bool(audio_row[1]) if audio_row[1] is not None else True
            except Exception:
                pass
        update_set_parts = [
            "split_enabled = ?",
            "crop_x_ratio = ?",
            "crop_y_ratio = ?",
            "crop_w_ratio = ?",
            "crop_h_ratio = ?",
        ]
        update_params: List[Any] = [
            split_enabled,
            x_val,
            y_val,
            w_val,
            h_val,
        ]
        if has_crop2_values:
            update_set_parts.extend(
                [
                    "crop2_x_ratio = ?",
                    "crop2_y_ratio = ?",
                    "crop2_w_ratio = ?",
                    "crop2_h_ratio = ?",
                ]
            )
            update_params.extend(
                [
                    crop2_values["crop2_x_ratio"],
                    crop2_values["crop2_y_ratio"],
                    crop2_values["crop2_w_ratio"],
                    crop2_values["crop2_h_ratio"],
                ]
            )
        update_set_parts.extend(
            [
                "static_visual_key = ?",
                "background_visual_key = ?",
                "crop_aspect = ?",
                "subscribe_overlay_enabled = ?",
            ]
        )
        update_sql = f"""
            UPDATE youtube_videos
            SET {", ".join(update_set_parts)}
            WHERE id = ?
              AND owner_user_id = ?
        """
        update_params.extend(
            [
                static_visual_key,
                background_visual_key,
                crop_aspect,
                subscribe_overlay_enabled,
                video_pk,
                editor_owner_user_id,
            ]
        )
        if "brand_id" in video_columns:
            if brand_id is None:
                update_sql += "\n AND brand_id IS NULL"
            else:
                update_sql += "\n AND brand_id = ?"
                update_params.append(brand_id)
        cursor = conn.execute(update_sql, update_params)
        conn.commit()
        if cursor.rowcount == 0:
            return jsonify(success=False, message="Video not found."), 404
        _save_subscribe_overlay_key(
            editor_owner_user_id,
            brand_id,
            video_pk,
            subscribe_overlay_key,
        )
    except Exception as exc:
        current_app.logger.exception("Crop save failed for video %s: %s", video_pk, exc)
        return jsonify(success=False, message="Unable to save crop settings."), 500
    finally:
        conn.close()

    return jsonify(
        success=True,
        crop={
            "split_enabled": split_enabled,
            "crop_x_ratio": x_val,
            "crop_y_ratio": y_val,
            "crop_w_ratio": w_val,
            "crop_h_ratio": h_val,
            "crop2_x_ratio": crop2_values["crop2_x_ratio"],
            "crop2_y_ratio": crop2_values["crop2_y_ratio"],
            "crop2_w_ratio": crop2_values["crop2_w_ratio"],
            "crop2_h_ratio": crop2_values["crop2_h_ratio"],
            "static_visual_key": static_visual_key,
            "background_visual_key": background_visual_key,
            "subscribe_overlay_image": subscribe_overlay_key,
            "crop_aspect": crop_aspect,
        },
    )


@video_shorts_bp.route("/generate/<int:video_pk>/download", methods=["POST"])
def download_video(video_pk):
    current_user = getattr(g, "vs_current_user", None)
    brand_id = current_brand_id()
    if current_user:
        conn_usage = get_db_readonly()
        try:
            usage = _get_user_storage_usage(conn_usage, current_user["id"])
        finally:
            conn_usage.close()
        if usage["used_bytes"] >= usage["limit_bytes"]:
            flash(
                _quota_block_message(
                    _format_size_bytes(usage["limit_bytes"]),
                    _format_size_bytes(usage["used_bytes"]),
                ),
                "danger",
            )
            return redirect(url_for("video_shorts_bp.shorts_storage_plans"))

    conn = get_db_readonly()
    row = conn.execute(
        """
        SELECT video_id, video_url
        FROM youtube_videos
        WHERE id = ?
          AND owner_user_id = ?
          AND ((? IS NULL AND brand_id IS NULL) OR brand_id = ?)
        """,
        [video_pk, current_user.get("id") if current_user else None, brand_id, brand_id],
    ).fetchone()
    conn.close()
    if not row:
        flash("Video not found", "danger")
        return redirect(url_for("video_shorts_bp.channels_page"))

    video_id, video_url = row
    if not video_url:
        flash("Video URL missing.", "danger")
        return redirect(url_for("video_shorts_bp.generate_short", video_pk=video_pk))

    SHORTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = SHORTS_DIR / f"{video_id}.mp4"

    try:
        try:
            import yt_dlp as youtube_dl
        except ImportError:
            flash("yt_dlp is not installed on the server; cannot download video.", "danger")
            return redirect(url_for("video_shorts_bp.generate_short", video_pk=video_pk))

        base_opts = {
            "outtmpl": str(out_path.with_suffix(".%(ext)s")),
            "merge_output_format": "mp4",
            "quiet": True,
            "noprogress": True,
        }

        # If a cookies file is provided, use it (for age/consent/anti-bot walls)
        if YT_DLP_COOKIES:
            cookie_path = Path(YT_DLP_COOKIES)
            if cookie_path.exists():
                base_opts["cookiefile"] = str(cookie_path)
            else:
                flash(
                    f"Cookies file not found at {cookie_path}. Skipping login cookies.",
                    "warning",
                )

        formats_to_try = [
            "bestvideo*+bestaudio/best",
            "best",  # most permissive fallback
        ]

        last_err = None
        for fmt in formats_to_try:
            opts = dict(base_opts)
            opts["format"] = fmt
            try:
                with youtube_dl.YoutubeDL(opts) as ydl:
                    ydl.download([video_url])
                last_err = None
                break
            except Exception as e:
                last_err = e

        if last_err:
            raise last_err

        # Normalize to .mp4 extension if different
        if not out_path.exists():
            candidates = list(out_path.parent.glob(f"{video_id}.*"))
            if candidates:
                candidates[0].rename(out_path)
        out_path = normalize_source_video_for_streaming(out_path, log=current_app.logger)

        if current_user:
            size_bytes = out_path.stat().st_size if out_path.exists() else 0
            conn_usage = get_db_readonly()
            try:
                usage = _get_user_storage_usage(conn_usage, current_user["id"])
            finally:
                conn_usage.close()
            if usage["used_bytes"] + size_bytes > usage["limit_bytes"]:
                try:
                    out_path.unlink()
                except Exception:
                    current_app.logger.warning("Failed to remove oversized download %s", out_path)
                flash(
                    _quota_block_message(
                        _format_size_bytes(usage["limit_bytes"]),
                        _format_size_bytes(usage["used_bytes"]),
                    ),
                    "danger",
                )
                return redirect(url_for("video_shorts_bp.shorts_storage_plans"))
            _upsert_storage_asset(
                f"downloaded:{out_path.name}",
                str(out_path),
                "downloaded",
                size_bytes,
                current_user["id"],
                brand_id=current_brand_id(),
            )

        # Mark as downloaded in the DB so UI reflects local availability
        conn_w = get_db()
        _ensure_video_crop_schema(conn_w)
        try:
            conn_w.execute(
                """
                UPDATE youtube_videos
                SET download_status = 'downloaded',
                    downloaded_at = now()
                WHERE id = ?
                  AND owner_user_id = ?
                  AND ((? IS NULL AND brand_id IS NULL) OR brand_id = ?)
                """,
                [video_pk, current_user["id"], current_brand_id(), current_brand_id()],
            )
        finally:
            conn_w.close()

        flash(f"Video downloaded to {out_path.name}", "success")
    except Exception as e:
        flash(f"Download failed: {e}", "danger")

    return redirect(url_for("video_shorts_bp.generate_short", video_pk=video_pk))


@video_shorts_bp.route("/shorts/storage")
def shorts_storage():
    current_user = getattr(g, "vs_current_user", None)
    if not current_user:
        return redirect(url_for("video_shorts_bp.login", next=request.url))
    is_admin = current_user.get("role") == "admin"
    if not is_admin:
        return redirect(url_for("video_shorts_bp.shorts_storage_plans"))
    brand_id = current_brand_id()
    force_sync = (request.args.get("sync") or "").strip().lower() in {"1", "true", "yes"}
    conn = get_db()
    ensure_storage_user_schema(conn)
    ensure_channel_owner_schema(conn)
    video_sql = """
        SELECT yv.id, yv.video_id, yv.title, yv.channel_id, ch.channel_name, yv.download_status
        FROM youtube_videos yv
        LEFT JOIN youtube_channels ch ON yv.channel_id = ch.channel_id
    """
    video_params: List[Any] = []
    video_where: List[str] = []
    if not is_admin:
        video_where.append("yv.owner_user_id = ?")
        video_params.append(current_user["id"])
    if brand_id:
        video_where.append("yv.brand_id = ?")
        video_params.append(brand_id)
    if video_where:
        video_sql += " WHERE " + " AND ".join(video_where)
    video_sql += " ORDER BY yv.id DESC"
    rows = conn.execute(video_sql, video_params).fetchall()
    video_meta: Dict[str, Dict[str, str]] = {}
    for db_id, video_id, title, channel_id, channel_name, download_status in rows:
        plan_stats = _load_short_plan_stats(video_id)
        meta = {
            "youtube_title": title or "",
            "db_id": str(db_id),
            "channel_name": channel_name or "",
            "video_pk": str(db_id),
            "status": download_status or "",
            "video_id": video_id,
            "plan_count": plan_stats["plan_count"],
            "created_count": plan_stats["created_count"],
            "desc_ready": plan_stats["desc_ready"],
        }
        variants = {
            video_id,
            f"{video_id}.mp4",
            f"{video_id}.mov",
            f"{video_id}.mkv",
            f"{video_id}.mp3",
            f"{video_id}.wav",
            f"{video_id}.m4a",
            f"{video_id}.aac",
            f"{video_id}.ogg",
            f"{video_id}.flac",
            f"{video_id}.mp4M",
            str(db_id),
            f"{str(db_id)}.mp4",
            f"{str(db_id)}.mov",
            f"{str(db_id)}.mkv",
            f"{str(db_id)}.mp3",
            f"{str(db_id)}.wav",
            f"{str(db_id)}.m4a",
            f"{str(db_id)}.aac",
            f"{str(db_id)}.ogg",
            f"{str(db_id)}.flac",
        }
        if title:
            meta["youtube_title"] = title
        for key in variants:
            video_meta[key] = meta
    allowed_video_ids = {
        str(meta.get("video_id") or "")
        for meta in video_meta.values()
        if str(meta.get("video_id") or "")
    }
    assets_map = {}
    for file_key, user_id_value, size_bytes in conn.execute(
        "SELECT file_key, CAST(user_id AS VARCHAR), size_bytes FROM shorts_storage_assets"
    ).fetchall():
        assets_map[file_key] = {"user_id": user_id_value, "size_bytes": size_bytes}
    plan_rows = conn.execute(
        "SELECT plan_id, label, quota_bytes FROM shorts_storage_plans ORDER BY sort_order, label"
    ).fetchall()
    users_rows = conn.execute(
        """
        SELECT
          CAST(u.id AS VARCHAR),
          u.name,
          u.email,
          u.plan_id,
          u.custom_limit_bytes,
          p.label,
          p.quota_bytes,
          u.username,
          u.role
        FROM shorts_users u
        LEFT JOIN shorts_storage_plans p ON p.plan_id = u.plan_id
        ORDER BY u.name
        """
    ).fetchall()
    users_lookup: Dict[str, Dict[str, Any]] = {}
    storage_users: List[Dict[str, Any]] = []
    for row in users_rows:
        limit_bytes = row[4] or row[6] or DEFAULT_USER_STORAGE_LIMIT
        user_dict = {
            "id": row[0],
            "name": row[1],
            "email": row[2],
            "plan_id": row[3],
            "custom_limit_bytes": row[4],
            "plan_label": row[5],
            "limit_bytes": limit_bytes,
            "username": row[7],
            "role": row[8] or "member",
            "plan_quota_bytes": row[6],
        }
        storage_users.append(user_dict)
        users_lookup[row[0]] = user_dict
    storage_entries, downloaded_total, short_total, local_backed_total, s3_backed_total = _collect_combined_storage_entries(video_meta, assets_map)
    current_user = getattr(g, "vs_current_user", None)
    default_owner_id = current_user["id"] if current_user else None
    if force_sync:
        _sync_storage_assets(conn, storage_entries, assets_map, default_owner_id)
        conn.commit()
    usage_rows_by_user = {
        row[0]: row[1]
        for row in conn.execute(
            """
            SELECT CAST(user_id AS VARCHAR), SUM(size_bytes)
            FROM shorts_storage_assets
            WHERE user_id IS NOT NULL AND (status = 'active' OR status IS NULL)
            GROUP BY user_id
            """
        ).fetchall()
    }
    conn.close()

    if not is_admin:
        storage_entries = [
            entry for entry in storage_entries if entry.get("owner_user_id") == current_user["id"]
        ]
    if brand_id:
        storage_entries = [
            entry for entry in storage_entries
            if str(entry.get("video_id") or "") in allowed_video_ids
        ]

    for entry in storage_entries:
        owner_id = entry.get("owner_user_id")
        if owner_id and owner_id in users_lookup:
            entry["owner_user_name"] = users_lookup[owner_id]["name"]
        else:
            entry["owner_user_name"] = None

    videos_dir_sort = request.args.get("videos_sort", "size")
    videos_dir_sort_dir = request.args.get("videos_dir", "desc")
    sort_dir_normalized = (videos_dir_sort_dir or "desc").lower()
    reverse = sort_dir_normalized != "asc"

    def _sort_key(entry):
        if videos_dir_sort == "size":
            return entry["size_bytes"]
        if videos_dir_sort == "backend":
            return (entry.get("storage_backend") or "").lower()
        if videos_dir_sort == "status":
            return (entry.get("status_label") or "").lower()
        if videos_dir_sort == "title":
            return (entry.get("youtube_title") or "").lower()
        if videos_dir_sort == "name":
            return (entry.get("name") or "").lower()
        if videos_dir_sort == "channel":
            return (entry.get("channel_name") or "").lower()
        if videos_dir_sort == "downloaded":
            return entry.get("modified") or ""
        if videos_dir_sort == "type":
            return entry.get("file_type") or ""
        return (entry.get("youtube_title") or entry.get("name") or "").lower()

    storage_entries.sort(key=_sort_key, reverse=reverse)

    downloaded_total = sum(entry["size_bytes"] for entry in storage_entries if entry["file_type"] == "downloaded")
    short_total = sum(entry["size_bytes"] for entry in storage_entries if entry["file_type"] == "short")
    local_backed_total = sum(int(entry.get("local_backed_bytes") or 0) for entry in storage_entries)
    s3_backed_total = sum(int(entry.get("s3_backed_bytes") or 0) for entry in storage_entries)
    storage_total_bytes = downloaded_total + short_total
    usage_by_user: Dict[str, int] = {uid: total or 0 for uid, total in usage_rows_by_user.items()}
    assigned_total = sum(usage_by_user.values())
    unassigned_bytes = storage_total_bytes - assigned_total

    users_summary: List[Dict[str, Any]] = []
    if not is_admin:
        storage_users = [
            user for user in storage_users if user["id"] == current_user["id"]
        ]
    for user in storage_users:
        used = usage_by_user.get(user["id"], 0)
        limit_bytes = user["limit_bytes"] or DEFAULT_USER_STORAGE_LIMIT
        percent = int(min(100, (used / limit_bytes * 100))) if limit_bytes else 0
        users_summary.append(
            {
                "id": user["id"],
                "name": user["name"],
                "username": user.get("username"),
                "plan_label": user["plan_label"] or "No plan",
                "limit_label": _format_size_bytes(limit_bytes),
                "used_label": _format_size_bytes(used),
                "used_bytes": used,
                "limit_label_short": _format_size_bytes(user["plan_quota_bytes"] or limit_bytes).replace(".00", ""),
                "limit_bytes": limit_bytes,
                "percent": percent,
            }
        )
    current_usage = None
    if current_user:
        user_limit_bytes = None
        user_plan_quota_bytes = None
        for summary in users_summary:
            if summary["id"] == current_user["id"]:
                user_limit_bytes = summary["limit_bytes"]
                break
        current_user_meta = users_lookup.get(current_user["id"])
        if current_user_meta:
            user_plan_quota_bytes = current_user_meta.get("plan_quota_bytes")
        limit_bytes = user_limit_bytes or DEFAULT_USER_STORAGE_LIMIT
        percent = int(min(100, (storage_total_bytes / limit_bytes * 100))) if limit_bytes else 0
        current_usage = {
            "id": current_user["id"],
            "used_label": _format_size_bytes(storage_total_bytes),
            "used_bytes": storage_total_bytes,
            "limit_label": _format_size_bytes(limit_bytes),
            "limit_label_short": _format_size_bytes(user_plan_quota_bytes or limit_bytes).replace(".00", ""),
            "limit_bytes": limit_bytes,
            "percent": percent,
        }

    plan_cards = [
        {"plan_id": row[0], "label": row[1], "quota_bytes": row[2], "quota_label": _format_size_bytes(row[2])}
        for row in plan_rows
    ]

    return render_template(
        "shorts_storage.html",
        current_time=datetime.now().strftime("%Y-%m-%d %H:%M"),
        storage_entries=storage_entries,
        storage_total_size=_format_size_bytes(storage_total_bytes),
        downloaded_total_size=_format_size_bytes(downloaded_total),
        short_total_size=_format_size_bytes(short_total),
        storage_total_bytes=storage_total_bytes,
        downloaded_total_bytes=downloaded_total,
        short_total_bytes=short_total,
        local_backed_total_bytes=local_backed_total,
        s3_backed_total_bytes=s3_backed_total,
        local_backed_total_size=_format_size_bytes(local_backed_total),
        s3_backed_total_size=_format_size_bytes(s3_backed_total),
        storage_sort=videos_dir_sort,
        storage_sort_dir=videos_dir_sort_dir,
        storage_users=storage_users,
        users_summary=users_summary,
        current_usage=current_usage,
        unassigned_bytes=unassigned_bytes,
        unassigned_label=_format_size_bytes(max(0, unassigned_bytes)),
        plan_cards=plan_cards,
    )


def _load_storage_plan_catalog(conn) -> List[Dict[str, Any]]:
    plans = load_usage_storage_plan_catalog(conn)
    plan_order = ["plan_free", "plan_2gb", "plan_10gb", "plan_100gb"]
    plan_lookup = {plan["plan_id"]: plan for plan in plans if plan.get("plan_id") in plan_order}
    return [plan_lookup[plan_id] for plan_id in plan_order if plan_id in plan_lookup]


def _format_admin_usd(amount: Optional[float], *, approx: bool = False) -> str:
    if amount is None:
        return "—"
    prefix = "≈" if approx else ""
    return f"{prefix}${amount:.2f}"


def _format_transcript_hours_compact(minutes: Any) -> str:
    try:
        hours = round(float(minutes or 0) / 60.0, 1)
    except Exception:
        hours = 0.0
    if abs(hours - round(hours)) < 1e-9:
        return f"{int(round(hours))}h"
    return f"{hours:.1f}h"


def _build_admin_transcription_usage_display(used_minutes: Any, limit_minutes: Any) -> Dict[str, Any]:
    try:
        used_value = float(used_minutes or 0)
    except Exception:
        used_value = 0.0
    limit_value: Optional[float]
    try:
        limit_value = None if limit_minutes is None else float(limit_minutes)
    except Exception:
        limit_value = None
    used_label = _format_transcript_hours_compact(used_value)
    limit_label = "∞" if limit_value is None else _format_transcript_hours_compact(limit_value)
    return {
        "used_minutes": used_value,
        "limit_minutes": limit_value,
        "used_label": used_label,
        "limit_label": limit_label,
        "display": f"{used_label} / {limit_label}",
        "is_over_limit": bool(limit_value is not None and used_value > limit_value),
    }


def _load_admin_transcription_usage(
    conn,
    user_ids: List[str],
    *,
    plan_id_map: Optional[Dict[str, str]] = None,
) -> Dict[str, Dict[str, Any]]:
    normalized_user_ids = [str(user_id or "").strip() for user_id in user_ids if str(user_id or "").strip()]
    if not normalized_user_ids:
        return {}

    placeholders = ", ".join("?" for _ in normalized_user_ids)
    current_month_start = datetime.now(timezone.utc).date().replace(day=1)
    used_minutes_map: Dict[str, float] = {}
    limit_minutes_map: Dict[str, Optional[float]] = {}

    usage_rows = conn.execute(
        f"""
        SELECT CAST(user_id AS VARCHAR), COALESCE(transcription_minutes_used, 0)
        FROM shorts_user_usage
        WHERE CAST(user_id AS VARCHAR) IN ({placeholders})
          AND period_start = ?
        """,
        normalized_user_ids + [current_month_start],
    ).fetchall()
    for row in usage_rows:
        used_minutes_map[str(row[0])] = float(row[1] or 0.0)

    resolved_plan_id_map = {str(key): str(value or "").strip() for key, value in (plan_id_map or {}).items() if str(key or "").strip()}
    missing_plan_users = [user_id for user_id in normalized_user_ids if user_id not in resolved_plan_id_map]
    if missing_plan_users:
        missing_placeholders = ", ".join("?" for _ in missing_plan_users)
        plan_rows = conn.execute(
            f"""
            SELECT CAST(id AS VARCHAR), COALESCE(plan_id, 'plan_free')
            FROM shorts_users
            WHERE CAST(id AS VARCHAR) IN ({missing_placeholders})
            """,
            missing_plan_users,
        ).fetchall()
        for row in plan_rows:
            resolved_plan_id_map[str(row[0])] = str(row[1] or "plan_free")

    plan_ids = sorted({plan_id for plan_id in resolved_plan_id_map.values() if plan_id})
    if plan_ids:
        plan_placeholders = ", ".join("?" for _ in plan_ids)
        plan_rows = conn.execute(
            f"""
            SELECT plan_id, monthly_transcription_minutes
            FROM shorts_storage_plans
            WHERE plan_id IN ({plan_placeholders})
            """,
            plan_ids,
        ).fetchall()
        for row in plan_rows:
            plan_id = str(row[0] or "").strip()
            limit_minutes_map[plan_id] = None if row[1] is None else float(row[1] or 0.0)

    usage_lookup: Dict[str, Dict[str, Any]] = {}
    for user_id in normalized_user_ids:
        plan_id = resolved_plan_id_map.get(user_id) or "plan_free"
        usage_lookup[user_id] = _build_admin_transcription_usage_display(
            used_minutes_map.get(user_id, 0.0),
            limit_minutes_map.get(plan_id),
        )
    return usage_lookup


def _load_admin_cost_estimates(
    conn,
    user_ids: List[str],
    *,
    created_at_map: Optional[Dict[str, Any]] = None,
) -> Dict[str, Dict[str, Any]]:
    normalized_user_ids = [str(user_id or "").strip() for user_id in user_ids if str(user_id or "").strip()]
    if not normalized_user_ids:
        return {}

    placeholders = ", ".join("?" for _ in normalized_user_ids)
    current_month_start = datetime.now(timezone.utc).date().replace(day=1)
    created_lookup = {str(key): value for key, value in (created_at_map or {}).items()}

    event_age_map: Dict[str, Any] = {}
    video_age_map: Dict[str, Any] = {}
    event_minutes_map: Dict[str, Dict[str, float]] = {}
    usage_minutes_map: Dict[str, float] = {}
    storage_bytes_map: Dict[str, int] = {}
    storage_row_users: set[str] = set()

    event_rows = conn.execute(
        f"""
        SELECT
          CAST(user_id AS VARCHAR) AS user_id,
          SUM(COALESCE(minutes, 0)) AS total_minutes,
          SUM(CASE WHEN period_start >= ? THEN COALESCE(minutes, 0) ELSE 0 END) AS this_month_minutes
        FROM shorts_transcription_usage_events
        WHERE CAST(user_id AS VARCHAR) IN ({placeholders})
        GROUP BY CAST(user_id AS VARCHAR)
        """,
        [current_month_start] + normalized_user_ids,
    ).fetchall()
    for row in event_rows:
        event_minutes_map[str(row[0])] = {
            "total": float(row[1] or 0.0),
            "this_month": float(row[2] or 0.0),
        }

    usage_rows = conn.execute(
        f"""
        SELECT CAST(user_id AS VARCHAR), COALESCE(transcription_minutes_used, 0)
        FROM shorts_user_usage
        WHERE CAST(user_id AS VARCHAR) IN ({placeholders})
        """,
        normalized_user_ids,
    ).fetchall()
    for row in usage_rows:
        usage_minutes_map[str(row[0])] = float(row[1] or 0.0)

    storage_rows = conn.execute(
        f"""
        SELECT
          CAST(user_id AS VARCHAR) AS user_id,
          SUM(COALESCE(size_bytes, 0)) AS total_bytes
        FROM shorts_storage_assets
        WHERE CAST(user_id AS VARCHAR) IN ({placeholders})
          AND COALESCE(status, 'active') = 'active'
        GROUP BY CAST(user_id AS VARCHAR)
        """,
        normalized_user_ids,
    ).fetchall()
    for row in storage_rows:
        storage_row_users.add(str(row[0]))
        storage_bytes_map[str(row[0])] = int(row[1] or 0)

    event_age_rows = conn.execute(
        f"""
        SELECT CAST(user_id AS VARCHAR), MIN(created_at)
        FROM user_events
        WHERE CAST(user_id AS VARCHAR) IN ({placeholders})
        GROUP BY CAST(user_id AS VARCHAR)
        """,
        normalized_user_ids,
    ).fetchall()
    for row in event_age_rows:
        event_age_map[str(row[0])] = row[1]

    video_age_rows = conn.execute(
        f"""
        SELECT CAST(owner_user_id AS VARCHAR), MIN(published_at)
        FROM youtube_videos
        WHERE CAST(owner_user_id AS VARCHAR) IN ({placeholders})
        GROUP BY CAST(owner_user_id AS VARCHAR)
        """,
        normalized_user_ids,
    ).fetchall()
    for row in video_age_rows:
        video_age_map[str(row[0])] = row[1]

    now_utc = datetime.now(timezone.utc)
    cost_map: Dict[str, Dict[str, Any]] = {}
    for user_id in normalized_user_ids:
        created_at = created_lookup.get(user_id)
        age_anchor = created_at or event_age_map.get(user_id) or video_age_map.get(user_id)
        if age_anchor is None:
            age_months = 1.0
        else:
            anchor_value = age_anchor
            if isinstance(anchor_value, datetime) and anchor_value.tzinfo is None:
                anchor_value = anchor_value.replace(tzinfo=timezone.utc)
            elif not isinstance(anchor_value, datetime):
                anchor_value = datetime.combine(anchor_value, datetime.min.time(), tzinfo=timezone.utc)
            age_months = max(1.0, (now_utc - anchor_value).total_seconds() / 2629800.0)

        if user_id in event_minutes_map:
            total_minutes = float(event_minutes_map[user_id]["total"])
            this_month_minutes = float(event_minutes_map[user_id]["this_month"])
            transcription_source = "events"
        elif user_id in usage_minutes_map:
            total_minutes = float(usage_minutes_map[user_id])
            this_month_minutes = 0.0
            transcription_source = "user_usage"
        else:
            total_minutes = 0.0
            this_month_minutes = 0.0
            transcription_source = "missing"

        total_storage_gb = float(storage_bytes_map.get(user_id, 0)) / 1_000_000_000.0
        transcription_total_usd = total_minutes * WHISPER_USD_PER_MINUTE
        storage_now_usd_month = total_storage_gb * S3_USD_PER_GB_MONTH
        this_month_usd = (this_month_minutes * WHISPER_USD_PER_MINUTE) + storage_now_usd_month
        total_usd = transcription_total_usd + storage_now_usd_month
        monthly_avg_usd = total_usd / age_months if age_months else total_usd
        is_young_account = age_months <= ADMIN_COST_APPROX_MONTH_THRESHOLD and total_usd > 0

        notes: List[str] = []
        if transcription_source == "missing":
            notes.append("missing_transcription")
        if user_id not in storage_row_users:
            notes.append("missing_storage")

        cost_map[user_id] = {
            "total_minutes": total_minutes,
            "this_month_minutes": this_month_minutes,
            "total_storage_gb": total_storage_gb,
            "transcription_total_usd": transcription_total_usd,
            "storage_now_usd_month": storage_now_usd_month,
            "this_month_usd": this_month_usd,
            "total_usd": total_usd,
            "monthly_avg_usd": monthly_avg_usd,
            "age_months": age_months,
            "age_anchor": age_anchor,
            "is_young_account": is_young_account,
            "transcription_source": transcription_source,
            "notes": notes,
            "display_total_usd": _format_admin_usd(total_usd),
            "display_this_month_usd": _format_admin_usd(this_month_usd),
            "display_monthly_avg_usd": _format_admin_usd(monthly_avg_usd, approx=is_young_account),
        }
    return cost_map


def _load_storage_plan_admin_users(
    conn,
    search_text: str = "",
    plan_ids: Optional[List[str]] = None,
    limit: int = 50,
    offset: int = 0,
) -> Tuple[List[Dict[str, Any]], int]:
    plan_label_map = {
        "plan_free": "Free",
        "plan_2gb": "Starter",
        "plan_10gb": "Creator",
        "plan_100gb": "Studio",
    }
    normalized_search = (search_text or "").strip().lower()
    normalized_plan_ids = [plan_id for plan_id in (plan_ids or []) if plan_id]
    where_clauses: List[str] = []
    params: List[Any] = []
    if normalized_search:
        where_clauses.append(
            """
            (
                lower(coalesce(u.name, '')) LIKE ?
                OR lower(coalesce(u.email, '')) LIKE ?
                OR lower(coalesce(u.username, '')) LIKE ?
                OR CAST(u.id AS VARCHAR) LIKE ?
            )
            """
        )
        search_value = f"%{normalized_search}%"
        params.extend([search_value, search_value, search_value, search_value])
    if normalized_plan_ids:
        placeholders = ", ".join(["?"] * len(normalized_plan_ids))
        where_clauses.append(f"u.plan_id IN ({placeholders})")
        params.extend(normalized_plan_ids)
    if autopilot_leads_table_ready(conn):
        where_clauses.append(
            """
            NOT EXISTS (
                SELECT 1
                FROM autopilot_leads l
                WHERE CAST(l.user_id AS VARCHAR) = CAST(u.id AS VARCHAR)
                  AND l.converted_at IS NULL
            )
            """
        )

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
    usage_rows = {
        row[0]: row[1]
        for row in conn.execute(
            """
            SELECT CAST(user_id AS VARCHAR), SUM(size_bytes)
            FROM shorts_storage_assets
            WHERE user_id IS NOT NULL AND (status = 'active' OR status IS NULL)
            GROUP BY user_id
            """
        ).fetchall()
    }
    total_count = conn.execute(
        f"""
        SELECT COUNT(*)
        FROM shorts_users u
        {where_sql}
        """,
        params,
    ).fetchone()[0]
    user_rows = conn.execute(
        f"""
        SELECT
          CAST(u.id AS VARCHAR),
          u.name,
          u.email,
          u.plan_id,
          u.custom_limit_bytes,
          p.label,
          p.quota_bytes,
          u.username
        FROM shorts_users u
        LEFT JOIN shorts_storage_plans p ON p.plan_id = u.plan_id
        {where_sql}
        ORDER BY lower(coalesce(u.name, '')), lower(coalesce(u.email, '')), CAST(u.id AS VARCHAR)
        LIMIT ? OFFSET ?
        """,
        params + [limit, offset],
    ).fetchall()
    users: List[Dict[str, Any]] = []
    for row in user_rows:
        limit_bytes = row[4] or row[6] or DEFAULT_USER_STORAGE_LIMIT
        used_bytes = usage_rows.get(row[0], 0)
        percent = int(min(100, (used_bytes / limit_bytes * 100))) if limit_bytes else 0
        users.append(
            {
                "id": row[0],
                "name": row[1],
                "email": row[2],
                "plan_id": row[3],
                "plan_label": plan_label_map.get(row[3], row[5]) or "No plan",
                "limit_label": _format_size_bytes(limit_bytes),
                "limit_bytes": limit_bytes,
                "used_label": _format_size_bytes(used_bytes),
                "used_bytes": used_bytes,
                "percent": percent,
                "custom_limit_gb": (row[4] / (1024 ** 3)) if row[4] else "",
                "username": row[7],
            }
        )
    return users, int(total_count or 0)


def _load_admin_auth_users(
    conn,
    search_text: str = "",
    limit: int = 50,
    offset: int = 0,
) -> Tuple[List[Dict[str, Any]], int]:
    normalized_search = (search_text or "").strip().lower()
    where_clauses: List[str] = []
    params: List[Any] = []

    if normalized_search:
        where_clauses.append(
            """
            (
                lower(coalesce(u.email, '')) LIKE ?
                OR lower(coalesce(u.name, '')) LIKE ?
                OR lower(coalesce(u.username, '')) LIKE ?
                OR CAST(u.id AS VARCHAR) LIKE ?
            )
            """
        )
        search_value = f"%{normalized_search}%"
        params.extend([search_value, search_value, search_value, search_value])
    if autopilot_leads_table_ready(conn):
        where_clauses.append(
            """
            NOT EXISTS (
                SELECT 1
                FROM autopilot_leads l
                WHERE CAST(l.user_id AS VARCHAR) = CAST(u.id AS VARCHAR)
                  AND l.converted_at IS NULL
            )
            """
        )

    youtube_columns = table_columns(conn, "youtube_videos")
    generated_columns = table_columns(conn, "shorts_generated_videos")

    youtube_activity_fields = [
        column
        for column in ("updated_at", "published_at", "downloaded_at", "last_checked_at")
        if column in youtube_columns
    ]
    youtube_activity_expr = (
        f"MAX(COALESCE({', '.join(youtube_activity_fields)})) AS last_video_activity"
        if youtube_activity_fields
        else "NULL AS last_video_activity"
    )
    youtube_join = ""
    if youtube_columns and "owner_user_id" in youtube_columns:
        youtube_join = f"""
        LEFT JOIN (
            SELECT
                CAST(owner_user_id AS VARCHAR) AS owner_user_id,
                COUNT(*) AS uploaded_videos,
                {youtube_activity_expr}
            FROM youtube_videos
            GROUP BY CAST(owner_user_id AS VARCHAR)
        ) yv ON yv.owner_user_id = CAST(u.id AS VARCHAR)
        """

    generated_activity_fields = [
        column
        for column in (
            "updated_at",
            "created_at",
            "published_at",
            "planned_publish_at",
            "youtube_published_at",
            "instagram_published_at",
            "facebook_published_at",
            "tiktok_published_at",
        )
        if column in generated_columns
    ]
    generated_activity_expr = (
        f"MAX(COALESCE({', '.join(generated_activity_fields)})) AS last_short_activity"
        if generated_activity_fields
        else "NULL AS last_short_activity"
    )
    published_short_expr = (
        "SUM(CASE WHEN lower(coalesce(publish_status, '')) = 'published' THEN 1 ELSE 0 END) AS shorts_published"
        if "publish_status" in generated_columns
        else "0 AS shorts_published"
    )
    scheduled_short_expr = (
        "SUM(CASE WHEN lower(coalesce(publish_status, '')) = 'scheduled' THEN 1 ELSE 0 END) AS scheduled_count"
        if "publish_status" in generated_columns
        else "0 AS scheduled_count"
    )
    failed_short_expr = (
        "SUM(CASE WHEN lower(coalesce(publish_status, '')) = 'failed' THEN 1 ELSE 0 END) AS failed_count"
        if "publish_status" in generated_columns
        else "0 AS failed_count"
    )
    overdue_short_expr = (
        """
        SUM(
            CASE
                WHEN lower(coalesce(publish_status, '')) <> 'published'
                 AND planned_publish_at IS NOT NULL
                 AND planned_publish_at <= now()
                THEN 1
                ELSE 0
            END
        ) AS overdue_count
        """
        if "publish_status" in generated_columns and "planned_publish_at" in generated_columns
        else "0 AS overdue_count"
    )
    generated_join = ""
    if generated_columns and "user_id" in generated_columns:
        generated_join = f"""
        LEFT JOIN (
            SELECT
                CAST(user_id AS VARCHAR) AS user_id,
                COUNT(*) AS shorts_generated,
                {published_short_expr},
                {scheduled_short_expr},
                {failed_short_expr},
                {overdue_short_expr},
                {generated_activity_expr}
            FROM shorts_generated_videos
            GROUP BY CAST(user_id AS VARCHAR)
        ) gv ON gv.user_id = CAST(u.id AS VARCHAR)
        """

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
    total_count = conn.execute(
        f"""
        SELECT COUNT(*)
        FROM shorts_users u
        {where_sql}
        """,
        params,
    ).fetchone()[0]
    rows = conn.execute(
        f"""
        SELECT
          CAST(u.id AS VARCHAR),
          u.name,
          u.email,
          u.username,
          u.plan_id,
          u.subscription_status,
          u.created_at,
          COALESCE(yv.uploaded_videos, 0),
          COALESCE(gv.shorts_generated, 0),
          COALESCE(gv.shorts_published, 0),
          COALESCE(gv.scheduled_count, 0),
          COALESCE(gv.failed_count, 0),
          COALESCE(gv.overdue_count, 0),
          yv.last_video_activity,
          gv.last_short_activity,
          COALESCE(u.service_mode, '')
        FROM shorts_users u
        {youtube_join}
        {generated_join}
        {where_sql}
        ORDER BY coalesce(u.created_at, u.updated_at) DESC, CAST(u.id AS VARCHAR) DESC
        LIMIT ? OFFSET ?
        """,
        params + [limit, offset],
    ).fetchall()
    cost_lookup: Dict[str, Dict[str, Any]] = {}
    transcription_usage_lookup: Dict[str, Dict[str, Any]] = {}
    try:
        created_at_map = {str(row[0] or ""): row[6] for row in rows}
        cost_lookup = _load_admin_cost_estimates(
            conn,
            [str(row[0] or "") for row in rows],
            created_at_map=created_at_map,
        )
    except Exception as exc:
        current_app.logger.exception("Admin cost list unavailable: %s", exc)
        cost_lookup = {}
    try:
        transcription_usage_lookup = _load_admin_transcription_usage(
            conn,
            [str(row[0] or "") for row in rows],
            plan_id_map={str(row[0] or ""): str(row[4] or "plan_free") for row in rows},
        )
    except Exception as exc:
        current_app.logger.exception("Admin transcription usage list unavailable: %s", exc)
        transcription_usage_lookup = {}
    users: List[Dict[str, Any]] = []
    for row in rows:
        user_id = str(row[0] or "")
        uploaded_videos = int(row[7] or 0)
        shorts_generated = int(row[8] or 0)
        shorts_published = int(row[9] or 0)
        scheduled_count = int(row[10] or 0)
        failed_count = int(row[11] or 0)
        overdue_count = int(row[12] or 0)
        last_activity = row[13]
        if row[14] and (last_activity is None or row[14] > last_activity):
            last_activity = row[14]
        if uploaded_videos == 0:
            stage = "no_upload"
        elif shorts_generated == 0:
            stage = "no_clips"
        elif shorts_published > 0:
            stage = "publishing"
        elif failed_count > 0:
            stage = "publish_failing"
        elif overdue_count > 0:
            stage = "overdue"
        elif scheduled_count > 0:
            stage = "scheduled"
        else:
            stage = "not_published"
        is_autopilot = str(row[15] or "").strip().lower() == "autopilot"
        users.append(
            {
                "id": user_id,
                "name": row[1] or row[3] or "Unnamed user",
                "email": row[2] or "",
                "username": row[3] or "",
                "plan_id": row[4] or "plan_free",
                "subscription_status": row[5] or "—",
                "created_at": row[6],
                "uploaded_videos": uploaded_videos,
                "shorts_generated": shorts_generated,
                "shorts_published": shorts_published,
                "scheduled_count": scheduled_count,
                "failed_count": failed_count,
                "overdue_count": overdue_count,
                "stage": stage,
                "service_mode": "autopilot" if is_autopilot else "self",
                "service_mode_label": "Autopilot" if is_autopilot else "Self-serve",
                "last_activity_at": last_activity,
                "cost_summary": cost_lookup.get(user_id) or {
                    "display_monthly_avg_usd": "—",
                },
                "transcription_usage": transcription_usage_lookup.get(user_id) or {"display": "—"},
            }
        )
    return users, int(total_count or 0)


def _load_admin_error_events(
    conn,
    *,
    event_name: str = "",
    status_query: str = "",
    email_query: str = "",
    time_range: str = "24h",
    include_noise: bool = False,
    limit: int = 200,
    offset: int = 0,
) -> Tuple[List[Dict[str, Any]], int]:
    normalized_event = (event_name or "").strip().lower()
    normalized_status = (status_query or "").strip().lower()
    normalized_email = (email_query or "").strip().lower()
    normalized_range = (time_range or "24h").strip().lower()

    allowed_ranges = {
        "24h": "now() - interval '24 hours'",
        "7d": "now() - interval '7 days'",
    }

    where_clauses = [
        "ue.event_name IN ('server_error', 'client_error')",
    ]
    params: List[Any] = []

    if normalized_event in {"server_error", "client_error"}:
        where_clauses.append("ue.event_name = ?")
        params.append(normalized_event)

    if normalized_status:
        where_clauses.append("lower(coalesce(ue.status, '')) LIKE ?")
        params.append(f"%{normalized_status}%")

    if normalized_email:
        where_clauses.append(
            """
            (
                lower(coalesce(su.email, '')) LIKE ?
                OR lower(coalesce(ue.user_id, '')) LIKE ?
            )
            """
        )
        like_value = f"%{normalized_email}%"
        params.extend([like_value, like_value])

    if normalized_range in allowed_ranges:
        where_clauses.append(f"ue.created_at >= {allowed_ranges[normalized_range]}")

    if not include_noise:
        if getattr(conn, "backend_name", "") == "postgres":
            path_expr = "lower(coalesce(ue.metadata->>'path', ue.metadata->>'source', ''))"
        else:
            path_expr = "lower(coalesce(json_extract_string(ue.metadata, '$.path'), json_extract_string(ue.metadata, '$.source'), ''))"
        like_clauses = " OR ".join(f"{path_expr} LIKE ?" for _ in NOISE_404_PATH_LIKE_PATTERNS)
        where_clauses.append(
            f"""
            NOT (
                lower(coalesce(ue.status, '')) = '404'
                AND ({like_clauses})
            )
            """
        )
        params.extend(pattern.lower() for pattern in NOISE_404_PATH_LIKE_PATTERNS)

    where_sql = " AND ".join(where_clauses)
    base_from_sql = f"""
        FROM user_events ue
        LEFT JOIN shorts_users su
          ON CAST(su.id AS VARCHAR) = ue.user_id
        WHERE {where_sql}
    """

    total_count = int(
        conn.execute(
            f"SELECT COUNT(*) {base_from_sql}",
            params,
        ).fetchone()[0]
        or 0
    )
    rows = conn.execute(
        f"""
        SELECT
          ue.created_at,
          ue.user_id,
          coalesce(su.email, ''),
          ue.event_name,
          coalesce(ue.status, ''),
          ue.metadata
        {base_from_sql}
        ORDER BY ue.created_at DESC
        LIMIT ? OFFSET ?
        """,
        params + [limit, offset],
    ).fetchall()

    events: List[Dict[str, Any]] = []
    for row in rows:
        metadata_value = row[5]
        if isinstance(metadata_value, str):
            try:
                metadata_value = json.loads(metadata_value)
            except Exception:
                metadata_value = {}
        if not isinstance(metadata_value, dict):
            metadata_value = {}
        short_message = (
            str(metadata_value.get("exception_message") or "").strip()
            or str(metadata_value.get("message") or "").strip()
        )
        path_value = (
            str(metadata_value.get("path") or "").strip()
            or str(metadata_value.get("source") or "").strip()
        )
        resolved_email = str(row[2] or "").strip()
        user_id_value = str(row[1] or "").strip() or "anonymous"
        if user_id_value == "anonymous":
            user_label = "anonymous"
        else:
            user_label = resolved_email or user_id_value
        events.append(
            {
                "created_at": row[0],
                "user_id": user_id_value,
                "user_label": user_label,
                "event_name": str(row[3] or "").strip(),
                "status": str(row[4] or "").strip(),
                "short_message": short_message,
                "path": path_value,
                "metadata": metadata_value,
            }
        )
    return events, total_count


def _share_link_event_join_sql(conn) -> str:
    event_names_sql = "('share_view', 'share_play', 'share_cta_click', 'share_watch_progress')"
    if getattr(conn, "backend_name", "") == "postgres":
        return """
        ue.event_name IN {event_names_sql}
        AND (
          (
            COALESCE(NULLIF(ue.metadata->>'share_link_id', ''), '') <> ''
            AND ue.metadata->>'share_link_id' = CAST(sl.id AS VARCHAR)
          )
          OR (
            COALESCE(NULLIF(ue.metadata->>'share_link_id', ''), '') = ''
            AND ue.metadata->>'token' = sl.token
          )
        )
        """.format(event_names_sql=event_names_sql)
    return """
    ue.event_name IN {event_names_sql}
    AND (
      CAST(ue.metadata AS VARCHAR) LIKE ('%"share_link_id": "' || CAST(sl.id AS VARCHAR) || '"%')
      OR (
        CAST(ue.metadata AS VARCHAR) NOT LIKE '%"share_link_id": "%'
        AND CAST(ue.metadata AS VARCHAR) LIKE ('%"token": "' || sl.token || '"%')
      )
    )
    """.format(event_names_sql=event_names_sql)


def _user_event_metadata_text_sql(conn, field_name: str) -> str:
    safe_field = re.sub(r"[^a-zA-Z0-9_]", "", str(field_name or ""))
    if getattr(conn, "backend_name", "") == "postgres":
        return f"NULLIF(ue.metadata->>'{safe_field}', '')"
    return f"NULLIF(json_extract_string(ue.metadata, '$.{safe_field}'), '')"


def _user_event_metadata_numeric_sql(conn, field_name: str) -> str:
    text_expr = _user_event_metadata_text_sql(conn, field_name)
    if getattr(conn, "backend_name", "") == "postgres":
        return f"CAST({text_expr} AS DOUBLE PRECISION)"
    return f"CAST({text_expr} AS DOUBLE)"


def _normalize_share_watch_device_type(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"mobile", "desktop", "tablet"}:
        return normalized
    return ""


def _normalize_share_watch_cta(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"autopilot", "self_serve"}:
        return normalized
    return ""


def _normalize_share_watch_seconds(value: Any) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numeric):
        return None
    return round(max(0.0, numeric), 3)


def _normalize_share_watch_percent(value: Any) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numeric):
        return None
    return round(min(100.0, max(0.0, numeric)), 2)


def _load_admin_share_links(
    conn,
    *,
    email_query: str = "",
    engagement_filter: str = "all",
    archive_filter: str = "all",
    sort_key: str = "created",
    sort_dir: str = "desc",
    limit: int = 200,
    offset: int = 0,
) -> Tuple[List[Dict[str, Any]], int, str, str, str]:
    share_link_columns = table_columns(conn, "short_share_links")
    onboarding_magic_link_columns = table_columns(conn, "onboarding_magic_links")
    has_emailed_at = "emailed_at" in share_link_columns
    has_archived = "archived" in share_link_columns
    has_language = "language" in share_link_columns
    has_trial_days = "trial_days" in share_link_columns
    has_followup_sent = "followup_sent" in share_link_columns
    has_followup_sent_at = "followup_sent_at" in share_link_columns
    has_magic_link_user_id = "user_id" in onboarding_magic_link_columns
    normalized_email = (email_query or "").strip().lower()
    normalized_filter = (engagement_filter or "all").strip().lower()
    normalized_archive_filter = (archive_filter or "all").strip().lower()
    normalized_sort_key = (sort_key or "created").strip().lower()
    normalized_sort_dir = (sort_dir or "desc").strip().lower()
    if normalized_filter not in {"all", "viewed", "played"}:
        normalized_filter = "all"
    if normalized_archive_filter not in {"all", "not_archived", "archived"}:
        normalized_archive_filter = "all"
    if normalized_sort_key not in {"first_seen", "last_seen", "created"}:
        normalized_sort_key = "created"
    if normalized_sort_dir not in {"asc", "desc"}:
        normalized_sort_dir = "desc"

    sort_column_map = {
        "first_seen": "sr.first_seen",
        "last_seen": "sr.last_seen",
        "created": "sr.created_at",
    }
    order_column = sort_column_map[normalized_sort_key]
    order_direction = "ASC" if normalized_sort_dir == "asc" else "DESC"

    where_clauses = ["1=1"]
    params: List[Any] = []
    if normalized_email:
        where_clauses.append("lower(coalesce(sl.recipient_email, '')) LIKE ?")
        params.append(f"%{normalized_email}%")
    if has_archived:
        if normalized_archive_filter == "not_archived":
            where_clauses.append("coalesce(sl.archived, false) = false")
        elif normalized_archive_filter == "archived":
            where_clauses.append("coalesce(sl.archived, false) = true")
    where_sql = " AND ".join(where_clauses)
    percent_watched_expr = _user_event_metadata_numeric_sql(conn, "percent_watched")
    cta_expr = _user_event_metadata_text_sql(conn, "cta")
    device_expr = _user_event_metadata_text_sql(conn, "device_type")
    if getattr(conn, "backend_name", "") == "postgres":
        channel_connected_agg_sql = "string_agg(DISTINCT ue.platform, ', ' ORDER BY ue.platform)"
    else:
        channel_connected_agg_sql = "group_concat(DISTINCT ue.platform)"
    redeemed_magic_cte_sql = ""
    if onboarding_magic_link_columns:
        redeemed_user_sql = "NULL"
        if has_magic_link_user_id:
            redeemed_user_sql = "NULLIF(CAST(oml.user_id AS VARCHAR), '')"
        redeemed_magic_cte_sql = f"""
        ,
        redeemed_magic_links AS (
            SELECT
              sl.id AS share_link_id,
              oml.used_at AS redeemed_at,
              {redeemed_user_sql} AS redeemed_user_id,
              ROW_NUMBER() OVER (
                PARTITION BY sl.id
                ORDER BY oml.used_at DESC, oml.id DESC
              ) AS rn
            FROM short_share_links sl
            JOIN onboarding_magic_links oml
              ON (
                (
                  oml.share_link_id IS NOT NULL
                  AND CAST(oml.share_link_id AS BIGINT) = CAST(sl.id AS BIGINT)
                )
                OR (
                  COALESCE(oml.share_link_token, '') <> ''
                  AND oml.share_link_token = sl.token
                )
              )
            WHERE oml.used_at IS NOT NULL
        ),
        latest_redeemed_magic_link AS (
            SELECT
              share_link_id,
              redeemed_at,
              redeemed_user_id
            FROM redeemed_magic_links
            WHERE rn = 1
        ),
        latest_share_cta AS (
            SELECT
              share_link_id,
              cta_clicked,
              device_type
            FROM (
              SELECT
                sl.id AS share_link_id,
                {cta_expr} AS cta_clicked,
                {device_expr} AS device_type,
                ROW_NUMBER() OVER (
                  PARTITION BY sl.id
                  ORDER BY ue.created_at DESC, ue.id DESC
                ) AS rn
              FROM short_share_links sl
              JOIN user_events ue
                ON {_share_link_event_join_sql(conn)}
              WHERE ue.event_name = 'share_cta_click'
                AND {cta_expr} IS NOT NULL
            ) ranked_share_cta
            WHERE rn = 1
        ),
        latest_share_device AS (
            SELECT
              share_link_id,
              device_type
            FROM (
              SELECT
                sl.id AS share_link_id,
                {device_expr} AS device_type,
                ROW_NUMBER() OVER (
                  PARTITION BY sl.id
                  ORDER BY ue.created_at DESC, ue.id DESC
                ) AS rn
              FROM short_share_links sl
              JOIN user_events ue
                ON {_share_link_event_join_sql(conn)}
              WHERE ue.event_name IN ('share_view', 'share_play', 'share_cta_click', 'share_watch_progress')
                AND {device_expr} IS NOT NULL
            ) ranked_share_device
            WHERE rn = 1
        ),
        redeemed_channel_connections AS (
            SELECT
              sl.id AS share_link_id,
              {channel_connected_agg_sql} AS connected_platforms,
              MAX(ue.created_at) AS connected_at
            FROM short_share_links sl
            JOIN latest_redeemed_magic_link lr
              ON lr.share_link_id = sl.id
            JOIN user_events ue
              ON ue.user_id = lr.redeemed_user_id
             AND ue.event_name = 'channel_connected'
             AND (lr.redeemed_at IS NULL OR ue.created_at >= lr.redeemed_at)
            GROUP BY sl.id
        )
        """
    else:
        redeemed_magic_cte_sql = f"""
        ,
        latest_share_cta AS (
            SELECT
              share_link_id,
              cta_clicked,
              device_type
            FROM (
              SELECT
                sl.id AS share_link_id,
                {cta_expr} AS cta_clicked,
                {device_expr} AS device_type,
                ROW_NUMBER() OVER (
                  PARTITION BY sl.id
                  ORDER BY ue.created_at DESC, ue.id DESC
                ) AS rn
              FROM short_share_links sl
              JOIN user_events ue
                ON {_share_link_event_join_sql(conn)}
              WHERE ue.event_name = 'share_cta_click'
                AND {cta_expr} IS NOT NULL
            ) ranked_share_cta
            WHERE rn = 1
        ),
        latest_share_device AS (
            SELECT
              share_link_id,
              device_type
            FROM (
              SELECT
                sl.id AS share_link_id,
                {device_expr} AS device_type,
                ROW_NUMBER() OVER (
                  PARTITION BY sl.id
                  ORDER BY ue.created_at DESC, ue.id DESC
                ) AS rn
              FROM short_share_links sl
              JOIN user_events ue
                ON {_share_link_event_join_sql(conn)}
              WHERE ue.event_name IN ('share_view', 'share_play', 'share_cta_click', 'share_watch_progress')
                AND {device_expr} IS NOT NULL
            ) ranked_share_device
            WHERE rn = 1
        )
        """

    base_cte_sql = f"""
        WITH share_rows AS (
            SELECT
              sl.id,
              sl.generated_video_id,
              sl.token,
              {("sl.language" if has_language else "'EN'")} AS language,
              {("COALESCE(sl.trial_days, 90)" if has_trial_days else "90")} AS trial_days,
              COALESCE(NULLIF(sl.recipient_name, ''), '—') AS recipient_name,
              COALESCE(NULLIF(sl.recipient_email, ''), '—') AS recipient_email,
              {("sl.emailed_at" if has_emailed_at else "NULL")} AS emailed_at,
              {("coalesce(sl.archived, false)" if has_archived else "FALSE")} AS archived,
              {("sl.followup_sent" if has_followup_sent else "FALSE")} AS followup_sent,
              {("sl.followup_sent_at" if has_followup_sent_at else "NULL")} AS followup_sent_at,
              sl.created_at,
              COALESCE(NULLIF(gv.generated_title, ''), NULLIF(gv.clip_filename, ''), 'Untitled short') AS clip_title,
              SUM(CASE WHEN ue.event_name = 'share_view' THEN 1 ELSE 0 END) AS views_count,
              SUM(CASE WHEN ue.event_name = 'share_play' THEN 1 ELSE 0 END) AS plays_count,
              MAX(CASE WHEN ue.event_name = 'share_watch_progress' THEN {percent_watched_expr} END) AS max_percent_watched,
              MIN(CASE WHEN ue.event_name = 'share_view' THEN ue.created_at END) AS first_seen,
              MAX(CASE WHEN ue.event_name IN ('share_view', 'share_play') THEN ue.created_at END) AS last_seen
            FROM short_share_links sl
            LEFT JOIN shorts_generated_videos gv
              ON CAST(gv.id AS BIGINT) = CAST(sl.generated_video_id AS BIGINT)
            LEFT JOIN user_events ue
              ON {_share_link_event_join_sql(conn)}
            WHERE {where_sql}
            GROUP BY
              sl.id,
              sl.generated_video_id,
              sl.token,
              {("sl.language," if has_language else "")}
              {("sl.trial_days," if has_trial_days else "")}
              sl.recipient_name,
              sl.recipient_email,
              {("sl.emailed_at," if has_emailed_at else "")}
              {("sl.archived," if has_archived else "")}
              {("sl.followup_sent," if has_followup_sent else "")}
              {("sl.followup_sent_at," if has_followup_sent_at else "")}
              sl.created_at,
              gv.generated_title,
              gv.clip_filename
        )
        {redeemed_magic_cte_sql}
    """

    having_clauses = ["1=1"]
    if normalized_filter == "viewed":
        having_clauses.append("coalesce(views_count, 0) > 0")
    elif normalized_filter == "played":
        having_clauses.append("coalesce(plays_count, 0) > 0")
    having_sql = " AND ".join(having_clauses)

    total_count = int(
        conn.execute(
            f"""
            {base_cte_sql}
            SELECT COUNT(*)
            FROM share_rows
            WHERE {having_sql}
            """,
            params,
        ).fetchone()[0]
        or 0
    )

    rows = conn.execute(
        f"""
        {base_cte_sql}
        SELECT
          sr.id,
          sr.generated_video_id,
          sr.token,
          sr.language,
          sr.trial_days,
          sr.recipient_name,
          sr.recipient_email,
          sr.emailed_at,
          sr.archived,
          sr.followup_sent,
          sr.followup_sent_at,
          sr.clip_title,
          COALESCE(sr.views_count, 0) AS views_count,
          COALESCE(sr.plays_count, 0) AS plays_count,
          sr.first_seen,
          sr.last_seen,
          sr.created_at,
          lsc.cta_clicked,
          COALESCE(lsd.device_type, lsc.device_type) AS device_type,
          sr.max_percent_watched,
          {("lr.redeemed_at," if onboarding_magic_link_columns else "NULL AS redeemed_at,")}
          {("lr.redeemed_user_id," if onboarding_magic_link_columns else "NULL AS redeemed_user_id,")}
          {("rcc.connected_platforms," if onboarding_magic_link_columns else "NULL AS connected_platforms,")}
          {("rcc.connected_at" if onboarding_magic_link_columns else "NULL AS connected_at")}
        FROM share_rows sr
        LEFT JOIN latest_share_cta lsc
          ON lsc.share_link_id = sr.id
        LEFT JOIN latest_share_device lsd
          ON lsd.share_link_id = sr.id
        {("LEFT JOIN latest_redeemed_magic_link lr ON lr.share_link_id = sr.id" if onboarding_magic_link_columns else "")}
        {("LEFT JOIN redeemed_channel_connections rcc ON rcc.share_link_id = sr.id" if onboarding_magic_link_columns else "")}
        WHERE {having_sql}
        ORDER BY {order_column} {order_direction} NULLS LAST, sr.id DESC
        LIMIT ? OFFSET ?
        """,
        params + [limit, offset],
    ).fetchall()

    items: List[Dict[str, Any]] = []
    for row in rows:
        views_count = int(row[12] or 0)
        plays_count = int(row[13] or 0)
        emailed_at = row[7]
        archived = bool(row[8])
        followup_sent = bool(row[9])
        followup_sent_at = row[10]
        trial_days = normalize_trial_days(row[4], default=LEGACY_SHARE_TRIAL_DAYS)
        language = normalize_outreach_language(row[3], default="EN")
        followup_eligible = False
        if emailed_at and not followup_sent and (views_count > 0 or plays_count > 0):
            try:
                emailed_at_utc = emailed_at if emailed_at.tzinfo else emailed_at.replace(tzinfo=timezone.utc)
            except AttributeError:
                emailed_at_utc = None
            if emailed_at_utc is not None:
                followup_eligible = emailed_at_utc <= (datetime.now(timezone.utc) - timedelta(days=3))
        items.append(
            {
                "id": row[0],
                "generated_video_id": row[1],
                "share_token": str(row[2] or "").strip(),
                "share_url": _share_public_url(str(row[2] or "").strip()) if str(row[2] or "").strip() else "",
                "preview_url": _share_preview_url(str(row[2] or "").strip()) if str(row[2] or "").strip() else "",
                "language": language,
                "trial_days": trial_days,
                "trial_duration_label": _trial_duration_label(trial_days, language),
                "recipient_name": row[5],
                "recipient_email": row[6],
                "emailed_at": emailed_at,
                "emailed_at_pst": _format_datetime_pst(emailed_at),
                "emailed": bool(emailed_at),
                "archived": archived,
                "followup_sent": followup_sent,
                "followup_sent_at": followup_sent_at,
                "followup_sent_at_pst": _format_datetime_pst(followup_sent_at),
                "followup_eligible": followup_eligible,
                "clip_title": row[11],
                "views_count": views_count,
                "plays_count": plays_count,
                "viewed": views_count > 0,
                "played": plays_count > 0,
                "first_seen": row[14],
                "first_seen_pst": _format_datetime_pst(row[14]),
                "last_seen": row[15],
                "last_seen_pst": _format_datetime_pst(row[15]),
                "created_at": row[16],
                "created_at_pst": _format_datetime_pst(row[16]),
                "cta_clicked": str(row[17] or "").strip(),
                "device_type": str(row[18] or "").strip(),
                "max_percent_watched": round(float(row[19] or 0), 2) if row[19] is not None else 0.0,
                "redeemed_at": row[20],
                "redeemed_at_pst": _format_datetime_pst(row[20]),
                "redeemed_user_id": str(row[21] or "").strip(),
                "redeemed": bool(row[20]),
                "connected_platforms": str(row[22] or "").strip(),
                "channel_connected": bool(str(row[22] or "").strip()),
                "connected_at": row[23],
                "connected_at_pst": _format_datetime_pst(row[23]),
                "redeemed_user_detail_url": (
                    url_for("video_shorts_bp.admin_user_detail", user_id=str(row[21]).strip())
                    if str(row[21] or "").strip()
                    else ""
                ),
            }
        )
    return (
        items,
        total_count,
        normalized_filter,
        normalized_archive_filter,
        normalized_sort_key,
        normalized_sort_dir,
    )


def _load_admin_autopilot_leads(
    conn,
    *,
    email_query: str = "",
    limit: int = 200,
    offset: int = 0,
) -> Tuple[List[Dict[str, Any]], int]:
    ensure_pricing_interest_schema(conn)
    normalized_email = (email_query or "").strip().lower()
    where_sql = ""
    params: List[Any] = []
    if normalized_email:
        where_sql = "WHERE lower(email) LIKE ?"
        params.append(f"%{normalized_email}%")
    total_row = conn.execute(
        f"""
        SELECT COUNT(*)
        FROM pricing_autopilot_leads
        {where_sql}
        """,
        params,
    ).fetchone()
    total_count = int((total_row[0] if total_row else 0) or 0)
    rows = conn.execute(
        f"""
        SELECT
            id,
            email,
            monthly_shorts,
            monthly_price,
            compare_price,
            source,
            created_at
        FROM pricing_autopilot_leads
        {where_sql}
        ORDER BY created_at DESC, id DESC
        LIMIT ? OFFSET ?
        """,
        [*params, limit, offset],
    ).fetchall()
    items: List[Dict[str, Any]] = []
    for row in rows:
        items.append(
            {
                "id": row[0],
                "email": str(row[1] or "").strip(),
                "monthly_shorts": int(row[2] or 0),
                "monthly_price": int(row[3] or 0),
                "compare_price": int(row[4] or 0),
                "source": str(row[5] or "").strip(),
                "created_at": row[6],
                "created_at_pst": _format_datetime_pst(row[6]),
            }
        )
    return items, total_count


def _load_admin_lead_records(conn, *, limit: int = 200) -> List[Dict[str, Any]]:
    """Load pre-conversion outreach leads without mixing them into customer operations."""
    if not autopilot_leads_table_ready(conn):
        return []

    rows = conn.execute(
        """
        SELECT
            l.id,
            l.creator_name,
            l.creator_email,
            l.youtube_channel_id,
            l.user_id,
            l.brand_id,
            l.first_video_id,
            l.created_at,
            l.converted_at,
            c.channel_name,
            v.title,
            v.video_id,
            v.thumbnail_url,
            v.download_status,
            COALESCE(
                (
                    SELECT COUNT(*)
                    FROM shorts_generated_videos gv
                    WHERE CAST(gv.source_video_id AS VARCHAR) = CAST(v.video_id AS VARCHAR)
                      AND CAST(gv.brand_id AS VARCHAR) = CAST(v.brand_id AS VARCHAR)
                ),
                0
            ) AS generated_short_count,
            COALESCE(u.service_mode, '') AS service_mode
        FROM autopilot_leads l
        LEFT JOIN youtube_channels c ON c.channel_id = l.channel_id
        LEFT JOIN youtube_videos v ON v.id = l.first_video_id
        LEFT JOIN shorts_users u ON CAST(u.id AS VARCHAR) = CAST(l.user_id AS VARCHAR)
        ORDER BY l.created_at DESC, l.id DESC
        LIMIT ?
        """,
        [limit],
    ).fetchall()

    items: List[Dict[str, Any]] = []
    for row in rows:
        has_owner = bool(str(row[4] or "").strip() and str(row[5] or "").strip())
        generated_count = int(row[14] or 0)
        items.append(
            {
                "id": str(row[0] or ""),
                "creator_name": str(row[1] or "").strip() or "Unknown creator",
                "creator_email": str(row[2] or "").strip(),
                "youtube_channel_id": str(row[3] or "").strip(),
                "owner_user_id": str(row[4] or "").strip(),
                "brand_id": str(row[5] or "").strip(),
                "first_video_id": int(row[6]) if row[6] is not None else None,
                "has_owner": has_owner,
                "lead_type_label": "Real lead" if has_owner else "Discovery",
                "created_at_pst": _format_datetime_pst(row[7]),
                "converted_at_pst": _format_datetime_pst(row[8]),
                "channel_name": str(row[9] or "").strip() or "YouTube channel",
                "video_title": str(row[10] or "").strip() or "Source video unavailable",
                "youtube_video_id": str(row[11] or "").strip(),
                "thumbnail_url": str(row[12] or "").strip(),
                "download_status": str(row[13] or "").strip().lower() or "pending",
                "generated_short_count": generated_count,
                "generation_label": f"{generated_count} short{'s' if generated_count != 1 else ''} generated",
                "converted": bool(row[8]) or str(row[15] or "").strip().lower() == "autopilot",
            }
        )
    return items


def _provision_autopilot_placeholder(
    conn,
    *,
    recipient_email: str,
    channel_name: str,
) -> Dict[str, str]:
    """Create an inactive owner and its first brand for a future autopilot customer."""
    email = str(recipient_email or "").strip().lower()
    brand_name = str(channel_name or "").strip()
    if not email or "@" not in email or email.startswith("@") or email.endswith("@"):
        raise ValueError("Enter a valid email address.")
    if not brand_name:
        raise ValueError("Enter the YouTube channel name.")

    ensure_storage_user_schema(conn)
    ensure_auth_user_schema(conn)
    ensure_brand_schema(conn)
    existing = conn.execute(
        """
        SELECT CAST(id AS VARCHAR)
        FROM shorts_users
        WHERE lower(email) = lower(?) OR lower(username) = lower(?)
        LIMIT 1
        """,
        [email, email],
    ).fetchone()
    if existing:
        # Never repurpose a real account or add a customer brand to it implicitly.
        raise ValueError("An account already exists for this email. No changes were made.")

    user_id = str(uuid4())
    try:
        conn.execute(
            """
            INSERT INTO shorts_users (
                id, username, password_hash, name, email, role, plan_id,
                email_verified, pending_service_intent, pending_service_tier,
                created_at, updated_at
            )
            VALUES (?, ?, NULL, ?, ?, 'member', ?, FALSE, 'autopilot', 15, now(), now())
            """,
            [user_id, email, brand_name, email, DEFAULT_USER_PLAN_ID],
        )
        brand = create_brand_record(
            conn,
            user_id=user_id,
            name=brand_name,
            make_default=True,
            commit=False,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"user_id": user_id, "brand_id": str(brand["id"]), "brand_name": brand_name, "email": email}


def _load_admin_autopilot_customers(
    conn,
    *,
    email_query: str = "",
    limit: int = 200,
    offset: int = 0,
) -> Tuple[List[Dict[str, Any]], int]:
    ensure_storage_user_schema(conn)
    ensure_auth_user_schema(conn)
    has_lead_records = autopilot_leads_table_ready(conn)
    normalized_email = (email_query or "").strip().lower()
    where_parts = ["lower(coalesce(u.service_mode, '')) = 'autopilot'"]
    params: List[Any] = []
    if normalized_email:
        where_parts.append("lower(coalesce(u.email, u.username, '')) LIKE ?")
        params.append(f"%{normalized_email}%")
    where_sql = " AND ".join(where_parts)
    total_row = conn.execute(
        f"""
        SELECT COUNT(*)
        FROM shorts_users u
        WHERE {where_sql}
        """,
        params,
    ).fetchone()
    total_count = int((total_row[0] if total_row else 0) or 0)
    customer_scope_join = ""
    customer_scope_fields = "NULL AS customer_brand_id, NULL AS customer_brand_name, NULL AS converted_lead_id, NULL AS creator_name"
    if has_lead_records:
        customer_scope_join = """
        LEFT JOIN autopilot_leads l
          ON l.id = (
              SELECT l2.id
              FROM autopilot_leads l2
              WHERE CAST(l2.user_id AS VARCHAR) = CAST(u.id AS VARCHAR)
                AND l2.converted_at IS NOT NULL
              ORDER BY l2.converted_at DESC NULLS LAST, l2.id DESC
              LIMIT 1
          )
        LEFT JOIN shorts_brands b
          ON CAST(b.id AS VARCHAR) = CAST(l.brand_id AS VARCHAR)
         AND CAST(b.owner_user_id AS VARCHAR) = CAST(u.id AS VARCHAR)
        """
        customer_scope_fields = """
            CAST(b.id AS VARCHAR) AS customer_brand_id,
            b.name AS customer_brand_name,
            CAST(l.id AS VARCHAR) AS converted_lead_id,
            l.creator_name
        """
    rows = conn.execute(
        f"""
        SELECT
            CAST(u.id AS VARCHAR),
            COALESCE(NULLIF(u.email, ''), NULLIF(u.username, ''), '') AS email,
            COALESCE(u.service_tier, 15) AS service_tier,
            u.service_mode_chosen_at,
            COALESCE(NULLIF(u.password_hash, ''), '') <> '' AS password_set,
            COALESCE(NULLIF(u.google_sub, ''), '') <> '' AS google_linked,
            (
                SELECT COUNT(*)
                FROM youtube_videos v
                WHERE CAST(v.owner_user_id AS VARCHAR) = CAST(u.id AS VARCHAR)
            ) AS source_video_count,
            {customer_scope_fields}
        FROM shorts_users u
        {customer_scope_join}
        WHERE {where_sql}
        ORDER BY u.service_mode_chosen_at DESC NULLS LAST, u.updated_at DESC NULLS LAST, u.created_at DESC NULLS LAST
        LIMIT ? OFFSET ?
        """,
        [*params, limit, offset],
    ).fetchall()
    items: List[Dict[str, Any]] = []
    for row in rows:
        user_id = str(row[0] or "").strip()
        youtube_connected = _token_table_has_any_rows("youtube_oauth_tokens_v2", user_id)
        source_video_count = int(row[6] or 0)
        customer_brand_id = str(row[7] or "").strip()
        customer_brand_name = str(row[8] or "").strip()
        converted_lead_id = str(row[9] or "").strip()
        creator_name = str(row[10] or "").strip()
        if not youtube_connected:
            next_action = "Connect channel"
        elif source_video_count <= 0:
            next_action = "Upload first long video"
        else:
            next_action = "Ready for manual autopilot setup"
        items.append(
            {
                "user_id": user_id,
                "email": str(row[1] or "").strip(),
                "service_tier": int(row[2] or 15),
                "chosen_at": row[3],
                "chosen_at_pst": _format_datetime_pst(row[3]),
                "password_set": bool(row[4]),
                "password_set_label": "Yes" if bool(row[4]) else "No",
                "google_linked": bool(row[5]),
                "google_linked_label": "Yes" if bool(row[5]) else "No",
                "youtube_connected": youtube_connected,
                "youtube_connected_label": "Yes" if youtube_connected else "No",
                "source_video_count": source_video_count,
                "next_action": next_action,
                "user_detail_url": url_for("video_shorts_bp.admin_user_detail", user_id=user_id),
                "customer_brand_id": customer_brand_id,
                "customer_brand_name": customer_brand_name,
                "converted_lead_id": converted_lead_id,
                "creator_name": creator_name,
                "has_customer_workspace": bool(customer_brand_id and converted_lead_id),
            }
        )
    return items, total_count


def _directory_size_bytes(path: Path) -> int:
    total = 0
    try:
        for child in path.rglob("*"):
            try:
                if child.is_file():
                    total += int(child.stat().st_size or 0)
            except Exception:
                continue
    except Exception:
        return 0
    return total


def _load_admin_server_stats() -> Dict[str, Any]:
    usage = shutil.disk_usage("/")
    tmp_dir = ensure_video_shorts_tmp_dir()
    tmp_size_bytes = _directory_size_bytes(tmp_dir)
    used_pct = int(round((usage.used / usage.total) * 100)) if usage.total else 0
    return {
        "disk_used_bytes": int(usage.used or 0),
        "disk_total_bytes": int(usage.total or 0),
        "disk_used_label": _format_size_bytes(int(usage.used or 0)),
        "disk_total_label": _format_size_bytes(int(usage.total or 0)),
        "disk_used_pct": used_pct,
        "tmp_dir": str(tmp_dir),
        "tmp_size_bytes": tmp_size_bytes,
        "tmp_size_label": _format_size_bytes(tmp_size_bytes),
    }


def _token_table_has_any_rows(table_name: str, owner_user_id: str) -> bool:
    owner_text = str(owner_user_id or "").strip()
    if not owner_text:
        return False
    try:
        conn = connect_store(read_only=True, retries=2, error_cls=RuntimeError)
    except Exception:
        return False
    try:
        row = conn.execute(
            f"SELECT 1 FROM {table_name} WHERE user_id = ? OR user_id LIKE ? LIMIT 1",
            [owner_text, f"{owner_text}::%"],
        ).fetchone()
        return bool(row)
    except Exception as exc:
        if relation_missing(exc, table_name):
            return False
        return False
    finally:
        conn.close()


def _load_admin_user_detail(conn, user_id: str) -> Optional[Dict[str, Any]]:
    youtube_columns = table_columns(conn, "youtube_videos")
    generated_columns = table_columns(conn, "shorts_generated_videos")
    render_job_columns = table_columns(conn, "shorts_render_jobs")
    onboarding_magic_link_columns = table_columns(conn, "onboarding_magic_links")

    row = conn.execute(
        """
        SELECT
          CAST(id AS VARCHAR),
          username,
          name,
          email,
          role,
          plan_id,
          subscription_status,
          subscription_current_period_end,
          created_at,
          updated_at,
          google_sub
        FROM shorts_users
        WHERE CAST(id AS VARCHAR) = ?
        LIMIT 1
        """,
        [user_id],
    ).fetchone()
    if not row:
        return None

    uploaded_videos = 0
    if youtube_columns and "owner_user_id" in youtube_columns:
        uploaded_videos = int(
            conn.execute(
                "SELECT COUNT(*) FROM youtube_videos WHERE owner_user_id = ?",
                [user_id],
            ).fetchone()[0]
            or 0
        )

    shorts_generated = 0
    shorts_published = 0
    funnel_breakdown = {
        "generated_total": 0,
        "scheduled": 0,
        "published": 0,
        "failed": 0,
        "overdue": 0,
    }
    if generated_columns and "user_id" in generated_columns:
        shorts_generated = int(
            conn.execute(
                "SELECT COUNT(*) FROM shorts_generated_videos WHERE user_id = ?",
                [user_id],
            ).fetchone()[0]
            or 0
        )
        if "publish_status" in generated_columns:
            shorts_published = int(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM shorts_generated_videos
                    WHERE user_id = ?
                      AND lower(coalesce(publish_status, '')) = 'published'
                    """,
                    [user_id],
                ).fetchone()[0]
                or 0
            )
        if "publish_status" in generated_columns:
            overdue_expr = (
                """
                SUM(
                    CASE
                        WHEN lower(coalesce(publish_status, '')) <> 'published'
                         AND planned_publish_at IS NOT NULL
                         AND planned_publish_at <= now()
                        THEN 1
                        ELSE 0
                    END
                )
                """
                if "planned_publish_at" in generated_columns
                else "0"
            )
            breakdown_row = conn.execute(
                f"""
                SELECT
                    COUNT(*) AS generated_total,
                    SUM(CASE WHEN lower(coalesce(publish_status, '')) = 'scheduled' THEN 1 ELSE 0 END) AS scheduled_count,
                    SUM(CASE WHEN lower(coalesce(publish_status, '')) = 'published' THEN 1 ELSE 0 END) AS published_count,
                    SUM(CASE WHEN lower(coalesce(publish_status, '')) = 'failed' THEN 1 ELSE 0 END) AS failed_count,
                    {overdue_expr} AS overdue_count
                FROM shorts_generated_videos
                WHERE user_id = ?
                """,
                [user_id],
            ).fetchone()
            if breakdown_row:
                funnel_breakdown = {
                    "generated_total": int(breakdown_row[0] or 0),
                    "scheduled": int(breakdown_row[1] or 0),
                    "published": int(breakdown_row[2] or 0),
                    "failed": int(breakdown_row[3] or 0),
                    "overdue": int(breakdown_row[4] or 0),
                }

    connected_platforms = {
        "youtube": _token_table_has_any_rows("youtube_oauth_tokens_v2", user_id),
        "instagram": _token_table_has_any_rows("instagram_oauth_tokens", user_id),
        "facebook": _token_table_has_any_rows("facebook_page_tokens", user_id),
        "tiktok": _token_table_has_any_rows("tiktok_oauth_tokens", user_id),
    }
    recent_failures: List[Dict[str, Any]] = []
    failures_unavailable_note = ""
    try:
        failure_items: List[Dict[str, Any]] = []
        if render_job_columns:
            render_failure_rows = conn.execute(
                """
                SELECT id, type, error, attempts, max_attempts, finished_at
                FROM shorts_render_jobs
                WHERE user_id = ?
                  AND lower(coalesce(status, '')) = 'failed'
                ORDER BY finished_at DESC NULLS LAST, updated_at DESC NULLS LAST, created_at DESC NULLS LAST
                LIMIT 20
                """,
                [user_id],
            ).fetchall()
            for failure_row in render_failure_rows:
                reason = str(failure_row[2] or "").strip()
                if len(reason) > 200:
                    reason = reason[:197].rstrip() + "..."
                attempts = int(failure_row[3] or 0)
                max_attempts = int(failure_row[4] or 0)
                attempt_label = f"{attempts}/{max_attempts}" if max_attempts else str(attempts)
                failure_items.append(
                    {
                        "source": "render",
                        "timestamp": failure_row[5],
                        "identifier": failure_row[1] or failure_row[0] or "render_job",
                        "reason": reason or "No error message recorded.",
                        "meta": f"attempts {attempt_label}",
                    }
                )
        elif not failures_unavailable_note:
            failures_unavailable_note = "Render job history is unavailable."

        if generated_columns and "user_id" in generated_columns and "publish_status" in generated_columns:
            publish_ts_order = "updated_at DESC" if "updated_at" in generated_columns else "created_at DESC"
            publish_id_col = "id" if "id" in generated_columns else "clip_filename"
            publish_failure_rows = conn.execute(
                f"""
                SELECT {publish_id_col}, planned_publish_at, updated_at
                FROM shorts_generated_videos
                WHERE user_id = ?
                  AND lower(coalesce(publish_status, '')) = 'failed'
                ORDER BY {publish_ts_order}
                LIMIT 20
                """,
                [user_id],
            ).fetchall()
            for failure_row in publish_failure_rows:
                planned_at = failure_row[1]
                reason = (
                    f"Scheduled for {planned_at} but lifecycle remained failed."
                    if planned_at
                    else "Publish lifecycle marked failed."
                )
                failure_items.append(
                    {
                        "source": "publish",
                        "timestamp": failure_row[2] or failure_row[1],
                        "identifier": str(failure_row[0] or "generated_video"),
                        "reason": reason,
                        "meta": "",
                    }
                )
        elif not failures_unavailable_note:
            failures_unavailable_note = "Publish lifecycle history is unavailable."

        failure_items.sort(
            key=lambda item: str(item.get("timestamp") or ""),
            reverse=True,
        )
        recent_failures = failure_items[:20]
    except Exception as exc:
        failures_unavailable_note = "Failure history is unavailable."
        current_app.logger.exception("Admin failure panel unavailable for user %s: %s", user_id, exc)

    timeline_rows: List[Dict[str, Any]] = []
    try:
        timeline = conn.execute(
            """
            SELECT created_at, event_name, status, platform, video_id, short_id
            FROM user_events
            WHERE user_id = ?
               OR user_id LIKE ?
            ORDER BY created_at DESC
            LIMIT 100
            """,
            [user_id, f"{user_id}::%"],
        ).fetchall()
        timeline_rows = [
            {
                "created_at": event_row[0],
                "event_name": event_row[1] or "",
                "status": event_row[2] or "",
                "platform": event_row[3] or "",
                "video_id": event_row[4] or "",
                "short_id": event_row[5] or "",
            }
            for event_row in timeline
        ]
    except Exception:
        timeline_rows = []

    cost_summary: Dict[str, Any]
    transcription_usage: Dict[str, Any]
    try:
        cost_summary = _load_admin_cost_estimates(
            conn,
            [user_id],
            created_at_map={user_id: row[8]},
        ).get(user_id) or {
            "display_total_usd": "—",
            "display_this_month_usd": "—",
            "display_monthly_avg_usd": "—",
        }
    except Exception as exc:
        current_app.logger.exception("Admin cost detail unavailable for user %s: %s", user_id, exc)
        cost_summary = {
            "display_total_usd": "—",
            "display_this_month_usd": "—",
            "display_monthly_avg_usd": "—",
        }
    try:
        transcription_usage = _load_admin_transcription_usage(
            conn,
            [user_id],
            plan_id_map={user_id: str(row[5] or "plan_free")},
        ).get(user_id) or {"display": "—"}
    except Exception as exc:
        current_app.logger.exception("Admin transcription usage detail unavailable for user %s: %s", user_id, exc)
        transcription_usage = {"display": "—"}

    latest_trial_days = None
    latest_trial_days_label = ""
    latest_trial_used_at = None
    trial_expires_at = None
    trial_status_label = ""
    trial_status_tone = ""
    if onboarding_magic_link_columns and "user_id" in onboarding_magic_link_columns:
        trial_days_sql = "COALESCE(trial_days, ?)" if "trial_days" in onboarding_magic_link_columns else "?"
        used_at_sql = "used_at" if "used_at" in onboarding_magic_link_columns else "NULL"
        trial_row = conn.execute(
            f"""
            SELECT
              {trial_days_sql} AS trial_days,
              {used_at_sql} AS used_at
            FROM onboarding_magic_links
            WHERE CAST(user_id AS VARCHAR) = ?
              AND used_at IS NOT NULL
            ORDER BY used_at ASC, created_at ASC, id ASC
            LIMIT 1
            """,
            [LEGACY_SHARE_TRIAL_DAYS, user_id],
        ).fetchone()
        if trial_row:
            latest_trial_days = normalize_trial_days(trial_row[0], default=LEGACY_SHARE_TRIAL_DAYS)
            latest_trial_days_label = _trial_duration_label(latest_trial_days, "EN")
            latest_trial_used_at = trial_row[1]
            if isinstance(latest_trial_used_at, datetime) and latest_trial_days:
                trial_expires_at = latest_trial_used_at + timedelta(days=latest_trial_days)
                now_value = datetime.now(trial_expires_at.tzinfo) if trial_expires_at.tzinfo else datetime.now()
                days_left = math.ceil((trial_expires_at - now_value).total_seconds() / 86400.0)
                if days_left > 1:
                    trial_status_label = f"{days_left} gün kaldı"
                elif days_left == 1:
                    trial_status_label = "1 gün kaldı"
                elif days_left == 0:
                    trial_status_label = "bugün doluyor"
                else:
                    trial_status_label = f"{abs(days_left)} gün önce doldu"

                if days_left > 14:
                    trial_status_tone = "neutral"
                elif days_left > 0:
                    trial_status_tone = "warning"
                else:
                    trial_status_tone = "danger"

    return {
        "id": row[0],
        "username": row[1] or "",
        "name": row[2] or row[1] or "Unnamed user",
        "email": row[3] or "",
        "role": row[4] or "member",
        "plan_id": row[5] or "plan_free",
        "subscription_status": row[6] or "",
        "subscription_current_period_end": row[7],
        "created_at": row[8],
        "updated_at": row[9],
        "google_sub_present": bool((row[10] or "").strip()),
        "trial_days": latest_trial_days,
        "trial_days_label": latest_trial_days_label,
        "trial_used_at": latest_trial_used_at,
        "trial_expires_at": trial_expires_at,
        "trial_status_label": trial_status_label,
        "trial_status_tone": trial_status_tone,
        "uploaded_videos": uploaded_videos,
        "shorts_generated": shorts_generated,
        "shorts_published": shorts_published,
        "funnel_breakdown": funnel_breakdown,
        "recent_failures": recent_failures,
        "failures_unavailable_note": failures_unavailable_note,
        "connected_platforms": connected_platforms,
        "timeline_events": timeline_rows,
        "cost_summary": cost_summary,
        "transcription_usage": transcription_usage,
    }


def _load_admin_user_brand_id(conn, user_id: str) -> Optional[str]:
    ensure_brand_schema(conn)
    row = conn.execute(
        """
        SELECT
            COALESCE(
                NULLIF(CAST(u.last_brand_id AS VARCHAR), ''),
                (
                    SELECT CAST(b.id AS VARCHAR)
                    FROM shorts_brands b
                    WHERE CAST(b.owner_user_id AS VARCHAR) = CAST(u.id AS VARCHAR)
                    ORDER BY COALESCE(b.is_default, false) DESC, b.created_at ASC
                    LIMIT 1
                )
            ) AS brand_id
        FROM shorts_users u
        WHERE CAST(u.id AS VARCHAR) = ?
        LIMIT 1
        """,
        [user_id],
    ).fetchone()
    return str((row[0] if row else "") or "").strip() or None


@video_shorts_bp.route("/shorts/scripts")
def shorts_scripts():
    current_user = getattr(g, "vs_current_user", None)
    if not current_user:
        return redirect(url_for("video_shorts_bp.login", next=request.url))

    def _segment_idx_range(segments: List[Dict[str, Any]], start: Any, end: Any) -> Tuple[Optional[int], Optional[int]]:
        if not segments:
            return None, None
        try:
            start_val = float(start) if start is not None else None
        except Exception:
            start_val = None
        try:
            end_val = float(end) if end is not None else None
        except Exception:
            end_val = None
        start_idx = None
        end_idx = None
        for idx, seg in enumerate(segments):
            seg_start = seg.get("start")
            seg_end = seg.get("end")
            if seg_end is None:
                seg_end = seg_start
            if start_val is not None and start_idx is None:
                try:
                    if seg_end is not None and float(seg_end) >= start_val:
                        start_idx = idx
                except Exception:
                    pass
            if end_val is not None:
                try:
                    if seg_start is not None and float(seg_start) <= end_val:
                        end_idx = idx
                except Exception:
                    pass
        if start_val is not None and start_idx is None:
            start_idx = len(segments) - 1
        if end_val is not None and end_idx is None:
            end_idx = len(segments) - 1
        if start_val is None and end_val is not None and start_idx is None:
            start_idx = end_idx
        if end_val is None and start_val is not None:
            end_idx = start_idx
        return start_idx, end_idx

    plan_suffix = "_plan.json"
    grouped: Dict[str, Dict[str, Any]] = {}
    conn = get_db_readonly()
    for plan_path in SHORTS_DIR.glob(f"*{plan_suffix}"):
        if plan_path.name.endswith("_plan_v2.json"):
            continue
        video_id = plan_path.name[: -len(plan_suffix)]
        try:
            data = json.loads(plan_path.read_text())
        except Exception:
            continue
        entries = data.get("plan") or data.get("clips") or []
        if not entries:
            continue
        if video_id not in grouped:
            full_text, segments = _fetch_transcript(conn, video_id)
            grouped[video_id] = {
                "video_id": video_id,
                "long_text": full_text or "",
                "segments": segments or [],
                "shorts": [],
            }
        group = grouped[video_id]
        for entry in entries:
            clip_filename = entry.get("clip_filename") or entry.get("output_filename")
            clip_path = SHORTS_DIR / clip_filename if clip_filename else None
            clip_exists = clip_path.exists() if clip_path else False
            status = (entry.get("status") or "").lower()
            if status != "created" and not clip_exists:
                continue
            short_text = entry.get("transcript_full") or entry.get("excerpt") or ""
            if not short_text:
                start = entry.get("start")
                end = entry.get("end")
                if group["segments"] and start is not None and end is not None:
                    try:
                        short_text = build_transcript_for_range(
                            group["segments"], start, end, prefer_tr=True
                        )
                    except Exception:
                        short_text = ""
            if not short_text:
                continue
            start_idx, end_idx = _segment_idx_range(
                group.get("segments") or [], entry.get("start"), entry.get("end")
            )
            group["shorts"].append(
                {
                    "text": short_text,
                    "start": entry.get("start"),
                    "end": entry.get("end"),
                    "start_label": _format_time_label(entry.get("start"))
                    if entry.get("start") is not None
                    else None,
                    "end_label": _format_time_label(entry.get("end"))
                    if entry.get("end") is not None
                    else None,
                    "start_idx": start_idx + 1 if start_idx is not None else None,
                    "end_idx": end_idx + 1 if end_idx is not None else None,
                }
            )
    conn.close()
    rows = []
    total_shorts = 0
    for video_id in sorted(grouped.keys()):
        group = grouped[video_id]
        shorts = group.get("shorts") or []
        if not shorts:
            continue
        total_shorts += len(shorts)
        long_segments = []
        for idx, seg in enumerate(group.get("segments") or [], 1):
            text = (seg.get("text") or "").strip()
            if not text:
                continue
            start = seg.get("start")
            end = seg.get("end")
            long_segments.append(
                {
                    "idx_label": f"idx-{idx}",
                    "start_label": _format_time_label(start) if start is not None else None,
                    "end_label": _format_time_label(end) if end is not None else None,
                    "text": text,
                }
            )
        rows.append(
            {
                "video_id": video_id,
                "long_text": group.get("long_text") or "",
                "long_segments": long_segments,
                "shorts": shorts,
            }
        )
    return render_template(
        "shorts_scripts.html",
        rows=rows,
        total_shorts=total_shorts,
    )


@video_shorts_bp.route("/shorts/storage/assign", methods=["POST"])
def assign_storage_owner():
    current_user = getattr(g, "vs_current_user", None)
    is_admin = current_user and current_user.get("role") == "admin"
    if not is_admin:
        flash("You do not have permission to change owners.", "danger")
        return redirect(url_for("video_shorts_bp.shorts_storage"))
    file_key = (request.form.get("file_key") or "").strip()
    user_id_raw = (request.form.get("user_id") or "").strip()
    redirect_params = {
        "videos_sort": request.form.get("videos_sort", "size"),
        "videos_dir": request.form.get("videos_dir", "desc"),
    }
    if not file_key:
        flash("Dosya anahtarı eksik.", "danger")
        return redirect(url_for("video_shorts_bp.shorts_storage", **redirect_params))
    user_id = user_id_raw or None
    conn = get_db()
    ensure_storage_user_schema(conn)
    try:
        conn.execute(
            """
            UPDATE shorts_storage_assets
            SET user_id = ?, updated_at = now()
            WHERE file_key = ?
            """,
            [user_id, file_key],
        )
        conn.commit()
        flash("Sahiplik güncellendi.", "success")
    except Exception as exc:
        conn.rollback()
        current_app.logger.exception("Failed to assign storage owner for %s: %s", file_key, exc)
        flash("Sahiplik güncellenemedi.", "danger")
    finally:
        conn.close()
    return redirect(url_for("video_shorts_bp.shorts_storage", **redirect_params))


@video_shorts_bp.route("/shorts/storage/delete", methods=["POST"])
def delete_storage_video():
    current_user = getattr(g, "vs_current_user", None)
    if not current_user:
        return jsonify(success=False, message="Authentication required"), 403
    is_admin = current_user.get("role") == "admin"
    target_dir = (request.form.get("target_dir") or "shorts").strip()
    filename = (request.form.get("filename") or "").strip()
    video_id = (request.form.get("video_id") or "").strip()
    base_dir = SHORTS_DIR if target_dir != "videos" else VIDEOS_DIR

    if filename:
        file_path = base_dir / filename
    elif video_id:
        file_path = base_dir / f"{video_id}.mp4"
    else:
        return jsonify(success=False, message="Video ID or filename missing"), 400
    try:
        real_path = file_path.resolve()
        if not str(real_path).startswith(str(base_dir.resolve())):
            raise ValueError("Invalid path")
    except Exception:
        return jsonify(success=False, message="Invalid file path"), 400
    if target_dir != "videos" and filename:
        if not _short_exists(filename):
            return jsonify(success=False, message="Dosya bulunamadı"), 404
    elif not real_path.exists():
        return jsonify(success=False, message="Dosya bulunamadı"), 404
    asset_key = f"{'videos' if target_dir == 'videos' else 'shorts'}:{filename or file_path.name}"
    conn_check = get_db_readonly()
    owner_row = conn_check.execute(
        "SELECT user_id FROM shorts_storage_assets WHERE file_key = ?",
        [asset_key],
    ).fetchone()
    conn_check.close()
    owner_id = owner_row[0] if owner_row else None
    if not is_admin and owner_id != current_user["id"]:
        return jsonify(success=False, message="You do not have permission to delete this file."), 403
    try:
        if target_dir == "videos":
            real_path.unlink()
        else:
            _delete_short_media(filename or file_path.name)
    except Exception as exc:
        current_app.logger.exception("Failed to delete storage video %s: %s", real_path, exc)
        return jsonify(success=False, message="Dosya silinemedi"), 500
    conn_assets = get_db()
    ensure_storage_user_schema(conn_assets)
    try:
        conn_assets.execute(
            "DELETE FROM shorts_storage_assets WHERE file_key = ?",
            [asset_key],
        )
        conn_assets.commit()
    finally:
        conn_assets.close()

    if target_dir != "videos":
        clip_name = filename or file_path.name
        source_video_id, plan_index = _resolve_clip_plan_identity(clip_name)
        if source_video_id and plan_index is not None:
            try:
                resolved_plan_index = _reset_deleted_clip_plan_entry(
                    source_video_id,
                    clip_name,
                    plan_index=plan_index,
                )
            except Exception as exc:
                current_app.logger.warning("Failed to update plan file after storage delete: %s", exc)
                resolved_plan_index = None
            try:
                if resolved_plan_index is not None:
                    clear_done_job_cache_for_plan(
                        user_id=str(current_user.get("id") or ""),
                        source_video_id=str(source_video_id or ""),
                        plan_index=int(resolved_plan_index),
                    )
            except Exception as exc:
                current_app.logger.warning("Failed to clear render cache after storage delete: %s", exc)

    if video_id and target_dir == "videos":
        conn = get_db()
        _ensure_video_crop_schema(conn)
        try:
            conn.execute(
                """
                UPDATE youtube_videos
                SET download_status = 'downloaded_deleted',
                    downloaded_at = NULL
                WHERE video_id = ?
                """,
                [video_id],
            )
            conn.commit()
        except Exception:
            current_app.logger.exception("Failed to update download_status for %s", video_id)
            return jsonify(success=False, message="Veritabanı güncellenemedi"), 500
        finally:
            conn.close()
    return jsonify(success=True)


@video_shorts_bp.route("/shorts/storage/plans", methods=["GET", "POST"])
def shorts_storage_plans():
    current_user = getattr(g, "vs_current_user", None)
    if not current_user:
        return redirect(url_for("video_shorts_bp.login", next=request.url))
    is_admin = current_user.get("role") == "admin"
    is_autopilot = str(current_user.get("service_mode") or "").strip().lower() == "autopilot"
    try:
        autopilot_service_tier = int(current_user.get("service_tier") or 15)
    except (TypeError, ValueError):
        autopilot_service_tier = 15
    autopilot_plan_label = f"Autopilot — {autopilot_service_tier} Shorts/mo"
    billing_user = load_billing_user_state(current_user["id"], refresh_live=True)
    effective_plan_id = (
        (billing_user or {}).get("plan_id")
        or current_user.get("plan_id")
        or "plan_free"
    )
    has_managed_subscription = user_has_managed_subscription(billing_user)
    subscription_cancel_notice = None
    subscription_cancel_effective_date = None
    free_period_notice = None
    free_period_renewal_date = None
    period_end = None
    if billing_user and billing_user.get("subscription_cancel_at_period_end"):
        period_end = billing_user.get("subscription_current_period_end")
        if isinstance(period_end, datetime):
            effective_date = period_end.astimezone(timezone.utc).strftime("%B %d, %Y")
            subscription_cancel_effective_date = effective_date
        else:
            effective_date = "the end of your billing period"
    else:
        period_end = (billing_user or {}).get("subscription_current_period_end")
    if billing_user and billing_user.get("free_period_active"):
        free_months = int(billing_user.get("free_months") or 0)
        if isinstance(period_end, datetime):
            free_period_renewal_date = period_end.astimezone(timezone.utc).strftime("%B %d, %Y")
        elif period_end:
            free_period_renewal_date = str(period_end)
        else:
            free_period_renewal_date = "the end of your billing period"
        if free_months == 1:
            free_period_notice = f"First month on us — renews {free_period_renewal_date}"
        elif free_months >= 2:
            free_period_notice = f"First {free_months} months on us — renews {free_period_renewal_date}"
    conn = get_db()
    ensure_storage_user_schema(conn)
    if request.method == "POST":
        if is_autopilot:
            conn.close()
            flash("Autopilot plan changes are handled by Minti. Send a tier request from this page and we'll contact you.", "info")
            return redirect(url_for("video_shorts_bp.shorts_storage_plans"))
        plan_id = (request.form.get("plan_id") or "").strip() or None
        normalized_plan_id = (plan_id or "").strip()
        if not is_admin:
            if normalized_plan_id and normalized_plan_id != "plan_free":
                conn.close()
                flash("Paid plan changes must go through checkout.", "warning")
                return redirect(url_for("video_shorts_bp.shorts_storage_plans"))
            if normalized_plan_id == "plan_free" and has_managed_subscription:
                conn.close()
                flash("Manage your active subscription in Stripe Billing Portal to switch to Free.", "warning")
                return redirect(url_for("video_shorts_bp.billing_portal"))
        conn.execute(
            """
            UPDATE shorts_users
            SET plan_id = ?, custom_limit_bytes = NULL
            WHERE id = ?
            """,
            [plan_id, current_user["id"]],
        )
        conn.commit()
        flash("Subscription updated.", "success")
        conn.close()
        return redirect(url_for("video_shorts_bp.shorts_storage_plans"))
    plans = _load_storage_plan_catalog(conn)
    conn.close()
    current_plan = next((plan for plan in plans if plan["plan_id"] == effective_plan_id), None)
    if billing_user and billing_user.get("subscription_cancel_at_period_end"):
        subscription_cancel_notice = f"Ends {effective_date} -> Free"

    return render_template(
        "shorts_storage_plans.html",
        plans=plans,
        is_admin=is_admin,
        is_autopilot=is_autopilot,
        autopilot_service_tier=autopilot_service_tier,
        autopilot_plan_label=autopilot_plan_label,
        current_plan_label=autopilot_plan_label if is_autopilot else ((current_plan or {}).get("label") or "Free"),
        stripe_ready=stripe_is_configured(),
        stripe_publishable_key=STRIPE_PUBLISHABLE_KEY,
        billing_has_managed_subscription=has_managed_subscription,
        billing_portal_url=url_for("video_shorts_bp.billing_portal"),
        subscription_cancel_notice=subscription_cancel_notice,
        free_period_notice=free_period_notice,
        current_plan_id=effective_plan_id,
        subscription_cancel_at_period_end=bool((billing_user or {}).get("subscription_cancel_at_period_end")),
        subscription_cancel_effective_date=subscription_cancel_effective_date,
    )


@video_shorts_bp.route("/admin/plans", methods=["GET", "POST"])
def admin_storage_plans():
    current_user = getattr(g, "vs_current_user", None)
    if not current_user:
        return redirect(url_for("video_shorts_bp.login", next=request.url))
    if current_user.get("role") != "admin":
        return redirect(url_for("video_shorts_bp.shorts_storage_plans"))

    conn = get_db()
    ensure_storage_user_schema(conn)
    if request.method == "POST":
        user_id = (request.form.get("user_id") or "").strip()
        plan_id = (request.form.get("plan_id") or "").strip() or None
        custom_limit = request.form.get("custom_limit") or ""
        custom_limit_bytes = None
        if custom_limit:
            try:
                custom_limit_bytes = int(float(custom_limit) * (1024 ** 3))
            except ValueError:
                custom_limit_bytes = None
        if user_id:
            conn.execute(
                """
                UPDATE shorts_users
                SET plan_id = ?, custom_limit_bytes = ?
                WHERE id = ?
                """,
                [plan_id, custom_limit_bytes, user_id],
            )
            conn.commit()
            flash("Subscription updated.", "success")
        else:
            flash("Select a user to update.", "danger")
        conn.close()
        redirect_args = request.args.to_dict(flat=False)
        return redirect(url_for("video_shorts_bp.admin_storage_plans", **redirect_args))

    plans = _load_storage_plan_catalog(conn)
    search_text = (request.args.get("q") or "").strip()
    selected_plan_ids = [plan_id for plan_id in request.args.getlist("plan_id") if plan_id]
    try:
        requested_page = int(request.args.get("page") or 1)
    except (TypeError, ValueError):
        requested_page = 1
    page = max(1, requested_page)
    per_page = 50
    total_users = _load_storage_plan_admin_users(
        conn,
        search_text=search_text,
        plan_ids=selected_plan_ids,
        limit=1,
        offset=0,
    )[1]
    total_pages = max(1, (total_users + per_page - 1) // per_page)
    page = min(page, total_pages)
    users, total_users = _load_storage_plan_admin_users(
        conn,
        search_text=search_text,
        plan_ids=selected_plan_ids,
        limit=per_page,
        offset=(page - 1) * per_page,
    )
    conn.close()
    return render_template(
        "shorts_storage_admin.html",
        plans=plans,
        users=users,
        search_text=search_text,
        selected_plan_ids=selected_plan_ids,
        page=page,
        per_page=per_page,
        total_users=total_users,
        total_pages=total_pages,
    )


@video_shorts_bp.route("/admin/users", methods=["GET", "POST"])
@require_admin
def admin_users():
    current_user = getattr(g, "vs_current_user", None) or {}

    conn = get_db()
    ensure_storage_user_schema(conn)
    ensure_auth_user_schema(conn)

    if request.method == "POST":
        action = (request.form.get("action") or "").strip()
        user_id = (request.form.get("user_id") or "").strip()
        redirect_args = request.args.to_dict(flat=False)
        if action == "resend_verification" and user_id:
            row = conn.execute(
                """
                SELECT
                    CAST(id AS VARCHAR),
                    username,
                    name,
                    email,
                    password_hash,
                    COALESCE(email_verified, FALSE),
                    email_verification_token_hash,
                    email_verification_expires_at,
                    email_verification_sent_at,
                    password_reset_sent_at,
                    google_sub
                FROM shorts_users
                WHERE CAST(id AS VARCHAR) = ?
                LIMIT 1
                """,
                [user_id],
            ).fetchone()
            conn.close()
            if not row:
                flash("User not found.", "danger")
            else:
                from app.video_shorts.routes.auth import _resend_verification_for_user

                try:
                    ok, message, _retry_after = _resend_verification_for_user(row, force=True)
                    flash(message, "success" if ok else "danger")
                except Exception as exc:
                    current_app.logger.exception("Admin resend verification failed for user %s: %s", user_id, exc)
                    flash("Couldn't resend the verification email.", "danger")
            return redirect(url_for("video_shorts_bp.admin_users", **redirect_args))
        conn.close()
        flash("Unsupported action.", "danger")
        return redirect(url_for("video_shorts_bp.admin_users", **redirect_args))

    search_text = (request.args.get("q") or "").strip()
    try:
        requested_page = int(request.args.get("page") or 1)
    except (TypeError, ValueError):
        requested_page = 1
    page = max(1, requested_page)
    per_page = 50
    current_admin_id = str(current_user.get("id") or "").strip()
    if current_admin_id:
        conn.execute(
            """
            UPDATE shorts_users
            SET admin_users_last_seen_at = now()
            WHERE CAST(id AS VARCHAR) = ?
            """,
            [current_admin_id],
        )
        conn.commit()
    total_users = _load_admin_auth_users(
        conn,
        search_text=search_text,
        limit=1,
        offset=0,
    )[1]
    total_pages = max(1, (total_users + per_page - 1) // per_page)
    page = min(page, total_pages)
    users, total_users = _load_admin_auth_users(
        conn,
        search_text=search_text,
        limit=per_page,
        offset=(page - 1) * per_page,
    )
    server_stats = _load_admin_server_stats()
    conn.close()
    return render_template(
        "shorts_admin_users.html",
        admin_title="Users",
        users=users,
        search_text=search_text,
        page=page,
        per_page=per_page,
        total_users=total_users,
        total_pages=total_pages,
        current_admin=current_user,
        server_stats=server_stats,
    )


@video_shorts_bp.route("/admin/users/<user_id>", methods=["GET"])
@require_admin
def admin_user_detail(user_id: str):
    conn = get_db_readonly()
    try:
        detail = _load_admin_user_detail(conn, user_id)
    finally:
        conn.close()
    if not detail:
        abort(404)
    return render_template(
        "shorts_admin_user_detail.html",
        admin_title=detail["name"],
        user_detail=detail,
    )


@video_shorts_bp.route("/admin/errors", methods=["GET"])
@require_admin
def admin_errors():
    selected_event_name = (request.args.get("event_name") or "").strip().lower()
    status_query = (request.args.get("status") or "").strip()
    email_query = (request.args.get("email") or "").strip()
    include_noise = (request.args.get("include_noise") or "").strip() == "1"
    time_range = (request.args.get("range") or "24h").strip().lower()
    if time_range not in {"24h", "7d", "all"}:
        time_range = "24h"
    try:
        requested_page = int(request.args.get("page") or 1)
    except (TypeError, ValueError):
        requested_page = 1
    page = max(1, requested_page)
    per_page = 200

    conn = get_db_readonly()
    try:
        total_events = _load_admin_error_events(
            conn,
            event_name=selected_event_name,
            status_query=status_query,
            email_query=email_query,
            time_range=time_range,
            include_noise=include_noise,
            limit=1,
            offset=0,
        )[1]
        total_pages = max(1, (total_events + per_page - 1) // per_page)
        page = min(page, total_pages)
        events, total_events = _load_admin_error_events(
            conn,
            event_name=selected_event_name,
            status_query=status_query,
            email_query=email_query,
            time_range=time_range,
            include_noise=include_noise,
            limit=per_page,
            offset=(page - 1) * per_page,
        )
    finally:
        conn.close()

    return render_template(
        "shorts_admin_errors.html",
        admin_title="Errors",
        events=events,
        selected_event_name=selected_event_name,
        status_query=status_query,
        email_query=email_query,
        include_noise=include_noise,
        selected_range=time_range,
        page=page,
        per_page=per_page,
        total_events=total_events,
        total_pages=total_pages,
    )


@video_shorts_bp.route("/admin/share-links", methods=["GET"])
@require_admin
def admin_share_links():
    email_query = (request.args.get("email") or "").strip()
    engagement_filter = (request.args.get("engagement") or "all").strip().lower()
    archive_filter = (request.args.get("archive") or "all").strip().lower()
    sort_key = (request.args.get("sort") or "created").strip().lower()
    sort_dir = (request.args.get("dir") or "desc").strip().lower()
    try:
        requested_page = int(request.args.get("page") or 1)
    except (TypeError, ValueError):
        requested_page = 1
    page = max(1, requested_page)
    per_page = 200

    conn = get_db_readonly()
    try:
        total_links = _load_admin_share_links(
            conn,
            email_query=email_query,
            engagement_filter=engagement_filter,
            archive_filter=archive_filter,
            sort_key=sort_key,
            sort_dir=sort_dir,
            limit=1,
            offset=0,
        )[1]
        total_pages = max(1, (total_links + per_page - 1) // per_page)
        page = min(page, total_pages)
        (
            links,
            total_links,
            normalized_filter,
            normalized_archive_filter,
            normalized_sort_key,
            normalized_sort_dir,
        ) = _load_admin_share_links(
            conn,
            email_query=email_query,
            engagement_filter=engagement_filter,
            archive_filter=archive_filter,
            sort_key=sort_key,
            sort_dir=sort_dir,
            limit=per_page,
            offset=(page - 1) * per_page,
        )
    finally:
        conn.close()

    return render_template(
        "shorts_admin_share_links.html",
        admin_title="Share links",
        links=links,
        email_query=email_query,
        selected_engagement=normalized_filter,
        selected_archive=normalized_archive_filter,
        sort_key=normalized_sort_key,
        sort_dir=normalized_sort_dir,
        page=page,
        per_page=per_page,
        total_links=total_links,
        total_pages=total_pages,
    )


@video_shorts_bp.route("/admin/autopilot-leads", methods=["GET"])
@require_admin
def admin_autopilot_leads():
    email_query = (request.args.get("email") or "").strip()
    try:
        requested_page = int(request.args.get("page") or 1)
    except (TypeError, ValueError):
        requested_page = 1
    page = max(1, requested_page)
    per_page = 200
    conn = get_db_readonly()
    try:
        total_customers = _load_admin_autopilot_customers(
            conn,
            email_query=email_query,
            limit=1,
            offset=0,
        )[1]
        total_leads = _load_admin_autopilot_leads(
            conn,
            email_query=email_query,
            limit=1,
            offset=0,
        )[1]
        total_items = max(total_customers, total_leads)
        total_pages = max(1, (total_items + per_page - 1) // per_page)
        page = min(page, total_pages)
        customers, total_customers = _load_admin_autopilot_customers(
            conn,
            email_query=email_query,
            limit=per_page,
            offset=(page - 1) * per_page,
        )
        leads, total_leads = _load_admin_autopilot_leads(
            conn,
            email_query=email_query,
            limit=per_page,
            offset=(page - 1) * per_page,
        )
    finally:
        conn.close()
    return render_template(
        "shorts_admin_autopilot_leads.html",
        admin_title="Autopilot",
        customers=customers,
        total_customers=total_customers,
        leads=leads,
        email_query=email_query,
        page=page,
        per_page=per_page,
        total_leads=total_leads,
        total_pages=total_pages,
    )


@video_shorts_bp.route("/admin/leads", methods=["GET"])
@require_admin
def admin_leads():
    conn = get_db_readonly()
    try:
        leads = _load_admin_lead_records(conn)
    finally:
        conn.close()
    return render_template(
        "shorts_admin_leads.html",
        admin_title="Leads",
        leads=leads,
    )


@video_shorts_bp.route("/admin/leads/<lead_id>/email", methods=["POST"])
@require_admin
def admin_provision_discovery_lead_email(lead_id: str):
    """Attach contact email to one discovery lead and provision its owner scope."""
    email = str(request.form.get("creator_email") or "").strip()
    conn = get_db()
    try:
        result = provision_discovery_lead_email(
            conn,
            lead_id=lead_id,
            creator_email=email,
        )
        conn.commit()
    except ValueError as exc:
        conn.rollback()
        flash(str(exc), "error")
    except Exception:
        conn.rollback()
        current_app.logger.exception("Failed to provision discovery lead %s", lead_id)
        flash("Could not provision this discovery lead. No changes were made.", "error")
    else:
        flash(
            f"Lead provisioned for {result['creator_email']}. Its source video now belongs to that lead's brand.",
            "success",
        )
    finally:
        conn.close()
    return redirect(url_for("video_shorts_bp.admin_leads"))


@video_shorts_bp.route("/admin/operation/select", methods=["POST"])
@require_admin
def admin_operation_select():
    """Validate an explicit target then redirect without mutating browser session state."""
    payload = request.get_json(silent=True) if request.is_json else request.form
    payload = payload or {}
    target_brand_id = str(payload.get("target_brand_id") or payload.get("brand_id") or "").strip()
    workspace_kind = str(payload.get("workspace") or "lead").strip().lower()
    scope = _require_admin_operation_scope(
        brand_id=target_brand_id,
        workspace_kind=workspace_kind,
    )
    track_event(
        scope["acting_admin_id"],
        "admin_operation_scope_selected",
        metadata={
            "target_owner_user_id": scope["owner_user_id"],
            "target_brand_id": scope["brand_id"],
            "workspace_kind": scope["workspace_kind"],
        },
    )
    if workspace_kind == "customer":
        _rehome_customer_sources_to_local_uploads(scope)
        redirect_target = url_for("video_shorts_bp.admin_operation_customer_workspace", brand_id=target_brand_id)
    else:
        redirect_target = url_for("video_shorts_bp.admin_operation_lead_workspace", brand_id=target_brand_id)
    if request.is_json:
        return jsonify({"ok": True, "target_owner_user_id": scope["owner_user_id"], "target_brand_id": scope["brand_id"]})
    return redirect(redirect_target)


@video_shorts_bp.route("/admin/operation/<workspace_kind>/<brand_id>/exit", methods=["POST"])
@require_admin
def admin_operation_exit_workspace(workspace_kind: str, brand_id: str):
    _require_admin_operation_scope(brand_id=brand_id, workspace_kind=workspace_kind)
    if request.is_json:
        return jsonify({"ok": True})
    return redirect(url_for(
        "video_shorts_bp.admin_autopilot_leads"
        if workspace_kind == "customer"
        else "video_shorts_bp.admin_leads"
    ))


@video_shorts_bp.route("/admin/operation/lead/<brand_id>/video/<int:video_pk>/generate", methods=["GET"])
@require_admin
def admin_operation_generate_lead_short(brand_id: str, video_pk: int):
    """Enter the shared editor with an explicit pre-conversion lead context."""
    _require_admin_operation_scope(brand_id=brand_id, workspace_kind="lead", video_pk=video_pk)
    return redirect(url_for(
        "video_shorts_bp.generate_short",
        video_pk=video_pk,
        admin_operation_brand=brand_id,
        admin_operation_kind="lead",
    ))


def _require_active_lead_workspace(scope: Dict[str, str]) -> Dict[str, str]:
    """Require a pre-conversion lead for this target scope; customer work is later."""
    conn = get_db_readonly()
    try:
        row = conn.execute(
            """
            SELECT l.id, l.creator_name, l.creator_email, b.name
            FROM autopilot_leads l
            JOIN shorts_brands b ON CAST(b.id AS VARCHAR) = CAST(l.brand_id AS VARCHAR)
            WHERE CAST(l.user_id AS VARCHAR) = CAST(? AS VARCHAR)
              AND CAST(l.brand_id AS VARCHAR) = CAST(? AS VARCHAR)
              AND l.converted_at IS NULL
            ORDER BY l.created_at DESC, l.id DESC
            LIMIT 1
            """,
            [scope["owner_user_id"], scope["brand_id"]],
        ).fetchone()
    finally:
        conn.close()
    if not row:
        abort(404)
    return {
        "id": str(row[0]),
        "creator_name": str(row[1] or "").strip() or "Unknown creator",
        "creator_email": str(row[2] or "").strip(),
        "brand_name": str(row[3] or "").strip() or "Unnamed brand",
    }


def _require_active_customer_workspace(scope: Dict[str, str]) -> Dict[str, str]:
    """Require a converted autopilot customer for this target scope."""
    conn = get_db_readonly()
    try:
        row = conn.execute(
            """
            SELECT l.id, l.creator_name, l.creator_email, b.name
            FROM autopilot_leads l
            JOIN shorts_users u ON CAST(u.id AS VARCHAR) = CAST(l.user_id AS VARCHAR)
            JOIN shorts_brands b ON CAST(b.id AS VARCHAR) = CAST(l.brand_id AS VARCHAR)
            WHERE CAST(l.user_id AS VARCHAR) = CAST(? AS VARCHAR)
              AND CAST(l.brand_id AS VARCHAR) = CAST(? AS VARCHAR)
              AND CAST(b.owner_user_id AS VARCHAR) = CAST(l.user_id AS VARCHAR)
              AND l.converted_at IS NOT NULL
              AND lower(coalesce(u.service_mode, '')) = 'autopilot'
            ORDER BY l.converted_at DESC, l.id DESC
            LIMIT 1
            """,
            [scope["owner_user_id"], scope["brand_id"]],
        ).fetchone()
    finally:
        conn.close()
    if not row:
        abort(404)
    return {
        "id": str(row[0]),
        "creator_name": str(row[1] or "").strip() or "Autopilot customer",
        "creator_email": str(row[2] or "").strip(),
        "brand_name": str(row[3] or "").strip() or "Unnamed brand",
    }


def _rehome_plan_files_to_local_video_id(source_video_id: str, local_video_id: str) -> None:
    """Copy durable-on-disk plan files so existing generated clips stay editable after re-home."""
    for plan_path_builder in (_plan_path, _plan_path_v2, _plan_path_v3, _plan_path_v4):
        source_path = plan_path_builder(source_video_id)
        target_path = plan_path_builder(local_video_id)
        if not source_path.exists() or target_path.exists():
            continue
        try:
            shutil.copyfile(source_path, target_path)
        except Exception:
            current_app.logger.exception(
                "Failed to copy customer plan file source_video_id=%s local_video_id=%s path=%s",
                source_video_id,
                local_video_id,
                source_path,
            )


def _rehome_customer_sources_to_local_uploads(scope: Dict[str, str]) -> int:
    """Convert target-owned outreach sources to local IDs without changing ownership.

    The downloader will fetch the original ``video_url`` into the new local S3 key.
    Transcript and generated-short references are copied/relinked in the same tenant so
    the customer's My Videos list can show existing work immediately.
    """
    conn = get_db()
    rehomed: List[Tuple[str, str]] = []
    try:
        local_channel_id = _get_or_create_local_uploads_channel(
            conn,
            owner_user_id=scope["owner_user_id"],
            brand_id=scope["brand_id"],
        )
        rows = conn.execute(
            """
            SELECT id, video_id
            FROM youtube_videos
            WHERE owner_user_id = ?
              AND brand_id = ?
              AND lower(coalesce(video_id, '')) NOT LIKE 'local_%%'
            ORDER BY id
            """,
            [scope["owner_user_id"], scope["brand_id"]],
        ).fetchall()
        for row in rows:
            video_pk = int(row[0])
            source_video_id = str(row[1] or "").strip()
            if not source_video_id:
                continue
            local_video_id = f"local_{uuid4().hex}"
            conn.execute(
                """
                INSERT INTO youtube_transcripts (video_id, full_text, segments_json, whisper_segments_json)
                SELECT ?, full_text, segments_json, whisper_segments_json
                FROM youtube_transcripts
                WHERE video_id = ?
                  AND NOT EXISTS (
                      SELECT 1
                      FROM youtube_transcripts
                      WHERE video_id = ?
                  )
                """,
                [local_video_id, source_video_id, local_video_id],
            )
            conn.execute(
                """
                UPDATE shorts_generated_videos
                SET source_video_id = ?
                WHERE source_video_id = ?
                  AND brand_id = ?
                """,
                [local_video_id, source_video_id, scope["brand_id"]],
            )
            conn.execute(
                """
                UPDATE youtube_videos
                SET video_id = ?,
                    channel_id = ?,
                    local_bucket_channel_id = ?,
                    download_status = 'pending',
                    downloaded_at = NULL
                WHERE id = ?
                  AND owner_user_id = ?
                  AND brand_id = ?
                """,
                [
                    local_video_id,
                    local_channel_id,
                    local_channel_id,
                    video_pk,
                    scope["owner_user_id"],
                    scope["brand_id"],
                ],
            )
            rehomed.append((source_video_id, local_video_id))
        conn.commit()
    finally:
        conn.close()
    for source_video_id, local_video_id in rehomed:
        _rehome_plan_files_to_local_video_id(source_video_id, local_video_id)
    if rehomed:
        track_event(
            scope["acting_admin_id"],
            "admin_operation_customer_sources_rehomed",
            metadata={
                "acting_admin_id": scope["acting_admin_id"],
                "target_owner_user_id": scope["owner_user_id"],
                "target_brand_id": scope["brand_id"],
                "count": len(rehomed),
            },
        )
    return len(rehomed)


@video_shorts_bp.route("/admin/operation/lead/<brand_id>/workspace", methods=["GET"])
@require_admin
def admin_operation_lead_workspace(brand_id: str):
    """Admin-only workspace for a revalidated, pre-conversion lead target."""
    scope = _require_admin_operation_scope(brand_id=brand_id, workspace_kind="lead")
    lead = _require_active_lead_workspace(scope)
    conn = get_db_readonly()
    try:
        first_row = conn.execute(
            "SELECT first_video_id FROM autopilot_leads WHERE id = ?",
            [lead["id"]],
        ).fetchone()
        rows = conn.execute(
            """
            SELECT id, video_id, title, thumbnail_url, download_status, transcript_status
            FROM youtube_videos
            WHERE owner_user_id = ? AND brand_id = ?
            ORDER BY id DESC
            """,
            [scope["owner_user_id"], scope["brand_id"]],
        ).fetchall()
    finally:
        conn.close()
    first_video_id = int(first_row[0]) if first_row and first_row[0] is not None else None
    videos = [
        {
            "id": int(row[0]),
            "video_id": str(row[1] or ""),
            "title": str(row[2] or "").strip() or "Untitled video",
            "thumbnail_url": str(row[3] or "").strip(),
            "download_status": str(row[4] or "").strip().lower() or "pending",
            "transcript_status": str(row[5] or "").strip().lower(),
        }
        for row in rows
    ]
    videos.sort(key=lambda video: (video["id"] != first_video_id, -video["id"]))
    lead["brand_id"] = scope["brand_id"]
    return render_template("shorts_admin_lead_workspace.html", admin_title="Lead workspace", lead=lead, videos=videos)


@video_shorts_bp.route("/admin/operation/customer/<brand_id>/workspace", methods=["GET"])
@require_admin
def admin_operation_customer_workspace(brand_id: str):
    """Admin-only workspace for a revalidated converted autopilot customer."""
    scope = _require_admin_operation_scope(brand_id=brand_id, workspace_kind="customer")
    customer = _require_active_customer_workspace(scope)
    conn = get_db_readonly()
    try:
        rows = conn.execute(
            """
            SELECT id, video_id, title, thumbnail_url, download_status, transcript_status
            FROM youtube_videos
            WHERE owner_user_id = ? AND brand_id = ?
            ORDER BY id DESC
            """,
            [scope["owner_user_id"], scope["brand_id"]],
        ).fetchall()
    finally:
        conn.close()
    videos = [
        {
            "id": int(row[0]),
            "video_id": str(row[1] or ""),
            "title": str(row[2] or "").strip() or "Untitled video",
            "thumbnail_url": str(row[3] or "").strip(),
            "download_status": str(row[4] or "").strip().lower() or "pending",
            "transcript_status": str(row[5] or "").strip().lower(),
        }
        for row in rows
    ]
    return render_template(
        "shorts_admin_customer_workspace.html",
        admin_title="Customer workspace",
        customer={**customer, "brand_id": scope["brand_id"]},
        videos=videos,
    )


@video_shorts_bp.route("/admin/operation/customer/<brand_id>/ingest", methods=["POST"])
@require_admin
def admin_operation_ingest_customer_video(brand_id: str):
    """Create a target-owned local source row for a customer YouTube URL."""
    scope = _require_admin_operation_scope(brand_id=brand_id, workspace_kind="customer")
    _require_active_customer_workspace(scope)
    raw_url = str(request.form.get("url") or "").strip()
    youtube_video_id = extract_video_id(raw_url)
    if not youtube_video_id:
        flash("Enter a valid YouTube video URL.", "warning")
        return redirect(url_for("video_shorts_bp.admin_operation_customer_workspace", brand_id=brand_id))
    canonical_url = f"https://www.youtube.com/watch?v={youtube_video_id}"
    try:
        meta = fetch_video_metadata(youtube_video_id)
    except YoutubeApiError as exc:
        current_app.logger.warning("Customer workspace video metadata failed: %s", exc)
        flash("Could not read that YouTube video right now.", "danger")
        return redirect(url_for("video_shorts_bp.admin_operation_customer_workspace", brand_id=brand_id))
    except Exception:
        current_app.logger.exception("Customer workspace video metadata failed")
        flash("Could not prepare that YouTube video right now.", "danger")
        return redirect(url_for("video_shorts_bp.admin_operation_customer_workspace", brand_id=brand_id))

    conn = get_db()
    try:
        local_channel_id = _get_or_create_local_uploads_channel(
            conn,
            owner_user_id=scope["owner_user_id"],
            brand_id=scope["brand_id"],
        )
        existing = conn.execute(
            """
            SELECT id
            FROM youtube_videos
            WHERE owner_user_id = ?
              AND brand_id = ?
              AND video_url = ?
              AND lower(coalesce(video_id, '')) LIKE 'local_%%'
            LIMIT 1
            """,
            [scope["owner_user_id"], scope["brand_id"], canonical_url],
        ).fetchone()
        if existing:
            conn.commit()
            flash("That video is already in this customer's My Videos.", "info")
            return redirect(url_for("video_shorts_bp.admin_operation_customer_workspace", brand_id=brand_id))
        local_video_id = f"local_{uuid4().hex}"
        conn.execute(
            """
            INSERT INTO youtube_videos (
                channel_id, video_id, title, published_at, thumbnail_url, fetch_transcript,
                duration_seconds, view_count, like_count, comment_count, video_url,
                local_bucket_channel_id, owner_user_id, brand_id, download_status,
                transcript_status, subtitle_style
            )
            VALUES (?, ?, ?, ?, ?, FALSE, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 'pending', 'karaoke')
            """,
            [
                local_channel_id,
                local_video_id,
                meta.get("title") or canonical_url,
                meta.get("published_at"),
                meta.get("thumbnail_url"),
                meta.get("duration_seconds"),
                meta.get("view_count"),
                meta.get("like_count"),
                meta.get("comment_count"),
                canonical_url,
                local_channel_id,
                scope["owner_user_id"],
                scope["brand_id"],
            ],
        )
        video_pk_row = conn.execute(
            """
            SELECT id FROM youtube_videos
            WHERE video_id = ? AND owner_user_id = ? AND brand_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            [local_video_id, scope["owner_user_id"], scope["brand_id"]],
        ).fetchone()
        conn.commit()
    finally:
        conn.close()
    track_event(
        scope["acting_admin_id"],
        "admin_operation_customer_video_ingested",
        video_id=local_video_id,
        metadata={
            "acting_admin_id": scope["acting_admin_id"],
            "target_owner_user_id": scope["owner_user_id"],
            "target_brand_id": scope["brand_id"],
            "video_pk": int(video_pk_row[0]) if video_pk_row else None,
            "source_youtube_video_id": youtube_video_id,
        },
    )
    flash("Video added to this customer's My Videos. Download it when ready.", "success")
    return redirect(url_for("video_shorts_bp.admin_operation_customer_workspace", brand_id=brand_id))


@video_shorts_bp.route("/admin/operation/customer/<brand_id>/video/<int:video_pk>/download", methods=["POST"])
@require_admin
def admin_operation_download_customer_video(brand_id: str, video_pk: int):
    """Queue a target-owned customer source for the existing downloader worker."""
    scope = _require_admin_operation_scope(brand_id=brand_id, workspace_kind="customer", video_pk=video_pk)
    _require_active_customer_workspace(scope)
    conn = get_db()
    try:
        row = _fetch_scoped_video_row_with_scope(
            conn,
            video_pk,
            "id, video_id, video_url, download_status",
            owner_user_id=scope["owner_user_id"],
            brand_id=scope["brand_id"],
        )
        if not row or not str(row[2] or "").strip():
            abort(404)
        if str(row[3] or "").strip().lower() != "downloaded":
            conn.execute(
                """
                UPDATE youtube_videos
                SET download_status = 'pending', downloaded_at = NULL
                WHERE id = ? AND owner_user_id = ? AND brand_id = ?
                """,
                [video_pk, scope["owner_user_id"], scope["brand_id"]],
            )
            conn.commit()
    finally:
        conn.close()
    track_event(
        scope["acting_admin_id"],
        "admin_operation_customer_download_queued",
        video_id=str(row[1]),
        metadata={
            "acting_admin_id": scope["acting_admin_id"],
            "target_owner_user_id": scope["owner_user_id"],
            "target_brand_id": scope["brand_id"],
            "video_pk": video_pk,
        },
    )
    flash("Video queued for download.", "success")
    return redirect(url_for("video_shorts_bp.admin_operation_customer_workspace", brand_id=brand_id))


@video_shorts_bp.route("/admin/operation/customer/<brand_id>/video/<int:video_pk>/generate", methods=["GET"])
@require_admin
def admin_operation_generate_customer_short(brand_id: str, video_pk: int):
    """Open the shared editor only after proving this is a converted customer scope."""
    _require_admin_operation_scope(brand_id=brand_id, workspace_kind="customer", video_pk=video_pk)
    return redirect(url_for(
        "video_shorts_bp.generate_short",
        video_pk=video_pk,
        admin_operation_brand=brand_id,
        admin_operation_kind="customer",
    ))


@video_shorts_bp.route("/admin/operation/lead/<brand_id>/video/<int:video_pk>/download", methods=["POST"])
@require_admin
def admin_operation_download_lead_video(brand_id: str, video_pk: int):
    """Queue a scoped source for the existing downloader worker."""
    scope = _require_admin_operation_scope(brand_id=brand_id, workspace_kind="lead", video_pk=video_pk)
    _require_active_lead_workspace(scope)
    conn = get_db()
    try:
        row = _fetch_scoped_video_row_with_scope(
            conn,
            video_pk,
            "id, video_id, video_url, download_status",
            owner_user_id=scope["owner_user_id"],
            brand_id=scope["brand_id"],
        )
        if not row or not str(row[2] or "").strip():
            abort(404)
        if str(row[3] or "").strip().lower() != "downloaded":
            conn.execute(
                """
                UPDATE youtube_videos
                SET download_status = 'pending', downloaded_at = NULL
                WHERE id = ? AND owner_user_id = ? AND brand_id = ?
                """,
                [video_pk, scope["owner_user_id"], scope["brand_id"]],
            )
            conn.commit()
    finally:
        conn.close()
    track_event(
        scope["acting_admin_id"],
        "admin_operation_lead_download_queued",
        video_id=str(row[1]),
        metadata={
            "acting_admin_id": scope["acting_admin_id"],
            "target_owner_user_id": scope["owner_user_id"],
            "target_brand_id": scope["brand_id"],
            "video_pk": video_pk,
        },
    )
    flash("Video queued for download.", "success")
    return redirect(url_for("video_shorts_bp.admin_operation_lead_workspace", brand_id=brand_id))


@video_shorts_bp.route("/admin/autopilot-leads/provision", methods=["POST"])
@require_admin
def admin_provision_autopilot_lead():
    recipient_email = (request.form.get("recipient_email") or "").strip()
    channel_name = (request.form.get("channel_name") or "").strip()
    conn = get_db()
    try:
        lead = _provision_autopilot_placeholder(
            conn,
            recipient_email=recipient_email,
            channel_name=channel_name,
        )
    except ValueError as exc:
        flash(str(exc), "warning")
    except Exception:
        current_app.logger.exception(
            "Autopilot lead provisioning failed for email=%s",
            recipient_email.lower(),
        )
        flash("The autopilot lead could not be provisioned. No changes were made.", "danger")
    else:
        acting_admin_id = str((getattr(g, "vs_current_user", None) or {}).get("id") or "").strip()
        try:
            track_event(
                lead["user_id"],
                "autopilot_lead_provisioned",
                metadata={"brand_id": lead["brand_id"], "acting_admin_user_id": acting_admin_id},
            )
        except Exception:
            current_app.logger.exception("Autopilot lead provision audit event failed for user_id=%s", lead["user_id"])
        flash(
            f"Provisioned inactive autopilot lead {lead['email']} with brand {lead['brand_name']}. "
            "They can only access it after redeeming a valid onboarding link.",
            "success",
        )
    finally:
        conn.close()
    return redirect(url_for("video_shorts_bp.admin_autopilot_leads"))


@video_shorts_bp.route("/admin/users/<user_id>/refresh-youtube-status", methods=["POST"])
@require_admin
def admin_refresh_user_youtube_status(user_id: str):
    conn = get_db()
    try:
        detail = _load_admin_user_detail(conn, user_id)
        if not detail:
            abort(404)
        brand_id = _load_admin_user_brand_id(conn, user_id)
        source_rows = conn.execute(
            """
            SELECT DISTINCT CAST(source_video_id AS VARCHAR)
            FROM shorts_generated_videos
            WHERE CAST(user_id AS VARCHAR) = ?
              AND youtube_video_id IS NOT NULL
              AND (
                    publish_status IS NULL
                 OR lower(publish_status) <> 'published'
              )
            ORDER BY CAST(source_video_id AS VARCHAR)
            """,
            [user_id],
        ).fetchall()
    finally:
        conn.close()

    redirect_target = url_for("video_shorts_bp.admin_user_detail", user_id=user_id)
    if not has_refresh_token(user_id=user_id, brand_id=brand_id):
        flash("No YouTube token available for this user.", "warning")
        return redirect(redirect_target)

    source_video_ids = [str(row[0] or "").strip() for row in source_rows if str(row[0] or "").strip()]
    if not source_video_ids:
        flash("Checked 0 YouTube clip(s) across 0 source video(s); 0 newly updated.", "info")
        return redirect(redirect_target)

    checked_count = 0
    updated_count = 0
    source_count = 0
    source_errors = 0
    for source_video_id in source_video_ids:
        try:
            plan_entries = _load_plan_entries(source_video_id)
            if not plan_entries:
                continue
            result = _refresh_plan_publish_status_from_youtube_for_owner(
                source_video_id,
                plan_entries,
                user_id=user_id,
                brand_id=brand_id,
            )
            checked_count += int(result.get("checked_count") or 0)
            updated_count += int(result.get("updated_count") or 0)
            source_count += 1
        except Exception as exc:
            source_errors += 1
            current_app.logger.exception(
                "Admin YouTube publish refresh failed for user=%s brand=%s source_video_id=%s: %s",
                user_id,
                brand_id,
                source_video_id,
                exc,
            )

    if source_errors and not checked_count and not updated_count:
        flash("YouTube API error while refreshing this user. Try again.", "warning")
    elif checked_count == 0:
        flash(f"Checked 0 YouTube clip(s) across {source_count} source video(s); 0 newly updated.", "info")
    elif updated_count:
        message = f"Checked {checked_count} YouTube clip(s) across {source_count} source video(s); updated {updated_count} to published."
        if source_errors:
            message += f" {source_errors} source video(s) could not be refreshed."
        flash(message, "success")
    else:
        message = f"Checked {checked_count} YouTube clip(s) across {source_count} source video(s); 0 newly updated."
        if source_errors:
            message += f" {source_errors} source video(s) could not be refreshed."
        flash(message, "info")
    return redirect(redirect_target)


@video_shorts_bp.route("/generate/<int:video_pk>/delete_clip", methods=["POST"])
def delete_clip(video_pk):
    ajax_request = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    filename = (request.form.get("filename") or "").strip()
    plan_index_raw = (request.form.get("plan_index") or "").strip()
    if not filename:
        message = "Missing clip filename."
        if ajax_request:
            return jsonify(success=False, message=message), 400
        flash(message, "danger")
        return redirect(url_for("video_shorts_bp.generate_short", video_pk=video_pk))

    conn = get_db_readonly()
    row = _fetch_scoped_video_row(conn, video_pk, "video_id")
    conn.close()
    conn = None
    if not row:
        message = "Video not found"
        if ajax_request:
            return jsonify(success=False, message=message), 404
        flash(message, "danger")
        return redirect(url_for("video_shorts_bp.channels_page"))

    message = "Clip not found."
    success = False
    target = (SHORTS_DIR / filename).resolve()
    try:
        # safety: ensure path is under shorts dir
        if not str(target).startswith(str(SHORTS_DIR.resolve())):
            message = "Invalid path."
        elif _short_exists(filename):
            _delete_short_media(filename)
            plan_entries = _load_plan_entries(row[0])
            if plan_entries:
                match_entry = None
                plan_index = None
                if plan_index_raw:
                    try:
                        plan_index = int(plan_index_raw)
                    except Exception:
                        plan_index = None
                if plan_index is not None:
                    for entry in plan_entries:
                        try:
                            if int(entry.get("plan_index") or 0) == plan_index:
                                match_entry = entry
                                break
                        except Exception:
                            continue
                if match_entry is None:
                    for entry in plan_entries:
                        if entry.get("clip_filename") == filename or entry.get("output_filename") == filename:
                            match_entry = entry
                            break
                if match_entry:
                    match_entry["status"] = "pending"
                    match_entry["output_filename"] = None
                    match_entry["audio_start"] = None
                    match_entry["audio_end"] = None
                    match_entry.pop("render_job_id", None)
                    match_entry.pop("render_error", None)
                    try:
                        _write_plan_entries(row[0], plan_entries)
                    except Exception as exc:
                        current_app.logger.warning("Failed to update plan file after delete: %s", exc)
                    try:
                        clear_done_job_cache_for_plan(
                            user_id=str(_active_editor_context()["owner_user_id"] or ""),
                            source_video_id=str(row[0] or ""),
                            plan_index=int(match_entry.get("plan_index") or 0),
                        )
                    except Exception as exc:
                        current_app.logger.warning("Failed to clear render cache after delete: %s", exc)
            message = f"Deleted clip: {filename}"
            success = True
        else:
            message = "Clip not found."
    except Exception as e:
        message = f"Delete failed: {e}"

    if ajax_request:
        status = 200 if success else 400
        return jsonify(success=success, message=message, plan_index=plan_index_raw, filename=filename), status

    flash(message, "success" if success else "warning")

    return redirect(url_for("video_shorts_bp.generate_short", video_pk=video_pk))


@video_shorts_bp.route("/generate/<int:video_pk>/remove_plan_entry", methods=["POST"])
def remove_plan_entry(video_pk):
    plan_index_raw = (request.form.get("plan_index") or "").strip()
    filename = (request.form.get("filename") or "").strip()
    ajax_request = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    if not plan_index_raw and not filename:
        message = "Plan entry index is missing."
        if ajax_request:
            return jsonify(success=False, message=message), 400
        flash(message, "danger")
        return redirect(url_for("video_shorts_bp.generate_short", video_pk=video_pk))

    video_id = _resolve_video_id_from_pk(video_pk)
    if not video_id:
        message = "Video not found."
        if ajax_request:
            return jsonify(success=False, message=message), 404
        flash(message, "danger")
        return redirect(url_for("video_shorts_bp.channels_page"))

    plan_entries = _load_plan_entries(video_id)
    if not plan_entries:
        message = "Plan data not found."
        if ajax_request:
            return jsonify(success=False, message=message), 404
        flash(message, "warning")
        return redirect(url_for("video_shorts_bp.generate_short", video_pk=video_pk))

    target_entry = None
    if plan_index_raw:
        for entry in plan_entries:
            entry_index = entry.get("plan_index")
            if entry_index is None:
                continue
            if str(entry_index) == plan_index_raw:
                target_entry = entry
                break
    if not target_entry and filename:
        for entry in plan_entries:
            if entry.get("clip_filename") == filename or entry.get("output_filename") == filename:
                target_entry = entry
                break

    if not target_entry:
        message = "Plan entry not found."
        if ajax_request:
            return jsonify(success=False, message=message), 404
        flash(message, "warning")
        return redirect(url_for("video_shorts_bp.generate_short", video_pk=video_pk))

    target_plan_index = None
    try:
        target_plan_index = int(target_entry.get("plan_index")) if target_entry.get("plan_index") is not None else None
    except Exception:
        target_plan_index = None

    clip_names = set()
    for key in ("clip_filename", "output_filename"):
        val = target_entry.get(key)
        if isinstance(val, str) and val:
            clip_names.add(val)

    try:
        plan_entries.remove(target_entry)
        _write_plan_entries(video_id, plan_entries)
    except Exception as exc:
        current_app.logger.warning("Failed to remove plan entry for %s: %s", video_id, exc)
        message = "Unable to remove the plan section."
        if ajax_request:
            return jsonify(success=False, message=message), 500
        flash(message, "danger")
        return redirect(url_for("video_shorts_bp.generate_short", video_pk=video_pk))

    if clip_names:
        for clip_name in clip_names:
            try:
                _delete_short_media(clip_name)
            except Exception as exc:
                current_app.logger.warning("Failed to delete clip media %s for %s: %s", clip_name, video_id, exc)
    try:
        if target_plan_index is not None:
            clear_done_job_cache_for_plan(
                user_id=str(_active_editor_context()["owner_user_id"] or ""),
                source_video_id=str(video_id or ""),
                plan_index=target_plan_index,
            )
    except Exception as exc:
        current_app.logger.warning("Failed to clear render cache after plan removal: %s", exc)

    if ajax_request:
        return jsonify(success=True, plan_index=target_entry.get("plan_index"), message="Plan section removed.")
    flash("Plan section removed.", "success")
    return redirect(url_for("video_shorts_bp.generate_short", video_pk=video_pk))


@video_shorts_bp.route("/generate/<int:video_pk>/delete_ai_suggestions", methods=["POST"])
def delete_ai_suggestions(video_pk):
    video_id = _resolve_video_id_from_pk(video_pk)
    if not video_id:
        flash("Video not found.", "danger")
        return redirect(url_for("video_shorts_bp.channels_page"))

    plan_entries = _load_plan_entries(video_id)
    if not plan_entries:
        flash("Plan data not found.", "warning")
        return redirect(url_for("video_shorts_bp.generate_short", video_pk=video_pk))

    removed_entries = [entry for entry in plan_entries if _is_removable_ai_suggestion(entry)]
    if not removed_entries:
        flash("No AI suggestions to remove.", "warning")
        return redirect(url_for("video_shorts_bp.generate_short", video_pk=video_pk))

    remaining_entries = [entry for entry in plan_entries if not _is_removable_ai_suggestion(entry)]
    remaining_entries = _reindex_v1_plan_entries(video_id, remaining_entries)
    try:
        _write_plan_entries(video_id, remaining_entries)
    except Exception as exc:
        current_app.logger.warning("Failed to remove AI suggestions for %s: %s", video_id, exc)
        flash("Unable to remove AI suggestions.", "danger")
        return redirect(url_for("video_shorts_bp.generate_short", video_pk=video_pk))

    clip_names = set()
    for entry in removed_entries:
        for key in ("clip_filename", "output_filename"):
            val = entry.get(key)
            if isinstance(val, str) and val:
                clip_names.add(val)
    if clip_names:
        shorts_base = SHORTS_DIR.resolve()
        for clip_name in clip_names:
            target_path = (SHORTS_DIR / clip_name).resolve()
            if str(target_path).startswith(str(shorts_base)) and target_path.exists():
                try:
                    target_path.unlink()
                except Exception:
                    current_app.logger.warning("Failed to delete AI suggestion clip %s", target_path)

    flash(f"Removed {len(removed_entries)} AI-suggested clips.", "success")
    return redirect(url_for("video_shorts_bp.generate_short", video_pk=video_pk))


@video_shorts_bp.route("/generate/<int:video_pk>/add_clip_section", methods=["POST"])
def add_clip_section(video_pk):
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    editor_context = _active_editor_context()

    def _respond(message, success=False, status=200, category="info", extras=None, redirect_to=None):
        if is_ajax:
            payload = {"success": success, "message": message}
            if extras:
                payload.update(extras)
            return jsonify(payload), status
        flash(message, category)
        target = redirect_to or url_for("video_shorts_bp.generate_short", video_pk=video_pk)
        return redirect(target)

    start_time_raw = (request.form.get("start_time") or "").strip()
    end_time_raw = (request.form.get("end_time") or "").strip()
    title = (request.form.get("title") or "").strip()

    if not start_time_raw or not end_time_raw:
        return _respond("Start and End are required.", status=400, category="warning")

    start_time = _parse_time_input(start_time_raw)
    if start_time is None or start_time < 0:
        return _respond(
            "Start format is invalid. Use MM:SS.mmm or seconds.",
            status=400,
            category="warning",
        )
    end_time = _parse_time_input(end_time_raw)
    if end_time is None or end_time < 0:
        return _respond(
            "End format is invalid. Use MM:SS.mmm or seconds.",
            status=400,
            category="warning",
        )
    if end_time <= start_time:
        return _respond("End time must be greater than Start.", status=400, category="warning")

    conn = get_db_readonly()
    row = _fetch_scoped_video_row(conn, video_pk, "video_id, duration_seconds")
    conn.close()
    if not row:
        return _respond(
            "Video not found",
            status=404,
            category="danger",
            redirect_to=url_for("video_shorts_bp.channels_page"),
        )
    video_id, duration_seconds = row
    duration = _to_float(duration_seconds) if duration_seconds is not None else None
    if duration is not None:
        if start_time > duration:
            return _respond("Start time exceeds video duration.", status=400, category="warning")
        if end_time > duration:
            return _respond("End time exceeds video duration.", status=400, category="warning")

    plan_entries = _load_plan_entries(video_id) or []

    next_plan_index = 1
    existing_indexes = []
    for entry in plan_entries:
        try:
            existing_indexes.append(int(entry.get("plan_index") or 0))
        except Exception:
            continue
    if existing_indexes:
        next_plan_index = max(existing_indexes) + 1

    transcript_full = ""
    segments = []
    conn_transcript = None
    try:
        conn_transcript = get_db_readonly()
        _, segments = _fetch_transcript(conn_transcript, video_id)
        if segments:
            transcript_full = build_transcript_for_range(
                segments,
                start_time,
                end_time,
                prefer_tr=True,
            ) or ""
    except Exception as exc:
        current_app.logger.warning(
            "Failed to build transcript for manual clip %s [%.3f, %.3f]: %s",
            video_id,
            start_time,
            end_time,
            exc,
        )
    finally:
        if conn_transcript:
            try:
                conn_transcript.close()
            except Exception:
                pass

    clip_title = title or _build_placeholder_clip_title(transcript_full, next_plan_index)
    inferred_language = _infer_clip_language_from_segments(
        segments,
        start_time,
        end_time,
        excerpt=transcript_full or clip_title,
    )
    new_entry = {
        "origin": "manual",
        "plan_index": next_plan_index,
        "title": clip_title,
        "start": round(start_time, 3),
        "end": round(end_time, 3),
        "clip_filename": f"{next_plan_index}_{video_id}.mp4",
        "status": "pending",
        "transcript_full": transcript_full,
        "excerpt": transcript_full,
    }
    if inferred_language:
        new_entry["language"] = inferred_language
    plan_entries.insert(0, new_entry)
    try:
        _write_plan_entries(video_id, plan_entries)
    except Exception as exc:
        current_app.logger.warning("Failed to add manual plan entry for %s: %s", video_id, exc)
        return _respond("The new clip section could not be saved.", status=500, category="danger")

    _schedule_async_clip_title_suggestion(
        video_id=video_id,
        plan_index=next_plan_index,
        excerpt=transcript_full,
        placeholder_title=clip_title,
        user_id=editor_context["owner_user_id"],
        language_hint=_infer_clip_language_from_segments(
            segments,
            start_time,
            end_time,
            excerpt=transcript_full,
        ),
        app_obj=current_app._get_current_object(),
    )

    return _respond(
        f"Clip section added (#{next_plan_index}).",
        success=True,
        category="success",
        extras={
            "plan_index": next_plan_index,
            "title": clip_title,
            "transcript_full": transcript_full,
            "excerpt": transcript_full,
        },
    )


@video_shorts_bp.route("/generate/<int:video_pk>/adjust_clip_timing", methods=["POST"])
def adjust_clip_timing(video_pk):
    plan_index_raw = request.form.get("plan_index")
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"

    def _respond(message, success=False, status=200, category="info", extras=None, redirect_to=None):
        if is_ajax:
            payload = {"success": success, "message": message}
            if extras:
                payload.update(extras)
            return jsonify(payload), status
        flash(message, category)
        target = redirect_to or url_for("video_shorts_bp.generate_short", video_pk=video_pk)
        return redirect(target)

    if not plan_index_raw:
        return _respond("Clip index is missing.", status=400, category="danger")
    try:
        plan_index = int(plan_index_raw)
    except Exception:
        return _respond("Clip index is invalid.", status=400, category="danger")

    title_raw = request.form.get("title")
    title_candidate = title_raw.strip() if title_raw is not None else ""
    start_time_raw = (request.form.get("start_time") or "").strip()
    end_time_raw = (request.form.get("end_time") or "").strip()
    start_time = None
    end_time = None
    if start_time_raw:
        start_time = _parse_time_input(start_time_raw)
        if start_time is None:
            return _respond(
                "Start time format is invalid. Use MM:SS.mmm or a numeric seconds value.",
                status=400,
                category="warning",
            )
        if start_time < 0:
            return _respond("Start time cannot be negative.", status=400, category="warning")
    if end_time_raw:
        end_time = _parse_time_input(end_time_raw)
        if end_time is None:
            return _respond(
                "End time format is invalid. Use MM:SS.mmm or a numeric seconds value.",
                status=400,
                category="warning",
            )
        if end_time < 0:
            return _respond("End time cannot be negative.", status=400, category="warning")

    if start_time is None and end_time is None and not title_candidate:
        return _respond(
            "Enter at least one time or a valid title to update the clip.",
            status=400,
            category="warning",
        )

    conn = get_db_readonly()
    row = _fetch_scoped_video_row(conn, video_pk, "video_id, duration_seconds")
    conn.close()
    if not row:
        return _respond(
            "Video not found",
            status=404,
            category="danger",
            redirect_to=url_for("video_shorts_bp.channels_page"),
        )

    video_id, duration_seconds = row
    duration = _to_float(duration_seconds) if duration_seconds is not None else None

    plan_path = SHORTS_DIR / f"{video_id}_plan.json"
    if not plan_path.exists():
        return _respond("Clip plan file not found; regenerate the plan first.", status=404, category="warning")

    try:
        plan_data = json.loads(plan_path.read_text())
    except Exception as exc:
        current_app.logger.warning("Failed to read plan file %s: %s", plan_path, exc)
        return _respond("Could not read the clip plan file.", status=500, category="danger")

    plan_entries = plan_data.get("plan") or plan_data.get("clips") or []
    plan_entry = None
    for idx, clip in enumerate(plan_entries, start=1):
        clip_index = clip.get("plan_index")
        try:
            clip_index = int(clip_index)
        except Exception:
            clip_index = idx
        if clip_index == plan_index:
            plan_entry = clip
            break

    if not plan_entry:
        return _respond("Selected clip was not found in the plan.", status=404, category="warning")

    orig_start = _to_float(plan_entry.get("start"))
    orig_end = _to_float(plan_entry.get("end"))
    if orig_start is None or orig_end is None:
        return _respond("Clip timing data is incomplete; regenerate the plan.", status=400, category="warning")

    new_start = orig_start
    if start_time is not None:
        new_start = start_time
    new_end = orig_end
    if end_time is not None:
        new_end = end_time

    if duration is not None:
        if start_time is not None and start_time > duration:
            return _respond("Start time exceeds video duration.", status=400, category="warning")
        if end_time is not None and end_time > duration:
            return _respond("End time exceeds video duration.", status=400, category="warning")

    if new_end <= new_start:
        return _respond(
            "End time must be greater than start time.",
            status=400,
            category="warning",
        )

    MIN_DURATION = 0.05
    if new_end - new_start <= MIN_DURATION:
        return _respond(
            "Time range would produce an invalid clip interval; try smaller values.",
            status=400,
            category="warning",
        )

    plan_entry["start"] = round(new_start, 3)
    plan_entry["end"] = round(new_end, 3)
    old_transcript = plan_entry.get("transcript_full")
    updated_transcript = None
    segments = []
    conn_transcript = None
    try:
        conn_transcript = get_db_readonly()
        _, segments = _fetch_transcript(conn_transcript, video_id)
    except Exception:
        segments = []
    finally:
        if conn_transcript:
            try:
                conn_transcript.close()
            except Exception:
                pass
    if segments:
        try:
            updated_transcript = build_transcript_for_range(
                segments,
                new_start,
                new_end,
                prefer_tr=True,
            )
        except Exception as exc:
            current_app.logger.warning(
                "build_transcript_for_range failed for %s [%.3f, %.3f]: %s",
                video_id,
                new_start,
                new_end,
                exc,
            )
    if isinstance(updated_transcript, str) and updated_transcript.strip():
        plan_entry["transcript_full"] = updated_transcript
    else:
        plan_entry["transcript_full"] = old_transcript
        current_app.logger.info(
            "Transcript for clip %s kept as is because new transcript was empty. Range=%.3f-%.3f",
            plan_index,
            new_start,
            new_end,
        )
    if title_candidate:
        plan_entry["title"] = title_candidate

    try:
        _write_plan_entries(video_id, plan_entries)
    except Exception as exc:
        current_app.logger.warning("Failed to write plan file %s: %s", plan_path, exc)
        return _respond("Could not save the adjusted clip timings.", status=500, category="danger")

    start_label = _format_time_label(new_start) or f"{new_start:.2f}"
    end_label = _format_time_label(new_end) or f"{new_end:.2f}"
    duration_val = max(new_end - new_start, 0.0)
    duration_label = _format_time_label(duration_val) or f"{duration_val:.2f}"
    message = f"Clip #{plan_index} updated: {start_label} → {end_label}"
    return _respond(
        message,
        success=True,
        category="success",
        extras={
            "plan_index": plan_index,
            "start_label": start_label,
            "end_label": end_label,
            "duration_label": duration_label,
            "start": round(new_start, 3),
            "end": round(new_end, 3),
            "duration": round(duration_val, 3),
        },
    )


@video_shorts_bp.route("/generate/<int:video_pk>/update_clip_title", methods=["POST"])
def update_clip_title(video_pk):
    new_title = (request.form.get("title") or "").strip()
    if not new_title:
        flash("Başlık boş olamaz.", "warning")
        return redirect(url_for("video_shorts_bp.generate_short", video_pk=video_pk))

    plan_index_raw = request.form.get("plan_index")
    if not plan_index_raw:
        flash("Clip index is missing.", "danger")
        return redirect(url_for("video_shorts_bp.generate_short", video_pk=video_pk))

    try:
        plan_index = int(plan_index_raw)
    except Exception:
        flash("Clip index is invalid.", "danger")
        return redirect(url_for("video_shorts_bp.generate_short", video_pk=video_pk))

    conn = get_db_readonly()
    row = _fetch_scoped_video_row(conn, video_pk, "video_id")
    conn.close()
    if not row:
        flash("Video not found", "danger")
        return redirect(url_for("video_shorts_bp.channels_page"))

    video_id = row[0]
    plan_path = SHORTS_DIR / f"{video_id}_plan.json"
    if not plan_path.exists():
        flash("Clip plan file not found; regenerate the plan first.", "warning")
        return redirect(url_for("video_shorts_bp.generate_short", video_pk=video_pk))

    try:
        plan_data = json.loads(plan_path.read_text())
    except Exception as exc:
        current_app.logger.warning("Failed to read plan file %s: %s", plan_path, exc)
        flash("Could not read the clip plan file.", "danger")
        return redirect(url_for("video_shorts_bp.generate_short", video_pk=video_pk))

    plan_entries = plan_data.get("plan") or plan_data.get("clips") or []
    plan_entry = None
    for idx, clip in enumerate(plan_entries, start=1):
        clip_index = clip.get("plan_index")
        try:
            clip_index = int(clip_index)
        except Exception:
            clip_index = idx
        if clip_index == plan_index:
            plan_entry = clip
            break

    if not plan_entry:
        flash("Selected clip was not found in the plan.", "warning")
        return redirect(url_for("video_shorts_bp.generate_short", video_pk=video_pk))

    plan_entry["title"] = new_title
    try:
        _write_plan_entries(video_id, plan_entries)
    except Exception as exc:
        current_app.logger.warning("Failed to write plan file %s: %s", plan_path, exc)
        flash("Could not save the updated clip title.", "danger")
        return redirect(url_for("video_shorts_bp.generate_short", video_pk=video_pk))

    flash("Klip başlığı güncellendi.", "success")
    return redirect(url_for("video_shorts_bp.generate_short", video_pk=video_pk))


@video_shorts_bp.route("/generate/<int:video_pk>/update_clip_category", methods=["POST"])
def update_clip_category(video_pk):
    current_user = getattr(g, "vs_current_user", None)
    if not current_user:
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify(success=False, message="Authentication required"), 403
        return redirect(url_for("video_shorts_bp.login", next=request.url))
    category = (request.form.get("category") or "").strip()
    category_options = _load_category_options(_active_editor_context()["owner_user_id"])
    plan_index_raw = request.form.get("plan_index")
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"

    def _respond(message: str, success: bool = False, status: int = 200, category_level: str = "info"):
        if is_ajax:
            return jsonify(success=success, message=message), status
        flash(message, category_level)
        return redirect(url_for("video_shorts_bp.generate_short", video_pk=video_pk))

    if not plan_index_raw:
        return _respond("Clip index is missing.", status=400, category_level="danger")
    try:
        plan_index = int(plan_index_raw)
    except Exception:
        return _respond("Clip index is invalid.", status=400, category_level="danger")

    if category and category not in category_options:
        return _respond("Kategori listede yok.", status=400, category_level="warning")

    conn = get_db_readonly()
    row = _fetch_scoped_video_row(conn, video_pk, "video_id")
    conn.close()
    if not row:
        if is_ajax:
            return jsonify(success=False, message="Video not found"), 404
        flash("Video not found", "danger")
        return redirect(url_for("video_shorts_bp.channels_page"))

    video_id = row[0]
    plan_path = SHORTS_DIR / f"{video_id}_plan.json"
    if not plan_path.exists():
        return _respond("Clip plan file not found; regenerate the plan first.", status=404, category_level="warning")

    try:
        plan_data = json.loads(plan_path.read_text())
    except Exception as exc:
        current_app.logger.warning("Failed to read plan file %s: %s", plan_path, exc)
        return _respond("Could not read the clip plan file.", status=500, category_level="danger")

    plan_entries = plan_data.get("plan") or plan_data.get("clips") or []
    plan_entry = None
    for idx, clip in enumerate(plan_entries, start=1):
        clip_index = clip.get("plan_index")
        try:
            clip_index = int(clip_index)
        except Exception:
            clip_index = idx
        if clip_index == plan_index:
            plan_entry = clip
            break

    if not plan_entry:
        return _respond("Selected clip was not found in the plan.", status=404, category_level="warning")

    if category:
        plan_entry["category"] = category
    else:
        plan_entry.pop("category", None)
    try:
        _write_plan_entries(video_id, plan_entries)
    except Exception as exc:
        current_app.logger.warning("Failed to write plan file %s: %s", plan_path, exc)
        return _respond("Kategori kaydedilemedi.", status=500, category_level="danger")

    return _respond("Kategori güncellendi.", success=True, category_level="success")


@video_shorts_bp.route("/generate/<int:video_pk>/suggest_clip_title", methods=["POST"])
def suggest_clip_title(video_pk):
    if not _openai_client:
        return jsonify(success=False, message="OPENAI_API_KEY missing"), 403
    plan_index = request.form.get("plan_index")
    if not plan_index:
        return jsonify(success=False, message="Plan index missing"), 400
    excerpt = (request.form.get("excerpt") or "").strip()
    expected_current_title = (request.form.get("expected_current_title") or "").strip()
    video_id = _resolve_video_id_from_pk(video_pk)
    if not video_id:
        return jsonify(success=False, message="Video not found"), 404
    entries = _load_plan_entries(video_id)
    plan_entry = _find_plan_entry(entries, plan_index)
    if not plan_entry:
        return jsonify(success=False, message="Clip not found"), 404
    if not excerpt:
        excerpt = plan_entry.get("excerpt") or plan_entry.get("transcript_full") or ""
    excerpt = excerpt[:2000]
    suggestion_excerpt = str(
        plan_entry.get("transcript_full")
        or plan_entry.get("excerpt")
        or excerpt
        or ""
    ).strip()
    existing_title = str(plan_entry.get("title") or "").strip()
    if expected_current_title and existing_title != expected_current_title:
        return jsonify(success=True, skipped=True, title=existing_title)
    language_hint = _normalize_title_prompt_language(
        plan_entry.get("language") or plan_entry.get("lang")
    )
    video_info = None
    if not language_hint:
        video_info = _fetch_video_with_transcript(video_pk)
        if video_info:
            _, _, _, _, segments = video_info
            language_hint = _infer_clip_language_from_segments(
                segments,
                plan_entry.get("start"),
                plan_entry.get("end"),
                excerpt=excerpt,
            )
    try:
        new_title = _request_short_title_suggestion(
            suggestion_excerpt or excerpt,
            user_id=_active_editor_context()["owner_user_id"],
            current_video_id=video_id,
            language_hint=language_hint,
        )
    except Exception as exc:
        current_app.logger.exception("Title suggestion failed: %s", exc)
        return jsonify(success=False, message="LLM hata verdi"), 500
    if not new_title:
        return jsonify(success=False, message="Başlık önerisi alınamadı"), 500
    plan_entry["title"] = new_title
    try:
        _write_plan_entries(video_id, entries)
    except Exception as exc:
        current_app.logger.warning("Failed to write plan for title suggestion: %s", exc)
        return jsonify(success=False, message="Başlık kaydedilemedi"), 500
    return jsonify(success=True, title=new_title)


@video_shorts_bp.route("/generate/<int:video_pk>/update_clip_subtitle", methods=["POST"])
def update_clip_subtitle(video_pk):
    ACTION_UPDATE = "update"
    ACTION_CORRECT = "correct"
    system_prompt = """
Sen bir altyazı düzeltme ajansın.

Görevin:
Sana verilen TÜRKÇE altyazı metnindeki açık yazım hatalarını ve yanlış yazılmış kelimeleri
mümkün olan EN AZ değişiklikle düzeltmek.

KURALLAR:
1) Metne YENİ kelime, cümle veya açıklama EKLEME.
2) Metinden hiçbir kelime veya cümle SİLME.
3) Cümleleri BÖLME veya BİRLEŞTİRME. Satır sonları varsa aynen koru.
4) Sadece bariz hataları düzelt:
   - Yanlış yazılmış kelimeler (ör: "iştahat" -> "içtihat")
   - Açık imla hataları (harf eksik, harf fazla vb.).
5) Noktalama işaretlerine sadece çok bariz bir yanlışlık varsa dokun.
6) Üslubu, anlamı ve kelime dizimini DEĞİŞTİRME.
7) Kelimenin doğru biçimini bulurken bağlama ve içerikle uyumlu olmasına dikkat et; önerdiğin kelime tek kelime, bağlamla çelişmeyen ve mevcut tonla uyumlu olmalı, ek, açıklama ya da ek bilgi ekleme.
8) ÇIKTI:
   - Sadece düzeltilmiş metni döndür.
   - Açıklama, özet, yorum, ek bilgi yazma.
"""
    action = (request.form.get("action") or ACTION_UPDATE).lower()
    request_subtitle = (request.form.get("subtitle") or "").strip()
    corrected_subtitle = None

    plan_index_raw = request.form.get("plan_index")
    if not plan_index_raw:
        flash("Clip index is missing.", "danger")
        return redirect(url_for("video_shorts_bp.generate_short", video_pk=video_pk))

    try:
        plan_index = int(plan_index_raw)
    except Exception:
        flash("Clip index is invalid.", "danger")
        return redirect(url_for("video_shorts_bp.generate_short", video_pk=video_pk))

    conn = get_db_readonly()
    row = _fetch_scoped_video_row(conn, video_pk, "video_id")
    conn.close()
    if not row:
        flash("Video not found", "danger")
        return redirect(url_for("video_shorts_bp.channels_page"))

    video_id = row[0]
    plan_path = SHORTS_DIR / f"{video_id}_plan.json"
    if not plan_path.exists():
        flash("Clip plan file not found; regenerate the plan first.", "warning")
        return redirect(url_for("video_shorts_bp.generate_short", video_pk=video_pk))

    try:
        plan_data = json.loads(plan_path.read_text())
    except Exception as exc:
        current_app.logger.warning("Failed to read plan file %s: %s", plan_path, exc)
        flash("Could not read the clip plan file.", "danger")
        return redirect(url_for("video_shorts_bp.generate_short", video_pk=video_pk))

    plan_entries = plan_data.get("plan") or plan_data.get("clips") or []
    plan_entry = None
    for idx, clip in enumerate(plan_entries, start=1):
        clip_index = clip.get("plan_index")
        try:
            clip_index = int(clip_index)
        except Exception:
            clip_index = idx
        if clip_index == plan_index:
            plan_entry = clip
            break

    if not plan_entry:
        flash("Selected clip was not found in the plan.", "warning")
        return redirect(url_for("video_shorts_bp.generate_short", video_pk=video_pk))

    if action == ACTION_CORRECT:
        if not _openai_client:
            flash("OPENAI_API_KEY missing; cannot correct subtitles automatically.", "danger")
            return redirect(url_for("video_shorts_bp.generate_short", video_pk=video_pk))
        if not request_subtitle:
            flash("Altyazı metni boş; önce metni yazın.", "warning")
            return redirect(url_for("video_shorts_bp.generate_short", video_pk=video_pk))
        try:
            resp = _openai_client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": request_subtitle},
                ],
                temperature=0.0,
            )
            corrected_subtitle = (resp.choices[0].message.content or "").strip()
        except Exception as exc:
            current_app.logger.exception("Subtitle correction failed: %s", exc)
            flash("LLM altyazıyı düzeltirken hata verdi.", "danger")
            return redirect(url_for("video_shorts_bp.generate_short", video_pk=video_pk))
        if not corrected_subtitle:
            flash("LLM boş bir çıktı döndürdü.", "warning")
            return redirect(url_for("video_shorts_bp.generate_short", video_pk=video_pk))

    new_subtitle = corrected_subtitle if action == ACTION_CORRECT else request_subtitle
    if action == ACTION_UPDATE and not request_subtitle:
        flash("Altyazı boş olamaz.", "warning")
        return redirect(url_for("video_shorts_bp.generate_short", video_pk=video_pk))
    plan_entry["transcript_full"] = new_subtitle
    plan_entry["transcript_full_custom"] = new_subtitle
    try:
        _write_plan_entries(video_id, plan_entries)
    except Exception as exc:
        current_app.logger.warning("Failed to write plan file %s: %s", plan_path, exc)
        flash("Could not save the updated clip subtitle.", "danger")
        return redirect(url_for("video_shorts_bp.generate_short", video_pk=video_pk))

    flash("Klip altyazı metni güncellendi.", "success")
    return redirect(url_for("video_shorts_bp.generate_short", video_pk=video_pk))


@video_shorts_bp.route("/youtube/connect")
def youtube_connect():
    return redirect(url_for("video_shorts_bp.social_connect"))


@video_shorts_bp.route("/social/connect")
def social_connect():
    current_user = getattr(g, "vs_current_user", None)
    brand_id = current_brand_id()
    show_autopilot_welcome = (
        request.args.get("autopilot_welcome") == "1"
        and str((current_user or {}).get("service_mode") or "").strip().lower() == "autopilot"
    )
    youtube_configured = bool(YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET and YOUTUBE_REDIRECT_URI)
    channel_info = None
    youtube_connected = False
    youtube_warning = None
    youtube_status = "not_connected"
    quota_active = False
    try:
        quota_conn = get_db_readonly()
    except duckdb.IOException:
        quota_conn = None
    if quota_conn is not None:
        try:
            quota_state = get_shorts_overview_quota_state(quota_conn)
            quota_active = bool(quota_state.get("active"))
        except Exception:
            quota_active = False
        finally:
            quota_conn.close()
    if youtube_configured:
        if quota_active:
            youtube_connected = has_refresh_token((current_user or {}).get("id"), brand_id=brand_id)
        else:
            channel_info, youtube_error_code = get_connected_channel_info((current_user or {}).get("id"), brand_id=brand_id)
            youtube_connected = bool(channel_info)
            if youtube_error_code == "invalid_grant":
                youtube_warning = "Connection expired — reconnect to keep publishing."
            elif youtube_error_code:
                youtube_warning = "Connection expired — reconnect to keep publishing."
            if not youtube_connected and not youtube_warning:
                youtube_connected = has_refresh_token((current_user or {}).get("id"), brand_id=brand_id)
    else:
        youtube_connected = has_refresh_token((current_user or {}).get("id"), brand_id=brand_id)
    if youtube_connected:
        youtube_status = "connected"
    elif youtube_warning:
        youtube_status = "reconnect_needed"

    instagram_info = None
    instagram_profile = None
    instagram_connected = False
    instagram_warning = None
    instagram_status = "not_connected"
    facebook_info = None
    facebook_connected = False
    facebook_warning = None
    facebook_status = "not_connected"
    if current_user:
        try:
            instagram_info = get_instagram_data(current_user["id"])
        except InstagramTokenStoreError as exc:
            current_app.logger.warning("Instagram token store unavailable: %s", exc)
            instagram_warning = "Connection expired — reconnect to keep publishing."
        if instagram_info:
            try:
                instagram_info = refresh_instagram_token_if_needed(user_id=current_user["id"], current=instagram_info) or instagram_info
            except Exception as exc:
                current_app.logger.warning("Instagram token refresh failed: %s", exc)
                instagram_warning = "Connection expired — reconnect to keep publishing."
            instagram_connected = _validate_instagram_connection(instagram_info)
            if instagram_connected:
                instagram_profile = _fetch_instagram_profile(
                    instagram_info.get("page_access_token"),
                    instagram_info.get("instagram_business_account_id"),
                )
                fetched_username = (instagram_profile or {}).get("username")
                fetched_account_type = (instagram_profile or {}).get("account_type")
                if (fetched_username and not instagram_info.get("instagram_username")) or (
                    fetched_account_type and fetched_account_type != instagram_info.get("instagram_account_type")
                ):
                    instagram_info["instagram_username"] = fetched_username
                    instagram_info["instagram_account_type"] = fetched_account_type or instagram_info.get("instagram_account_type")
                    try:
                        store_instagram_token(
                            user_id=current_user["id"],
                            page_access_token=instagram_info.get("page_access_token") or "",
                            instagram_business_account_id=instagram_info.get("instagram_business_account_id") or "",
                            instagram_username=fetched_username,
                            facebook_page_id=instagram_info.get("facebook_page_id") or "",
                            facebook_page_name=instagram_info.get("facebook_page_name") or "",
                            instagram_user_id=instagram_info.get("instagram_user_id") or instagram_info.get("instagram_business_account_id"),
                            instagram_account_type=instagram_info.get("instagram_account_type"),
                            expires_at=instagram_info.get("expires_at"),
                            scopes=instagram_info.get("scopes") or "",
                        )
                    except InstagramTokenStoreError as exc:
                        current_app.logger.warning("Failed to persist Instagram username: %s", exc)
            elif instagram_info.get("page_access_token") and instagram_info.get("instagram_business_account_id"):
                instagram_warning = _instagram_account_upgrade_message()
        if instagram_connected:
            instagram_status = "connected"
        elif instagram_warning:
            instagram_status = "reconnect_needed"
        try:
            facebook_info = get_facebook_page_data(current_user["id"])
        except FacebookTokenStoreError as exc:
            current_app.logger.warning("Facebook token store unavailable: %s", exc)
            facebook_warning = "Connection expired — reconnect to keep publishing."
        if facebook_info:
            facebook_connected = _validate_facebook_page_connection(facebook_info)
        if facebook_connected:
            facebook_status = "connected"
        elif facebook_warning:
            facebook_status = "reconnect_needed"
    instagram_business_id = (
        instagram_info.get("instagram_business_account_id") if instagram_info else None
    )
    tiktok_configured = bool(TIKTOK_CLIENT_KEY and TIKTOK_CLIENT_SECRET and TIKTOK_REDIRECT_URI)
    tiktok_info = None
    tiktok_connected = False
    tiktok_warning = None
    tiktok_connected_at = None
    tiktok_status = "not_connected"
    if current_user:
        try:
            tiktok_info = get_tiktok_data(current_user["id"])
        except TikTokTokenStoreError as exc:
            current_app.logger.warning("TikTok token store unavailable: %s", exc)
            tiktok_warning = "Connection expired — reconnect to keep publishing."
    if tiktok_info and tiktok_info.get("access_token"):
        if _is_token_expired(tiktok_info.get("expires_at")):
            tiktok_warning = "Connection expired — reconnect to keep publishing."
        else:
            tiktok_connected = True
            tiktok_connected_at = _format_simple_datetime(tiktok_info.get("updated_at"))
            if not (tiktok_info.get("display_name") or tiktok_info.get("username")):
                profile = _fetch_tiktok_profile(tiktok_info.get("access_token"))
                if profile:
                    try:
                        store_tiktok_token(
                            user_id=current_user["id"],
                            access_token=tiktok_info.get("access_token"),
                            refresh_token=tiktok_info.get("refresh_token"),
                            open_id=profile.get("open_id") or tiktok_info.get("open_id"),
                            username=profile.get("username") or tiktok_info.get("username"),
                            display_name=profile.get("display_name") or tiktok_info.get("display_name"),
                            scopes=tiktok_info.get("scopes") or "",
                            expires_at=tiktok_info.get("expires_at"),
                            refresh_expires_at=tiktok_info.get("refresh_expires_at"),
                        )
                        tiktok_info.update(
                            {
                                "open_id": profile.get("open_id") or tiktok_info.get("open_id"),
                                "username": profile.get("username") or tiktok_info.get("username"),
                                "display_name": profile.get("display_name") or tiktok_info.get("display_name"),
                            }
                        )
                    except TikTokTokenStoreError as exc:
                        current_app.logger.warning("Failed to persist TikTok profile: %s", exc)
    if tiktok_connected:
        tiktok_status = "connected"
    elif tiktok_warning:
        tiktok_status = "reconnect_needed"
    return render_template(
        "social_connect.html",
        youtube_configured=youtube_configured,
        youtube_connected=youtube_connected,
        youtube_status=youtube_status,
        channel=channel_info,
        instagram_connected=instagram_connected,
        instagram_status=instagram_status,
        instagram_info=instagram_info,
        instagram_business_id=instagram_business_id,
        instagram_profile=instagram_profile,
        api_base=IG_API_BASE,
        youtube_warning=youtube_warning,
        instagram_warning=instagram_warning,
        facebook_connected=facebook_connected,
        facebook_status=facebook_status,
        facebook_info=facebook_info,
        facebook_warning=facebook_warning,
        instagram_app_id=IG_APP_ID,
        facebook_app_id=FB_APP_ID,
        tiktok_configured=tiktok_configured,
        tiktok_connected=tiktok_connected,
        tiktok_status=tiktok_status,
        tiktok_info=tiktok_info,
        tiktok_warning=tiktok_warning,
        tiktok_connected_at=tiktok_connected_at,
        show_autopilot_welcome=show_autopilot_welcome,
    )


def _facebook_oauth_url(state: str) -> Optional[str]:
    if not (FB_APP_ID and FB_REDIRECT_URI):
        return None
    version = FB_API_BASE.rstrip("/").split("/")[-1]
    oauth_base = f"https://www.facebook.com/{version}/dialog/oauth"
    scopes = ",".join(s.strip() for s in FB_OAUTH_SCOPES.split(",") if s and s.strip())
    params = {
        "client_id": FB_APP_ID,
        "redirect_uri": FB_REDIRECT_URI,
        "state": state,
        "scope": scopes,
        "response_type": "code",
        "auth_type": "rerequest",
    }
    return f"{oauth_base}?{urlencode(params)}"


def _safe_oauth_next(value: Optional[str], fallback: str) -> str:
    raw = str(value or "").strip()
    if raw.startswith("/") and not raw.startswith("//"):
        return raw
    return fallback


@video_shorts_bp.route("/facebook/connect")
def facebook_connect():
    current_user = getattr(g, "vs_current_user", None)
    if not current_user:
        flash("Sign in to connect a Facebook Page.", "danger")
        return redirect(url_for("video_shorts_bp.login", next=request.url))
    if not (FB_APP_ID and FB_APP_SECRET and FB_REDIRECT_URI):
        flash("Facebook isn't configured yet. Please finish setup.", "danger")
        return redirect(url_for("video_shorts_bp.social_connect"))
    state_token = secrets.token_urlsafe(16)
    session["fb_oauth_state"] = {
        "nonce": state_token,
        "user_id": current_user.get("id"),
    }
    auth_url = _facebook_oauth_url(state_token)
    if not auth_url:
        flash("Could not build the Facebook OAuth URL.", "danger")
        return redirect(url_for("video_shorts_bp.social_connect"))
    return redirect(auth_url)


@video_shorts_bp.route("/facebook/oauth/callback")
def facebook_oauth_callback():
    safe_args = {}
    for key, value in request.args.items():
        safe_args[key] = "MASKED" if key == "code" else value
    current_app.logger.info("Facebook OAuth callback args: %s", safe_args)
    error = request.args.get("error")
    if error:
        error_reason = request.args.get("error_reason")
        error_desc = request.args.get("error_description")
        current_app.logger.warning(
            "Facebook OAuth error=%s reason=%s description=%s",
            error,
            error_reason,
            error_desc,
        )
        message = f"Facebook OAuth error: {error}"
        if error_desc:
            message = f"{message} ({error_desc})"
        flash(message, "danger")
        return redirect(url_for("video_shorts_bp.social_connect"))

    state = request.args.get("state")
    saved_state = session.pop("fb_oauth_state", None)
    expected_state = saved_state.get("nonce") if isinstance(saved_state, dict) else None
    if not expected_state or state != expected_state:
        current_app.logger.warning(
            "Facebook OAuth state mismatch: %s vs %s",
            state,
            expected_state,
        )
        flash("Facebook OAuth verification failed.", "danger")
        return redirect(url_for("video_shorts_bp.social_connect"))
    user_id = saved_state.get("user_id") if isinstance(saved_state, dict) else None
    if not user_id:
        current_app.logger.warning("Facebook OAuth missing user_id in state payload.")
        flash("Could not read the Facebook OAuth code.", "danger")
        return redirect(url_for("video_shorts_bp.social_connect"))

    code = request.args.get("code")
    if not code:
        flash("Could not read the Facebook OAuth code.", "danger")
        return redirect(url_for("video_shorts_bp.social_connect"))

    token_url = f"{FB_API_BASE.rstrip('/')}/oauth/access_token"
    short_params = {
        "client_id": FB_APP_ID,
        "redirect_uri": FB_REDIRECT_URI,
        "client_secret": FB_APP_SECRET,
        "code": code,
    }
    short_resp = None
    try:
        short_resp = requests.get(token_url, params=short_params, timeout=12)
        short_resp.raise_for_status()
        short_data = short_resp.json()
    except Exception as exc:
        current_app.logger.exception("Facebook token exchange failed: %s", exc)
        flash("Could not get a Facebook token.", "danger")
        return redirect(url_for("video_shorts_bp.social_connect"))

    short_token = short_data.get("access_token")
    if not short_token:
        flash("Facebook returned an unexpected short-lived token response.", "danger")
        return redirect(url_for("video_shorts_bp.social_connect"))

    long_params = {
        "grant_type": "fb_exchange_token",
        "client_id": FB_APP_ID,
        "client_secret": FB_APP_SECRET,
        "fb_exchange_token": short_token,
    }
    long_resp = None
    try:
        long_resp = requests.get(token_url, params=long_params, timeout=12)
        long_resp.raise_for_status()
        long_data = long_resp.json()
    except Exception as exc:
        payload = {}
        if long_resp is not None:
            try:
                payload = long_resp.json()
            except Exception:
                pass
        current_app.logger.exception(
            "Facebook long token exchange failed (status=%s payload=%s): %s",
            getattr(long_resp, "status_code", None),
            payload,
            exc,
        )
        flash("Could not get a long-lived Facebook token.", "danger")
        return redirect(url_for("video_shorts_bp.social_connect"))

    long_token = long_data.get("access_token")
    expires_at = None
    expires_in = long_data.get("expires_in")
    if expires_in:
        try:
            expires_seconds = int(expires_in)
            expires_at = (datetime.utcnow() + timedelta(seconds=expires_seconds)).isoformat()
        except Exception:
            expires_at = None

    if not long_token:
        flash("Could not validate the Facebook OAuth code.", "danger")
        return redirect(url_for("video_shorts_bp.social_connect"))

    debug_payload = _log_facebook_debug_token(long_token)
    current_app.logger.info(
        "Facebook OAuth token_scopes=%s token_type=%s",
        debug_payload.get("scopes"),
        debug_payload.get("type"),
    )

    fb_user_id = None
    try:
        me_resp = requests.get(
            f"{FB_API_BASE.rstrip('/')}/me",
            params={"access_token": long_token, "fields": "id,name"},
            timeout=8,
        )
        me_resp.raise_for_status()
        fb_user_id = (me_resp.json() or {}).get("id")
    except Exception as exc:
        current_app.logger.warning("Facebook /me failed: %s", exc)

    try:
        perms_resp = requests.get(
            f"{FB_API_BASE.rstrip('/')}/me/permissions",
            params={"access_token": long_token},
            timeout=8,
        )
        perms_payload = perms_resp.json() if perms_resp is not None else {}
        current_app.logger.info(
            "Facebook OAuth /me/permissions status=%s body=%s",
            getattr(perms_resp, "status_code", None),
            perms_payload,
        )
    except Exception as exc:
        current_app.logger.warning("Facebook /me/permissions log failed: %s", exc)

    pages_resp = None
    pages_data = []
    try:
        pages_resp = requests.get(
            f"{FB_API_BASE.rstrip('/')}/me/accounts",
            params={"access_token": long_token, "fields": "id,name,access_token"},
            timeout=12,
        )
        pages_resp.raise_for_status()
        pages_payload = pages_resp.json()
        pages_data = (pages_payload or {}).get("data") or []
    except Exception as exc:
        error_payload = {}
        try:
            error_payload = pages_resp.json()
        except Exception:
            pass
        error_obj = error_payload.get("error") if isinstance(error_payload, dict) else None
        current_app.logger.warning(
            "Facebook page list fetch failed: %s | status=%s message=%s code=%s subcode=%s payload=%s",
            exc,
            getattr(pages_resp, "status_code", None),
            error_obj.get("message") if isinstance(error_obj, dict) else None,
            error_obj.get("code") if isinstance(error_obj, dict) else None,
            error_obj.get("error_subcode") if isinstance(error_obj, dict) else None,
            error_payload,
        )
        flash("Could not load your Facebook Pages. Please try again.", "danger")
        return redirect(url_for("video_shorts_bp.social_connect"))

    current_app.logger.info(
        "Facebook /me/accounts response status=%s length=%s payload=%s",
        getattr(pages_resp, "status_code", None),
        len(pages_data),
        pages_payload,
    )
    current_app.logger.info(
        "Facebook /me/accounts page list=%s",
        [{"id": page.get("id"), "name": page.get("name")} for page in pages_data],
    )

    if not pages_data and FB_TARGET_PAGE_ID:
        target_resp = None
        try:
            target_resp = requests.get(
                f"{FB_API_BASE.rstrip('/')}/{FB_TARGET_PAGE_ID}",
                params={"fields": "id,name,access_token", "access_token": long_token},
                timeout=8,
            )
            target_resp.raise_for_status()
            payload = target_resp.json() or {}
            if payload.get("access_token"):
                pages_data = [payload]
                current_app.logger.info(
                    "Facebook /me/accounts fallback page_id=%s name=%s",
                    payload.get("id"),
                    payload.get("name"),
                )
        except Exception as exc:
            current_app.logger.warning(
                "Facebook /me/accounts fallback failed page_id=%s status=%s: %s",
                FB_TARGET_PAGE_ID,
                getattr(target_resp, "status_code", None),
                exc,
            )

    if not pages_data:
        flash("No Facebook Pages were returned. Check the Page permissions and try again.", "warning")
        return redirect(url_for("video_shorts_bp.social_connect"))

    target_page = None
    if FB_TARGET_PAGE_ID:
        for page in pages_data:
            if str(page.get("id")) == str(FB_TARGET_PAGE_ID):
                target_page = page
                break
        if not target_page:
            flash("The target Facebook Page could not be found. Check the selected Page and permissions.", "warning")
            return redirect(url_for("video_shorts_bp.social_connect"))
    elif len(pages_data) == 1:
        target_page = pages_data[0]
    else:
        flash("Multiple Facebook Pages were found. Set a target Page first.", "warning")
        return redirect(url_for("video_shorts_bp.social_connect"))

    page_access_token = target_page.get("access_token")
    if not page_access_token:
        flash("Could not get the Facebook Page access token.", "danger")
        return redirect(url_for("video_shorts_bp.social_connect"))

    store_facebook_page_token(
        user_id=user_id,
        fb_user_id=fb_user_id,
        page_id=target_page.get("id") or "",
        page_name=target_page.get("name"),
        page_access_token=page_access_token,
        expires_at=expires_at,
        scopes=FB_OAUTH_SCOPES,
    )
    track_event(user_id, "channel_connected", platform="facebook")
    saved = get_facebook_page_data(user_id)
    current_app.logger.info(
        "Facebook OAuth saved db_record_id=%s fb_user_id=%s page_id=%s token_tail=%s updated_at=%s",
        (saved or {}).get("user_id"),
        (saved or {}).get("fb_user_id"),
        (saved or {}).get("page_id"),
        _token_tail((saved or {}).get("page_access_token")),
        (saved or {}).get("updated_at"),
    )
    flash("Facebook Page connected.", "success")
    return redirect(url_for("video_shorts_bp.social_connect"))


@video_shorts_bp.route("/facebook/disconnect", methods=["POST"])
def facebook_disconnect():
    current_user = getattr(g, "vs_current_user", None)
    if not current_user:
        flash("Sign in to disconnect Facebook.", "danger")
        return redirect(url_for("video_shorts_bp.login", next=request.url))
    clear_facebook_page_token(current_user.get("id"))
    flash("Facebook Page disconnected.", "info")
    return redirect(url_for("video_shorts_bp.social_connect"))


def _instagram_oauth_url(state: str) -> Optional[str]:
    if not (IG_APP_ID and IG_REDIRECT_URI):
        return None
    scopes = ",".join(
        s.strip() for s in IG_OAUTH_SCOPES.split(",") if s and s.strip()
    )
    params = {
        "client_id": IG_APP_ID,
        "redirect_uri": IG_REDIRECT_URI,
        "state": state,
        "scope": scopes,
        "response_type": "code",
    }
    return f"{IG_AUTH_BASE}?{urlencode(params)}"


def _log_instagram_auth_redirect(auth_url: str):
    if not auth_url:
        return
    try:
        parsed = urlparse(auth_url)
        query_params = parse_qsl(parsed.query, keep_blank_values=True)
        sanitized = []
        for key, value in query_params:
            if key == "state":
                sanitized.append((key, "MASKED"))
            else:
                sanitized.append((key, value))
        safe_url = parsed._replace(query=urlencode(sanitized)).geturl()
        current_app.logger.info("Instagram OAuth redirect URL: %s", safe_url)
    except Exception as exc:
        current_app.logger.warning("Unable to log Instagram OAuth URL: %s", exc)


def _token_tail(token: Optional[str]) -> Optional[str]:
    if not token:
        return None
    return token[-6:]


def _validate_instagram_connection(info: Optional[Dict[str, Any]]) -> bool:
    if not info:
        return False
    token = info.get("page_access_token")
    business_id = info.get("instagram_business_account_id")
    account_type = str(info.get("instagram_account_type") or "").upper()
    if not token or not business_id:
        return False
    return account_type in {"BUSINESS", "CREATOR"}


def _instagram_account_upgrade_message() -> str:
    return "Instagram bağlamak için Business veya Creator hesabı gerekiyor — Instagram ayarlarından ücretsiz geçebilirsin."


def _validate_facebook_page_connection(info: Optional[Dict[str, Any]]) -> bool:
    if not info:
        return False
    if not info.get("page_access_token") or not info.get("page_id"):
        return False
    return True


def _fetch_instagram_profile(page_access_token: Optional[str], instagram_business_id: Optional[str]) -> Optional[Dict[str, Any]]:
    if not page_access_token or not instagram_business_id:
        return None
    resp = None
    try:
        resp = requests.get(
            f"{IG_GRAPH_API_BASE.rstrip('/')}/{instagram_business_id}",
            params={"fields": "id,username,name,profile_picture_url,account_type", "access_token": page_access_token},
            timeout=8,
        )
        resp.raise_for_status()
        payload = resp.json() or {}
        profile = {
            "id": payload.get("id") or instagram_business_id,
            "username": payload.get("username"),
            "name": payload.get("name"),
            "profile_picture_url": payload.get("profile_picture_url"),
            "account_type": payload.get("account_type"),
        }
        current_app.logger.info(
            "Fetched Instagram profile: ig_id=%s username=%s account_type=%s",
            instagram_business_id,
            profile["username"],
            profile["account_type"],
        )
        return profile
    except Exception as exc:
        payload = {}
        body_text = ""
        if resp is not None:
            try:
                payload = resp.json() or {}
            except Exception:
                payload = {}
            try:
                body_text = (resp.text or "").strip()
            except Exception:
                body_text = ""
        current_app.logger.warning(
            "Instagram profile lookup failed: %s | ig_id=%s status=%s payload=%s body=%s",
            exc,
            instagram_business_id,
            getattr(resp, "status_code", None),
            payload,
            body_text,
        )
        return None


def _log_instagram_debug_token(user_access_token: str) -> Dict[str, Any]:
    return {}


def _log_instagram_permissions(access_token: str):
    current_app.logger.info("Instagram OAuth scopes=%s", IG_OAUTH_SCOPES)


def _log_facebook_debug_token(user_access_token: str) -> Dict[str, Any]:
    if not (FB_APP_ID and FB_APP_SECRET):
        return {}
    try:
        resp = requests.get(
            f"{FB_API_BASE.rstrip('/')}/debug_token",
            params={
                "input_token": user_access_token,
                "access_token": f"{FB_APP_ID}|{FB_APP_SECRET}",
            },
            timeout=8,
        )
        resp.raise_for_status()
        payload = resp.json().get("data") or {}
        current_app.logger.info(
            "Facebook OAuth debug_token: %s",
            {
                "is_valid": payload.get("is_valid"),
                "user_id": payload.get("user_id"),
                "expires_at": payload.get("expires_at"),
                "scopes": payload.get("scopes"),
                "granular_scopes": payload.get("granular_scopes"),
                "type": payload.get("type"),
            },
        )
        return payload
    except Exception as exc:
        current_app.logger.warning("Facebook debug_token fetch failed: %s", exc)
    return {}


def _log_instagram_connect_validation(
    context: str,
    page_id: Optional[str],
    ig_id: Optional[str],
    token: Optional[str],
) -> None:
    if not ig_id or not token:
        current_app.logger.warning(
            "Instagram OAuth verify skipped context=%s ig_id=%s token_present=%s",
            context,
            ig_id,
            bool(token),
        )
        return
    ig_resp = None
    try:
        ig_resp = requests.get(
            f"{IG_GRAPH_API_BASE.rstrip('/')}/{ig_id}",
            params={"fields": "id,username,account_type", "access_token": token},
            timeout=8,
        )
        ig_resp.raise_for_status()
        ig_payload = ig_resp.json() or {}
        current_app.logger.info(
            "Instagram OAuth verify ig context=%s ig_id=%s status=%s payload=%s",
            context,
            ig_id,
            getattr(ig_resp, "status_code", None),
            {
                "id": ig_payload.get("id"),
                "username": ig_payload.get("username"),
                "account_type": ig_payload.get("account_type"),
            },
        )
    except Exception as exc:
        current_app.logger.warning(
            "Instagram OAuth verify ig failed context=%s ig_id=%s status=%s: %s",
            context,
            ig_id,
            getattr(ig_resp, "status_code", None),
            exc,
        )


def _fetch_instagram_me(access_token: str) -> Dict[str, Any]:
    resp = requests.get(
        f"{IG_GRAPH_API_BASE.rstrip('/')}/me",
        params={"access_token": access_token, "fields": "user_id,username"},
        timeout=8,
    )
    try:
        resp.raise_for_status()
    except Exception:
        data = {}
        try:
            data = resp.json()
        except Exception:
            pass
        raise InstagramGraphError(data)
    data = resp.json()
    current_app.logger.info(
        "Instagram OAuth /me response status=%s body=%s",
        resp.status_code,
        data,
    )
    return data


def _tiktok_oauth_url(state: str) -> Optional[str]:
    if not (TIKTOK_CLIENT_KEY and TIKTOK_REDIRECT_URI):
        return None
    scopes = ",".join(_build_tiktok_scopes())
    params = {
        "client_key": TIKTOK_CLIENT_KEY,
        "redirect_uri": TIKTOK_REDIRECT_URI,
        "state": state,
        "scope": scopes,
        "response_type": "code",
    }
    return f"{TIKTOK_AUTH_BASE}?{urlencode(params)}"


def _fetch_tiktok_profile(access_token: str) -> Optional[Dict[str, Any]]:
    try:
        resp = requests.get(
            f"{TIKTOK_API_BASE.rstrip('/')}/user/info/",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"fields": "open_id,union_id,avatar_url,display_name,username"},
            timeout=8,
        )
        resp.raise_for_status()
        payload = resp.json() or {}
    except Exception as exc:
        current_app.logger.warning("TikTok profile lookup failed: %s", exc)
        return None
    data = payload.get("data") if isinstance(payload, dict) else None
    user = (data or {}).get("user") if isinstance(data, dict) else None
    if not user and isinstance(payload, dict):
        user = payload.get("user")
    if not isinstance(user, dict):
        return None
    return {
        "open_id": user.get("open_id"),
        "username": user.get("username") or user.get("display_name"),
        "display_name": user.get("display_name"),
        "avatar_url": user.get("avatar_url"),
    }


@video_shorts_bp.route("/instagram/authorize")
def instagram_authorize():
    current_user = getattr(g, "vs_current_user", None)
    if not current_user:
        flash("Sign in to connect Instagram.", "danger")
        return redirect(url_for("video_shorts_bp.login", next=request.url))
    if not (IG_APP_ID and IG_APP_SECRET and IG_REDIRECT_URI):
        flash(
            "Instagram isn't configured yet. Please finish setup.",
            "danger",
        )
        return redirect(url_for("video_shorts_bp.social_connect"))

    state_token = secrets.token_urlsafe(16)
    next_url = _safe_oauth_next(request.args.get("next"), url_for("video_shorts_bp.social_connect"))
    session["ig_oauth_state"] = {
        "nonce": state_token,
        "user_id": current_user["id"],
        "next": next_url,
    }
    auth_url = _instagram_oauth_url(state_token)
    if not auth_url:
        flash("Could not build the Instagram OAuth URL.", "danger")
        return redirect(url_for("video_shorts_bp.social_connect"))

    _log_instagram_auth_redirect(auth_url)
    return redirect(auth_url)


@video_shorts_bp.route("/instagram/oauth/callback")
def instagram_oauth_callback():
    safe_args = {}
    for key, value in request.args.items():
        safe_args[key] = "MASKED" if key == "code" else value
    current_app.logger.info("Instagram OAuth callback args: %s", safe_args)
    error = request.args.get("error")
    if error:
        current_app.logger.warning(
            "Instagram OAuth error=%s reason=%s description=%s",
            error,
            request.args.get("error_reason"),
            request.args.get("error_description"),
        )
        flash(f"Instagram OAuth error: {error}", "danger")
        return redirect(url_for("video_shorts_bp.social_connect"))

    state = request.args.get("state")
    saved_state = session.pop("ig_oauth_state", None)
    next_url = _safe_oauth_next((saved_state or {}).get("next"), url_for("video_shorts_bp.social_connect"))
    expected_state = saved_state.get("nonce") if isinstance(saved_state, dict) else None
    if not expected_state or state != expected_state:
        current_app.logger.warning("Instagram OAuth state mismatch: %s vs %s", state, expected_state)
        flash("Instagram OAuth verification failed.", "danger")
        return redirect(next_url)
    user_id = saved_state.get("user_id") if isinstance(saved_state, dict) else None
    if not user_id:
        flash("Could not read the Instagram OAuth code.", "danger")
        return redirect(next_url)

    code = request.args.get("code")
    if not code:
        flash("Could not read the Instagram OAuth code.", "danger")
        return redirect(next_url)

    short_resp = None
    try:
        short_resp = requests.post(
            "https://api.instagram.com/oauth/access_token",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "client_id": IG_APP_ID,
                "client_secret": IG_APP_SECRET,
                "grant_type": "authorization_code",
                "redirect_uri": IG_REDIRECT_URI,
                "code": code,
            },
            timeout=12,
        )
        short_resp.raise_for_status()
        short_data = short_resp.json() or {}
    except Exception as exc:
        payload = {}
        body_text = ""
        if short_resp is not None:
            try:
                payload = short_resp.json()
            except Exception:
                pass
            try:
                body_text = (short_resp.text or "").strip()
            except Exception:
                body_text = ""
        current_app.logger.exception(
            "Instagram token exchange failed status=%s payload=%s body=%s: %s",
            getattr(short_resp, "status_code", None),
            payload,
            body_text,
            exc,
        )
        flash("Could not get an Instagram token.", "danger")
        return redirect(next_url)

    short_token = short_data.get("access_token")
    instagram_user_id = str(short_data.get("user_id") or "").strip()
    if not short_token:
        flash("Instagram returned an unexpected short-lived token response.", "danger")
        return redirect(next_url)

    long_resp = None
    try:
        long_resp = requests.get(
            f"{IG_GRAPH_API_BASE.rstrip('/')}/access_token",
            params={
                "grant_type": "ig_exchange_token",
                "client_secret": IG_APP_SECRET,
                "access_token": short_token,
            },
            timeout=12,
        )
        long_resp.raise_for_status()
        long_data = long_resp.json() or {}
    except Exception as exc:
        payload = {}
        body_text = ""
        if long_resp is not None:
            try:
                payload = long_resp.json()
            except Exception:
                pass
            try:
                body_text = (long_resp.text or "").strip()
            except Exception:
                body_text = ""
        current_app.logger.exception(
            "Instagram long token exchange failed status=%s payload=%s body=%s: %s",
            getattr(long_resp, "status_code", None),
            payload,
            body_text,
            exc,
        )
        flash("Could not get a long-lived Instagram token.", "danger")
        return redirect(next_url)

    long_token = long_data.get("access_token")
    expires_at = None
    expires_in = long_data.get("expires_in")
    if expires_in:
        try:
            expires_at = (datetime.utcnow() + timedelta(seconds=int(expires_in))).replace(microsecond=0).isoformat()
        except Exception:
            expires_at = None
    if not long_token:
        flash("Could not validate the Instagram OAuth code.", "danger")
        return redirect(next_url)

    debug_payload = _log_instagram_debug_token(long_token)
    current_app.logger.info(
        "Instagram OAuth token_scopes=%s token_type=%s user_id=%s",
        debug_payload.get("scopes"),
        debug_payload.get("type"),
        instagram_user_id,
    )
    try:
        me_payload = _fetch_instagram_me(long_token)
    except Exception as exc:
        current_app.logger.warning("Instagram /me validation failed: %s", exc)
        flash("Instagram account validation failed. Please reconnect.", "danger")
        return redirect(next_url)

    graph_ig_id = str(me_payload.get("id") or instagram_user_id or "").strip()
    instagram_user_id_value = str(me_payload.get("user_id") or "").strip()
    instagram_profile = _fetch_instagram_profile(long_token, graph_ig_id)
    if not instagram_profile:
        flash("Could not load the Instagram profile. Please reconnect.", "danger")
        return redirect(next_url)

    account_type = str(instagram_profile.get("account_type") or "").upper()
    if account_type not in {"BUSINESS", "CREATOR"}:
        flash(_instagram_account_upgrade_message(), "warning")
        return redirect(next_url)

    scopes = ",".join(s.strip() for s in IG_OAUTH_SCOPES.split(",") if s and s.strip())
    _log_instagram_permissions(long_token)
    store_instagram_token(
        user_id=user_id,
        page_access_token=long_token,
        instagram_business_account_id=graph_ig_id,
        instagram_username=instagram_profile.get("username"),
        facebook_page_id="",
        facebook_page_name="",
        instagram_user_id=instagram_user_id_value or graph_ig_id,
        instagram_account_type=account_type,
        expires_at=expires_at,
        scopes=scopes,
    )
    track_event(user_id, "channel_connected", platform="instagram")
    try:
        subscribe_payload = subscribe_instagram_comment_webhooks(graph_ig_id, long_token)
        current_app.logger.info(
            "Instagram webhook subscription ensured ig_user_id=%s success=%s",
            graph_ig_id,
            subscribe_payload.get("success"),
        )
    except InstagramActionError as exc:
        current_app.logger.warning(
            "Instagram webhook subscription failed ig_user_id=%s: %s",
            graph_ig_id,
            exc,
        )
    saved = get_instagram_data(user_id)
    current_app.logger.info(
        "Instagram OAuth saved db_record_id=%s ig_id=%s username=%s account_type=%s token_tail=%s expires_at=%s",
        (saved or {}).get("user_id"),
        (saved or {}).get("instagram_business_account_id"),
        (saved or {}).get("instagram_username"),
        (saved or {}).get("instagram_account_type"),
        _token_tail((saved or {}).get("page_access_token")),
        (saved or {}).get("expires_at"),
    )
    _log_instagram_connect_validation("callback", None, graph_ig_id, long_token)
    flash("Instagram connected.", "success")
    return redirect(next_url)


@video_shorts_bp.route("/instagram/select_page", methods=["GET", "POST"])
def instagram_select_page():
    flash("Instagram now connects directly with the account. No Facebook Page is required.", "info")
    return redirect(url_for("video_shorts_bp.social_connect"))


@video_shorts_bp.route("/instagram/disconnect", methods=["POST"])
def instagram_disconnect():
    current_user = getattr(g, "vs_current_user", None)
    if not current_user:
        flash("Sign in to disconnect Instagram.", "danger")
        return redirect(url_for("video_shorts_bp.login", next=request.url))
    clear_instagram_token(current_user["id"])
    flash("Instagram disconnected.", "info")
    return redirect(url_for("video_shorts_bp.social_connect"))


@video_shorts_bp.route("/tiktok/authorize")
def tiktok_authorize():
    current_user = getattr(g, "vs_current_user", None)
    if not current_user:
        flash("Sign in to connect TikTok.", "danger")
        return redirect(url_for("video_shorts_bp.login", next=request.url))
    if not (TIKTOK_CLIENT_KEY and TIKTOK_CLIENT_SECRET and TIKTOK_REDIRECT_URI):
        flash(
            "TikTok isn't configured yet. Please finish setup.",
            "danger",
        )
        return redirect(url_for("video_shorts_bp.social_connect"))

    state_token = secrets.token_urlsafe(16)
    session["tt_oauth_state"] = {
        "nonce": state_token,
        "user_id": current_user["id"],
    }
    auth_url = _tiktok_oauth_url(state_token)
    if not auth_url:
        flash("Could not build the TikTok OAuth URL.", "danger")
        return redirect(url_for("video_shorts_bp.social_connect"))
    current_app.logger.info("TikTok OAuth scopes = %s", ",".join(_build_tiktok_scopes()))
    return redirect(auth_url)


@video_shorts_bp.route("/tiktok/oauth/callback")
def tiktok_oauth_callback():
    safe_args = {}
    for key, value in request.args.items():
        safe_args[key] = "MASKED" if key == "code" else value
    current_app.logger.info("TikTok OAuth callback args: %s", safe_args)
    error = request.args.get("error")
    if error:
        error_desc = request.args.get("error_description")
        current_app.logger.warning(
            "TikTok OAuth error=%s description=%s",
            error,
            error_desc,
        )
        message = f"TikTok OAuth error: {error}"
        if error_desc:
            message = f"{message} ({error_desc})"
        flash(message, "danger")
        return redirect(url_for("video_shorts_bp.social_connect"))

    state = request.args.get("state")
    saved_state = session.pop("tt_oauth_state", None)
    expected_state = saved_state.get("nonce") if isinstance(saved_state, dict) else None
    if not expected_state or state != expected_state:
        current_app.logger.warning(
            "TikTok OAuth state mismatch: %s vs %s",
            state,
            expected_state,
        )
        flash("TikTok OAuth verification failed.", "danger")
        return redirect(url_for("video_shorts_bp.social_connect"))
    user_id = saved_state.get("user_id") if isinstance(saved_state, dict) else None
    if not user_id:
        current_app.logger.warning("TikTok OAuth missing user_id in state payload.")
        flash("Could not read the TikTok OAuth code.", "danger")
        return redirect(url_for("video_shorts_bp.social_connect"))

    code = request.args.get("code")
    if not code:
        flash("Could not read the TikTok OAuth code.", "danger")
        return redirect(url_for("video_shorts_bp.social_connect"))

    token_url = f"{TIKTOK_API_BASE.rstrip('/')}/oauth/token/"
    token_resp = None
    try:
        token_resp = requests.post(
            token_url,
            data={
                "client_key": TIKTOK_CLIENT_KEY,
                "client_secret": TIKTOK_CLIENT_SECRET,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": TIKTOK_REDIRECT_URI,
            },
            timeout=12,
        )
        token_resp.raise_for_status()
        token_payload = token_resp.json() or {}
    except Exception as exc:
        payload = {}
        if token_resp is not None:
            try:
                payload = token_resp.json()
            except Exception:
                pass
        current_app.logger.exception(
            "TikTok token exchange failed (status=%s payload=%s): %s",
            getattr(token_resp, "status_code", None),
            payload,
            exc,
        )
        flash("Could not get a TikTok token.", "danger")
        return redirect(url_for("video_shorts_bp.social_connect"))

    token_data = token_payload.get("data") if isinstance(token_payload, dict) else None
    if not isinstance(token_data, dict):
        token_data = token_payload if isinstance(token_payload, dict) else {}

    access_token = token_data.get("access_token")
    if not access_token:
        error_desc = token_payload.get("message") if isinstance(token_payload, dict) else None
        error_code = token_payload.get("error_code") if isinstance(token_payload, dict) else None
        detail = error_desc or error_code or "unknown_error"
        current_app.logger.warning("TikTok OAuth missing access token: %s", detail)
        flash("Could not get a TikTok token.", "danger")
        return redirect(url_for("video_shorts_bp.social_connect"))

    refresh_token = token_data.get("refresh_token")
    open_id = token_data.get("open_id")
    scopes = token_data.get("scope") or ",".join(_build_tiktok_scopes())
    expires_at = None
    refresh_expires_at = None
    expires_in = token_data.get("expires_in")
    refresh_expires_in = token_data.get("refresh_expires_in")
    if expires_in:
        try:
            expires_at = (datetime.utcnow() + timedelta(seconds=int(expires_in))).isoformat()
        except Exception:
            expires_at = None
    if refresh_expires_in:
        try:
            refresh_expires_at = (datetime.utcnow() + timedelta(seconds=int(refresh_expires_in))).isoformat()
        except Exception:
            refresh_expires_at = None

    profile = _fetch_tiktok_profile(access_token)
    username = profile.get("username") if profile else None
    display_name = profile.get("display_name") if profile else None
    if profile and not open_id:
        open_id = profile.get("open_id")

    store_tiktok_token(
        user_id=user_id,
        access_token=access_token,
        refresh_token=refresh_token,
        open_id=open_id,
        username=username,
        display_name=display_name,
        scopes=scopes,
        expires_at=expires_at,
        refresh_expires_at=refresh_expires_at,
    )
    track_event(user_id, "channel_connected", platform="tiktok")
    flash("TikTok connected.", "success")
    return redirect(url_for("video_shorts_bp.social_connect"))


@video_shorts_bp.route("/tiktok/disconnect", methods=["POST"])
def tiktok_disconnect():
    current_user = getattr(g, "vs_current_user", None)
    if not current_user:
        flash("Sign in to disconnect TikTok.", "danger")
        return redirect(url_for("video_shorts_bp.login", next=request.url))
    clear_tiktok_token(current_user["id"])
    flash("TikTok disconnected.", "info")
    return redirect(url_for("video_shorts_bp.social_connect"))


@video_shorts_bp.route("/youtube/authorize")
def youtube_authorize():
    current_user = getattr(g, "vs_current_user", None)
    if not current_user:
        flash("Sign in to connect YouTube.", "danger")
        return redirect(url_for("video_shorts_bp.login", next=request.url))
    next_url = _safe_oauth_next(request.args.get("next"), url_for("video_shorts_bp.social_connect"))
    flow = build_oauth_flow()
    authorization_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    session["yt_oauth_state"] = {
        "nonce": state,
        "user_id": current_user.get("id"),
        "code_verifier": flow.code_verifier,
        "next": next_url,
    }
    return redirect(authorization_url)


@video_shorts_bp.route("/oauth2callback")
def youtube_oauth_callback():
    current_user = getattr(g, "vs_current_user", None)
    if not current_user:
        flash("Sign in to reconnect YouTube.", "danger")
        return redirect(url_for("video_shorts_bp.login", next=request.url))
    error = request.args.get("error")
    saved_state = session.pop("yt_oauth_state", None)
    next_url = _safe_oauth_next((saved_state or {}).get("next"), url_for("video_shorts_bp.social_connect"))
    if error:
        flash(f"YouTube OAuth error: {error}", "danger")
        return redirect(next_url)

    state = request.args.get("state")
    flow = build_oauth_flow(state=state)
    expected_state = saved_state.get("nonce") if isinstance(saved_state, dict) else None
    saved_code_verifier = saved_state.get("code_verifier") if isinstance(saved_state, dict) else None
    if expected_state and state != expected_state:
        current_app.logger.warning("YouTube OAuth state mismatch: %s vs %s", state, expected_state)
    if saved_code_verifier:
        flow.code_verifier = saved_code_verifier
    try:
        flow.fetch_token(authorization_response=request.url)
    except Exception as exc:
        current_app.logger.exception("Failed to fetch YouTube OAuth token: %s", exc)
        flash("Could not get the YouTube OAuth result.", "danger")
        return redirect(next_url)

    credentials = flow.credentials
    refresh_token = credentials.refresh_token
    if not refresh_token:
        flash("No refresh token was returned from YouTube OAuth.", "warning")
        return redirect(next_url)

    store_refresh_token(refresh_token, user_id=current_user["id"])
    track_event(current_user["id"], "channel_connected", platform="youtube")
    flash("YouTube connection saved; you can upload videos to YouTube later.", "success")
    return redirect(next_url)


@video_shorts_bp.route("/video_shorts/youtube/disconnect", methods=["POST"])
def youtube_disconnect():
    current_user = getattr(g, "vs_current_user", None)
    if not current_user:
        flash("Sign in to disconnect YouTube.", "danger")
        return redirect(url_for("video_shorts_bp.login", next=request.url))
    clear_refresh_token(current_user["id"])
    flash("YouTube disconnected.", "info")
    return redirect(url_for("video_shorts_bp.youtube_connect"))


@video_shorts_bp.route("/youtube/upload_clip", methods=["POST"])
def upload_clip_to_youtube():
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"

    def _ajax_fail(message: str, status: int = 400):
        return jsonify(success=False, message=message), status

    def _ajax_ok(message: str = "Publish queued."):
        return jsonify(success=True, message=message)

    youtube_enabled = (request.form.get("youtube_enabled") or "0").strip().lower() in {"1", "true", "yes", "on"}
    schedule_instagram_reel = (request.form.get("schedule_instagram_reel") or "").strip().lower() in {"1", "true", "yes", "on"}
    schedule_instagram_feed = (request.form.get("schedule_instagram_feed") or "").strip().lower() in {"1", "true", "yes", "on"}
    schedule_facebook_reel = (request.form.get("schedule_facebook_reel") or "").strip().lower() in {"1", "true", "yes", "on"}
    schedule_facebook_feed = (request.form.get("schedule_facebook_feed") or "").strip().lower() in {"1", "true", "yes", "on"}
    tiktok_enabled = (request.form.get("schedule_tiktok") or "").strip().lower() in {"1", "true", "yes", "on"}
    force_requeue_instagram = (request.form.get("force_requeue_instagram") or "").strip().lower() in {"1", "true", "yes", "on"}
    force_requeue_tiktok = (request.form.get("force_requeue_tiktok") or "").strip().lower() in {"1", "true", "yes", "on"}
    instagram_targets = []
    if schedule_instagram_reel:
        instagram_targets.append("reel")
    if schedule_instagram_feed:
        instagram_targets.append("feed")
    facebook_targets = []
    if schedule_facebook_reel:
        facebook_targets.append("reel")
    if schedule_facebook_feed:
        facebook_targets.append("feed")
    current_user = getattr(g, "vs_current_user", None)
    user_tz_name = (current_user or {}).get("time_zone") or DEFAULT_TIME_ZONE
    instagram_mode = (request.form.get("instagram_mode") or "sync").strip().lower()
    if instagram_mode not in {"sync", "now", "schedule"}:
        instagram_mode = "sync"
    instagram_publish_at_value = (request.form.get("instagram_publish_at") or "").strip()
    instagram_schedule_iso = None
    if instagram_targets and instagram_mode == "schedule":
        if not instagram_publish_at_value:
            message = "Instagram için planlanan zamanı seçin."
            if is_ajax:
                return _ajax_fail(message)
            flash(message, "warning")
            return redirect(url_for("video_shorts_bp.generate_short", video_pk=request.form.get("video_pk")))
        try:
            instagram_schedule_iso = local_to_utc_rfc3339(instagram_publish_at_value, user_tz_name)
        except Exception:
            message = "Geçersiz Instagram yayın zamanı; YYYY-MM-DDTHH:MM formatında giriniz."
            if is_ajax:
                return _ajax_fail(message)
            flash(message, "warning")
            return redirect(url_for("video_shorts_bp.generate_short", video_pk=request.form.get("video_pk")))
    facebook_mode = (request.form.get("facebook_mode") or "sync").strip().lower()
    if facebook_mode not in {"sync", "now", "schedule"}:
        facebook_mode = "sync"
    facebook_publish_at_value = (request.form.get("facebook_publish_at") or "").strip()
    facebook_schedule_iso = None
    if facebook_targets and facebook_mode == "schedule":
        if not facebook_publish_at_value:
            message = "Facebook için planlanan zamanı seçin."
            if is_ajax:
                return _ajax_fail(message)
            flash(message, "warning")
            return redirect(url_for("video_shorts_bp.generate_short", video_pk=request.form.get("video_pk")))
        try:
            facebook_schedule_iso = local_to_utc_rfc3339(facebook_publish_at_value, user_tz_name)
        except Exception:
            message = "Geçersiz Facebook yayın zamanı; YYYY-MM-DDTHH:MM formatında giriniz."
            if is_ajax:
                return _ajax_fail(message)
            flash(message, "warning")
            return redirect(url_for("video_shorts_bp.generate_short", video_pk=request.form.get("video_pk")))
    tiktok_mode = (request.form.get("tiktok_mode") or "sync").strip().lower()
    if tiktok_mode not in {"sync", "now", "schedule"}:
        tiktok_mode = "sync"
    tiktok_publish_at_value = (request.form.get("tiktok_publish_at") or "").strip()
    tiktok_schedule_iso = None
    if tiktok_enabled and tiktok_mode == "schedule":
        if not tiktok_publish_at_value:
            message = "TikTok için planlanan zamanı seçin."
            if is_ajax:
                return _ajax_fail(message)
            flash(message, "warning")
            return redirect(url_for("video_shorts_bp.generate_short", video_pk=request.form.get("video_pk")))
        try:
            tiktok_schedule_iso = local_to_utc_rfc3339(tiktok_publish_at_value, user_tz_name)
        except Exception:
            message = "Geçersiz TikTok yayın zamanı; YYYY-MM-DDTHH:MM formatında giriniz."
            if is_ajax:
                return _ajax_fail(message)
            flash(message, "warning")
            return redirect(url_for("video_shorts_bp.generate_short", video_pk=request.form.get("video_pk")))
    video_pk = request.form.get("video_pk")
    brand_id = current_brand_id()
    if youtube_enabled and not has_refresh_token((current_user or {}).get("id"), brand_id=brand_id):
        message = "YouTube bağlantısı yok; önce bağlantı kurun."
        if is_ajax:
            return _ajax_fail(message, status=403)
        flash(message, "warning")
        return redirect(url_for("video_shorts_bp.youtube_connect"))
    if not youtube_enabled and not instagram_targets and not facebook_targets and not tiktok_enabled:
        message = "YouTube paylaşımı kapalı; en az bir Instagram, Facebook veya TikTok seçeneği belirleyin."
        if is_ajax:
            return _ajax_fail(message)
        flash(message, "warning")
        return redirect(url_for("video_shorts_bp.generate_short", video_pk=video_pk))
    filename = (request.form.get("filename") or "").strip()
    plan_index_raw = (request.form.get("plan_index") or "").strip()
    if not filename:
        message = "Klip dosyası seçilmedi."
        if is_ajax:
            return _ajax_fail(message)
        flash(message, "warning")
        return redirect(url_for("video_shorts_bp.generate_short", video_pk=request.form.get("video_pk")))

    out_path = SHORTS_DIR / filename
    publish_at_value = (request.form.get("publish_at") or "").strip()
    publish_at = None
    if publish_at_value:
        try:
            publish_at = local_to_utc_rfc3339(publish_at_value, user_tz_name)
            current_app.logger.info(
                "Scheduling YouTube upload for %s -> %s UTC", publish_at_value, publish_at
            )
        except Exception:
            message = "Geçersiz yayın zamanı; YYYY-MM-DDTHH:MM formatında giriniz."
            if is_ajax:
                return _ajax_fail(message)
            flash(message, "warning")
            return redirect(url_for("video_shorts_bp.generate_short", video_pk=request.form.get("video_pk")))

    out_path = SHORTS_DIR / filename
    source_video_id = _resolve_video_id_from_pk(video_pk)
    plan_entries = []
    target_entry = None
    plan_index = None
    if source_video_id:
        plan_entries = _load_plan_entries(source_video_id)
        if plan_entries:
            if plan_index_raw:
                try:
                    plan_index = int(plan_index_raw)
                except Exception:
                    plan_index = None
            if plan_index is not None:
                for entry in plan_entries:
                    try:
                        if int(entry.get("plan_index") or 0) == plan_index:
                            target_entry = entry
                            break
                    except Exception:
                        continue
            if target_entry is None:
                for entry in plan_entries:
                    if entry.get("clip_filename") == filename or entry.get("output_filename") == filename:
                        target_entry = entry
                        break
    if not _short_exists(filename):
        message = "Klip dosyası sunucuda bulunamadı."
        if is_ajax:
            return _ajax_fail(message)
        flash(message, "warning")
        return redirect(url_for("video_shorts_bp.generate_short", video_pk=request.form.get("video_pk")))

    title = (request.form.get("title") or f"Short {filename}").strip()
    description = (request.form.get("description") or "").strip()

    youtube_short_id = None
    instagram_queue_allowed = not youtube_enabled
    facebook_queue_allowed = not youtube_enabled
    existing_yt_id = target_entry.get("yt_video_id") if target_entry else None
    existing_publish_status = (target_entry.get("publish_status") or "").lower() if target_entry else ""
    existing_publish_at_iso = target_entry.get("publish_at_iso") if target_entry else None
    existing_publish_at_local = target_entry.get("publish_at") if target_entry else None
    generated_guard_row = None
    normalized_existing_yt_id = str(existing_yt_id or "").strip()
    if source_video_id and filename and brand_id:
        generated_guard_row = _load_generated_video_publish_guard_row(
            source_video_id=source_video_id,
            clip_filename=filename,
            brand_id=brand_id,
        )
    if not normalized_existing_yt_id and generated_guard_row:
        existing_yt_id = generated_guard_row.get("youtube_video_id")
        normalized_existing_yt_id = str(existing_yt_id or "").strip()
    if existing_publish_status not in {"scheduled", "published", "uploaded"} and generated_guard_row:
        fallback_publish_status = str(generated_guard_row.get("publish_status") or "").strip().lower()
        if fallback_publish_status in {"scheduled", "published", "uploaded"}:
            existing_publish_status = fallback_publish_status
    skip_youtube_upload = False
    should_update_youtube = False
    if youtube_enabled and existing_yt_id and existing_publish_status in {"scheduled", "published", "uploaded"}:
        skip_youtube_upload = True
        youtube_short_id = existing_yt_id
        instagram_queue_allowed = True
        facebook_queue_allowed = True
        if existing_publish_at_iso and not publish_at_value:
            publish_at = existing_publish_at_iso
            if existing_publish_at_local:
                publish_at_value = existing_publish_at_local
        existing_title = (target_entry.get("yt_title") or target_entry.get("title") or "").strip() if target_entry else ""
        existing_description = (
            (target_entry.get("yt_description") or target_entry.get("description") or "").strip()
            if target_entry
            else ""
        )
        if existing_publish_status != "published":
            if publish_at and publish_at != existing_publish_at_iso:
                should_update_youtube = True
            if title and title != existing_title:
                should_update_youtube = True
            if description and description != existing_description:
                should_update_youtube = True
        elif publish_at and existing_publish_at_iso and existing_publish_at_iso != publish_at:
            flash("YouTube zaten yayınlandı; yayın zamanını güncelleyemezsiniz.", "warning")
            publish_at = existing_publish_at_iso
            if existing_publish_at_local:
                publish_at_value = existing_publish_at_local
    if youtube_enabled and not skip_youtube_upload:
        upload_path = _resolve_short_path_for_processing(filename)
        if not upload_path or not upload_path.exists():
            message = "Klip dosyası YouTube yüklemesi için çözülemedi."
            if is_ajax:
                return _ajax_fail(message, status=404)
            flash(message, "warning")
            return redirect(url_for("video_shorts_bp.generate_short", video_pk=request.form.get("video_pk")))
        try:
            response = upload_video_with_refresh_token(
                video_path=str(upload_path),
                title=title,
                description=description,
                publish_at=publish_at,
                privacy_status="private",
                user_id=(current_user or {}).get("id"),
                brand_id=brand_id,
            )
            youtube_short_id = response.get("id") if response else None
            message_label = "YouTube'a yükleme başladı"
            flash(f"{message_label} (ID: {youtube_short_id}).", "success")
            publish_status_key = "scheduled" if publish_at else "uploaded"
            _update_plan_entry_publish_state(
                video_pk=video_pk,
                plan_index=plan_index_raw,
                filename=filename,
                publish_status=publish_status_key,
                publish_at_local=publish_at_value or None,
                publish_at_iso=publish_at,
                title=title,
                description=description,
                youtube_id=youtube_short_id,
            )
            instagram_queue_allowed = True
            facebook_queue_allowed = True
        except RefreshError as exc:
            current_app.logger.warning("Invalid YouTube refresh token during upload: %s", exc)
            error_message = (
                "YouTube bağlantısı geçersiz; yeniden bağlanın."
                if "invalid_grant" in str(exc)
                else "YouTube bağlantısında sorun var; yeniden bağlanın."
            )
            if is_ajax:
                return _ajax_fail(error_message, status=403)
            flash(error_message, "danger")
            instagram_queue_allowed = False
            facebook_queue_allowed = False
        except Exception as exc:
            current_app.logger.exception("YouTube upload failed: %s", exc)
            message = f"YouTube yüklemesi başarısız: {exc}"
            if is_ajax:
                return _ajax_fail(message, status=500)
            flash(message, "danger")
            instagram_queue_allowed = False
            facebook_queue_allowed = False
        finally:
            try:
                if upload_path and upload_path.resolve() != out_path.resolve() and upload_path.exists():
                    upload_path.unlink()
            except Exception:
                current_app.logger.exception("Failed to cleanup temporary short upload path filename=%s path=%s", filename, upload_path)
    elif youtube_enabled and should_update_youtube:
        try:
            response = update_video_with_refresh_token(
                video_id=existing_yt_id,
                title=title,
                description=description,
                publish_at=publish_at,
                privacy_status="private",
                user_id=(current_user or {}).get("id"),
                brand_id=brand_id,
            )
            if response:
                youtube_short_id = existing_yt_id
            update_ok = True
            if publish_at:
                status_map = fetch_video_statuses([existing_yt_id], user_id=(current_user or {}).get("id"), brand_id=brand_id)
                status = status_map.get(existing_yt_id, {}) if status_map else {}
                current_publish_at = status.get("publishAt")
                if current_publish_at:
                    current_dt = _parse_to_utc(current_publish_at)
                    target_dt = _parse_to_utc(publish_at)
                    if current_dt and target_dt and abs((current_dt - target_dt).total_seconds()) > 60:
                        update_ok = False
                else:
                    update_ok = False
            if not update_ok:
                message = "YouTube plan güncellemesi uygulanmadı; yayın zamanı değişmedi."
                if is_ajax:
                    return _ajax_fail(message, status=409)
                flash(message, "warning")
            publish_status_key = "scheduled" if publish_at else "uploaded"
            _update_plan_entry_publish_state(
                video_pk=video_pk,
                plan_index=plan_index_raw,
                filename=filename,
                publish_status=publish_status_key,
                publish_at_local=publish_at_value or None,
                publish_at_iso=publish_at,
                title=title,
                description=description,
                youtube_id=existing_yt_id,
            )
            if update_ok:
                flash("YouTube yayın zamanı güncellendi.", "success")
        except RefreshError as exc:
            current_app.logger.warning("Invalid YouTube refresh token during update: %s", exc)
            error_message = (
                "YouTube bağlantısı geçersiz; yeniden bağlanın."
                if "invalid_grant" in str(exc)
                else "YouTube bağlantısında sorun var; yeniden bağlanın."
            )
            if is_ajax:
                return _ajax_fail(error_message, status=403)
            flash(error_message, "danger")
            instagram_queue_allowed = False
            facebook_queue_allowed = False
        except Exception as exc:
            current_app.logger.exception("YouTube update failed: %s", exc)
            message = f"YouTube güncellemesi başarısız: {exc}"
            if is_ajax:
                return _ajax_fail(message, status=500)
            flash(message, "danger")
            instagram_queue_allowed = False
            facebook_queue_allowed = False
    elif not youtube_enabled:
        flash("YouTube paylaşımı kapalı; sadece Instagram kuyruğu kullanılacak.", "info")
        if facebook_targets:
            flash("YouTube paylaşımı kapalı; Facebook kuyruğu kullanılacak.", "info")

    if instagram_targets and instagram_queue_allowed:
        if not current_user:
            flash("Instagram kuyruğu için giriş yapın.", "warning")
        elif not target_entry:
            flash("Instagram kuyruğu için plan kaydı bulunamadı.", "warning")
        else:
            try:
                instagram_creds = get_instagram_credentials(current_user["id"])
            except InstagramTokenStoreError as exc:
                current_app.logger.warning("Instagram creds unavailable for queue: %s", exc)
                instagram_creds = None
            if not instagram_creds:
                flash("Instagram bağlantısı bulunamadı; Social Connect sayfasından bağlayın.", "warning")
            else:
                current_app.logger.info(
                    "Instagram publish creds user_id=%s page_id=%s ig_id=%s token_tail=%s fb_user_id=%s selected_page_id=%s",
                    current_user.get("id") if current_user else None,
                    instagram_creds.get("facebook_page_id"),
                    instagram_creds.get("instagram_business_account_id"),
                    _token_tail(instagram_creds.get("page_access_token")),
                    instagram_creds.get("meta_fb_user_id"),
                    instagram_creds.get("selected_page_id"),
                )
                if not _validate_instagram_connection(instagram_creds):
                    flash(_instagram_account_upgrade_message(), "danger")
                    return redirect(url_for("video_shorts_bp.generate_short", video_pk=request.form.get("video_pk")))
                caption_source = (
                    target_entry.get("ig_caption")
                    or target_entry.get("yt_description")
                    or description
                    or target_entry.get("excerpt")
                    or title
                )
                caption_text = (caption_source or "").strip()
                if instagram_mode == "schedule":
                    publish_at_iso = instagram_schedule_iso
                elif instagram_mode == "sync":
                    publish_at_iso = publish_at
                elif instagram_mode == "now":
                    publish_at_iso = datetime.utcnow().isoformat() + "Z"
                else:
                    publish_at_iso = None
                clip_name = target_entry.get("output_filename") or filename
                plan_index_key = str(target_entry.get("plan_index") or plan_index_raw or "")
                for media_type in instagram_targets:
                    try:
                        queue_id = enqueue_instagram_clip(
                            user_id=current_user.get("id") if current_user else None,
                            video_id=source_video_id,
                            plan_index=plan_index_key,
                            clip_filename=clip_name,
                            caption_text=caption_text,
                            publish_at_iso=publish_at_iso,
                            instagram_business_account_id=instagram_creds.get("instagram_business_account_id"),
                            instagram_username=instagram_creds.get("instagram_username"),
                            youtube_video_id=source_video_id,
                            youtube_short_id=youtube_short_id,
                            plan_title=target_entry.get("title") or target_entry.get("yt_title") or title,
                            media_type=media_type,
                            force_requeue=force_requeue_instagram,
                        )
                        current_app.logger.info(
                            "Instagram queue created id=%s plan=%s video=%s media_type=%s mode=%s",
                            queue_id,
                            plan_index_key,
                            video_pk,
                            media_type,
                            instagram_mode,
                        )
                        label = "Reels" if media_type == "reel" else "Feed"
                        flash(f"Instagram {label} kuyruğuna eklendi.", "info")
                    except Exception as exc:
                        current_app.logger.warning("Failed to enqueue Instagram job: %s", exc)
                        flash("Instagram kuyruğu oluşturulamadı; logları kontrol edin.", "danger")

    if facebook_targets and facebook_queue_allowed:
        if not current_user:
            flash("Facebook kuyruğu için giriş yapın.", "warning")
        elif not target_entry:
            flash("Facebook kuyruğu için plan kaydı bulunamadı.", "warning")
        else:
            try:
                facebook_info = get_facebook_page_data(current_user["id"])
            except FacebookTokenStoreError as exc:
                current_app.logger.warning("Facebook info unavailable for publish: %s", exc)
                facebook_info = None
            if not facebook_info or not facebook_info.get("page_access_token"):
                flash("Facebook bağlantısı bulunamadı; Social Connect sayfasından bağlayın.", "warning")
            else:
                caption_source = (
                    target_entry.get("fb_caption")
                    or target_entry.get("yt_description")
                    or description
                    or target_entry.get("excerpt")
                    or title
                )
                caption_text = (caption_source or "").strip()
                if facebook_mode == "schedule":
                    facebook_publish_at_iso = facebook_schedule_iso
                elif facebook_mode == "sync":
                    facebook_publish_at_iso = publish_at
                elif facebook_mode == "now":
                    facebook_publish_at_iso = datetime.utcnow().isoformat() + "Z"
                else:
                    facebook_publish_at_iso = None
                clip_name = target_entry.get("output_filename") or filename
                plan_index_key = str(target_entry.get("plan_index") or plan_index_raw or "")
                for media_type in facebook_targets:
                    try:
                        queue_id = enqueue_facebook_clip(
                            user_id=current_user.get("id") if current_user else None,
                            video_id=source_video_id,
                            plan_index=plan_index_key,
                            clip_filename=clip_name,
                            caption_text=caption_text,
                            publish_at_iso=facebook_publish_at_iso,
                            page_id=facebook_info.get("page_id"),
                            page_name=facebook_info.get("page_name"),
                            plan_title=target_entry.get("title") or target_entry.get("yt_title") or title,
                            media_type=media_type,
                        )
                        current_app.logger.info(
                            "Facebook queue created id=%s plan=%s video=%s media_type=%s mode=%s",
                            queue_id,
                            plan_index_key,
                            video_pk,
                            media_type,
                            facebook_mode,
                        )
                        label = "Reels" if media_type == "reel" else "Feed"
                        flash(f"Facebook {label} kuyruğuna eklendi.", "info")
                    except Exception as exc:
                        current_app.logger.warning("Failed to enqueue Facebook job: %s", exc)
                        flash("Facebook kuyruğu oluşturulamadı; logları kontrol edin.", "danger")

    if tiktok_enabled:
        if not current_user:
            flash("TikTok paylaşımı için giriş yapın.", "warning")
        elif not target_entry:
            flash("TikTok kuyruğu için plan kaydı bulunamadı.", "warning")
        else:
            try:
                tiktok_info = get_tiktok_data(current_user["id"])
            except TikTokTokenStoreError as exc:
                current_app.logger.warning("TikTok info unavailable for publish: %s", exc)
                tiktok_info = None
            if not tiktok_info or not tiktok_info.get("access_token") or _is_token_expired(tiktok_info.get("expires_at")):
                flash("TikTok bağlantısı bulunamadı; Social Connect sayfasından bağlayın.", "warning")
            else:
                caption_source = (
                    target_entry.get("tt_caption")
                    or target_entry.get("yt_description")
                    or description
                    or target_entry.get("excerpt")
                    or title
                )
                caption_text = (caption_source or "").strip()
                if tiktok_mode == "schedule":
                    tiktok_publish_at_iso = tiktok_schedule_iso
                elif tiktok_mode == "sync":
                    tiktok_publish_at_iso = publish_at
                elif tiktok_mode == "now":
                    tiktok_publish_at_iso = datetime.utcnow().isoformat() + "Z"
                else:
                    tiktok_publish_at_iso = None
                plan_index_key = str(target_entry.get("plan_index") or plan_index_raw or "")
                try:
                    queue_id = enqueue_tiktok_clip(
                        user_id=current_user.get("id") if current_user else None,
                        video_id=source_video_id,
                        plan_index=plan_index_key,
                        clip_filename=target_entry.get("output_filename") or filename,
                        caption_text=caption_text,
                        publish_at_iso=tiktok_publish_at_iso,
                        tiktok_open_id=tiktok_info.get("open_id"),
                        tiktok_username=tiktok_info.get("username"),
                        plan_title=target_entry.get("title") or target_entry.get("yt_title") or title,
                        force_requeue=force_requeue_tiktok,
                    )
                    current_app.logger.info(
                        "TikTok queue created id=%s plan=%s video=%s mode=%s",
                        queue_id,
                        plan_index_key,
                        video_pk,
                        tiktok_mode,
                    )
                    flash("TikTok kuyruğuna eklendi.", "info")
                except Exception as exc:
                    current_app.logger.warning("Failed to enqueue TikTok job: %s", exc)
                    flash("TikTok kuyruğu oluşturulamadı; logları kontrol edin.", "danger")

    if is_ajax:
        return _ajax_ok()
    return redirect(url_for("video_shorts_bp.generate_short", video_pk=request.form.get("video_pk")))


def _set_transcribe_job_state(video_pk: int, **updates: Any) -> Dict[str, Any]:
    with _TRANSCRIBE_JOB_LOCK:
        state = dict(_TRANSCRIBE_JOB_STATE.get(video_pk) or {})
        state.update(updates)
        state["updated_at"] = datetime.utcnow().isoformat()
        state = _sanitize_transcribe_state(state)
        _TRANSCRIBE_JOB_STATE[video_pk] = state
    _persist_transcribe_job_state(video_pk, state)
    return state


def _parse_request_bool(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _load_transcript_summary(conn, video_id: str) -> Dict[str, Any]:
    summary = {
        "exists": False,
        "segment_count": 0,
        "text": "",
    }
    if not video_id:
        return summary
    transcript_row = conn.execute(
        "SELECT full_text, segments_json, whisper_segments_json FROM youtube_transcripts WHERE video_id = ?",
        [video_id],
    ).fetchone()
    if not transcript_row:
        return summary
    transcript_text = (transcript_row[0] or "").strip()
    transcript_raw = transcript_row[2] or transcript_row[1]
    transcript_segment_count = 0
    if transcript_raw:
        try:
            parsed = json.loads(transcript_raw)
            if isinstance(parsed, list):
                transcript_segment_count = len(parsed)
        except Exception:
            transcript_segment_count = 0
    summary["exists"] = bool(transcript_text or transcript_segment_count)
    summary["segment_count"] = transcript_segment_count
    summary["text"] = transcript_text
    return summary


def _should_force_retranscribe(form_data: Any) -> bool:
    if _parse_request_bool(form_data.get("force_retranscribe")):
        return True
    reuse_existing_raw = form_data.get("reuse_existing")
    if reuse_existing_raw is None:
        return False
    return str(reuse_existing_raw or "").strip().lower() in {"0", "false", "no", "off"}


def _build_transcript_reuse_state(video_pk: int, video_id: str, transcript_segment_count: int) -> Dict[str, Any]:
    segment_suffix = f" ({transcript_segment_count} segments)" if transcript_segment_count else ""
    return _set_transcribe_job_state(
        video_pk,
        status="completed",
        running=False,
        stage="completed",
        message=f"Existing transcript found{segment_suffix}. OpenAI call skipped.",
        video_id=video_id,
        segment_count=transcript_segment_count or None,
        error=None,
        elapsed_seconds=0.0,
        finished_at=datetime.utcnow().isoformat(),
    )


def _run_transcribe_job(video_pk: int, video_id: str, source_path: Path, source_path_is_temp: bool, app_obj) -> None:
    start_ts = time.monotonic()
    conn = None

    def _progress(stage: str, message: str, extra: Dict[str, Any]) -> None:
        payload: Dict[str, Any] = {
            "status": "running",
            "running": True,
            "stage": stage,
            "message": message,
            "elapsed_seconds": round(time.monotonic() - start_ts, 1),
        }
        if isinstance(extra, dict):
            payload.update(extra)
        _set_transcribe_job_state(video_pk, **payload)

    with app_obj.app_context():
        try:
            _set_transcribe_job_state(
                video_pk,
                status="running",
                running=True,
                stage="queued",
                message="Job queued.",
                video_id=video_id,
                started_at=datetime.utcnow().isoformat(),
                error=None,
                elapsed_seconds=0.0,
            )
            full_text, segments = _transcribe_with_whisper(source_path, progress_cb=_progress)
            if not segments:
                raise RuntimeError("Whisper did not return any segments.")

            _set_transcribe_job_state(
                video_pk,
                status="running",
                running=True,
                stage="db_write",
                message="Saving transcript to database.",
                elapsed_seconds=round(time.monotonic() - start_ts, 1),
            )
            conn = get_db()
            _ensure_transcript_schema(conn)
            ensure_postgres_youtube_transcripts_id_default(conn)
            event_video_id, should_emit_transcript_completed = prepare_transcript_completed_transition(
                conn,
                video_pk=video_pk,
            )
            segments_json = json.dumps(segments, ensure_ascii=False)
            whisper_segments_json = segments_json
            exists = conn.execute(
                "SELECT 1 FROM youtube_transcripts WHERE video_id = ?",
                [video_id],
            ).fetchone()
            if exists:
                conn.execute(
                    "UPDATE youtube_transcripts SET full_text = ?, segments_json = ?, whisper_segments_json = ? WHERE video_id = ?",
                    [full_text, segments_json, whisper_segments_json, video_id],
                )
            else:
                conn.execute(
                    "INSERT INTO youtube_transcripts (video_id, full_text, segments_json, whisper_segments_json) VALUES (?, ?, ?, ?)",
                    [video_id, full_text, segments_json, whisper_segments_json],
                )

            conn.execute(
                """
                UPDATE youtube_videos
                SET transcript_status = 'done',
                    fetch_transcript = FALSE,
                    last_checked_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                [video_pk],
            )
            conn.commit()
            owner_user_id = None
            try:
                owner_row = conn.execute(
                    "SELECT owner_user_id, duration_seconds, title FROM youtube_videos WHERE id = ?",
                    [video_pk],
                ).fetchone()
                if owner_row:
                    owner_user_id = owner_row[0]
                    audio_minutes = _duration_minutes(owner_row[1])
                    if owner_user_id and audio_minutes > 0:
                        add_transcription_minutes(
                            str(owner_user_id),
                            audio_minutes,
                            video_id=video_id,
                            video_title=owner_row[2],
                        )
            except Exception:
                app_obj.logger.exception("Failed to meter transcription usage for video_pk=%s", video_pk)
            if should_emit_transcript_completed and owner_user_id:
                track_event(
                    str(owner_user_id),
                    "transcript_completed",
                    video_id=event_video_id or video_id,
                    status="completed",
                )
            elapsed = round(time.monotonic() - start_ts, 1)
            _set_transcribe_job_state(
                video_pk,
                status="completed",
                running=False,
                stage="completed",
                message=f"Transcript completed ({len(segments)} segments).",
                segment_count=len(segments),
                elapsed_seconds=elapsed,
                finished_at=datetime.utcnow().isoformat(),
                error=None,
            )
        except Exception as exc:
            elapsed = round(time.monotonic() - start_ts, 1)
            _set_transcribe_job_state(
                video_pk,
                status="failed",
                running=False,
                stage="failed",
                message=str(exc),
                error=str(exc),
                elapsed_seconds=elapsed,
                finished_at=datetime.utcnow().isoformat(),
            )
            app_obj.logger.exception("Background transcribe failed for video_pk=%s: %s", video_pk, exc)
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
            _cleanup_resolved_source_video(source_path, source_path_is_temp)


@video_shorts_bp.route("/generate/<int:video_pk>/transcribe/start", methods=["POST"])
def transcribe_video_start(video_pk):
    cleanup_video_shorts_temp_dir()
    metered_user_id = _active_editor_context()["owner_user_id"]
    try:
        conn = get_db_readonly()
        try:
            row = _fetch_scoped_video_row(
                conn,
                video_pk,
                """
                video_id,
                duration_seconds,
                COALESCE(transcript_status, ''),
                COALESCE(fetch_transcript, FALSE)
                """,
            )
            transcript_exists = False
            transcript_segment_count = 0
            if row:
                transcript_summary = _load_transcript_summary(conn, row[0])
                transcript_exists = bool(transcript_summary["exists"])
                transcript_segment_count = int(transcript_summary["segment_count"] or 0)
        finally:
            conn.close()
        if not row:
            return jsonify({"ok": False, "message": "Video not found."}), 404

        video_id = row[0]
        duration_seconds = row[1]
        transcript_status = (row[2] or "").strip().lower()
        force_retranscribe = _should_force_retranscribe(request.form)
        has_completed_transcript = transcript_status == "done" or transcript_exists

        existing = _load_transcribe_job_state(video_pk)
        if not existing:
            with _TRANSCRIBE_JOB_LOCK:
                existing = dict(_TRANSCRIBE_JOB_STATE.get(video_pk) or {})
        if existing.get("running"):
            # If a prior worker died mid-job, status can remain "running" forever.
            # Treat stale jobs as failed so users can start a fresh transcription.
            stale_after = timedelta(minutes=12)
            last_update_raw = existing.get("updated_at") or existing.get("started_at")
            is_stale = False
            if isinstance(last_update_raw, str) and last_update_raw:
                try:
                    parsed_dt = datetime.fromisoformat(last_update_raw.replace("Z", "+00:00"))
                    if parsed_dt.tzinfo is not None:
                        parsed_dt = parsed_dt.astimezone(timezone.utc).replace(tzinfo=None)
                    is_stale = (datetime.utcnow() - parsed_dt) > stale_after
                except Exception:
                    is_stale = False
            if not is_stale:
                return jsonify(
                    {
                        "ok": True,
                        "started": False,
                        "state": _sanitize_transcribe_state(existing),
                        "message": "Transcription already running.",
                    }
                )
            _set_transcribe_job_state(
                video_pk,
                status="failed",
                running=False,
                stage="failed",
                message="Previous transcription job became stale. Starting a fresh run.",
                error="stale_transcribe_job",
                finished_at=datetime.utcnow().isoformat(),
            )

        # Default behavior is idempotent: if a usable transcript already exists,
        # skip Whisper entirely. Only an explicit force flag should re-run it.
        if has_completed_transcript and not force_retranscribe:
            state = _build_transcript_reuse_state(video_pk, video_id, transcript_segment_count)
            return jsonify(
                {
                    "ok": True,
                    "started": False,
                    "reused": True,
                    "state": state,
                    "message": state["message"],
                }
            )

        quota_error = _transcription_quota_error_for_user(metered_user_id, duration_seconds)
        if quota_error:
            return jsonify({"ok": False, "message": quota_error}), 403
        if disk_guard_triggered(operation="transcribe_video_start", log=current_app.logger):
            return jsonify({"ok": False, "message": USER_FACING_DISK_GUARD_MESSAGE}), 503
        source_path, source_path_is_temp = _resolve_source_video(video_id)
        if not source_path or not source_path.exists():
            return (
                jsonify({"ok": False, "message": "Source file not found on server. Upload or download it first."}),
                404,
            )

        _set_transcribe_job_state(
            video_pk,
            status="running",
            running=True,
            stage="starting",
            message="Starting transcription...",
            video_id=video_id,
            started_at=datetime.utcnow().isoformat(),
            error=None,
            elapsed_seconds=0.0,
        )
        thread = threading.Thread(
            target=_run_transcribe_job,
            args=(video_pk, video_id, source_path, source_path_is_temp, current_app._get_current_object()),
            daemon=True,
        )
        thread.start()
        return jsonify({"ok": True, "started": True, "message": "Transcription started."})
    except Exception as exc:
        current_app.logger.exception("Failed to start transcribe job for video_pk=%s", video_pk)
        return jsonify({"ok": False, "message": str(exc)}), 500


@video_shorts_bp.route("/generate/<int:video_pk>/transcribe/status", methods=["GET"])
def transcribe_video_status(video_pk):
    try:
        if not _resolve_video_id_from_pk(video_pk):
            return jsonify({"ok": False, "status": "failed", "message": "Video not found."}), 404
        state = _load_transcribe_job_state(video_pk)
        if not state:
            with _TRANSCRIBE_JOB_LOCK:
                state = dict(_TRANSCRIBE_JOB_STATE.get(video_pk) or {})
        if not state:
            return jsonify({"ok": True, "status": "idle", "running": False, "stage": "idle", "message": "No active transcription."})
        return jsonify({"ok": True, **_sanitize_transcribe_state(state)})
    except Exception as exc:
        current_app.logger.exception("Failed to read transcribe status for video_pk=%s", video_pk)
        return jsonify({"ok": False, "status": "failed", "message": str(exc)}), 500


@video_shorts_bp.route("/generate/<int:video_pk>/admin-operation/transcribe/status", methods=["GET"])
@require_admin
def admin_operation_editor_transcribe_status(video_pk: int):
    """Return target-owned analysis state for a marked admin workspace editor."""
    if not request_admin_operation_scope():
        abort(404)
    return transcribe_video_status(video_pk)


@video_shorts_bp.route("/generate/<int:video_pk>/transcribe", methods=["POST"])
def transcribe_video(video_pk):
    cleanup_video_shorts_temp_dir()
    editor_context = _active_editor_context()
    target_owner_user_id = editor_context["owner_user_id"]
    target_brand_id = editor_context["brand_id"]

    def _generate_redirect():
        return redirect(_editor_url("generate_short", video_pk=video_pk))

    conn = get_db_readonly()
    row = _fetch_scoped_video_row(conn, video_pk, "video_id, title, duration_seconds, COALESCE(transcript_status, '')")
    transcript_exists = False
    transcript_segment_count = 0
    if row:
        transcript_summary = _load_transcript_summary(conn, row[0])
        transcript_exists = bool(transcript_summary["exists"])
        transcript_segment_count = int(transcript_summary["segment_count"] or 0)
    conn.close()
    if not row:
        flash("Video not found", "danger")
        return redirect(url_for("video_shorts_bp.channels_page"))

    vid = row[0]
    video_title = row[1]
    duration_seconds = row[2] if len(row) > 2 else None
    transcript_status = (row[3] or "").strip().lower() if len(row) > 3 else ""
    force_retranscribe = _should_force_retranscribe(request.form)
    has_completed_transcript = transcript_status == "done" or transcript_exists
    if has_completed_transcript and not force_retranscribe:
        segment_suffix = f" ({transcript_segment_count} segments)" if transcript_segment_count else ""
        flash(f"Existing transcript found{segment_suffix}. OpenAI call skipped.", "info")
        return _generate_redirect()
    quota_error = _transcription_quota_error_for_user(target_owner_user_id, duration_seconds)
    if quota_error:
        flash(quota_error, "warning")
        return _generate_redirect()
    if disk_guard_triggered(operation="transcribe_video_sync", log=current_app.logger):
        flash(USER_FACING_DISK_GUARD_MESSAGE, "warning")
        return _generate_redirect()
    source_path, source_path_is_temp = _resolve_source_video(vid)
    if not source_path or not source_path.exists():
        conn.close()
        flash("Source video file not found on server. Upload or download it first.", "danger")
        return _generate_redirect()

    try:
        full_text, segments = _transcribe_with_whisper(source_path)
        if not segments:
            flash("Whisper did not return any segments.", "warning")
            return _generate_redirect()

        conn = get_db()
        _ensure_transcript_schema(conn)
        event_video_id, should_emit_transcript_completed = prepare_transcript_completed_transition(
            conn,
            video_pk=video_pk,
        )
        segments_json = json.dumps(segments, ensure_ascii=False)
        whisper_segments_json = segments_json
        exists = conn.execute(
            "SELECT 1 FROM youtube_transcripts WHERE video_id = ?",
            [vid],
        ).fetchone()
        if exists:
            conn.execute(
                "UPDATE youtube_transcripts SET full_text = ?, segments_json = ?, whisper_segments_json = ? WHERE video_id = ?",
                [full_text, segments_json, whisper_segments_json, vid],
            )
        else:
            conn.execute(
                "INSERT INTO youtube_transcripts (video_id, full_text, segments_json, whisper_segments_json) VALUES (?, ?, ?, ?)",
                [vid, full_text, segments_json, whisper_segments_json],
            )

        conn.execute(
            """
            UPDATE youtube_videos
            SET transcript_status = 'done',
                fetch_transcript = FALSE,
                last_checked_at = CURRENT_TIMESTAMP
            WHERE id = ?
              AND owner_user_id = ?
              AND ((? IS NULL AND brand_id IS NULL) OR brand_id = ?)
            """,
            [video_pk, target_owner_user_id, target_brand_id, target_brand_id],
        )
        conn.commit()
        audio_minutes = _duration_minutes(_probe_media_duration_seconds(source_path))
        if should_emit_transcript_completed and target_owner_user_id:
            track_event(
                str(target_owner_user_id),
                "transcript_completed",
                video_id=event_video_id or vid,
                status="completed",
            )
        if target_owner_user_id and audio_minutes > 0:
            try:
                add_transcription_minutes(
                    str(target_owner_user_id),
                    audio_minutes,
                    video_id=vid,
                    video_title=video_title,
                )
            except Exception:
                current_app.logger.exception(
                    "Failed to meter synchronous transcription usage for video_pk=%s",
                    video_pk,
                )
        flash("Transkript başarıyla Whisper ile üretildi.", "success")
    except Exception as e:
        flash(f"Whisper transcription failed: {e}", "danger")
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass
        _cleanup_resolved_source_video(source_path, source_path_is_temp)

    return _generate_redirect()


@video_shorts_bp.route("/generate/<int:video_pk>/segment_edit", methods=["POST"])
def edit_segment_text(video_pk):
    payload = request.get_json(silent=True)
    if payload is None:
        payload = request.form

    index_raw = payload.get("index")
    new_text = (payload.get("text") or "").strip()

    try:
        segment_index = int(index_raw)
    except (TypeError, ValueError):
        return jsonify({"success": False, "message": "Invalid segment index."}), 400

    conn = get_db()
    _ensure_transcript_schema(conn)
    row = _fetch_scoped_video_row(conn, video_pk, "video_id")
    if not row:
        conn.close()
        return jsonify({"success": False, "message": "Video not found."}), 404

    video_id = row[0]
    full_text, segments = _fetch_transcript(conn, video_id)
    if not segments:
        conn.close()
        return jsonify({"success": False, "message": "No transcript segments available."}), 400

    if segment_index < 0 or segment_index >= len(segments):
        conn.close()
        return jsonify({"success": False, "message": "Segment index out of range."}), 400

    seg = segments[segment_index]
    segment_start = None
    segment_end = None
    try:
        segment_start = float(seg.get("start")) if seg.get("start") is not None else None
    except Exception:
        segment_start = None
    try:
        segment_end = float(seg.get("end")) if seg.get("end") is not None else None
    except Exception:
        segment_end = None
    if segment_end is None and segment_start is not None:
        try:
            duration = float(seg.get("duration")) if seg.get("duration") is not None else 0.0
        except Exception:
            duration = 0.0
        segment_end = segment_start + max(duration, 0.0)
    seg["tr_text"] = new_text
    seg["text"] = new_text
    seg["words"] = []
    if "word_tags" in seg:
        seg["word_tags"] = []

    updated_full_text = _joined_transcript_tr(segments)
    segments_json = json.dumps(segments, ensure_ascii=False)
    try:
        conn.execute(
            "UPDATE youtube_transcripts SET full_text = ?, segments_json = ?, whisper_segments_json = ? WHERE video_id = ?",
            [updated_full_text, segments_json, segments_json, video_id],
        )
        conn.commit()
    except Exception as exc:
        conn.close()
        return jsonify({"success": False, "message": str(exc)}), 500

    conn.close()

    if segment_start is not None and segment_end is not None and segment_end > segment_start:
        try:
            user_id = str(_active_editor_context()["owner_user_id"] or "")
            if user_id:
                plan_entries = _load_plan_entries(video_id)
                for entry in plan_entries:
                    try:
                        plan_index = int(entry.get("plan_index"))
                        entry_start = float(entry.get("start"))
                        entry_end = float(entry.get("end"))
                    except Exception:
                        continue
                    if entry_end <= segment_start or entry_start >= segment_end:
                        continue
                    try:
                        clear_done_job_cache_for_plan(
                            user_id=user_id,
                            source_video_id=str(video_id or ""),
                            plan_index=plan_index,
                        )
                    except Exception as exc:
                        current_app.logger.warning(
                            "Failed to clear render cache after transcript edit video_id=%s plan_index=%s: %s",
                            video_id,
                            plan_index,
                            exc,
                        )
        except Exception as exc:
            current_app.logger.warning(
                "Failed to inspect overlapping plan entries after transcript edit video_id=%s: %s",
                video_id,
                exc,
            )

    return jsonify({"success": True, "transcript": updated_full_text, "segment_text": new_text})


@video_shorts_bp.route("/generate/<int:video_pk>/segment_non_speech", methods=["POST"])
def update_segment_non_speech(video_pk):
    payload = request.get_json(silent=True) or {}
    index_raw = payload.get("index")
    non_speech_type = (payload.get("non_speech_type") or "").strip().lower()
    if non_speech_type == "":
        non_speech_type = "speech"
    allowed_types = {"speech", "music", "applause", "silence", "other"}
    if non_speech_type not in allowed_types:
        return jsonify({"success": False, "message": "Invalid non-speech type."}), 400
    try:
        segment_index = int(index_raw)
    except (TypeError, ValueError):
        return jsonify({"success": False, "message": "Invalid segment index."}), 400
    conn = get_db_readonly()
    row = _fetch_scoped_video_row(conn, video_pk, "video_id")
    if not row:
        conn.close()
        return jsonify({"success": False, "message": "Video not found."}), 404
    video_id = row[0]
    _, segments = _fetch_transcript(conn, video_id)
    conn.close()
    if not segments:
        return jsonify({"success": False, "message": "No transcript segments available."}), 400
    if segment_index < 0 or segment_index >= len(segments):
        return jsonify({"success": False, "message": "Segment index out of range."}), 400
    overrides = load_non_speech_overrides(video_id)
    if non_speech_type == "speech":
        overrides.pop(str(segment_index), None)
    else:
        overrides[str(segment_index)] = non_speech_type
    save_non_speech_overrides(video_id, overrides)
    return jsonify({"success": True, "non_speech_type": non_speech_type})


def _set_plan_job_state(video_pk: int, **updates: Any) -> Dict[str, Any]:
    with _PLAN_JOB_LOCK:
        state = dict(_PLAN_JOB_STATE.get(video_pk) or {})
        state.update(updates)
        state["updated_at"] = datetime.utcnow().isoformat()
        state = _set_plan_job_state_locked(video_pk, state)
    return state


def _generate_clip_plan_for_video(
    video_pk: int,
    form_data: Dict[str, Any],
    *,
    debug_flag: bool = False,
    progress_cb: Optional[Callable[[str, str, Dict[str, Any]], None]] = None,
    owner_user_id: Any = None,
    brand_id: Any = None,
    preloaded_video_info: Optional[Tuple[str, str, Optional[float], str, List[Dict[str, Any]]]] = None,
) -> Dict[str, Any]:
    def _emit(stage: str, message: str, **extra: Any) -> None:
        if not progress_cb:
            return
        try:
            progress_cb(stage, message, extra)
        except Exception:
            pass

    cleanup_video_shorts_temp_dir()
    _emit("load_video", "Loading video and transcript.")
    if preloaded_video_info is not None:
        video_info = preloaded_video_info
    elif owner_user_id is not None or brand_id is not None:
        video_info = _fetch_video_with_transcript_for_scope(
            video_pk,
            owner_user_id=owner_user_id,
            brand_id=brand_id,
        )
    else:
        video_info = _fetch_video_with_transcript(video_pk)
    if not video_info:
        raise FileNotFoundError("Video not found.")
    if not _openai_client:
        raise RuntimeError("OPENAI_API_KEY is missing; cannot create clip plan.")

    vid, _video_title, duration_seconds, transcript_text, segments = video_info
    non_speech_overrides = load_non_speech_overrides(vid)
    if non_speech_overrides:
        for idx_str, override in non_speech_overrides.items():
            try:
                idx = int(idx_str)
            except Exception:
                continue
            if idx < 0 or idx >= len(segments):
                continue
            if override:
                segments[idx]["non_speech_type"] = override

    computed_duration = int(duration_seconds or 0)
    if not computed_duration and segments:
        try:
            last = max(
                (
                    float(s.get("end", 0.0) or 0.0)
                    if s.get("end") is not None
                    else (float(s.get("start", 0.0) or 0.0) + float(s.get("duration", 0.0) or 0.0))
                )
                for s in segments
            )
            computed_duration = int(last)
        except Exception:
            computed_duration = 0

    transcript_language = _resolve_video_language(segments, transcript_text or "")
    requested_language = _normalize_title_prompt_language(form_data.get("language"))
    plan_language = transcript_language or requested_language or "tr"
    plan_focus = normalize_plan_focus(form_data.get("plan_focus") or "")
    focus_categories = normalize_focus_categories(form_data.get("focus_categories"), default_to_all=True)
    clip_plan: List[Dict[str, Any]] = []
    debug_info: Dict[str, Any] = {}
    plan_path = SHORTS_DIR / f"{vid}_plan.json"
    existing_plan_entries = _load_plan_entries(vid)

    try:
        _emit("llm_plan", "Generating clip plan with AI.")
        clip_plan, debug_info = propose_clips_with_agents(
            segments,
            transcript_text,
            computed_duration,
            _openai_client,
            OPENAI_MODEL,
            debug=debug_flag,
            plan_focus=plan_focus,
            focus_categories=focus_categories,
            language=plan_language,
        )
    except Exception as ag:
        current_app.logger.warning("Agent pipeline failed, falling back. %s", ag)
        _emit("fallback", "Primary planner failed; using fallback plan.")

    if debug_info:
        try:
            debug_path = SHORTS_DIR / f"{vid}_debug.json"
            debug_path.write_text(json.dumps(debug_info, ensure_ascii=False, indent=2))
        except Exception:
            pass

    if debug_info:
        win_info = debug_info.get("window_candidates") or []
        finalc = debug_info.get("final_plan") or []
        for idx, info in enumerate(win_info, 1):
            win = info.get("window", {}) or {}
            seg_cnt = info.get("seg_count")
            agent1_cnt = len(info.get("agent1_clips") or [])
            final_cnt = len(info.get("final_clips") or [])
            current_app.logger.info(
                "[CLIP_AGENT] window %s: %.0f-%.0f sec, segs=%s, candidates=%s (agent1 raw=%s)",
                idx,
                win.get("start", 0),
                win.get("end", 0),
                seg_cnt,
                final_cnt,
                agent1_cnt,
            )
        current_app.logger.info("[CLIP_AGENT] final=%s clips", len(finalc))

    if not clip_plan:
        _emit("fallback", "Using fallback clip plan.")
        clip_plan = _fallback_clip_plan(computed_duration)
    if not clip_plan:
        raise RuntimeError("LLM did not return any clip suggestions and fallback couldn't generate clips.")

    timestamped_plan = []
    _emit("prepare_plan", "Preparing plan entries.", clip_count=len(clip_plan))
    for idx, clip in enumerate(clip_plan):
        plan_entry = dict(clip, plan_index=idx + 1)
        start = plan_entry.get("start")
        end = plan_entry.get("end")
        if segments and start is not None and end is not None and "transcript_full" not in plan_entry:
            try:
                plan_entry["transcript_full"] = build_transcript_for_range(
                    segments, start, end, prefer_tr=(plan_language != "en")
                )
            except Exception:
                pass
        plan_entry["language"] = _infer_clip_language_from_segments(
            segments,
            start,
            end,
            excerpt=plan_entry.get("transcript_full") or plan_entry.get("excerpt") or "",
        )
        plan_entry["origin"] = "ai"
        plan_entry["focus_categories"] = list(focus_categories)
        plan_entry["status"] = "pending"
        plan_entry["clip_filename"] = plan_entry.get("clip_filename") or f"{idx + 1}_{vid}.mp4"
        plan_entry["publish_status"] = "not_ready"
        plan_entry.setdefault("yt_description", None)
        plan_entry.setdefault("yt_status", None)
        plan_entry.setdefault("audio_start", None)
        plan_entry.setdefault("audio_end", None)
        timestamped_plan.append(plan_entry)

    combined_plan = _reindex_v1_plan_entries(vid, [*existing_plan_entries, *timestamped_plan])

    SHORTS_DIR.mkdir(parents=True, exist_ok=True)
    _emit("save_plan", "Saving plan to disk.", clip_count=len(combined_plan))
    try:
        _write_plan_entries(vid, combined_plan)
    except Exception as pe:
        current_app.logger.warning("Failed to write plan file %s: %s", plan_path, pe)
        raise RuntimeError("Could not save the clip plan.")

    if debug_info:
        current_app.logger.info(
            "[CLIP_AGENT] finished windows=%s openai_calls=%s candidates_before_selector=%s clips_after_selector=%s",
            debug_info.get("window_count") or 0,
            debug_info.get("openai_call_count") or 0,
            debug_info.get("produced_clip_count") or 0,
            debug_info.get("clips_after_selector_count") or len(timestamped_plan),
        )

    focus_name = get_plan_focus_label(plan_focus)
    focus_label = f" ({focus_name})" if focus_name else ""
    return {
        "video_id": vid,
        "clip_count": len(timestamped_plan),
        "message": f"Clip plan{focus_label} added {len(timestamped_plan)} AI clips.",
    }


def _run_plan_job(video_pk: int, form_data: Dict[str, Any], app_obj) -> None:
    start_ts = time.monotonic()
    owner_user_id = form_data.get("_owner_user_id")
    brand_id = form_data.get("_brand_id")
    preloaded_video_info = app_obj.config.get("_plan_prefetched_video_info", {}).pop(video_pk, None)

    def _progress(stage: str, message: str, extra: Dict[str, Any]) -> None:
        payload: Dict[str, Any] = {
            "status": "running",
            "running": True,
            "stage": stage,
            "message": message,
            "elapsed_seconds": round(time.monotonic() - start_ts, 1),
        }
        if isinstance(extra, dict):
            payload.update(extra)
        _set_plan_job_state(video_pk, **payload)

    with app_obj.app_context():
        try:
            _set_plan_job_state(
                video_pk,
                status="running",
                running=True,
                stage="queued",
                message="Plan job queued.",
                started_at=datetime.utcnow().isoformat(),
                error=None,
                elapsed_seconds=0.0,
                form_data=form_data,
            )
            result = _generate_clip_plan_for_video(
                video_pk,
                form_data,
                debug_flag=str(form_data.get("debug") or "").strip() == "1",
                progress_cb=_progress,
                owner_user_id=owner_user_id,
                brand_id=brand_id,
                preloaded_video_info=preloaded_video_info,
            )
            elapsed = round(time.monotonic() - start_ts, 1)
            latest_state = _current_plan_job_state(video_pk)
            next_run_count = int(latest_state.get("run_count") or 0) + 1
            completed_at = datetime.utcnow().isoformat()
            _set_plan_job_state(
                video_pk,
                status="completed",
                running=False,
                stage="completed",
                message=result.get("message") or "Clip plan created.",
                clip_count=result.get("clip_count"),
                elapsed_seconds=elapsed,
                run_count=next_run_count,
                completed_at=completed_at,
                finished_at=datetime.utcnow().isoformat(),
                error=None,
            )
        except Exception as exc:
            elapsed = round(time.monotonic() - start_ts, 1)
            _set_plan_job_state(
                video_pk,
                status="failed",
                running=False,
                stage="failed",
                message=str(exc),
                error=str(exc),
                elapsed_seconds=elapsed,
                finished_at=datetime.utcnow().isoformat(),
            )
            app_obj.logger.exception("Background plan generation failed for video_pk=%s: %s", video_pk, exc)


@video_shorts_bp.route("/generate/<int:video_pk>/create_plan", methods=["POST"])
def create_clip_plan(video_pk):
    cleanup_video_shorts_temp_dir()
    try:
        result = _generate_clip_plan_for_video(
            video_pk,
            dict(request.form.items()),
            debug_flag=request.args.get("debug") == "1",
        )
    except FileNotFoundError:
        flash("Video not found", "danger")
        return redirect(url_for("video_shorts_bp.channels_page"))
    except RuntimeError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("video_shorts_bp.generate_short", video_pk=video_pk))

    flash(result["message"], "success")
    return redirect(url_for("video_shorts_bp.generate_short", video_pk=video_pk))


@video_shorts_bp.route("/generate/<int:video_pk>/create_plan/start", methods=["POST"])
def create_clip_plan_start(video_pk):
    try:
        video_info = _fetch_video_with_transcript(video_pk)
        if not video_info:
            return jsonify({"ok": False, "message": "Video not found."}), 404

        editor_context = _active_editor_context()
        form_data = dict(request.form.items())
        form_data["_owner_user_id"] = editor_context["owner_user_id"]
        form_data["_brand_id"] = editor_context["brand_id"]
        with _PLAN_JOB_LOCK:
            existing = dict(_PLAN_JOB_STATE.get(video_pk) or _load_plan_job_state(video_pk) or {})
            blocked = _clip_plan_block_info(existing)
            if blocked:
                return jsonify(
                    {
                        "ok": True,
                        "started": False,
                        "state": _sanitize_plan_state(existing),
                        "message": blocked["message"],
                        "reason": blocked["reason"],
                    }
                )
            next_state = dict(existing)
            next_state.update(
                {
                    "status": "running",
                    "running": True,
                    "stage": "starting",
                    "message": "Starting clip plan generation...",
                    "started_at": datetime.utcnow().isoformat(),
                    "error": None,
                    "elapsed_seconds": 0.0,
                    "form_data": form_data,
                }
            )
            next_state.pop("cooldown_remaining_seconds", None)
            _set_plan_job_state_locked(video_pk, next_state)
        prefetched = current_app.config.setdefault("_plan_prefetched_video_info", {})
        prefetched[video_pk] = video_info
        thread = threading.Thread(
            target=_run_plan_job,
            args=(video_pk, form_data, current_app._get_current_object()),
            daemon=True,
        )
        thread.start()
        return jsonify({"ok": True, "started": True, "message": "Clip plan generation started."})
    except Exception as exc:
        current_app.logger.exception("Failed to start plan job for video_pk=%s", video_pk)
        return jsonify({"ok": False, "message": str(exc)}), 500


@video_shorts_bp.route("/generate/<int:video_pk>/create_plan/status", methods=["GET"])
def create_clip_plan_status(video_pk):
    try:
        if not _resolve_video_id_from_pk(video_pk):
            return jsonify({"ok": False, "status": "failed", "message": "Video not found."}), 404
        state = _load_plan_job_state(video_pk)
        if not state:
            with _PLAN_JOB_LOCK:
                state = dict(_PLAN_JOB_STATE.get(video_pk) or {})
        if not state:
            return jsonify({"ok": True, "status": "idle", "running": False, "stage": "idle", "message": "No active plan job."})
        return jsonify({"ok": True, **_sanitize_plan_state(state)})
    except Exception as exc:
        current_app.logger.exception("Failed to read plan status for video_pk=%s", video_pk)
        return jsonify({"ok": False, "status": "failed", "message": str(exc)}), 500


@video_shorts_bp.route("/generate/<int:video_pk>/create_plan_v2", methods=["POST"])
def create_clip_plan_v2(video_pk):
    video_info = _fetch_video_with_transcript(video_pk)
    if not video_info:
        flash("Video not found", "danger")
        return redirect(url_for("video_shorts_bp.channels_page"))
    if not _openai_client:
        flash("OPENAI_API_KEY is missing; cannot create clip plan.", "danger")
        return redirect(url_for("video_shorts_bp.generate_short", video_pk=video_pk))
    vid, video_title, duration_seconds, transcript_text, segments = video_info
    computed_duration = int(duration_seconds or 0)
    if not computed_duration and segments:
        try:
            last = max(
                (
                    float(s.get("end", 0.0) or 0.0)
                    if s.get("end") is not None
                    else (float(s.get("start", 0.0) or 0.0) + float(s.get("duration", 0.0) or 0.0))
                )
                for s in segments
            )
            computed_duration = int(last)
        except Exception:
            computed_duration = 0

    font_key = request.form.get("font") or session.get("vs_font") or DEFAULT_EDITOR_TITLE_FONT_KEY
    session["vs_font"] = font_key
    sub_font_key = request.form.get("sub_font") or session.get("vs_sub_font") or DEFAULT_SUB_FONT_KEY
    session["vs_sub_font"] = sub_font_key
    try:
        title_font_size = int(
            request.form.get("title_font_size")
            or session.get("vs_title_font_size")
            or DEFAULT_EDITOR_TITLE_FONT_SIZE
        )
    except Exception:
        title_font_size = DEFAULT_EDITOR_TITLE_FONT_SIZE
    try:
        sub_font_size = int(request.form.get("sub_font_size") or session.get("vs_sub_font_size") or DEFAULT_SUB_FONT_SIZE)
    except Exception:
        sub_font_size = DEFAULT_SUB_FONT_SIZE
    try:
        sub_margin = int(request.form.get("sub_margin") or session.get("vs_sub_margin") or SUB_MARGIN_DEFAULT)
    except Exception:
        sub_margin = SUB_MARGIN_DEFAULT
    try:
        title_margin = int(
            request.form.get("title_margin")
            or session.get("vs_title_margin")
            or DEFAULT_TITLE_MARGIN
        )
    except Exception:
        title_margin = DEFAULT_TITLE_MARGIN
    title_bg_color = _normalize_hex_color(
        request.form.get("title_bg_color")
        or session.get("vs_title_bg_color")
        or DEFAULT_EDITOR_TITLE_BG_COLOR,
        DEFAULT_TITLE_BG_COLOR,
    )
    title_bg_alpha = _normalize_alpha_percent(
        request.form.get("title_bg_alpha")
        or session.get("vs_title_bg_alpha")
        or DEFAULT_EDITOR_TITLE_BG_ALPHA,
        DEFAULT_TITLE_BG_ALPHA,
    )
    title_text_color = _normalize_hex_color(
        request.form.get("title_text_color")
        or session.get("vs_title_text_color")
        or DEFAULT_EDITOR_TITLE_TEXT_COLOR,
        DEFAULT_TITLE_TEXT_COLOR,
    )
    subtitle_text_color = _normalize_hex_color(
        request.form.get("subtitle_text_color")
        or session.get("vs_subtitle_text_color")
        or DEFAULT_SUBTITLE_TEXT_COLOR,
        DEFAULT_SUBTITLE_TEXT_COLOR,
    )
    subtitle_style = (request.form.get("subtitle_style") or session.get("vs_subtitle_style") or "plain").strip().lower()
    if subtitle_style not in {"plain", "karaoke"}:
        subtitle_style = "plain"
    session["vs_title_font_size"] = title_font_size
    session["vs_sub_font_size"] = sub_font_size
    session["vs_sub_margin"] = sub_margin
    session["vs_title_margin"] = title_margin
    session["vs_title_bg_color"] = title_bg_color
    session["vs_title_bg_alpha"] = title_bg_alpha
    session["vs_title_text_color"] = title_text_color
    session["vs_subtitle_text_color"] = subtitle_text_color
    session["vs_subtitle_style"] = subtitle_style
    conn_update = get_db()
    try:
        conn_update.execute(
            """UPDATE youtube_videos
               SET title_font_key = ?, title_font_size = ?, subtitle_font_key = ?, subtitle_font_size = ?, subtitle_margin = ?, subtitle_style = ?, title_margin = ?, title_bg_color = ?, title_bg_alpha = ?, title_text_color = ?, subtitle_text_color = ?
             WHERE video_id = ?""",
            [font_key, title_font_size, sub_font_key, sub_font_size, sub_margin, subtitle_style, title_margin, title_bg_color, title_bg_alpha, title_text_color, subtitle_text_color, vid],
        )
        conn_update.commit()
    except Exception as exc:
        current_app.logger.warning(
            "Failed to persist font settings for video %s: %s", vid, exc
        )
    finally:
        conn_update.close()

    debug_flag = request.args.get("debug") == "1"
    clip_plan = []
    debug_info = {}
    plan_path = SHORTS_DIR / f"{vid}_plan_v2.json"
    v1_entries = _load_plan_entries(vid)
    if v1_entries:
        target_clip_count = len(v1_entries)
    else:
        target_clip_count = len(_fallback_clip_plan(computed_duration))
    try:
        clip_plan, debug_info = propose_clips_with_agents_v2(
            segments,
            transcript_text,
            computed_duration,
            _openai_client,
            OPENAI_MODEL,
            debug=debug_flag,
            target_clip_count=target_clip_count,
        )
    except Exception as ag:
        current_app.logger.warning("Agent v2 pipeline failed, falling back. %s", ag)

    if debug_info:
        try:
            debug_path = SHORTS_DIR / f"{vid}_debug_v2.json"
            debug_path.write_text(json.dumps(debug_info, ensure_ascii=False, indent=2))
        except Exception:
            pass
    if debug_info:
        win_info = debug_info.get("window_candidates") or []
        finalc = debug_info.get("final_plan") or []
        for idx, info in enumerate(win_info, 1):
            win = info.get("window", {}) or {}
            seg_cnt = info.get("seg_count")
            agent_cnt = len(info.get("agent_clips") or [])
            final_cnt = len(info.get("final_clips") or [])
            current_app.logger.info(
                "[CLIP_AGENT_V2] window %s: %.0f-%.0f sec, segs=%s, candidates=%s (agent raw=%s)",
                idx,
                win.get("start", 0),
                win.get("end", 0),
                seg_cnt,
                final_cnt,
                agent_cnt,
            )
        current_app.logger.info("[CLIP_AGENT_V2] final=%s clips", len(finalc))

    if not clip_plan:
        fallback = _fallback_clip_plan(computed_duration)
        clip_plan = fallback
    if not clip_plan:
        flash("LLM did not return any clip suggestions and fallback couldn't generate clips.", "warning")
        return redirect(url_for("video_shorts_bp.generate_short", video_pk=video_pk))

    timestamped_plan = []
    for idx, clip in enumerate(clip_plan):
        plan_entry = dict(clip, plan_index=idx + 1)
        start = plan_entry.get("start")
        end = plan_entry.get("end")
        if segments and start is not None and end is not None and "transcript_full" not in plan_entry:
            try:
                plan_entry["transcript_full"] = build_transcript_for_range(segments, start, end, prefer_tr=True)
            except Exception:
                pass
        plan_entry["language"] = _infer_clip_language_from_segments(
            segments,
            start,
            end,
            excerpt=plan_entry.get("transcript_full") or plan_entry.get("excerpt") or "",
        )
        plan_entry["status"] = "pending"
        plan_entry["clip_filename"] = plan_entry.get("clip_filename") or f"{idx + 1}_{vid}.mp4"
        plan_entry["publish_status"] = "not_ready"
        plan_entry.setdefault("yt_description", None)
        plan_entry.setdefault("yt_status", None)
        plan_entry.setdefault("audio_start", None)
        plan_entry.setdefault("audio_end", None)
        timestamped_plan.append(plan_entry)

    SHORTS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        _write_plan_entries_v2(vid, timestamped_plan)
    except Exception as pe:
        current_app.logger.warning("Failed to write plan file %s: %s", plan_path, pe)
        flash("Could not save the clip plan.", "danger")
        return redirect(url_for("video_shorts_bp.generate_short", video_pk=video_pk))

    flash(f"Clip plan v2 created with {len(timestamped_plan)} clips.", "success")
    return redirect(url_for("video_shorts_bp.generate_short", video_pk=video_pk))


@video_shorts_bp.route("/generate/<int:video_pk>/create_plan_v3", methods=["POST"])
def create_clip_plan_v3(video_pk):
    video_info = _fetch_video_with_transcript(video_pk)
    if not video_info:
        flash("Video not found", "danger")
        return redirect(url_for("video_shorts_bp.channels_page"))
    if not _openai_client:
        flash("OPENAI_API_KEY is missing; cannot create clip plan.", "danger")
        return redirect(url_for("video_shorts_bp.generate_short", video_pk=video_pk))
    vid, video_title, duration_seconds, transcript_text, segments = video_info
    non_speech_overrides = load_non_speech_overrides(vid)
    if non_speech_overrides:
        for idx_str, override in non_speech_overrides.items():
            try:
                idx = int(idx_str)
            except Exception:
                continue
            if idx < 0 or idx >= len(segments):
                continue
            if override:
                segments[idx]["non_speech_type"] = override
    computed_duration = int(duration_seconds or 0)
    if not computed_duration and segments:
        try:
            last = max(
                (
                    float(s.get("end", 0.0) or 0.0)
                    if s.get("end") is not None
                    else (float(s.get("start", 0.0) or 0.0) + float(s.get("duration", 0.0) or 0.0))
                )
                for s in segments
            )
            computed_duration = int(last)
        except Exception:
            computed_duration = 0

    debug_flag = request.args.get("debug") == "1"
    clip_plan = []
    debug_info = {}
    plan_path = SHORTS_DIR / f"{vid}_plan_v3.json"
    try:
        clip_plan, debug_info = propose_clips_with_agents_v3(
            segments,
            transcript_text,
            computed_duration,
            _openai_client,
            OPENAI_MODEL,
            debug=debug_flag,
        )
    except Exception as ag:
        current_app.logger.warning("Agent v3 pipeline failed, falling back. %s", ag)

    if debug_info:
        try:
            debug_path = SHORTS_DIR / f"{vid}_debug_v3.json"
            debug_path.write_text(json.dumps(debug_info, ensure_ascii=False, indent=2))
        except Exception:
            pass

    if not clip_plan:
        fallback = _fallback_clip_plan(computed_duration)
        clip_plan = fallback
    if not clip_plan:
        flash("LLM did not return any clip suggestions and fallback couldn't generate clips.", "warning")
        return redirect(url_for("video_shorts_bp.generate_short", video_pk=video_pk))

    timestamped_plan = []
    for idx, clip in enumerate(clip_plan):
        plan_entry = dict(clip, plan_index=idx + 1)
        start = plan_entry.get("start") or plan_entry.get("start_time")
        end = plan_entry.get("end") or plan_entry.get("end_time")
        plan_entry["start"] = start
        plan_entry["end"] = end
        if segments and start is not None and end is not None and "transcript_full" not in plan_entry:
            try:
                plan_entry["transcript_full"] = build_transcript_for_range(segments, start, end, prefer_tr=True)
            except Exception:
                pass
        plan_entry["language"] = _infer_clip_language_from_segments(
            segments,
            start,
            end,
            excerpt=plan_entry.get("transcript_full") or plan_entry.get("excerpt") or "",
        )
        plan_entry["status"] = "pending"
        plan_entry["clip_filename"] = plan_entry.get("clip_filename") or f"{idx + 1}_{vid}.mp4"
        plan_entry["publish_status"] = "not_ready"
        plan_entry.setdefault("yt_description", None)
        plan_entry.setdefault("yt_status", None)
        plan_entry.setdefault("audio_start", None)
        plan_entry.setdefault("audio_end", None)
        timestamped_plan.append(plan_entry)

    SHORTS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        _write_plan_entries_v3(vid, timestamped_plan)
    except Exception as pe:
        current_app.logger.warning("Failed to write plan file %s: %s", plan_path, pe)
        flash("Could not save the clip plan.", "danger")
        return redirect(url_for("video_shorts_bp.generate_short", video_pk=video_pk))

    flash(f"Clip plan v3 created with {len(timestamped_plan)} clips.", "success")
    return redirect(url_for("video_shorts_bp.generate_short", video_pk=video_pk))


@video_shorts_bp.route("/generate/<int:video_pk>/create_plan_v4", methods=["POST"])
def create_clip_plan_v4(video_pk):
    video_info = _fetch_video_with_transcript(video_pk)
    if not video_info:
        flash("Video not found", "danger")
        return redirect(url_for("video_shorts_bp.channels_page"))
    if not _openai_client:
        flash("OPENAI_API_KEY is missing; cannot create clip plan.", "danger")
        return redirect(url_for("video_shorts_bp.generate_short", video_pk=video_pk))
    vid, video_title, duration_seconds, transcript_text, segments = video_info
    non_speech_overrides = load_non_speech_overrides(vid)
    if non_speech_overrides:
        for idx_str, override in non_speech_overrides.items():
            try:
                idx = int(idx_str)
            except Exception:
                continue
            if idx < 0 or idx >= len(segments):
                continue
            if override:
                segments[idx]["non_speech_type"] = override
    computed_duration = int(duration_seconds or 0)
    if not computed_duration and segments:
        try:
            last = max(
                (
                    float(s.get("end", 0.0) or 0.0)
                    if s.get("end") is not None
                    else (float(s.get("start", 0.0) or 0.0) + float(s.get("duration", 0.0) or 0.0))
                )
                for s in segments
            )
            computed_duration = int(last)
        except Exception:
            computed_duration = 0

    debug_flag = request.args.get("debug") == "1"
    clip_plan = []
    debug_info = {}
    plan_path = SHORTS_DIR / f"{vid}_plan_v4.json"
    try:
        clip_plan, debug_info = propose_clips_with_agents_v4(
            segments,
            transcript_text,
            computed_duration,
            _openai_client,
            OPENAI_MODEL,
            debug=debug_flag,
        )
    except Exception as ag:
        current_app.logger.warning("Agent v4 pipeline failed, falling back. %s", ag)

    if debug_info:
        try:
            debug_path = SHORTS_DIR / f"{vid}_debug_v4.json"
            debug_path.write_text(json.dumps(debug_info, ensure_ascii=False, indent=2))
        except Exception:
            pass

    if not clip_plan:
        fallback = _fallback_clip_plan(computed_duration)
        clip_plan = fallback
    if not clip_plan:
        flash("LLM did not return any clip suggestions and fallback couldn't generate clips.", "warning")
        return redirect(url_for("video_shorts_bp.generate_short", video_pk=video_pk))

    timestamped_plan = []
    for idx, clip in enumerate(clip_plan):
        plan_entry = dict(clip, plan_index=idx + 1)
        start = plan_entry.get("start") or plan_entry.get("start_time")
        end = plan_entry.get("end") or plan_entry.get("end_time")
        plan_entry["start"] = start
        plan_entry["end"] = end
        if segments and start is not None and end is not None and "transcript_full" not in plan_entry:
            try:
                plan_entry["transcript_full"] = build_transcript_for_range(segments, start, end, prefer_tr=True)
            except Exception:
                pass
        plan_entry["language"] = _infer_clip_language_from_segments(
            segments,
            start,
            end,
            excerpt=plan_entry.get("transcript_full") or plan_entry.get("excerpt") or "",
        )
        plan_entry["status"] = "pending"
        plan_entry["clip_filename"] = plan_entry.get("clip_filename") or f"{idx + 1}_{vid}.mp4"
        plan_entry["publish_status"] = "not_ready"
        plan_entry.setdefault("yt_description", None)
        plan_entry.setdefault("yt_status", None)
        plan_entry.setdefault("audio_start", None)
        plan_entry.setdefault("audio_end", None)
        timestamped_plan.append(plan_entry)

    SHORTS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        _write_plan_entries_v4(vid, timestamped_plan)
    except Exception as pe:
        current_app.logger.warning("Failed to write plan file %s: %s", plan_path, pe)
        flash("Could not save the clip plan.", "danger")
        return redirect(url_for("video_shorts_bp.generate_short", video_pk=video_pk))

    flash(f"Clip plan v4 created with {len(timestamped_plan)} clips.", "success")
    return redirect(url_for("video_shorts_bp.generate_short", video_pk=video_pk))

@video_shorts_bp.route("/generate/<int:video_pk>/delete_plan_v2", methods=["POST"])
def delete_clip_plan_v2(video_pk):
    video_info = _fetch_video_with_transcript(video_pk)
    if not video_info:
        flash("Video not found", "danger")
        return redirect(url_for("video_shorts_bp.channels_page"))
    vid, _, _, _, _ = video_info
    plan_path = SHORTS_DIR / f"{vid}_plan_v2.json"
    debug_path = SHORTS_DIR / f"{vid}_debug_v2.json"
    deleted_any = False
    try:
        if plan_path.exists():
            plan_path.unlink()
            deleted_any = True
    except Exception:
        current_app.logger.warning("Failed to delete v2 plan file %s", plan_path)
    try:
        if debug_path.exists():
            debug_path.unlink()
            deleted_any = True
    except Exception:
        current_app.logger.warning("Failed to delete v2 debug file %s", debug_path)
    if deleted_any:
        flash("V2 clip plan deleted.", "success")
    else:
        flash("V2 clip plan not found.", "warning")
    return redirect(url_for("video_shorts_bp.generate_short", video_pk=video_pk))


@video_shorts_bp.route("/generate/<int:video_pk>/v2_rules/add_keyword", methods=["POST"])
def add_v2_non_speech_keyword(video_pk):
    current_user = getattr(g, "vs_current_user", None)
    if not current_user or current_user.get("role") != "admin":
        flash("You do not have permission to update rules.", "danger")
        return redirect(url_for("video_shorts_bp.generate_short", video_pk=video_pk))
    keyword = (request.form.get("keyword") or "").strip()
    if not keyword:
        flash("Keyword is required.", "warning")
        return redirect(url_for("video_shorts_bp.generate_short", video_pk=video_pk))
    add_non_speech_keyword(keyword)
    flash("Non-speech keyword added.", "success")
    return redirect(url_for("video_shorts_bp.generate_short", video_pk=video_pk))


@video_shorts_bp.route("/generate/<int:video_pk>/save_short_settings", methods=["POST"])
def save_short_settings(video_pk):
    current_user = getattr(g, "vs_current_user", None)
    editor_context = _active_editor_context()
    brand_id = editor_context["brand_id"]
    editor_owner_user_id = editor_context["owner_user_id"]
    font_key = request.form.get("font") or DEFAULT_EDITOR_TITLE_FONT_KEY
    sub_font_key = request.form.get("sub_font") or DEFAULT_SUB_FONT_KEY
    try:
        title_font_size = int(request.form.get("title_font_size") or DEFAULT_EDITOR_TITLE_FONT_SIZE)
    except Exception:
        title_font_size = DEFAULT_EDITOR_TITLE_FONT_SIZE
    try:
        subtitle_font_size = int(request.form.get("sub_font_size") or DEFAULT_SUB_FONT_SIZE)
    except Exception:
        subtitle_font_size = DEFAULT_SUB_FONT_SIZE
    try:
        subtitle_margin = int(request.form.get("sub_margin") or SUB_MARGIN_DEFAULT)
    except Exception:
        subtitle_margin = SUB_MARGIN_DEFAULT
    try:
        title_margin = int(request.form.get("title_margin") or DEFAULT_TITLE_MARGIN)
    except Exception:
        title_margin = DEFAULT_TITLE_MARGIN
    try:
        title_line_spacing = int(request.form.get("title_line_spacing") or session.get("vs_title_line_spacing") or -4)
    except Exception:
        title_line_spacing = -4
    try:
        video_overlay_offset = int(request.form.get("video_overlay_offset") or DEFAULT_VIDEO_OVERLAY_OFFSET)
    except Exception:
        video_overlay_offset = DEFAULT_VIDEO_OVERLAY_OFFSET
    try:
        video_date_top = int(request.form.get("video_date_top") or DEFAULT_VIDEO_DATE_TOP)
    except Exception:
        video_date_top = DEFAULT_VIDEO_DATE_TOP
    title_bg_color = _normalize_hex_color(request.form.get("title_bg_color") or DEFAULT_EDITOR_TITLE_BG_COLOR, DEFAULT_TITLE_BG_COLOR)
    title_bg_alpha = _normalize_alpha_percent(request.form.get("title_bg_alpha") or DEFAULT_EDITOR_TITLE_BG_ALPHA, DEFAULT_TITLE_BG_ALPHA)
    title_text_color = _normalize_hex_color(request.form.get("title_text_color") or DEFAULT_EDITOR_TITLE_TEXT_COLOR, DEFAULT_TITLE_TEXT_COLOR)
    subtitle_text_color = _normalize_hex_color(request.form.get("subtitle_text_color") or DEFAULT_SUBTITLE_TEXT_COLOR, DEFAULT_SUBTITLE_TEXT_COLOR)
    subtitle_bg_color = _normalize_hex_color(request.form.get("subtitle_bg_color") or DEFAULT_SUBTITLE_BG_COLOR, DEFAULT_SUBTITLE_BG_COLOR)
    subtitle_bg_alpha = _normalize_alpha_percent(request.form.get("subtitle_bg_alpha") or DEFAULT_SUBTITLE_BG_ALPHA, DEFAULT_SUBTITLE_BG_ALPHA)
    subtitle_text_alpha = _normalize_alpha_percent(request.form.get("subtitle_text_alpha") or DEFAULT_SUBTITLE_TEXT_ALPHA, DEFAULT_SUBTITLE_TEXT_ALPHA)
    subtitle_style = (request.form.get("subtitle_style") or "plain").strip().lower()
    if subtitle_style not in {"plain", "karaoke"}:
        subtitle_style = "plain"
    subtitle_preset = str(request.form.get("subtitle_preset") or DEFAULT_SUBTITLE_PRESET).strip() or DEFAULT_SUBTITLE_PRESET
    if subtitle_preset not in SUBTITLE_PRESETS:
        subtitle_preset = DEFAULT_SUBTITLE_PRESET
    subscribe_overlay_key = _normalize_subscribe_overlay_key(
        request.form.get("subscribe_overlay_image"),
        expected_owner_user_id=editor_owner_user_id,
        expected_brand_id=brand_id,
    )
    subscribe_overlay_value = request.form.get("enable_subscribe_overlay")
    subscribe_overlay_enabled = (subscribe_overlay_value or "").lower() in {"1", "true", "yes", "on"}
    show_title = (request.form.get("show_title") or "").lower() not in {"0", "false", "no", "off"}
    show_subtitle = (request.form.get("show_subtitle") or "").lower() not in {"0", "false", "no", "off"}
    is_music_only = (request.form.get("is_music_only") or "").lower() in {"1", "true", "yes", "on"}
    visual_mode = (request.form.get("visual_mode") or "").strip().lower()
    if visual_mode not in {"video", "static", "created", "podcast"}:
        visual_mode = "video"
    podcast_audio_filename = Path((request.form.get("podcast_audio_filename") or "").strip()).name
    if podcast_audio_filename and podcast_audio_filename != (request.form.get("podcast_audio_filename") or "").strip():
        podcast_audio_filename = ""
    if visual_mode != "podcast":
        podcast_audio_filename = ""
    podcast_overlay_short_ids: List[str] = []
    raw_overlay_short_ids = (request.form.get("podcast_overlay_short_ids") or "").strip()
    if visual_mode == "podcast" and raw_overlay_short_ids:
        try:
            parsed_overlay_short_ids = json.loads(raw_overlay_short_ids)
            if isinstance(parsed_overlay_short_ids, list):
                podcast_overlay_short_ids = [
                    str(item).strip()
                    for item in parsed_overlay_short_ids
                    if str(item or "").strip().startswith("short:")
                ]
        except Exception:
            podcast_overlay_short_ids = []
    podcast_overlay_short_ids = list(dict.fromkeys(podcast_overlay_short_ids))[:2]

    conn = get_db()
    _ensure_video_crop_schema(conn)
    video_columns = table_columns(conn, "youtube_videos")
    select_sql = """
        SELECT video_id, subscribe_overlay_enabled
        FROM youtube_videos
        WHERE id = ?
          AND owner_user_id = ?
    """
    select_params: List[Any] = [video_pk, editor_owner_user_id]
    if "brand_id" in video_columns:
        if brand_id is None:
            select_sql += "\n AND brand_id IS NULL"
        else:
            select_sql += "\n AND brand_id = ?"
            select_params.append(brand_id)
    row = conn.execute(select_sql, select_params).fetchone()
    if not row:
        conn.close()
        return jsonify(success=False, message="Video not found"), 404
    if subscribe_overlay_value is None:
        subscribe_overlay_enabled = bool(row[1]) if len(row) > 1 and row[1] is not None else True
    if podcast_audio_filename and editor_owner_user_id:
        if not _resolve_user_podcast_audio_path(editor_owner_user_id, podcast_audio_filename):
            podcast_audio_filename = ""
    if editor_owner_user_id and podcast_overlay_short_ids:
        try:
            allowed_paths = _resolve_user_short_clip_source_paths(editor_owner_user_id, podcast_overlay_short_ids, max_items=2)
            allowed_keys = {f"short:{path.name}" for path in allowed_paths}
            podcast_overlay_short_ids = [key for key in podcast_overlay_short_ids if key in allowed_keys][:2]
        except Exception as exc:
            current_app.logger.warning("Podcast overlay short validation skipped for %s: %s", video_pk, exc)
            podcast_overlay_short_ids = podcast_overlay_short_ids[:2]
    elif visual_mode != "podcast":
        podcast_overlay_short_ids = []
    video_date_text = (request.form.get("video_date_text") or "").strip()
    try:
        update_values = {
            "title_font_key": font_key,
            "title_font_size": title_font_size,
            "subtitle_font_key": sub_font_key,
            "subtitle_font_size": subtitle_font_size,
            "subtitle_margin": subtitle_margin,
            "subtitle_style": subtitle_style,
            "subtitle_preset": subtitle_preset,
            "title_margin": title_margin,
            "title_line_spacing": title_line_spacing,
            "title_bg_color": title_bg_color,
            "title_bg_alpha": title_bg_alpha,
            "title_text_color": title_text_color,
            "subtitle_text_color": subtitle_text_color,
            "subtitle_bg_color": subtitle_bg_color,
            "subtitle_bg_alpha": subtitle_bg_alpha,
            "subtitle_text_alpha": subtitle_text_alpha,
            "video_date_text": video_date_text,
            "video_date_top": video_date_top,
            "show_title": show_title,
            "show_subtitle": show_subtitle,
            "subscribe_overlay_enabled": subscribe_overlay_enabled,
            "is_music_only": is_music_only,
            "video_overlay_offset": video_overlay_offset,
            "podcast_audio_filename": podcast_audio_filename or None,
            "visual_mode": visual_mode,
            "podcast_overlay_short_ids": json.dumps(podcast_overlay_short_ids, ensure_ascii=False),
        }
        assignments = []
        params: List[Any] = []
        for column, value in update_values.items():
            if column in video_columns:
                assignments.append(f"{column} = ?")
                params.append(value)
        if assignments:
            params.extend([video_pk, editor_owner_user_id])
            where_brand = ""
            if "brand_id" in video_columns:
                if brand_id is None:
                    where_brand = "\n AND brand_id IS NULL"
                else:
                    where_brand = "\n AND brand_id = ?"
                    params.append(brand_id)
            conn.execute(
                f"""
                UPDATE youtube_videos
                SET {", ".join(assignments)}
                WHERE id = ?
                  AND owner_user_id = ?
                  {where_brand}
                """,
                params,
            )
        conn.commit()
        _save_subscribe_overlay_key(
            editor_owner_user_id,
            brand_id,
            video_pk,
            subscribe_overlay_key,
        )
    except Exception as exc:
        conn.close()
        current_app.logger.warning("Failed to save short settings for %s: %s", video_pk, exc)
        return jsonify(success=False, message="Ayarlar kaydedilemedi"), 500
    conn.close()

    session["vs_font"] = font_key
    session["vs_title_font_size"] = title_font_size
    session["vs_sub_font"] = sub_font_key
    session["vs_sub_font_size"] = subtitle_font_size
    session["vs_sub_margin"] = subtitle_margin
    session["vs_subtitle_style"] = subtitle_style
    session["vs_subtitle_preset"] = subtitle_preset
    session["vs_title_margin"] = title_margin
    session["vs_title_line_spacing"] = title_line_spacing
    session["vs_title_bg_color"] = title_bg_color
    session["vs_title_bg_alpha"] = title_bg_alpha
    session["vs_title_text_color"] = title_text_color
    session["vs_subtitle_text_color"] = subtitle_text_color
    session["vs_subtitle_bg_color"] = subtitle_bg_color
    session["vs_subtitle_bg_alpha"] = subtitle_bg_alpha
    session["vs_subtitle_text_alpha"] = subtitle_text_alpha
    session["vs_video_date_text"] = video_date_text
    session["vs_video_date_top"] = video_date_top
    session["vs_show_title"] = show_title
    session["vs_show_subtitle"] = show_subtitle
    session["vs_subscribe_overlay"] = subscribe_overlay_enabled
    session["vs_is_music_only"] = is_music_only
    session["vs_podcast_audio_filename"] = podcast_audio_filename
    session["vs_visual_mode"] = visual_mode
    session["vs_podcast_overlay_short_ids"] = podcast_overlay_short_ids
    session["vs_video_overlay_offset"] = video_overlay_offset
    return jsonify(success=True, message="Ayarlar kaydedildi")


@video_shorts_bp.route("/generate/<int:video_pk>/render-jobs/<job_id>/status", methods=["GET"])
@require_admin
def admin_operation_render_job_status(video_pk: int, job_id: str):
    """Return one target-owned render job for the marked admin editor request."""
    scope = request_admin_operation_scope()
    if not scope:
        abort(404)

    job = get_job(job_id, user_id=str(scope["owner_user_id"]))
    payload = (job or {}).get("payload") or {}
    if (
        not job
        or str(payload.get("brand_id") or "") != str(scope["brand_id"])
        or str(payload.get("video_pk") or "") != str(video_pk)
    ):
        abort(404)

    error_text = str(job.get("error") or "").strip()
    error_code = None
    lowered_error = error_text.lower()
    if "export limit reached" in lowered_error or "monthly export limit reached" in lowered_error:
        error_code = "export_limit_reached"
    return jsonify(
        {
            "id": job["id"],
            "status": job["status"],
            "priority": job["priority"],
            "created_at": job["created_at"],
            "started_at": job["started_at"],
            "finished_at": job["finished_at"],
            "result": job.get("result"),
            "error": job.get("error"),
            "error_code": error_code,
            "queue_position": job.get("queue_position"),
        }
    )


@video_shorts_bp.route("/generate/<int:video_pk>/autoclip", methods=["POST"])
def autoclip_video(video_pk):
    cleanup_video_shorts_temp_dir()
    plan_index_raw = (request.form.get("plan_index") or "").strip()
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    queued_job = (request.form.get("_queued_job") or "").strip() in {"1", "true", "yes"}

    def _respond(message, success=False, status=200, category="info", extras=None, redirect_to=None):
        if is_ajax:
            payload = {"success": success, "message": message}
            if extras:
                payload.update(extras)
            return jsonify(payload), status
        if category:
            flash(message, category)
        target = redirect_to or url_for("video_shorts_bp.generate_short", video_pk=video_pk)
        return redirect(target)

    current_user = getattr(g, "vs_current_user", None)
    editor_context = _active_editor_context()
    target_owner_user_id = editor_context["owner_user_id"]
    brand_id = editor_context["brand_id"]
    export_reserved = False
    usage = None
    if not current_user:
        return _respond("Authentication required.", success=False, status=401, category="danger")
    if current_user:
        conn_usage = get_db_readonly()
        try:
            usage = _get_user_storage_usage(conn_usage, target_owner_user_id)
        finally:
            conn_usage.close()
        if usage["used_bytes"] >= usage["limit_bytes"]:
            return _respond(
                _quota_block_message(
                    _format_size_bytes(usage["limit_bytes"]),
                    _format_size_bytes(usage["used_bytes"]),
                ),
                success=False,
                status=403,
                category="danger",
                redirect_to=url_for("video_shorts_bp.shorts_storage_plans"),
            )

    if not plan_index_raw:
        return _respond("Clip index is missing.", status=400, category="danger")
    try:
        plan_index = int(plan_index_raw)
    except Exception:
        return _respond("Clip index is invalid.", status=400, category="danger")

    video_info = _fetch_video_with_transcript(video_pk)
    if not video_info:
        return _respond(
            "Video not found",
            status=404,
            category="danger",
            redirect_to=url_for("video_shorts_bp.channels_page"),
        )
    vid, video_title, duration_seconds, _, segments = video_info
    plan_entries = _load_plan_entries(vid)
    video_crop_ratios = {}
    video_static_visual_key = None
    video_background_visual_key = None
    video_crop_aspect = "landscape"
    video_visual_mode = "video"
    video_podcast_overlay_short_ids: List[str] = []
    video_font_key = None
    video_title_font_size = None
    video_sub_font_key = None
    video_sub_font_size = None
    video_sub_margin = None
    video_title_margin = None
    video_title_line_spacing = -4
    video_title_bg_color = None
    video_title_bg_alpha = DEFAULT_EDITOR_TITLE_BG_ALPHA
    video_title_text_color = None
    video_subtitle_text_color = None
    video_subtitle_bg_color = None
    video_subtitle_bg_alpha = DEFAULT_SUBTITLE_BG_ALPHA
    video_subtitle_text_alpha = DEFAULT_SUBTITLE_TEXT_ALPHA
    video_subtitle_style = "plain"
    video_subtitle_preset = DEFAULT_SUBTITLE_PRESET
    video_date_text = None
    video_date_top = DEFAULT_VIDEO_DATE_TOP
    video_subscribe_overlay = True
    video_show_title = True
    video_show_subtitle = True
    video_is_music_only = False
    video_podcast_audio_filename = ""
    video_overlay_offset = DEFAULT_VIDEO_OVERLAY_OFFSET
    video_owner_user_id = None
    try:
        conn_rw = get_db()
        _ensure_video_crop_schema(conn_rw)
    finally:
        try:
            conn_rw.close()
        except Exception:
            pass
    conn = get_db_readonly()
    try:
        crop_columns = table_columns(conn, "youtube_videos")
        crop_sql = (
            "SELECT split_enabled, crop_x_ratio, crop_y_ratio, crop_w_ratio, crop_h_ratio, crop2_x_ratio, crop2_y_ratio, crop2_w_ratio, crop2_h_ratio, "
            "crop_aspect, "
            "title_font_key, title_font_size, subtitle_font_key, subtitle_font_size, subtitle_margin, title_margin, title_line_spacing, title_bg_color, video_date_text, video_date_top, show_title, show_subtitle, subscribe_overlay_enabled, is_music_only, static_visual_key, background_visual_key, video_overlay_offset, podcast_audio_filename, visual_mode, podcast_overlay_short_ids, owner_user_id, title_text_color, subtitle_text_color, title_bg_alpha, subtitle_bg_color, subtitle_bg_alpha, subtitle_text_alpha, subtitle_style"
        )
        if "subtitle_preset" in crop_columns:
            crop_sql += ", subtitle_preset"
        crop_sql += " FROM youtube_videos WHERE video_id = ?"
        crop_params: List[Any] = [vid]
        crop_sql += " AND owner_user_id = ? AND brand_id = ?"
        crop_params.extend([target_owner_user_id, brand_id])
        crop_row = conn.execute(crop_sql, crop_params).fetchone()
        if crop_row:
            # Backward/forward compatible fetch across schema versions.
            if len(crop_row) >= 39:
                query_indexes = {
                    "split_enabled": 0,
                    "crop_aspect": 9,
                    "title_font_key": 10,
                    "title_font_size": 11,
                    "subtitle_font_key": 12,
                    "subtitle_font_size": 13,
                    "subtitle_margin": 14,
                    "title_margin": 15,
                    "title_line_spacing": 16,
                    "title_bg_color": 17,
                    "video_date_text": 18,
                    "video_date_top": 19,
                    "show_title": 20,
                    "show_subtitle": 21,
                    "subscribe_overlay_enabled": 22,
                    "is_music_only": 23,
                    "static_visual_key": 24,
                    "background_visual_key": 25,
                    "video_overlay_offset": 26,
                    "podcast_audio_filename": 27,
                    "visual_mode": 28,
                    "podcast_overlay_short_ids": 29,
                    "owner_user_id": 30,
                    "title_text_color": 31,
                    "subtitle_text_color": 32,
                    "title_bg_alpha": 33,
                    "subtitle_bg_color": 34,
                    "subtitle_bg_alpha": 35,
                    "subtitle_text_alpha": 36,
                    "subtitle_style": 37,
                    "subtitle_preset": 38,
                }
            elif len(crop_row) >= 38:
                query_indexes = {
                    "split_enabled": 0,
                    "crop_aspect": 9,
                    "title_font_key": 10,
                    "title_font_size": 11,
                    "subtitle_font_key": 12,
                    "subtitle_font_size": 13,
                    "subtitle_margin": 14,
                    "title_margin": 15,
                    "title_line_spacing": 16,
                    "title_bg_color": 17,
                    "video_date_text": 18,
                    "video_date_top": 19,
                    "show_title": 20,
                    "show_subtitle": 21,
                    "subscribe_overlay_enabled": 22,
                    "is_music_only": 23,
                    "static_visual_key": 24,
                    "background_visual_key": 25,
                    "video_overlay_offset": 26,
                    "podcast_audio_filename": 27,
                    "visual_mode": 28,
                    "podcast_overlay_short_ids": 29,
                    "owner_user_id": 30,
                    "title_text_color": 31,
                    "subtitle_text_color": 32,
                    "title_bg_alpha": 33,
                    "subtitle_bg_color": 34,
                    "subtitle_bg_alpha": 35,
                    "subtitle_text_alpha": 36,
                    "subtitle_style": 37,
                }
            elif len(crop_row) >= 37:
                query_indexes = {
                    "split_enabled": 0,
                    "crop_aspect": 9,
                    "title_font_key": 10,
                    "title_font_size": 11,
                    "subtitle_font_key": 12,
                    "subtitle_font_size": 13,
                    "subtitle_margin": 14,
                    "title_margin": 15,
                    "title_line_spacing": 16,
                    "title_bg_color": 17,
                    "video_date_text": 18,
                    "video_date_top": 19,
                    "show_title": 20,
                    "show_subtitle": 21,
                    "subscribe_overlay_enabled": 22,
                    "is_music_only": 23,
                    "static_visual_key": 24,
                    "background_visual_key": 25,
                    "video_overlay_offset": 26,
                    "podcast_audio_filename": 27,
                    "visual_mode": 28,
                    "podcast_overlay_short_ids": 29,
                    "owner_user_id": 30,
                    "title_text_color": 31,
                    "subtitle_text_color": 32,
                    "title_bg_alpha": 33,
                    "subtitle_bg_color": 34,
                    "subtitle_bg_alpha": 35,
                    "subtitle_text_alpha": 36,
                    "subtitle_style": None,
                }
            elif len(crop_row) >= 35:
                query_indexes = {
                    "split_enabled": 0,
                    "crop_aspect": 9,
                    "title_font_key": 10,
                    "title_font_size": 11,
                    "subtitle_font_key": 12,
                    "subtitle_font_size": 13,
                    "subtitle_margin": 14,
                    "title_margin": 15,
                    "title_line_spacing": 16,
                    "title_bg_color": 17,
                    "video_date_text": 18,
                    "video_date_top": 19,
                    "show_title": None,
                    "show_subtitle": None,
                    "subscribe_overlay_enabled": 20,
                    "is_music_only": 21,
                    "static_visual_key": 22,
                    "background_visual_key": 23,
                    "video_overlay_offset": 24,
                    "podcast_audio_filename": 25,
                    "visual_mode": 26,
                    "podcast_overlay_short_ids": 27,
                    "owner_user_id": 28,
                    "title_text_color": 29,
                    "subtitle_text_color": 30,
                    "title_bg_alpha": 31,
                    "subtitle_bg_color": 32,
                    "subtitle_bg_alpha": 33,
                    "subtitle_text_alpha": 34,
                    "subtitle_style": None,
                }
            elif len(crop_row) >= 30:
                query_indexes = {
                    "split_enabled": None,
                    "crop_aspect": 4,
                    "title_font_key": 5,
                    "title_font_size": 6,
                    "subtitle_font_key": 7,
                    "subtitle_font_size": 8,
                    "subtitle_margin": 9,
                    "title_margin": 10,
                    "title_line_spacing": 11,
                    "title_bg_color": 12,
                    "video_date_text": 13,
                    "video_date_top": 14,
                    "show_title": None,
                    "show_subtitle": None,
                    "subscribe_overlay_enabled": 15,
                    "is_music_only": 16,
                    "static_visual_key": 17,
                    "background_visual_key": 18,
                    "video_overlay_offset": 19,
                    "podcast_audio_filename": 20,
                    "visual_mode": 21,
                    "podcast_overlay_short_ids": 22,
                    "owner_user_id": 23,
                    "title_text_color": 24,
                    "subtitle_text_color": 25,
                    "title_bg_alpha": 26,
                    "subtitle_bg_color": 27,
                    "subtitle_bg_alpha": 28,
                    "subtitle_text_alpha": 29,
                    "subtitle_style": None,
                }
            elif len(crop_row) >= 29:
                query_indexes = {
                    "split_enabled": None,
                    "crop_aspect": 4,
                    "title_font_key": 5,
                    "title_font_size": 6,
                    "subtitle_font_key": 7,
                    "subtitle_font_size": 8,
                    "subtitle_margin": 9,
                    "title_margin": 10,
                    "title_line_spacing": 11,
                    "title_bg_color": 12,
                    "video_date_text": 13,
                    "video_date_top": None,
                    "show_title": None,
                    "show_subtitle": None,
                    "subscribe_overlay_enabled": 14,
                    "is_music_only": 15,
                    "static_visual_key": 16,
                    "background_visual_key": 17,
                    "video_overlay_offset": 18,
                    "podcast_audio_filename": 19,
                    "visual_mode": 20,
                    "podcast_overlay_short_ids": 21,
                    "owner_user_id": 22,
                    "title_text_color": 23,
                    "subtitle_text_color": 24,
                    "title_bg_alpha": 25,
                    "subtitle_bg_color": None,
                    "subtitle_bg_alpha": None,
                    "subtitle_text_alpha": None,
                    "subtitle_style": None,
                }
            elif len(crop_row) >= 23:
                query_indexes = {
                    "split_enabled": None,
                    "crop_aspect": 4,
                    "title_font_key": 5,
                    "title_font_size": 6,
                    "subtitle_font_key": 7,
                    "subtitle_font_size": 8,
                    "subtitle_margin": 9,
                    "title_margin": 10,
                    "title_line_spacing": 11,
                    "title_bg_color": 12,
                    "video_date_text": 13,
                    "video_date_top": None,
                    "show_title": None,
                    "show_subtitle": None,
                    "subscribe_overlay_enabled": 14,
                    "is_music_only": 15,
                    "static_visual_key": 16,
                    "background_visual_key": 17,
                    "video_overlay_offset": 18,
                    "podcast_audio_filename": 19,
                    "visual_mode": 20,
                    "podcast_overlay_short_ids": 21,
                    "owner_user_id": 22,
                    "title_text_color": None,
                    "subtitle_text_color": None,
                    "title_bg_alpha": None,
                    "subtitle_bg_color": None,
                    "subtitle_bg_alpha": None,
                    "subtitle_text_alpha": None,
                    "subtitle_style": None,
                }
            elif len(crop_row) >= 22:
                query_indexes = {
                    "split_enabled": None,
                    "crop_aspect": 4,
                    "title_font_key": 5,
                    "title_font_size": 6,
                    "subtitle_font_key": 7,
                    "subtitle_font_size": 8,
                    "subtitle_margin": 9,
                    "title_margin": 10,
                    "title_line_spacing": 11,
                    "title_bg_color": 12,
                    "video_date_text": 13,
                    "video_date_top": None,
                    "show_title": None,
                    "show_subtitle": None,
                    "subscribe_overlay_enabled": 14,
                    "is_music_only": 15,
                    "static_visual_key": 16,
                    "background_visual_key": 17,
                    "video_overlay_offset": 18,
                    "podcast_audio_filename": 19,
                    "visual_mode": 20,
                    "podcast_overlay_short_ids": None,
                    "owner_user_id": 21,
                    "title_text_color": None,
                    "subtitle_text_color": None,
                    "title_bg_alpha": None,
                    "subtitle_bg_color": None,
                    "subtitle_bg_alpha": None,
                    "subtitle_text_alpha": None,
                    "subtitle_style": None,
                }
            elif len(crop_row) >= 21:
                # Has title_line_spacing + podcast_audio_filename, but no visual_mode.
                query_indexes = {
                    "split_enabled": None,
                    "crop_aspect": 4,
                    "title_font_key": 5,
                    "title_font_size": 6,
                    "subtitle_font_key": 7,
                    "subtitle_font_size": 8,
                    "subtitle_margin": 9,
                    "title_margin": 10,
                    "title_line_spacing": 11,
                    "title_bg_color": 12,
                    "video_date_text": 13,
                    "video_date_top": None,
                    "show_title": None,
                    "show_subtitle": None,
                    "subscribe_overlay_enabled": 14,
                    "is_music_only": 15,
                    "static_visual_key": 16,
                    "background_visual_key": 17,
                    "video_overlay_offset": 18,
                    "podcast_audio_filename": 19,
                    "visual_mode": None,
                    "podcast_overlay_short_ids": None,
                    "owner_user_id": 20,
                    "title_text_color": None,
                    "subtitle_text_color": None,
                    "title_bg_alpha": None,
                    "subtitle_bg_color": None,
                    "subtitle_bg_alpha": None,
                    "subtitle_text_alpha": None,
                    "subtitle_style": None,
                }
            elif len(crop_row) >= 20:
                # Has podcast_audio_filename, but no title_line_spacing/visual_mode.
                query_indexes = {
                    "split_enabled": None,
                    "crop_aspect": 4,
                    "title_font_key": 5,
                    "title_font_size": 6,
                    "subtitle_font_key": 7,
                    "subtitle_font_size": 8,
                    "subtitle_margin": 9,
                    "title_margin": 10,
                    "title_line_spacing": None,
                    "title_bg_color": 11,
                    "video_date_text": 12,
                    "video_date_top": None,
                    "show_title": None,
                    "show_subtitle": None,
                    "subscribe_overlay_enabled": 13,
                    "is_music_only": 14,
                    "static_visual_key": 15,
                    "background_visual_key": 16,
                    "video_overlay_offset": 17,
                    "podcast_audio_filename": 18,
                    "visual_mode": None,
                    "podcast_overlay_short_ids": None,
                    "owner_user_id": 19,
                    "title_text_color": None,
                    "subtitle_text_color": None,
                    "title_bg_alpha": None,
                    "subtitle_bg_color": None,
                    "subtitle_bg_alpha": None,
                    "subtitle_text_alpha": None,
                    "subtitle_style": None,
                }
            else:
                # Legacy: no podcast_audio_filename, no title_line_spacing/visual_mode.
                query_indexes = {
                    "split_enabled": None,
                    "crop_aspect": 4,
                    "title_font_key": 5,
                    "title_font_size": 6,
                    "subtitle_font_key": 7,
                    "subtitle_font_size": 8,
                    "subtitle_margin": 9,
                    "title_margin": 10,
                    "title_line_spacing": None,
                    "title_bg_color": 11,
                    "video_date_text": 12,
                    "video_date_top": None,
                    "show_title": None,
                    "show_subtitle": None,
                    "subscribe_overlay_enabled": 13,
                    "is_music_only": 14,
                    "static_visual_key": 15,
                    "background_visual_key": 16,
                    "video_overlay_offset": 17,
                    "podcast_audio_filename": None,
                    "visual_mode": None,
                    "podcast_overlay_short_ids": None,
                    "owner_user_id": 18,
                    "title_text_color": None,
                    "subtitle_text_color": None,
                    "title_bg_alpha": None,
                    "subtitle_bg_color": None,
                    "subtitle_bg_alpha": None,
                    "subtitle_text_alpha": None,
                    "subtitle_style": None,
                }
            has_split_columns = len(crop_row) >= 35
            video_crop_ratios = {
                "split_enabled": bool(crop_row[query_indexes["split_enabled"]]) if query_indexes["split_enabled"] is not None else False,
                "crop_x_ratio": crop_row[1] if has_split_columns else crop_row[0],
                "crop_y_ratio": crop_row[2] if has_split_columns else crop_row[1],
                "crop_w_ratio": crop_row[3] if has_split_columns else crop_row[2],
                "crop_h_ratio": crop_row[4] if has_split_columns else crop_row[3],
                "crop2_x_ratio": crop_row[5] if has_split_columns else None,
                "crop2_y_ratio": crop_row[6] if has_split_columns else None,
                "crop2_w_ratio": crop_row[7] if has_split_columns else None,
                "crop2_h_ratio": crop_row[8] if has_split_columns else None,
            }
            video_crop_aspect = crop_row[query_indexes["crop_aspect"]] or "landscape"
            video_font_key = crop_row[query_indexes["title_font_key"]]
            video_title_font_size = crop_row[query_indexes["title_font_size"]]
            video_sub_font_key = crop_row[query_indexes["subtitle_font_key"]]
            video_sub_font_size = crop_row[query_indexes["subtitle_font_size"]]
            video_sub_margin = crop_row[query_indexes["subtitle_margin"]]
            video_title_margin = crop_row[query_indexes["title_margin"]]
            if query_indexes["title_line_spacing"] is not None:
                video_title_line_spacing = crop_row[query_indexes["title_line_spacing"]]
            video_title_bg_color = crop_row[query_indexes["title_bg_color"]]
            if query_indexes["title_bg_alpha"] is not None:
                video_title_bg_alpha = _normalize_alpha_percent(crop_row[query_indexes["title_bg_alpha"]], DEFAULT_TITLE_BG_ALPHA)
            if query_indexes["title_text_color"] is not None:
                video_title_text_color = crop_row[query_indexes["title_text_color"]]
            if query_indexes["subtitle_text_color"] is not None:
                video_subtitle_text_color = crop_row[query_indexes["subtitle_text_color"]]
            if query_indexes["subtitle_bg_color"] is not None:
                video_subtitle_bg_color = crop_row[query_indexes["subtitle_bg_color"]]
            if query_indexes["subtitle_bg_alpha"] is not None:
                video_subtitle_bg_alpha = _normalize_alpha_percent(crop_row[query_indexes["subtitle_bg_alpha"]], DEFAULT_SUBTITLE_BG_ALPHA)
            if query_indexes["subtitle_text_alpha"] is not None:
                video_subtitle_text_alpha = _normalize_alpha_percent(crop_row[query_indexes["subtitle_text_alpha"]], DEFAULT_SUBTITLE_TEXT_ALPHA)
            if query_indexes["subtitle_style"] is not None:
                video_subtitle_style = str(crop_row[query_indexes["subtitle_style"]] or "plain").strip().lower()
            subtitle_preset_index = query_indexes.get("subtitle_preset")
            if subtitle_preset_index is not None:
                video_subtitle_preset = str(crop_row[subtitle_preset_index] or DEFAULT_SUBTITLE_PRESET).strip() or DEFAULT_SUBTITLE_PRESET
            video_date_text = crop_row[query_indexes["video_date_text"]]
            if query_indexes["video_date_top"] is not None:
                try:
                    video_date_top = int(crop_row[query_indexes["video_date_top"]] or DEFAULT_VIDEO_DATE_TOP)
                except Exception:
                    video_date_top = DEFAULT_VIDEO_DATE_TOP
            raw_subscribe_overlay = crop_row[query_indexes["subscribe_overlay_enabled"]]
            raw_show_title = crop_row[query_indexes["show_title"]] if query_indexes["show_title"] is not None else None
            raw_show_subtitle = crop_row[query_indexes["show_subtitle"]] if query_indexes["show_subtitle"] is not None else None
            raw_is_music_only = crop_row[query_indexes["is_music_only"]]
            video_static_visual_key = crop_row[query_indexes["static_visual_key"]]
            video_background_visual_key = crop_row[query_indexes["background_visual_key"]]
            try:
                db_offset = crop_row[query_indexes["video_overlay_offset"]]
                if db_offset is not None:
                    video_overlay_offset = int(db_offset)
            except Exception:
                pass
            if query_indexes["podcast_audio_filename"] is not None:
                video_podcast_audio_filename = str(crop_row[query_indexes["podcast_audio_filename"]] or "").strip()
            if query_indexes["visual_mode"] is not None:
                video_visual_mode = str(crop_row[query_indexes["visual_mode"]] or "video").strip().lower()
            if query_indexes["podcast_overlay_short_ids"] is not None:
                raw_overlay = str(crop_row[query_indexes["podcast_overlay_short_ids"]] or "").strip()
                if raw_overlay:
                    try:
                        parsed_overlay = json.loads(raw_overlay)
                        if isinstance(parsed_overlay, list):
                            video_podcast_overlay_short_ids = [
                                str(item).strip()
                                for item in parsed_overlay
                                if str(item or "").strip().startswith("short:")
                            ][:2]
                    except Exception:
                        video_podcast_overlay_short_ids = []
            video_owner_user_id = crop_row[query_indexes["owner_user_id"]]
            video_subscribe_overlay = _normalize_optional_bool(raw_subscribe_overlay, default=True)
            video_show_title = _normalize_optional_bool(raw_show_title, default=True)
            video_show_subtitle = _normalize_optional_bool(raw_show_subtitle, default=True)
            video_is_music_only = bool(raw_is_music_only) if raw_is_music_only is not None else False
    finally:
        conn.close()
    video_font_key = video_font_key or DEFAULT_EDITOR_TITLE_FONT_KEY
    video_title_font_size = video_title_font_size or DEFAULT_EDITOR_TITLE_FONT_SIZE
    video_title_bg_color = _normalize_hex_color(video_title_bg_color, DEFAULT_EDITOR_TITLE_BG_COLOR)
    video_title_text_color = _normalize_hex_color(video_title_text_color, DEFAULT_EDITOR_TITLE_TEXT_COLOR)
    video_subtitle_text_color = _normalize_hex_color(video_subtitle_text_color, DEFAULT_SUBTITLE_TEXT_COLOR)
    if video_subtitle_style not in {"plain", "karaoke"}:
        video_subtitle_style = "plain"
    if video_subtitle_preset not in SUBTITLE_PRESETS:
        video_subtitle_preset = DEFAULT_SUBTITLE_PRESET
    try:
        video_title_line_spacing = int(video_title_line_spacing if video_title_line_spacing is not None else -4)
    except Exception:
        video_title_line_spacing = -4
    if not plan_entries:
        return _respond(
            "Clip plan not found. Please create the plan first.",
            status=404,
            category="warning",
        )

    plan_entry = None
    for entry in plan_entries:
        pi = entry.get("plan_index")
        try:
            pi = int(pi)
        except Exception:
            continue
        if pi == plan_index:
            plan_entry = entry
            break
    if not plan_entry:
        return _respond("Selected clip was not found in the plan.", status=404, category="warning")

    if queued_job and plan_entry.get("status") == "created":
        return _respond(
            "Clip already generated for this plan index; delete the existing clip before regenerating.",
            status=409,
            category="info",
        )

    title_override = (request.form.get("title") or "").strip()
    if title_override:
        plan_entry["title"] = title_override
        try:
            _write_plan_entries(vid, plan_entries)
        except Exception as exc:
            current_app.logger.warning(
                "Failed to persist clip title update for %s plan %s: %s",
                vid,
                plan_index,
                exc,
            )

    start = _to_float(plan_entry.get("start"))
    end = _to_float(plan_entry.get("end"))
    if start is None or end is None:
        return _respond(
            "Clip timing is incomplete; regenerate the plan.",
            status=400,
            category="warning",
        )
    if disk_guard_triggered(operation="render_enqueue", log=current_app.logger):
        return _respond(
            USER_FACING_DISK_GUARD_MESSAGE,
            success=False,
            status=503,
            category="warning",
        )
    podcast_audio_path = None
    podcast_audio_duration = None
    podcast_overlay_video_sources: List[Path] = []
    if video_visual_mode == "podcast" and video_podcast_audio_filename and video_owner_user_id:
        podcast_audio_path = _resolve_user_podcast_audio_path(video_owner_user_id, video_podcast_audio_filename)
        if video_podcast_overlay_short_ids:
            podcast_overlay_video_sources = _resolve_user_short_clip_source_paths(
                str(video_owner_user_id),
                video_podcast_overlay_short_ids,
                max_items=2,
            )
        if podcast_audio_path:
            video_crop_aspect = "landscape"
            podcast_audio_duration = _probe_media_duration_seconds(podcast_audio_path)
            # Podcast mode renders a long-form export from the start instead of a short clip window.
            start = 0.0
            if podcast_audio_duration and podcast_audio_duration > 0:
                end = float(podcast_audio_duration)
            elif duration_seconds:
                end = float(duration_seconds)
    if duration_seconds and not podcast_audio_path:
        end = min(end, float(duration_seconds))
    if end <= start:
        return _respond(
            "Clip timing is invalid; please regenerate the plan.",
            status=400,
            category="warning",
        )
    selected_subscribe_overlay_key = _normalize_subscribe_overlay_key(
        _load_subscribe_overlay_key(video_owner_user_id, brand_id, video_pk),
        expected_owner_user_id=video_owner_user_id,
        expected_brand_id=brand_id,
    )
    if not selected_subscribe_overlay_key:
        auto_subscribe_path = choose_deterministic_system_subscribe(vid)
        if auto_subscribe_path:
            selected_subscribe_overlay_key = make_system_subscribe_key(auto_subscribe_path.name)
    src_path = None
    src_path_is_temp = False
    if not queued_job:
        try:
            effective_subtitle_text = (
                (plan_entry.get("transcript_full_custom") or "").strip()
                or build_transcript_for_range(segments, start, end, prefer_tr=True)
                or ""
            )
            job_options = _build_render_job_options(
                plan_index=plan_index,
                title=plan_entry.get("title") or video_title,
                brand_id=brand_id,
                crop_ratios=video_crop_ratios,
                crop_aspect=video_crop_aspect,
                title_font_key=video_font_key,
                title_font_size=video_title_font_size,
                subtitle_font_key=video_sub_font_key,
                subtitle_font_size=video_sub_font_size,
                subtitle_margin=video_sub_margin,
                subtitle_style=video_subtitle_style,
                subtitle_preset=video_subtitle_preset,
                title_margin=video_title_margin,
                title_line_spacing=video_title_line_spacing,
                title_bg_color=video_title_bg_color,
                title_bg_alpha=video_title_bg_alpha,
                title_text_color=video_title_text_color,
                subtitle_text_color=video_subtitle_text_color,
                subtitle_bg_color=video_subtitle_bg_color,
                subtitle_bg_alpha=video_subtitle_bg_alpha,
                subtitle_text_alpha=video_subtitle_text_alpha,
                date_text=video_date_text,
                date_top=video_date_top,
                show_title=video_show_title,
                show_subtitle=video_show_subtitle,
                subscribe_overlay=video_subscribe_overlay,
                subscribe_overlay_image=_subscribe_storage_reference_for_asset(
                    selected_subscribe_overlay_key,
                    expected_owner_user_id=video_owner_user_id,
                    expected_brand_id=brand_id,
                ),
                is_music_only=video_is_music_only,
                static_visual_key=video_static_visual_key,
                background_visual_key=video_background_visual_key,
                visual_mode=video_visual_mode,
                podcast_audio_filename=video_podcast_audio_filename,
                podcast_overlay_short_ids=video_podcast_overlay_short_ids,
                video_overlay_offset=video_overlay_offset,
                subtitle_text=effective_subtitle_text,
            )
            input_hash = build_input_hash(
                source_id=vid,
                start=start,
                end=end,
                options=job_options,
            )
            payload = {
                "video_pk": int(video_pk),
                "source_video_id": vid,
                "brand_id": brand_id,
                "plan_index": int(plan_index),
                "title": plan_entry.get("title") or video_title,
                "start": start,
                "end": end,
                "options": job_options,
            }
            enqueue_result = enqueue_render_job(
                user_id=str(target_owner_user_id),
                payload=payload,
                input_hash=input_hash,
            )
            kind = enqueue_result.get("kind")
            job = enqueue_result.get("job") or {}
            if kind == "cached":
                if not is_render_job_cache_valid(job):
                    try:
                        invalidate_done_job_cache(str(job.get("id") or ""))
                    except Exception as exc:
                        current_app.logger.warning("Failed to invalidate stale cached render job %s: %s", job.get("id"), exc)
                    enqueue_result = enqueue_render_job(
                        user_id=str(target_owner_user_id),
                        payload=payload,
                        input_hash=input_hash,
                    )
                    kind = enqueue_result.get("kind")
                    job = enqueue_result.get("job") or {}
                if kind == "cached":
                    return _respond(
                        "Matching clip already exists.",
                        success=True,
                        status=200,
                        category="success",
                        extras={
                            "job_id": job.get("id"),
                            "status": job.get("status"),
                            "cached": True,
                            "result": job.get("result"),
                        },
                    )
            if kind == "existing":
                _update_plan_entry_job_state(
                    vid,
                    plan_entries,
                    plan_index=plan_index,
                    status=str(job.get("status") or "queued"),
                    render_job_id=job.get("id"),
                )
                return _respond(
                    "Matching render job is already in progress.",
                    success=True,
                    status=202,
                    category="info",
                    extras={
                        "job_id": job.get("id"),
                        "status": job.get("status"),
                        "queue_position": job.get("queue_position"),
                    },
                )
            if kind == "concurrency_limit":
                return _respond(
                    "You already have too many render jobs in progress.",
                    success=False,
                    status=429,
                    category="warning",
                    extras={
                        "code": "concurrency_limit",
                        "limit": enqueue_result.get("limit"),
                        "inflight": enqueue_result.get("inflight"),
                    },
                )
            _update_plan_entry_job_state(
                vid,
                plan_entries,
                plan_index=plan_index,
                status="queued",
                render_job_id=job.get("id"),
            )
            reserve_result = reserve_export(target_owner_user_id)
            if not reserve_result.get("allowed", False):
                if job.get("id"):
                    try:
                        cancel_job(job["id"], "Export limit reached before queue admission.")
                    except Exception:
                        current_app.logger.exception(
                            "Failed to cancel queued render job after reserve rejection job_id=%s",
                            job.get("id"),
                        )
                _update_plan_entry_job_state(
                    vid,
                    plan_entries,
                    plan_index=plan_index,
                    status="pending",
                    render_job_id=None,
                )
                return _respond(
                    "Monthly export limit reached for your plan.",
                    success=False,
                    status=403,
                    category="danger",
                    extras={"code": "export_limit_reached", "remaining": reserve_result.get("remaining")},
                )
            return _respond(
                "Render job queued.",
                success=True,
                status=202,
                category="info",
                extras={
                    "job_id": job.get("id"),
                    "status": "queued",
                    "queue_position": job.get("queue_position"),
                },
            )
        finally:
            _cleanup_video_shorts_temp_path(podcast_audio_path)
            for overlay_source in podcast_overlay_video_sources:
                _cleanup_video_shorts_temp_path(overlay_source)
    src_path, src_path_is_temp = _resolve_source_video(vid)
    if not src_path or not src_path.exists():
        return _respond(
            "Source video file not found on server. Upload or download it first.",
            status=404,
            category="danger",
        )
    video_subtitle_bg_color = _normalize_hex_color(video_subtitle_bg_color, DEFAULT_SUBTITLE_BG_COLOR)
    font_choice, sub_font_name, title_font_size, sub_font_size, sub_margin, title_margin, title_bg_color, title_bg_alpha, title_text_color, subtitle_text_color, subtitle_bg_color, subtitle_bg_alpha, subtitle_text_alpha = _get_font_settings_from_session(
        video_font_key=video_font_key,
        title_font_size_override=video_title_font_size,
        video_sub_font_key=video_sub_font_key,
        subtitle_font_size_override=video_sub_font_size,
        subtitle_margin_override=video_sub_margin,
        title_margin_override=video_title_margin,
        title_bg_color_override=video_title_bg_color,
        title_bg_alpha_override=video_title_bg_alpha,
        title_text_color_override=video_title_text_color,
        subtitle_text_color_override=video_subtitle_text_color,
        subtitle_bg_color_override=video_subtitle_bg_color,
        subtitle_bg_alpha_override=video_subtitle_bg_alpha,
        subtitle_text_alpha_override=video_subtitle_text_alpha,
    )
    fallback_font_choice = _build_title_font_choice(DEFAULT_EDITOR_TITLE_FONT_KEY)
    if not font_choice and fallback_font_choice:
        font_choice = fallback_font_choice
    default_font_label = (
        fallback_font_choice["label"]
        if fallback_font_choice and fallback_font_choice.get("label")
        else "Oswald Regular"
    )
    title_font_name = (
        font_choice["label"]
        if font_choice and font_choice.get("label")
        else default_font_label
    )
    normalized_title_font_key = _resolve_title_font_key(video_font_key or DEFAULT_EDITOR_TITLE_FONT_KEY)
    selected_title_font_key = normalized_title_font_key
    title_template_behavior = _resolve_title_template_behavior(
        subtitle_preset=video_subtitle_preset,
        title_font_key=normalized_title_font_key,
    )
    font_path = font_choice["path"] if font_choice else None
    font_exists = Path(font_path).exists() if font_path else False
    current_app.logger.info(
        "SHORTFONT raw=%s normalized=%s path=%s exists=%s",
        video_font_key,
        normalized_title_font_key,
        font_path,
        font_exists,
    )

    temp_subs = []
    static_clip_path = None
    created_video_path = None
    created_video_is_temp = False
    bg_path_is_temp = False
    subscribe_overlay_path = None
    subscribe_overlay_path_is_temp = False
    subtitle_overlay_video_path = None
    subtitle_overlay_specs: List[Dict[str, Any]] = []
    subtitle_overlay_cleanup_paths: List[Path] = []
    made = 0
    missing_outputs = 0
    clip_filename = plan_entry.get("clip_filename") or f"{plan_index}_{vid}.mp4"
    output_path = SHORTS_DIR / clip_filename
    error_message = None
    try:
        START_PAD = 0.25
        END_PAD = 0.5
        adj_start = max(0.0, start - START_PAD)
        adj_end = end + END_PAD
        if duration_seconds and not podcast_audio_path:
            adj_end = min(adj_end, float(duration_seconds))
        custom_transcript = str(plan_entry.get("transcript_full_custom") or "").strip()
        subtitle_srt = None
        subtitle_text = ""
        clip_text = ""
        if custom_transcript:
            clip_text = custom_transcript
            subtitle_text = custom_transcript
            if video_subtitle_style == "karaoke":
                override_segments = _build_override_segments_for_karaoke(custom_transcript, adj_start, adj_end)
                if video_subtitle_style == "karaoke":
                    active_subtitle_preset = str(video_subtitle_preset or "").strip()
                    active_preset_config = SUBTITLE_PRESETS.get(active_subtitle_preset) or {}
                    if bool(active_preset_config.get("active_pill")) or bool(active_preset_config.get("pillow_engine")):
                        subtitle_overlay_video_path, subtitle_overlay_cleanup_paths, overlay_meta = _build_word_highlight_caption_overlay(
                            override_segments,
                            adj_start,
                            adj_end,
                            subtitle_font_size=sub_font_size,
                            subtitle_margin=sub_margin,
                            subtitle_preset=video_subtitle_preset,
                        )
                        subtitle_overlay_specs = list((overlay_meta or {}).get("specs") or [])
                        overlay_frame_count = int((overlay_meta or {}).get("frame_count") or len(subtitle_overlay_specs))
                        if subtitle_overlay_video_path and Path(subtitle_overlay_video_path).exists():
                            current_app.logger.info(
                                "Using Pillow word_highlight subtitle overlay clip=%s frames=%s timing=word_timestamps mode=single_overlay_video",
                                clip_filename,
                                overlay_frame_count,
                            )
                        else:
                            subtitle_srt = _build_ass_karaoke_for_clip(
                                override_segments,
                                adj_start,
                                adj_end,
                                subtitle_font=sub_font_name,
                                subtitle_font_size=sub_font_size,
                                subtitle_margin=sub_margin,
                                subtitle_text_color=subtitle_text_color,
                                subtitle_text_alpha=subtitle_text_alpha,
                                subtitle_bg_color=subtitle_bg_color,
                                subtitle_bg_alpha=subtitle_bg_alpha,
                                subtitle_preset=video_subtitle_preset,
                            )
                    else:
                        subtitle_srt = _build_ass_karaoke_for_clip(
                            override_segments,
                            adj_start,
                            adj_end,
                            subtitle_font=sub_font_name,
                            subtitle_font_size=sub_font_size,
                            subtitle_margin=sub_margin,
                            subtitle_text_color=subtitle_text_color,
                            subtitle_text_alpha=subtitle_text_alpha,
                            subtitle_bg_color=subtitle_bg_color,
                            subtitle_bg_alpha=subtitle_bg_alpha,
                            subtitle_preset=video_subtitle_preset,
                        )
                if subtitle_srt:
                    temp_subs.append(subtitle_srt)
            else:
                subtitle_srt = _build_srt_from_text(custom_transcript, adj_start, adj_end)
                if subtitle_srt:
                    temp_subs.append(subtitle_srt)
        else:
            sub_segments = []
            for s in (segments or []):
                if s.get("start") is None:
                    continue
                st = float(s.get("start"))
                dur_val = s.get("duration")
                end_val = s.get("end")
                try:
                    dur = float(dur_val) if dur_val is not None else None
                except Exception:
                    dur = None
                try:
                    en = float(end_val) if end_val is not None else None
                except Exception:
                    en = None
                if en is None:
                    en = st + max(dur or 0.0, 0.0)
                if dur is None:
                    dur = max(en - st, 0.0)
                if en <= adj_start or st >= adj_end:
                    continue
                sub_segments.append(s.get("tr_text") or s.get("text") or s.get("ar_text") or "")
            clip_text = build_transcript_for_range(segments, start, end, prefer_tr=True)
            subtitle_text = _sanitize_text_for_overlay(" ".join(sub_segments), 400)
            if video_subtitle_style == "karaoke":
                active_subtitle_preset = str(video_subtitle_preset or "").strip()
                active_preset_config = SUBTITLE_PRESETS.get(active_subtitle_preset) or {}
                if bool(active_preset_config.get("active_pill")) or bool(active_preset_config.get("pillow_engine")):
                    subtitle_overlay_video_path, subtitle_overlay_cleanup_paths, overlay_meta = _build_word_highlight_caption_overlay(
                        segments,
                        adj_start,
                        adj_end,
                        subtitle_font_size=sub_font_size,
                        subtitle_margin=sub_margin,
                        subtitle_preset=video_subtitle_preset,
                    )
                    subtitle_overlay_specs = list((overlay_meta or {}).get("specs") or [])
                    overlay_frame_count = int((overlay_meta or {}).get("frame_count") or len(subtitle_overlay_specs))
                    if subtitle_overlay_video_path and Path(subtitle_overlay_video_path).exists():
                        current_app.logger.info(
                            "Using Pillow word_highlight subtitle overlay clip=%s frames=%s timing=word_timestamps mode=single_overlay_video",
                            clip_filename,
                            overlay_frame_count,
                        )
                    else:
                        current_app.logger.warning(
                            "Word highlight Pillow overlay unavailable; falling back to ASS clip=%s meta=%s",
                            clip_filename,
                            overlay_meta,
                        )
                        subtitle_srt = _build_ass_karaoke_for_clip(
                            segments,
                            adj_start,
                            adj_end,
                            subtitle_font=sub_font_name,
                            subtitle_font_size=sub_font_size,
                            subtitle_margin=sub_margin,
                            subtitle_text_color=subtitle_text_color,
                            subtitle_text_alpha=subtitle_text_alpha,
                            subtitle_bg_color=subtitle_bg_color,
                            subtitle_bg_alpha=subtitle_bg_alpha,
                            subtitle_preset=video_subtitle_preset,
                        )
                else:
                    subtitle_srt = _build_ass_karaoke_for_clip(
                        segments,
                        adj_start,
                        adj_end,
                        subtitle_font=sub_font_name,
                        subtitle_font_size=sub_font_size,
                        subtitle_margin=sub_margin,
                        subtitle_text_color=subtitle_text_color,
                        subtitle_text_alpha=subtitle_text_alpha,
                        subtitle_bg_color=subtitle_bg_color,
                        subtitle_bg_alpha=subtitle_bg_alpha,
                        subtitle_preset=video_subtitle_preset,
                    )
            else:
                subtitle_srt = _build_srt_for_clip(segments, adj_start, adj_end)
            if subtitle_srt:
                temp_subs.append(subtitle_srt)

        final_file = None
        current_app.logger.info(
            "Clip %s video_start=%s video_end=%s audio_start=%s audio_end=%s",
            clip_filename,
            start,
            end,
            start,
            end,
        )
        crop_settings = {
            key: val
            for key, val in video_crop_ratios.items()
            if key in {
                "split_enabled",
                "crop_x_ratio",
                "crop_y_ratio",
                "crop_w_ratio",
                "crop_h_ratio",
                "crop2_x_ratio",
                "crop2_y_ratio",
                "crop2_w_ratio",
                "crop2_h_ratio",
            }
        }
        preferred_bg_key = load_background_preference(video_owner_user_id, brand_id) if video_owner_user_id else None
        bg_visual_key = preferred_bg_key or video_background_visual_key
        if selected_subscribe_overlay_key:
            subscribe_overlay_path, subscribe_overlay_path_is_temp = _resolve_subscribe_overlay_path(
                selected_subscribe_overlay_key,
                expected_owner_user_id=video_owner_user_id,
                expected_brand_id=brand_id,
            )
        if not subscribe_overlay_path:
            video_subscribe_overlay = False
        bg_path = BGCOVER_PATH
        if not bg_path.exists():
            static_fallback = STATIC_IMG_DIR / bg_path.name
            if static_fallback.exists():
                bg_path = static_fallback
        resolved_selected_background = False
        if not bg_visual_key:
            auto_bg_path = choose_deterministic_system_background(vid)
            if auto_bg_path:
                bg_path = auto_bg_path
        if bg_visual_key:
            if bg_visual_key.startswith("userbg:"):
                bg_image_id = bg_visual_key.split(":", 1)[1]
                user_bg_path, bg_path_is_temp = _resolve_user_static_image_path(
                    bg_image_id,
                    expected_owner_user_id=video_owner_user_id,
                    expected_brand_id=brand_id,
                )
                if user_bg_path:
                    bg_path = user_bg_path
                    resolved_selected_background = True
                else:
                    current_app.logger.warning(
                        "Background image missing for key=%s",
                        bg_visual_key,
                    )
            elif bg_visual_key.startswith("systembg:"):
                system_bg_path = resolve_system_background_path(bg_visual_key)
                if system_bg_path and system_bg_path.exists():
                    bg_path = system_bg_path
                    resolved_selected_background = True
                else:
                    current_app.logger.warning(
                        "System background missing for key=%s",
                        bg_visual_key,
                    )
            else:
                bg_match = next(
                    (entry for entry in BACKGROUND_VISUAL_PRESETS if entry.get("key") == bg_visual_key),
                    None,
                )
                if bg_match and bg_match.get("image_filename"):
                    safe_name = Path(bg_match["image_filename"]).name
                    candidate = STATIC_IMG_DIR / safe_name
                    bg_path = candidate if candidate.exists() else (VIDEOS_DIR / safe_name)
                    resolved_selected_background = bg_path.exists()
        if bg_visual_key and not resolved_selected_background:
            auto_bg_path = choose_deterministic_system_background(vid)
            if auto_bg_path:
                bg_path = auto_bg_path
        bgcover_exists = bg_path.exists()
        clip_trim_start = adj_start
        clip_trim_end = adj_end
        current_app.logger.info(
            "Static key=%s bg_key=%s bgcover_exists=%s crop=%s",
            video_static_visual_key,
            bg_visual_key,
            bgcover_exists,
            crop_settings,
        )
        if bgcover_exists:
            bg_out = output_path
            font_exists = Path(font_path).exists() if font_path else False
            current_app.logger.info(
                "SHORTFONT raw=%s normalized=%s path=%s exists=%s",
                video_font_key,
                selected_title_font_key,
                font_path,
                font_exists,
            )
            clip_source = src_path
            override_source = None
            podcast_bg_image_source = None
            created_video_is_temp = False
            if video_static_visual_key:
                user_static_image_path = None
                is_user_static = video_static_visual_key.startswith("user:")
                is_created_video = video_static_visual_key.startswith("i2v:")
                if is_user_static:
                    image_id = video_static_visual_key.split(":", 1)[1]
                    conn_images = get_db_readonly()
                    try:
                        row = conn_images.execute(
                            "SELECT user_id, filename, brand_id FROM shorts_static_images WHERE id = ? AND COALESCE(asset_kind, 'background') = 'background'",
                            [image_id],
                        ).fetchone()
                    finally:
                        conn_images.close()
                    if row and row[1]:
                        owner_id = row[0]
                        owner_brand_id = row[2]
                        if owner_id and video_owner_user_id and owner_id != video_owner_user_id:
                            current_app.logger.warning(
                                "Static image owner mismatch for key=%s owner=%s video_owner=%s",
                                video_static_visual_key,
                                owner_id,
                                video_owner_user_id,
                            )
                        elif owner_brand_id and brand_id and owner_brand_id != brand_id:
                            current_app.logger.warning(
                                "Static image brand mismatch for key=%s owner_brand=%s video_brand=%s",
                                video_static_visual_key,
                                owner_brand_id,
                                brand_id,
                            )
                        else:
                            candidate = STATIC_USER_IMAGES_DIR / owner_id / row[1]
                            if candidate.exists():
                                user_static_image_path = candidate
                if is_user_static and not user_static_image_path:
                    current_app.logger.warning(
                        "Static image missing for key=%s", video_static_visual_key
                    )
                    video_static_visual_key = None
                created_video_path = None
                if is_created_video:
                    job_id = video_static_visual_key.split(":", 1)[1]
                    conn_jobs = get_db_readonly()
                    try:
                        job_row = conn_jobs.execute(
                            "SELECT user_id, output_url, brand_id FROM image_to_video_jobs WHERE job_id = ?",
                            [job_id],
                        ).fetchone()
                    finally:
                        conn_jobs.close()
                    if job_row and job_row[1]:
                        owner_id = job_row[0]
                        owner_brand_id = job_row[2]
                        if owner_id and video_owner_user_id and owner_id != video_owner_user_id:
                            current_app.logger.warning(
                                "Created video owner mismatch for key=%s owner=%s video_owner=%s",
                                video_static_visual_key,
                                owner_id,
                                video_owner_user_id,
                            )
                        elif owner_brand_id and brand_id and owner_brand_id != brand_id:
                            current_app.logger.warning(
                                "Created video brand mismatch for key=%s owner_brand=%s video_brand=%s",
                                video_static_visual_key,
                                owner_brand_id,
                                brand_id,
                            )
                        else:
                            try:
                                output_url = str(job_row[1] or "")
                                created_video_path, _, created_video_is_temp = _resolve_image_to_video_media(
                                    job_id,
                                    output_url,
                                )
                            except Exception:
                                created_video_path = None
                                created_video_is_temp = False
                if is_created_video and not created_video_path:
                    current_app.logger.warning(
                        "Created video missing for key=%s", video_static_visual_key
                    )
                    video_static_visual_key = None
                if is_user_static and podcast_audio_path and user_static_image_path and video_static_visual_key:
                    # Podcast mode fast path: pass the raw image to compositor and skip building
                    # a long temporary static mp4 (major timeout risk for 10-60 min renders).
                    podcast_bg_image_source = user_static_image_path
                    clip_source = src_path
                    override_source = None
                    current_app.logger.info(
                        "Podcast fast-path visual source enabled key=%s image=%s",
                        video_static_visual_key,
                        user_static_image_path,
                    )
                elif is_user_static and video_static_visual_key:
                    static_duration = max(1.0, adj_end - adj_start)
                    current_app.logger.info(
                        "Building static visual clip key=%s duration=%.2f",
                        video_static_visual_key,
                        static_duration,
                    )
                    static_clip_path = _build_static_visual_clip(
                        video_static_visual_key,
                        static_duration,
                        font_path,
                        image_path=user_static_image_path,
                    )
                    static_exists = static_clip_path.exists()
                    static_size = None
                    if static_exists:
                        try:
                            static_size = static_clip_path.stat().st_size
                        except Exception:
                            pass
                    current_app.logger.info(
                        "Static clip path=%s exists=%s size=%s",
                        static_clip_path,
                        static_exists,
                        static_size,
                    )
                    clip_source = static_clip_path
                    override_source = static_clip_path
                elif is_created_video and video_static_visual_key and created_video_path:
                    clip_source = created_video_path
                    override_source = created_video_path
                    current_app.logger.info(
                        "Using created video visual source key=%s path=%s",
                        video_static_visual_key,
                        created_video_path,
                    )
                elif video_static_visual_key:
                    try:
                        static_duration = max(1.0, adj_end - adj_start)
                        static_clip_path = _build_static_visual_clip(
                            video_static_visual_key,
                            static_duration,
                            font_path,
                            image_path=user_static_image_path,
                        )
                        clip_source = static_clip_path
                        override_source = static_clip_path
                    except Exception:
                        current_app.logger.warning(
                            "Visual override build failed for key=%s", video_static_visual_key
                        )
                        video_static_visual_key = None
            current_app.logger.info(
                "Composing with clip_source=%s trim=%.2f-%.2f",
                clip_source,
                clip_trim_start,
                clip_trim_end,
            )
            if not video_static_visual_key:
                override_source = None
            overlay_offset = locals().get("selected_video_overlay_offset", video_overlay_offset)
            _compose_trimmed_with_background(
                bg_path,
                src_path,
                clip_trim_start,
                clip_trim_end,
                str(plan_entry.get("title") or video_title),
                subtitle_text,
                bg_out,
                font_path,
                title_font_name,
                subtitle_srt,
                subtitle_overlay_video_path=subtitle_overlay_video_path,
                subtitle_overlay_specs=subtitle_overlay_specs,
                subtitle_font=sub_font_name,
                title_font_size=title_font_size,
                title_margin=title_margin,
                title_line_spacing=video_title_line_spacing,
                title_engine=title_template_behavior["title_engine"],
                title_uppercase=title_template_behavior["title_uppercase"],
                title_language=plan_entry.get("language"),
                title_bg_color=title_bg_color,
                title_bg_alpha=title_bg_alpha,
                title_text_color=title_text_color,
                subtitle_font_size=sub_font_size,
                subtitle_margin=sub_margin,
                subtitle_text_color=subtitle_text_color,
                subtitle_bg_color=subtitle_bg_color,
                subtitle_bg_alpha=subtitle_bg_alpha,
                subtitle_text_alpha=subtitle_text_alpha,
                subtitle_style=video_subtitle_style,
                subtitle_preset=video_subtitle_preset,
                video_date_text=video_date_text,
                video_date_top=video_date_top,
                show_title=video_show_title,
                show_subtitle=video_show_subtitle,
                subscribe_overlay_enabled=video_subscribe_overlay,
                subscribe_overlay_path=subscribe_overlay_path,
                crop_settings=crop_settings,
                video_override_source=override_source,
                audio_override_source=podcast_audio_path,
                podcast_background_image_source=podcast_bg_image_source,
                podcast_overlay_video_sources=podcast_overlay_video_sources,
                video_overlay_offset=overlay_offset,
                crop_aspect=video_crop_aspect,
                music_only=video_is_music_only,
            )
            final_file = bg_out
        else:
            _cut_clip(
                src_path,
                adj_start,
                adj_end,
                output_path,
                subtitle_srt if video_show_subtitle else None,
                sub_font_name,
                sub_font_size,
                sub_margin,
                subtitle_text_color,
                subtitle_bg_color,
                subtitle_bg_alpha,
                subtitle_text_alpha,
                video_subtitle_style,
            )
            final_file = output_path

        if not final_file or not final_file.exists():
            missing_outputs += 1
            current_app.logger.warning("Clip %s did not produce an output file at %s", plan_index, final_file)
        else:
            current_app.logger.info(
                "short local output ready clip_filename=%s path=%s exists=%s backend=%s",
                clip_filename,
                final_file,
                final_file.exists(),
                getattr(get_media_storage(), "backend_name", "unknown"),
            )
            short_output_key = _short_storage_key(clip_filename)
            storage = get_media_storage()
            if getattr(storage, "backend_name", "local") == "s3":
                current_app.logger.info(
                    "short s3 upload begin clip_filename=%s key=%s path=%s",
                    clip_filename,
                    short_output_key,
                    final_file,
                )
                storage.put_file(final_file, short_output_key)
                current_app.logger.info(
                    "short s3 upload success clip_filename=%s key=%s",
                    clip_filename,
                    short_output_key,
                )
                invalidate_short_cdn_cache(clip_filename)
            if current_user:
                size_bytes = final_file.stat().st_size if final_file.exists() else 0
                usage = usage or {"used_bytes": 0, "limit_bytes": DEFAULT_USER_STORAGE_LIMIT}
                if usage["used_bytes"] + size_bytes > usage["limit_bytes"]:
                    try:
                        final_file.unlink()
                    except Exception:
                        current_app.logger.warning("Failed to remove oversized clip %s", final_file)
                    if export_reserved and target_owner_user_id:
                        release_export(target_owner_user_id)
                        export_reserved = False
                    return _respond(
                        _quota_block_message(
                            _format_size_bytes(usage["limit_bytes"]),
                            _format_size_bytes(usage["used_bytes"]),
                        ),
                        success=False,
                        status=403,
                        category="danger",
                        redirect_to=url_for("video_shorts_bp.shorts_storage_plans"),
                    )
                current_app.logger.info(
                    "short db update begin clip_filename=%s file_key=%s",
                    clip_filename,
                    f"short:{clip_filename}",
                )
                _upsert_storage_asset(
                    f"short:{clip_filename}",
                    str(final_file),
                    "short",
                    size_bytes,
                    target_owner_user_id,
                    brand_id=brand_id,
                )
                current_app.logger.info(
                    "short db update success clip_filename=%s file_key=%s",
                    clip_filename,
                    f"short:{clip_filename}",
                )
            if getattr(storage, "backend_name", "local") == "s3" and final_file.exists():
                try:
                    final_file.unlink()
                    current_app.logger.info(
                        "short local cleanup success clip_filename=%s path=%s",
                        clip_filename,
                        final_file,
                    )
                except Exception:
                    current_app.logger.exception(
                        "short local cleanup failed clip_filename=%s path=%s",
                        clip_filename,
                        final_file,
                    )
            plan_entry["status"] = "created"
            plan_entry["transcript_full"] = clip_text or plan_entry.get("transcript_full", "")
            plan_entry["audio_start"] = adj_start
            plan_entry["audio_end"] = adj_end
            plan_entry["output_filename"] = clip_filename
            try:
                _maybe_autogenerate_description_for_plan_entry(
                    current_user={"id": target_owner_user_id},
                    brand_id=brand_id,
                    video_id=vid,
                    video_title=video_title,
                    video_date_text=video_date_text,
                    duration_seconds=duration_seconds,
                    plan_entry=plan_entry,
                    segments=segments,
                )
            except Exception as exc:
                current_app.logger.warning(
                    "Auto description hook skipped after render video_id=%s plan_index=%s: %s",
                    vid,
                    plan_entry.get("plan_index"),
                    exc,
                )
            plan_entry.setdefault("publish_status", "ready" if plan_entry.get("yt_description") else "not_ready")
            try:
                current_app.logger.info(
                    "short plan update begin clip_filename=%s video_id=%s",
                    clip_filename,
                    vid,
                )
                _write_plan_entries(vid, plan_entries)
                current_app.logger.info(
                    "short plan update success clip_filename=%s video_id=%s",
                    clip_filename,
                    vid,
                )
            except Exception as exc:
                current_app.logger.warning("Failed to update plan file %s: %s", _plan_path(vid), exc)
            try:
                _sync_generated_video_from_plan_entry(
                    source_video_id=vid,
                    clip_filename=clip_filename,
                    plan_entry=plan_entry,
                    generation_status="created",
                )
            except Exception as exc:
                current_app.logger.warning(
                    "Failed to sync generated lifecycle row for %s clip=%s: %s",
                    vid,
                    clip_filename,
                    exc,
                )
            owner_event_user_id = str(video_owner_user_id or (current_user or {}).get("id") or "").strip()
            if owner_event_user_id:
                track_event(
                    owner_event_user_id,
                    "short_generated",
                    video_id=vid,
                    short_id=clip_filename,
                    status="completed",
                )
            made += 1
    except MediaSubprocessTimeoutError as e:
        current_app.logger.exception("Short generation failed plan_index=%s clip_filename=%s", plan_index, clip_filename)
        try:
            _sync_generated_video_from_plan_entry(
                source_video_id=vid,
                clip_filename=clip_filename,
                plan_entry=plan_entry,
                generation_status="failed",
            )
        except Exception as exc:
            current_app.logger.warning(
                "Failed to sync failed lifecycle row for %s clip=%s: %s",
                vid,
                clip_filename,
                exc,
            )
        owner_event_user_id = str(video_owner_user_id or (current_user or {}).get("id") or "").strip()
        if owner_event_user_id:
            track_event(
                owner_event_user_id,
                "short_generated",
                video_id=vid,
                short_id=clip_filename,
                status="failed",
            )
        raise
    except FileNotFoundError:
        current_app.logger.exception("Short generation failed plan_index=%s clip_filename=%s", plan_index, clip_filename)
        try:
            _sync_generated_video_from_plan_entry(
                source_video_id=vid,
                clip_filename=clip_filename,
                plan_entry=plan_entry,
                generation_status="failed",
            )
        except Exception as exc:
            current_app.logger.warning(
                "Failed to sync failed lifecycle row for %s clip=%s: %s",
                vid,
                clip_filename,
                exc,
            )
        owner_event_user_id = str(video_owner_user_id or (current_user or {}).get("id") or "").strip()
        if owner_event_user_id:
            track_event(
                owner_event_user_id,
                "short_generated",
                video_id=vid,
                short_id=clip_filename,
                status="failed",
            )
        raise
    except OSError as e:
        current_app.logger.exception("Short generation failed plan_index=%s clip_filename=%s", plan_index, clip_filename)
        try:
            _sync_generated_video_from_plan_entry(
                source_video_id=vid,
                clip_filename=clip_filename,
                plan_entry=plan_entry,
                generation_status="failed",
            )
        except Exception as exc:
            current_app.logger.warning(
                "Failed to sync failed lifecycle row for %s clip=%s: %s",
                vid,
                clip_filename,
                exc,
            )
        owner_event_user_id = str(video_owner_user_id or (current_user or {}).get("id") or "").strip()
        if owner_event_user_id:
            track_event(
                owner_event_user_id,
                "short_generated",
                video_id=vid,
                short_id=clip_filename,
                status="failed",
            )
        if getattr(e, "errno", None) == errno.ENOSPC:
            raise
        error_message = "This video could not be processed right now. Please try again. If it keeps happening, contact support."
    except Exception as e:
        current_app.logger.exception("Short generation failed plan_index=%s clip_filename=%s", plan_index, clip_filename)
        try:
            _sync_generated_video_from_plan_entry(
                source_video_id=vid,
                clip_filename=clip_filename,
                plan_entry=plan_entry,
                generation_status="failed",
            )
        except Exception as exc:
            current_app.logger.warning(
                "Failed to sync failed lifecycle row for %s clip=%s: %s",
                vid,
                clip_filename,
                exc,
            )
        owner_event_user_id = str(video_owner_user_id or (current_user or {}).get("id") or "").strip()
        if owner_event_user_id:
            track_event(
                owner_event_user_id,
                "short_generated",
                video_id=vid,
                short_id=clip_filename,
                status="failed",
            )
        error_message = "This video could not be processed right now. Please try again. If it keeps happening, contact support."
    finally:
        if export_reserved and not made and target_owner_user_id:
            try:
                release_export(target_owner_user_id)
            except Exception:
                current_app.logger.exception(
                    "Failed to release export reservation for user_id=%s plan_index=%s",
                    target_owner_user_id,
                    plan_index,
                )
        for p in temp_subs:
            try:
                p.unlink()
            except Exception:
                pass
        temp_subs.clear()
        for cleanup_path in subtitle_overlay_cleanup_paths:
            try:
                if cleanup_path.is_dir():
                    shutil.rmtree(cleanup_path, ignore_errors=True)
                else:
                    cleanup_path.unlink(missing_ok=True)
            except Exception:
                pass
        subtitle_overlay_cleanup_paths.clear()
        if static_clip_path:
            try:
                static_clip_path.unlink()
            except Exception:
                pass
        if created_video_is_temp and created_video_path:
            try:
                created_video_path.unlink()
            except Exception:
                pass
        if bg_path_is_temp and bg_path:
            try:
                bg_path.unlink()
            except Exception:
                pass
        if subscribe_overlay_path_is_temp and subscribe_overlay_path:
            try:
                subscribe_overlay_path.unlink()
            except Exception:
                pass
        _cleanup_video_shorts_temp_path(podcast_audio_path)
        for overlay_source in podcast_overlay_video_sources:
            _cleanup_video_shorts_temp_path(overlay_source)
        _cleanup_resolved_source_video(src_path, src_path_is_temp)

    if missing_outputs:
        summary = f"{missing_outputs} clip(s) reported missing output files; check ffmpeg logs."
        return _respond(summary, success=False, category="warning")
    if made:
        message = f"Generated clip for plan index {plan_index}."
        extras = {
            "plan_index": plan_index,
            "clip_filename": clip_filename,
            "status": plan_entry.get("status"),
            "publish_status": plan_entry.get("publish_status"),
        }
        return _respond(message, success=True, category="success", extras=extras)
    if error_message:
        return _respond(error_message, success=False, status=500, category="warning")
    return _respond("No clip was generated.", success=False, category="warning")


def _description_generation_gate_error(
    current_user: Dict[str, Any],
    brand_id: Optional[str],
) -> Optional[str]:
    current_plan_id = str((current_user or {}).get("plan_id") or "").strip().lower()
    llm_description_enabled = current_plan_id not in {"", "free"}
    if not has_refresh_token((current_user or {}).get("id"), brand_id=brand_id):
        return "Connect YouTube first."
    if not llm_description_enabled:
        return "Upgrade your plan to use AI descriptions."
    if not _openai_client:
        return "OPENAI_API_KEY is missing; cannot prepare description."
    return None


def _existing_plan_entry_description(plan_entry: Optional[Dict[str, Any]]) -> Optional[str]:
    entry = plan_entry or {}
    return _first_non_empty(entry.get("yt_description"), entry.get("description"))


def _generate_description_for_plan_entry(
    *,
    current_user: Dict[str, Any],
    brand_id: Optional[str],
    video_id: str,
    video_title: Optional[str],
    video_date_text: Optional[str],
    duration_seconds: Any,
    plan_entry: Dict[str, Any],
    segments: List[Dict[str, Any]],
    published_at: Any = None,
) -> str:
    start = plan_entry.get("start")
    end = plan_entry.get("end")
    if start is None or end is None:
        raise ValueError("Clip timing not found in plan entry.")

    clip_transcript = plan_entry.get("transcript_full") or build_transcript_for_range(segments, start, end, prefer_tr=True)
    try:
        clip_start = max(0.0, float(start or 0.0))
    except Exception:
        clip_start = 0.0
    try:
        clip_end = max(clip_start, float(end or clip_start))
    except Exception:
        clip_end = clip_start
    try:
        max_duration = float(duration_seconds) if duration_seconds is not None else None
    except Exception:
        max_duration = None
    context_start = max(0.0, clip_start - DESCRIPTION_CONTEXT_PADDING_SECONDS)
    context_end = clip_end + DESCRIPTION_CONTEXT_PADDING_SECONDS
    if max_duration is not None:
        context_end = min(max_duration, context_end)
    context_transcript = build_transcript_for_range(segments, context_start, context_end, prefer_tr=True)
    transcripts_source = (
        "WIDER CONTEXT TRANSCRIPT (use this for understanding and summarizing the clip in context; "
        "do not copy it into the Full Transcript section unless it appears inside the clip itself):\n"
        f"{context_transcript or clip_transcript or ''}\n\n"
        "CLIP TRANSCRIPT (use this exact text for the Full Transcript section):\n"
        f"{clip_transcript or ''}"
    )

    date_str = ""
    try:
        if published_at:
            if hasattr(published_at, "strftime"):
                date_str = published_at.strftime("%Y-%m-%d")
            else:
                date_str = str(published_at)[:10]
    except Exception:
        date_str = ""

    video_date_hint = (video_date_text or "").strip()
    if video_date_hint:
        contains_letters = bool(re.search(r"[A-Za-zÇĞİÖŞÜçğışöü]", video_date_hint))
        if contains_letters:
            date_instruction_text = (
                f"Video date text: {video_date_hint}. This text includes date/location context. "
                "Use it naturally in the description when relevant."
            )
        else:
            date_instruction_text = (
                f"Video date text: {video_date_hint}. If you mention the date, phrase it naturally like "
                f"“Recorded on {video_date_hint} ...”."
            )
        date_note_line = f"- Date note: {date_instruction_text}"
    elif date_str:
        date_instruction_text = (
            f"If it fits naturally, you may mention that the clip is from a video recorded or published on {date_str}."
        )
        date_note_line = f"- Date note: {date_instruction_text}"
    else:
        date_note_line = ""

    saved_prompt, description_languages = load_description_settings((current_user or {}).get("id"), brand_id)

    if description_languages == "tr":
        language_instruction = (
            "Write the full output in Turkish only. Use Turkish section labels and Turkish hashtags. "
            "Do not include any English section."
        )
        template_block = (
            "TEMPLATE\n"
            "Açıklama\n"
            "<2-3 paragraf Türkçe açıklama>\n\n"
            "Hashtagler\n"
            "#etiket1 #etiket2 #etiket3 ...\n\n"
            "Tam Metin\n"
            "<klip transkriptini aynen buraya koy>"
        )
        system_message = "You write YouTube descriptions in Turkish based on user instructions."
    elif description_languages == "en,tr":
        language_instruction = (
            "Write both an English and a Turkish version. Keep both versions aligned in meaning. "
            "Use the exact template sections provided below and keep the clip transcript section in both languages."
        )
        template_block = (
            "TEMPLATE\n"
            "Description\n"
            "<2-3 paragraph English description>\n\n"
            "Hashtags\n"
            "#tag1 #tag2 #tag3 ...\n\n"
            "Full Transcript\n"
            "<put the clip transcript here exactly as provided>\n\n"
            "Açıklama\n"
            "<2-3 paragraf Türkçe açıklama>\n\n"
            "Hashtagler\n"
            "#etiket1 #etiket2 #etiket3 ...\n\n"
            "Tam Metin\n"
            "<klip transkriptini aynen buraya koy>"
        )
        system_message = "You write multilingual YouTube descriptions in English and Turkish based on user instructions."
    else:
        language_instruction = (
            "Write the full output in English only. Use English section labels and English hashtags. "
            "Do not include any Turkish section."
        )
        template_block = (
            "TEMPLATE\n"
            "Description\n"
            "<2-3 paragraph English description>\n\n"
            "Hashtags\n"
            "#tag1 #tag2 #tag3 ...\n\n"
            "Full Transcript\n"
            "<put the clip transcript here exactly as provided>"
        )
        system_message = "You write YouTube descriptions in English based on user instructions."

    prompt_template = saved_prompt or DEFAULT_DESCRIPTION_PROMPT
    prompt = (
        prompt_template
        .replace("{language_instruction}", language_instruction)
        .replace("{template_block}", template_block)
        .replace("{date_note_line}", date_note_line)
        .replace("{transcripts_source}", transcripts_source)
    )

    resp = _openai_client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {
                "role": "system",
                "content": system_message,
            },
            {"role": "user", "content": prompt},
        ],
    )
    description_text = resp.choices[0].message.content if resp.choices else ""
    return str(description_text or "").strip()


def _maybe_autogenerate_description_for_plan_entry(
    *,
    current_user: Dict[str, Any],
    brand_id: Optional[str],
    video_id: str,
    video_title: Optional[str],
    video_date_text: Optional[str],
    duration_seconds: Any,
    plan_entry: Dict[str, Any],
    segments: List[Dict[str, Any]],
    published_at: Any = None,
) -> bool:
    if _existing_plan_entry_description(plan_entry):
        return False
    gate_error = _description_generation_gate_error(current_user, brand_id)
    if gate_error:
        current_app.logger.info(
            "Auto description skipped video_id=%s plan_index=%s reason=%s",
            video_id,
            plan_entry.get("plan_index"),
            gate_error,
        )
        return False
    if plan_entry.get("start") is None or plan_entry.get("end") is None:
        current_app.logger.info(
            "Auto description skipped video_id=%s plan_index=%s reason=missing_timing",
            video_id,
            plan_entry.get("plan_index"),
        )
        return False
    if not (plan_entry.get("transcript_full") or segments):
        current_app.logger.info(
            "Auto description skipped video_id=%s plan_index=%s reason=missing_transcript",
            video_id,
            plan_entry.get("plan_index"),
        )
        return False
    try:
        description_text = _generate_description_for_plan_entry(
            current_user=current_user,
            brand_id=brand_id,
            video_id=video_id,
            video_title=video_title,
            published_at=published_at,
            video_date_text=video_date_text,
            duration_seconds=duration_seconds,
            plan_entry=plan_entry,
            segments=segments,
        )
        if not description_text:
            current_app.logger.info(
                "Auto description skipped video_id=%s plan_index=%s reason=empty_response",
                video_id,
                plan_entry.get("plan_index"),
            )
            return False
        plan_entry["yt_description"] = description_text
        plan_entry["yt_status"] = "ready"
        return True
    except Exception as exc:
        current_app.logger.warning(
            "Auto description generation failed video_id=%s plan_index=%s: %s",
            video_id,
            plan_entry.get("plan_index"),
            exc,
        )
        return False


@video_shorts_bp.route("/generate/<int:video_pk>/prepare_description", methods=["POST"])
def prepare_description(video_pk):
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    plan_index_raw = (request.form.get("plan_index") or "").strip()

    def _respond(
        success: bool = False,
        message: Optional[str] = None,
        category: str = "danger",
        status: int = 200,
        description: Optional[str] = None,
        redirect_to: Optional[str] = None,
    ):
        if is_ajax:
            payload: Dict[str, Any] = {"success": success}
            if message:
                payload["message"] = message
            if description is not None:
                payload["description"] = description
            return jsonify(payload), status
        if message:
            flash(message, category)
        target = redirect_to or url_for("video_shorts_bp.generate_short", video_pk=video_pk)
        return redirect(target)

    current_user = getattr(g, "vs_current_user", None) or {}
    editor_context = _active_editor_context()
    brand_id = editor_context["brand_id"]
    description_user = current_user
    if editor_context["is_admin_operation"]:
        conn = get_db_readonly()
        try:
            target_user = conn.execute(
                "SELECT id, plan_id FROM shorts_users WHERE id = ?",
                [editor_context["owner_user_id"]],
            ).fetchone()
        finally:
            conn.close()
        if not target_user:
            return _respond(False, "Video not found.", status=404)
        description_user = {"id": target_user[0], "plan_id": target_user[1]}

    gate_error = _description_generation_gate_error(description_user, brand_id)
    if gate_error:
        redirect_to = url_for("video_shorts_bp.youtube_connect") if gate_error == "Connect YouTube first." else None
        return _respond(
            False,
            gate_error,
            category="warning" if gate_error != "OPENAI_API_KEY is missing; cannot prepare description." else "danger",
            status=403,
            redirect_to=redirect_to,
        )

    filename = (request.form.get("filename") or "").strip()
    if not filename:
        return _respond(False, "Missing clip filename.", category="warning", status=400)
    if not _openai_client:
        return _respond(
            False,
            "OPENAI_API_KEY is missing; cannot prepare description.",
            category="danger",
            status=403,
        )

    conn = get_db_readonly()
    row = _fetch_scoped_video_row(conn, video_pk, "video_id, title, published_at, video_date_text, duration_seconds")
    conn.close()
    if not row:
        return _respond(
            False,
            "Video not found.",
            category="danger",
            status=404,
            redirect_to=url_for("video_shorts_bp.channels_page"),
        )

    vid = row[0]
    video_title = row[1]
    published_at = row[2]
    video_date_text = (row[3] or "").strip()
    duration_seconds = row[4]

    plan_path = SHORTS_DIR / f"{vid}_plan.json"
    if not plan_path.exists():
        return _respond(False, "Clip plan not found; regenerate first.", category="warning", status=404)
    try:
        plan_data = json.loads(plan_path.read_text())
    except Exception as e:
        return _respond(False, f"Could not read plan file: {e}", category="danger", status=500)

    plan_entries = plan_data.get("plan") or plan_data.get("clips") or []
    plan_entry = None
    plan_index = None
    if plan_index_raw:
        try:
            plan_index = int(plan_index_raw)
        except Exception:
            plan_index = None
    if plan_index is not None:
        for entry in plan_entries:
            try:
                if int(entry.get("plan_index") or 0) == plan_index:
                    plan_entry = entry
                    break
            except Exception:
                continue
    if not plan_entry and filename:
        plan_entry = next(
            (entry for entry in plan_entries if entry.get("clip_filename") == filename),
            None,
        )
    if not plan_entry:
        return _respond(False, "Clip not found in plan.", category="warning", status=404)

    conn = get_db_readonly()
    _, segments = _fetch_transcript(conn, vid)
    conn.close()

    try:
        description_text = _generate_description_for_plan_entry(
            current_user=description_user,
            brand_id=brand_id,
            video_id=vid,
            video_title=video_title,
            published_at=published_at,
            video_date_text=video_date_text,
            duration_seconds=duration_seconds,
            plan_entry=plan_entry,
            segments=segments,
        )
    except Exception as e:
        return _respond(
            False,
            f"Description generation failed: {e}",
            category="danger",
            status=500,
        )

    if not description_text:
        return _respond(
            False,
            "LLM did not return a description.",
            category="warning",
            status=400,
        )

    try:
        plan_entry["yt_description"] = description_text
        plan_entry["yt_status"] = "ready"
        _write_plan_entries(vid, plan_entries)
    except Exception as e:
        return _respond(
            False,
            f"Could not save description: {e}",
            category="danger",
            status=500,
        )

    return _respond(
        True,
        "Description generated and saved for this clip.",
        category="success",
        status=200,
        description=description_text,
    )
class InstagramGraphError(Exception):
    def __init__(self, payload: Optional[Dict[str, Any]] = None):
        super().__init__(payload or {})
        self.payload = payload or {}
