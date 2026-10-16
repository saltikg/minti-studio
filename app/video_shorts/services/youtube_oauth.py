import datetime
import logging
import requests
from typing import Any, Dict, List, Optional, Tuple

from flask import current_app, has_app_context
from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from app.video_shorts.config import (
    YOUTUBE_CLIENT_ID,
    YOUTUBE_CLIENT_SECRET,
    YOUTUBE_OAUTH_SCOPES,
    YOUTUBE_REDIRECT_URI,
)
from app.video_shorts.services.youtube_quota import record_youtube_data_api_call

DEFAULT_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.force-ssl",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]
IDENTITY_SCOPES = [
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "openid",
]

SCOPES = [scope.strip() for scope in (YOUTUBE_OAUTH_SCOPES or "").split(",") if scope.strip()]
if not SCOPES:
    SCOPES = DEFAULT_SCOPES.copy()

for scope in DEFAULT_SCOPES:
    if scope not in SCOPES:
        SCOPES.append(scope)
for identity_scope in IDENTITY_SCOPES:
    if identity_scope not in SCOPES:
        SCOPES.append(identity_scope)
# Re-export the shared store API for existing callers (routes, jobs and tasks).
from app.video_shorts.services.youtube_token_store import (
    YouTubeTokenStoreError,
    clear_refresh_token,
    get_stored_refresh_token,
    has_refresh_token,
    is_reauth_required,
    list_stored_refresh_tokens,
    mark_refresh_token_invalid,
    resolve_stored_token_owner_brand,
    resolve_token_lookup_user_id,
    split_scoped_user_id,
    store_refresh_token,
    get_refresh_token_for_revocation,
)

logger = logging.getLogger(__name__)


class YouTubeTransientError(RuntimeError):
    """Sanitized temporary provider failure, safe to retry using the stored session."""



def granted_scopes_from_flow(flow) -> List[str]:
    """Record only Google's returned grant, never the requested/default scopes."""
    scopes = getattr(flow.credentials, "granted_scopes", None)
    if scopes is None:
        scopes = (flow.oauth2session.token or {}).get("scope")
    if isinstance(scopes, str):
        scopes = scopes.split()
    return sorted({str(scope).strip() for scope in (scopes or []) if str(scope).strip()})


def disconnect_youtube(user_id: str, brand_id: Optional[str] = None) -> bool:
    """Revoke first, then forget locally even when Google cannot be reached."""
    revoked = False
    try:
        token = get_refresh_token_for_revocation(user_id, brand_id=brand_id)
        if token:
            response = requests.post(
                "https://oauth2.googleapis.com/revoke",
                data={"token": token},
                timeout=15,
                allow_redirects=False,
            )
            revoked = response.status_code == 200
            if not revoked:
                logger.warning("YouTube revoke failed (HTTP %s); removing local connection.", response.status_code)
        else:
            revoked = True
    except Exception:
        # Provider exceptions may contain request bodies. Never log their text/traceback.
        logger.warning("YouTube revoke failed; removing local connection. Manual Google revocation is required.")
    finally:
        clear_refresh_token(user_id=user_id, brand_id=brand_id)
    return revoked


def build_oauth_flow(state: Optional[str] = None) -> Flow:
    client_config = {
        "web": {
            "client_id": YOUTUBE_CLIENT_ID,
            "client_secret": YOUTUBE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [YOUTUBE_REDIRECT_URI],
        }
    }
    flow = Flow.from_client_config(
        client_config,
        scopes=SCOPES,
        redirect_uri=YOUTUBE_REDIRECT_URI,
    )
    if state:
        flow.state = state
    return flow


def upload_video_with_refresh_token(
    video_path: str,
    title: str,
    description: str,
    publish_at: Optional[str] = None,
    refresh_token: Optional[str] = None,
    privacy_status: str = "private",
    user_id: Optional[str] = None,
    brand_id: Optional[str] = None,
    tags: Optional[List[str]] = None,
    resumable_uri: Optional[str] = None,
    save_resumable_uri=None,
):
    youtube = build_authenticated_youtube(refresh_token, user_id=user_id, brand_id=brand_id)
    if not youtube:
        raise ValueError("No refresh token is available for YouTube upload.")
    body = {
        "snippet": {
            "title": title,
            "description": description,
            "categoryId": "22",
        },
        "status": {
            "privacyStatus": privacy_status,
        },
    }
    if publish_at:
        body["status"]["publishAt"] = publish_at
    clean_tags = [str(tag or "").strip() for tag in (tags or []) if str(tag or "").strip()]
    if clean_tags:
        body["snippet"]["tags"] = clean_tags[:500]

    media = MediaFileUpload(video_path, chunksize=-1, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    if resumable_uri:
        request.resumable_uri = resumable_uri
        request._in_error_state = True  # Query the existing session before sending more media.
    if save_resumable_uri:
        original_http = request.http

        class PersistSessionHttp:
            def request(self, uri, method="GET", *args, **kwargs):
                response, content = original_http.request(uri, method, *args, **kwargs)
                if uri == request.uri and method == "POST" and response.status == 200 and response.get("location"):
                    # Commit the session before next_chunk can transmit the media.
                    save_resumable_uri(response["location"])
                return response, content

        response = request.execute(http=PersistSessionHttp())
    else:
        response = request.execute()
    record_youtube_data_api_call(feature="publish", method="videos.insert")
    return response


def update_video_with_refresh_token(
    video_id: str,
    title: str,
    description: str,
    publish_at: Optional[str] = None,
    privacy_status: str = "private",
    user_id: Optional[str] = None,
    brand_id: Optional[str] = None,
    tags: Optional[List[str]] = None,
):
    youtube = build_authenticated_youtube(user_id=user_id, brand_id=brand_id)
    if not youtube:
        raise ValueError("No refresh token is available for YouTube update.")
    body = {
        "id": video_id,
        "snippet": {
            "title": title,
            "description": description,
            "categoryId": "22",
        },
        "status": {
            "privacyStatus": privacy_status,
        },
    }
    if publish_at:
        body["status"]["publishAt"] = publish_at
    clean_tags = [str(tag or "").strip() for tag in (tags or []) if str(tag or "").strip()]
    if clean_tags:
        body["snippet"]["tags"] = clean_tags[:500]

    request = youtube.videos().update(part="snippet,status", body=body)
    response = request.execute()
    record_youtube_data_api_call(feature="publish", method="videos.update")
    return response


def _refresh_credentials(
    refresh_token: Optional[str] = None,
    user_id: Optional[str] = None,
    brand_id: Optional[str] = None,
) -> Optional[Credentials]:
    token = refresh_token or get_stored_refresh_token(user_id=user_id, brand_id=brand_id)
    if not token:
        return None
    creds = Credentials(
        token=None,
        refresh_token=token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=YOUTUBE_CLIENT_ID,
        client_secret=YOUTUBE_CLIENT_SECRET,
        scopes=SCOPES,
    )
    try:
        creds.refresh(Request())
    except RefreshError as exc:
        message = str(exc).lower()
        if "invalid_grant" in message or "unauthorized_client" in message:
            log = current_app.logger if has_app_context() else logger
            log.warning(
                "YouTube refresh token rejected (%s); marking reauth required.",
                "unauthorized_client" if "unauthorized_client" in message else "invalid_grant",
            )
            mark_refresh_token_invalid(user_id=user_id, brand_id=brand_id)
            return None
        if getattr(exc, "retryable", False):
            raise YouTubeTransientError("YouTube token refresh is temporarily unavailable.") from None
        raise RefreshError("YouTube credentials could not be refreshed.") from None
    except (TransportError, TimeoutError, ConnectionError, OSError):
        raise YouTubeTransientError("YouTube token refresh is temporarily unavailable.") from None
    except Exception:
        raise RuntimeError("YouTube token refresh failed.") from None
    return creds


def build_authenticated_youtube(
    refresh_token: Optional[str] = None,
    user_id: Optional[str] = None,
    brand_id: Optional[str] = None,
):
    creds = _refresh_credentials(refresh_token, user_id=user_id, brand_id=brand_id)
    if not creds:
        return None
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def build_authenticated_youtube_analytics(
    refresh_token: Optional[str] = None,
    user_id: Optional[str] = None,
    brand_id: Optional[str] = None,
):
    creds = _refresh_credentials(refresh_token, user_id=user_id, brand_id=brand_id)
    if not creds:
        return None
    return build("youtubeAnalytics", "v2", credentials=creds, cache_discovery=False)


def get_access_token(
    refresh_token: Optional[str] = None,
    user_id: Optional[str] = None,
    brand_id: Optional[str] = None,
) -> Optional[str]:
    """
    Return a fresh OAuth access token for YouTube API calls.
    """
    creds = _refresh_credentials(refresh_token, user_id=user_id, brand_id=brand_id)
    if not creds:
        return None
    return creds.token


def _parse_youtube_publish_at(value: Optional[str]) -> Optional[datetime.datetime]:
    if not value:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(datetime.timezone.utc)


def delete_youtube_video_if_safe(
    *,
    video_id: str,
    expected_video_ids: List[str],
    scheduled_publish_at: Optional[str] = None,
    allow_unpublished_private: bool = False,
    require_live_schedule: bool = False,
    user_id: Optional[str] = None,
    brand_id: Optional[str] = None,
    reason: str = "",
) -> Dict[str, Any]:
    """Delete a YouTube video only after verifying it is our private, unpublished upload."""
    clean_video_id = str(video_id or "").strip()
    expected_ids = {str(item or "").strip() for item in expected_video_ids or [] if str(item or "").strip()}
    if not clean_video_id or clean_video_id not in expected_ids:
        current_app.logger.warning(
            "YouTube safe delete blocked reason=id_not_in_db video_id=%s expected=%s context=%s",
            clean_video_id,
            sorted(expected_ids),
            reason,
        )
        return {"deleted": False, "blocked": True, "reason": "id_not_in_db"}

    try:
        youtube = build_authenticated_youtube(user_id=user_id, brand_id=brand_id)
    except Exception:
        return {"deleted": False, "blocked": True, "reason": "credentials_error"}
    if not youtube:
        current_app.logger.warning("YouTube safe delete blocked reason=no_token video_id=%s context=%s", clean_video_id, reason)
        return {"deleted": False, "blocked": True, "reason": "no_token"}

    try:
        response = youtube.videos().list(part="status", id=clean_video_id).execute()
        record_youtube_data_api_call(feature="publish", method="videos.list")
    except Exception as exc:
        current_app.logger.warning(
            "YouTube safe delete blocked reason=list_error video_id=%s context=%s",
            clean_video_id,
            reason,
        )
        return {"deleted": False, "blocked": True, "reason": "list_error"}

    items = response.get("items") or []
    if not items:
        current_app.logger.warning("YouTube safe delete blocked reason=not_found video_id=%s context=%s", clean_video_id, reason)
        return {"deleted": False, "blocked": True, "reason": "not_found"}

    status = (items[0] or {}).get("status") or {}
    privacy_status = str(status.get("privacyStatus") or "").strip().lower()
    live_status = {"privacy_status": privacy_status, "publish_at": status.get("publishAt")}
    if privacy_status != "private":
        current_app.logger.warning(
            "YouTube safe delete blocked reason=not_private video_id=%s privacy=%s context=%s",
            clean_video_id,
            privacy_status,
            reason,
        )
        return {"deleted": False, "blocked": True, "reason": "not_private", **live_status}

    now_utc = datetime.datetime.now(datetime.timezone.utc)
    api_publish_at = _parse_youtube_publish_at(status.get("publishAt"))
    db_publish_at = _parse_youtube_publish_at(scheduled_publish_at)
    future_publish_at = api_publish_at if require_live_schedule else (api_publish_at or db_publish_at)
    is_future_scheduled = bool(future_publish_at and future_publish_at > now_utc)
    if not is_future_scheduled and (require_live_schedule or not allow_unpublished_private):
        current_app.logger.warning(
            "YouTube safe delete blocked reason=not_future_scheduled video_id=%s api_publish_at=%s db_publish_at=%s context=%s",
            clean_video_id,
            status.get("publishAt"),
            scheduled_publish_at,
            reason,
        )
        return {"deleted": False, "blocked": True, "reason": "not_future_scheduled", **live_status}

    try:
        youtube.videos().delete(id=clean_video_id).execute()
        record_youtube_data_api_call(feature="publish", method="videos.delete")
    except Exception as exc:
        current_app.logger.warning(
            "YouTube safe delete failed video_id=%s context=%s",
            clean_video_id,
            reason,
        )
        status_code = getattr(getattr(exc, "resp", None), "status", None)
        transient = status_code in {408, 429, 500, 502, 503, 504} or isinstance(
            exc, (TimeoutError, ConnectionError, requests.Timeout, requests.ConnectionError)
        )
        return {
            "deleted": False, "blocked": True, "reason": "delete_error",
            "transient_delete_failure": transient, **live_status,
        }

    current_app.logger.info("YouTube safe delete success video_id=%s context=%s", clean_video_id, reason)
    return {"deleted": True, "blocked": False, "reason": "deleted", "privacy_status": privacy_status}


def unschedule_youtube_video_if_safe(*, title: str, description: str, **kwargs) -> Dict[str, Any]:
    """Retained compatibility guard; uploaded videos are managed in YouTube Studio."""
    return {"unscheduled": False, "deleted": False, "blocked": True, "reason": "uploaded_locked"}


def fetch_video_statuses(
    video_ids: List[str],
    refresh_token: Optional[str] = None,
    user_id: Optional[str] = None,
    brand_id: Optional[str] = None,
) -> Dict[str, Dict[str, Any]]:
    if not video_ids:
        return {}
    youtube = build_authenticated_youtube(refresh_token, user_id=user_id, brand_id=brand_id)
    if not youtube:
        return {}
    statuses: Dict[str, Dict[str, Any]] = {}
    try:
        response = youtube.videos().list(part="status", id=",".join(video_ids)).execute()
        record_youtube_data_api_call(feature="reconcile", method="videos.list")
        for item in response.get("items", []):
            vid = item.get("id")
            if not vid:
                continue
            statuses[vid] = item.get("status", {})
        # A successful videos.list response omits ids that no longer exist (or
        # are no longer visible to the authenticated channel). Preserve that
        # distinction from an API failure, which returns no synthetic states.
        for video_id in video_ids:
            clean_video_id = str(video_id or "").strip()
            if clean_video_id and clean_video_id not in statuses:
                statuses[clean_video_id] = {"_minti_not_found": True}
    except Exception:
        current_app.logger.exception("Failed to fetch YouTube video statuses")
    return statuses


def youtube_reconciliation_state(status: Optional[Dict[str, Any]]) -> str:
    """Classify a read-only videos.list result for every reconciliation caller."""
    value = status or {}
    if value.get("_minti_not_found") is True:
        return "removed"
    privacy_status = str(value.get("privacyStatus") or "").strip().lower()
    upload_status = str(value.get("uploadStatus") or "").strip().lower()
    if privacy_status in {"public", "unlisted"} and upload_status in {"", "processed"}:
        return "published"
    if value:
        return "pending"
    return "unknown"

def get_connected_channel_info(
    user_id: Optional[str] = None,
    brand_id: Optional[str] = None,
) -> Tuple[Optional[Dict[str, object]], Optional[str]]:
    token = get_stored_refresh_token(user_id=user_id, brand_id=brand_id)
    if not token:
        return None, None
    creds = Credentials(
        token=None,
        refresh_token=token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=YOUTUBE_CLIENT_ID,
        client_secret=YOUTUBE_CLIENT_SECRET,
        scopes=SCOPES,
    )
    try:
        creds.refresh(Request())
        youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)
        resp = youtube.channels().list(part="snippet,contentDetails,statistics", mine=True, maxResults=1).execute()
        items = resp.get("items", [])
        if not items:
            return None, None
        item = items[0]
        snippet = item.get("snippet", {})
        stats = item.get("statistics") or {}
        try:
            subscriber_count = int(stats.get("subscriberCount"))
        except (TypeError, ValueError):
            subscriber_count = None
        return {
            "title": snippet.get("title", "Unknown channel"),
            "description": snippet.get("description", "")[:180],
            "thumbnail": snippet.get("thumbnails", {}).get("default", {}).get("url"),
            "subscriber_count": subscriber_count,
        }, None
    except RefreshError as exc:
        current_app.logger.warning("YouTube channel info fetch failed due to refresh error.")
        return None, "invalid_grant"
    except Exception as exc:  # pragma: no cover - network failures
        current_app.logger.warning("YouTube channel info fetch failed.")
        return None, "general_error"
