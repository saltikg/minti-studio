# app/video_shorts/youtube_api.py

import os
import requests
import re
from typing import Optional, Dict, Any, List
from urllib.parse import urlparse, parse_qs

from google.auth.exceptions import RefreshError

from app.video_shorts.services.youtube_oauth import get_access_token, list_stored_refresh_tokens

YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY")


class YoutubeApiError(Exception):
    def __init__(self, message: str, status_code: Optional[int] = None, payload: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


def _require_api_key():
    if not YOUTUBE_API_KEY:
        raise YoutubeApiError("YOUTUBE_API_KEY is not set in environment")


def _youtube_videos_list_response(video_ids: List[str]) -> Dict[str, Any]:
    chunk = [str(video_id or "").strip() for video_id in video_ids if str(video_id or "").strip()]
    if not chunk:
        return {}
    ids_param = ",".join(chunk)
    url = (
        "https://www.googleapis.com/youtube/v3/videos"
        f"?part=snippet,contentDetails,statistics&id={ids_param}"
    )
    if YOUTUBE_API_KEY:
        resp = requests.get(f"{url}&key={YOUTUBE_API_KEY}", timeout=10)
        try:
            resp.raise_for_status()
        except requests.HTTPError as exc:
            payload = None
            try:
                payload = resp.json()
            except Exception:
                payload = None
            raise YoutubeApiError(
                f"YouTube stats fetch failed: {exc}",
                status_code=resp.status_code,
                payload=payload,
            ) from exc
        return resp.json() or {}

    for token_info in list_stored_refresh_tokens():
        if token_info.get("reauth_required"):
            continue
        access_token = get_access_token(user_id=token_info.get("user_id"))
        if not access_token:
            continue
        try:
            resp = requests.get(
                url,
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=10,
            )
            if resp.status_code == 401:
                continue
            resp.raise_for_status()
            return resp.json() or {}
        except requests.HTTPError as exc:
            payload = None
            try:
                payload = resp.json()
            except Exception:
                payload = None
            raise YoutubeApiError(
                f"YouTube stats fetch failed: {exc}",
                status_code=resp.status_code,
                payload=payload,
            ) from exc
        except (requests.RequestException, RefreshError):
            continue
    return {}


def extract_channel_id(channel_url: str):
    """
    Mevcut fonksiyonun korunuyor.

    Desteklemeye çalıştığımız formatlar:
    - https://www.youtube.com/channel/UCxxxxxx
    - https://www.youtube.com/@NadirKilicOfficial
    """

    _require_api_key()
    url = channel_url.strip()

    # 1) /channel/UCxxxxxx formatı
    if "/channel/" in url:
        return url.split("/channel/")[-1].split("/")[0]

    # 2) @handle formatı: önce yeni forHandle API, olmazsa eski forUsername
    if "@" in url:
        username = url.split("@")[-1].split("/")[0]
        # Newer param (handles)
        api_url = (
            "https://www.googleapis.com/youtube/v3/channels"
            f"?part=id&forHandle={username}&key={YOUTUBE_API_KEY}"
        )
        resp = requests.get(api_url)
        resp.raise_for_status()
        data = resp.json()
        items = data.get("items", [])
        if items:
            return items[0]["id"]

        # Fallback: legacy username lookup (eski hesaplar için)
        legacy_url = (
            "https://www.googleapis.com/youtube/v3/channels"
            f"?part=id&forUsername={username}&key={YOUTUBE_API_KEY}"
        )
        resp = requests.get(legacy_url)
        resp.raise_for_status()
        data = resp.json()
        items = data.get("items", [])
        if items:
            return items[0]["id"]

    # Bulamazsak None döner
    return None


def extract_video_id(video_url: str) -> Optional[str]:
    """
    Extract a YouTube video ID from common URL formats.
    """
    if not video_url:
        return None
    candidate = video_url.strip()
    if not candidate:
        return None
    if "://" not in candidate and "/" not in candidate and "?" not in candidate:
        return candidate
    try:
        parsed = urlparse(candidate)
    except Exception:
        return None

    host = (parsed.netloc or "").lower()
    path = parsed.path or ""
    query = parse_qs(parsed.query)

    if "youtu.be" in host:
        return path.lstrip("/").split("/")[0] or None

    if "youtube.com" in host or "youtube-nocookie.com" in host:
        if path.startswith("/watch"):
            return (query.get("v") or [None])[0]
        if path.startswith("/shorts/") or path.startswith("/embed/"):
            return path.split("/")[2] if len(path.split("/")) > 2 else None
    return None


def fetch_video_metadata(video_id: str) -> Dict[str, Any]:
    """
    Fetch snippet + contentDetails + statistics for a single video.
    """
    if not video_id:
        raise YoutubeApiError("Missing video_id")
    data = _youtube_videos_list_response([video_id])
    items = data.get("items") or []
    if not items:
        if not YOUTUBE_API_KEY:
            has_oauth_token = any(
                not token_info.get("reauth_required")
                for token_info in list_stored_refresh_tokens()
            )
            if not has_oauth_token:
                raise YoutubeApiError("YouTube API key veya aktif OAuth baglantisi gerekli")
        raise YoutubeApiError("Video not found on YouTube")

    item = items[0]
    snippet = item.get("snippet") or {}
    content_details = item.get("contentDetails") or {}
    statistics = item.get("statistics") or {}

    dur_iso = content_details.get("duration") or ""
    duration_seconds = _parse_duration_iso8601(dur_iso)

    def _to_int(val):
        try:
            return int(val)
        except Exception:
            return None

    thumbs = snippet.get("thumbnails") or {}
    def _thumb_url_from(obj):
        if isinstance(obj, dict):
            return obj.get("url")
        return obj
    thumb_url = (
        _thumb_url_from(thumbs.get("maxres")) or
        _thumb_url_from(thumbs.get("standard")) or
        _thumb_url_from(thumbs.get("high")) or
        _thumb_url_from(thumbs.get("medium")) or
        _thumb_url_from(thumbs.get("default"))
    )

    return {
        "video_id": video_id,
        "title": snippet.get("title"),
        "published_at": snippet.get("publishedAt"),
        "thumbnail_url": thumb_url,
        "channel_id": snippet.get("channelId"),
        "channel_title": snippet.get("channelTitle"),
        "duration_seconds": duration_seconds or None,
        "view_count": _to_int(statistics.get("viewCount")),
        "like_count": _to_int(statistics.get("likeCount")),
        "comment_count": _to_int(statistics.get("commentCount")),
    }


def fetch_channel_subscriber_counts(channel_ids: List[str]) -> Dict[str, Dict[str, Optional[str]]]:
    if not channel_ids:
        return {}
    _require_api_key()
    results: Dict[str, Dict[str, Optional[str]]] = {}
    chunk_size = 50
    for i in range(0, len(channel_ids), chunk_size):
        chunk = channel_ids[i : i + chunk_size]
        url = (
            "https://www.googleapis.com/youtube/v3/channels"
            f"?part=snippet,statistics&id={','.join(chunk)}&key={YOUTUBE_API_KEY}"
        )
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        payload = resp.json()
        for item in payload.get("items", []):
            channel_id = item.get("id")
            if not channel_id:
                continue
            snippet = item.get("snippet") or {}
            stats = item.get("statistics") or {}
            try:
                subscriber_count = int(stats.get("subscriberCount"))
            except (TypeError, ValueError):
                subscriber_count = None
            results[channel_id] = {
                "subscriber_count": subscriber_count,
                "channel_title": snippet.get("title"),
            }
    return results


def fetch_videos(channel_id: str, max_results: int = 20):
    """
    Kanalın son videolarını çeker.
    channel_id parametresi UCxxxx ile başlayan gerçek kanal ID olmalı.

    Bu fonksiyon eski kodunla uyumlu, search endpointini kullanıyor.
    Yeni tam tarama yapımız playlistItems ile çalışacak ama
    bunu bozmayalım, mevcut kullanım devam edebilsin.
    """

    _require_api_key()

    search_url = (
        "https://www.googleapis.com/youtube/v3/search"
        f"?key={YOUTUBE_API_KEY}"
        f"&channelId={channel_id}"
        "&part=snippet,id"
        "&order=date"
        f"&maxResults={max_results}"
    )

    resp = requests.get(search_url)
    resp.raise_for_status()
    r = resp.json()
    items = r.get("items", [])

    videos = []

    for it in items:
        if it["id"].get("kind") != "youtube#video":
            continue

        vid = it["id"]["videoId"]
        snippet = it["snippet"]

        thumb = None
        thumbs = snippet.get("thumbnails") or {}
        if "high" in thumbs:
            thumb = thumbs["high"].get("url")
        elif "default" in thumbs:
            thumb = thumbs["default"].get("url")

        videos.append(
            {
                "video_id": vid,
                "title": snippet["title"],
                "published_at": snippet["publishedAt"],
                "thumbnail_url": thumb,
            }
        )

    return videos


def get_channel_metadata(channel_url: str):
    """
    Kanal için metadata getirir:
    - youtube_channel_id (UC ile başlayan id)
    - uploads_playlist_id
    - total_videos (statistics.videoCount)

    Yeni tam tarama mimarisinde bunu kullanacağız.
    """

    _require_api_key()
    url = channel_url.strip()

    # Önce mümkünse channel_id çıkarmaya çalış
    channel_id = None

    # /channel/UCxxxxxx formatı
    if "/channel/" in url:
        channel_id = url.split("/channel/")[-1].split("/")[0]
        params = {
            "part": "contentDetails,statistics",
            "id": channel_id,
            "key": YOUTUBE_API_KEY,
        }
    else:
        # Diğer durumlarda extract_channel_id ile dene (handle öncelikli)
        channel_id = extract_channel_id(url)
        params = {
            "part": "contentDetails,statistics",
            "key": YOUTUBE_API_KEY,
        }
        if channel_id:
            params["id"] = channel_id
        else:
            # Hala id yoksa handle üzerinden forHandle dene
            if "@" in url:
                username = url.split("@")[-1].split("/")[0]
                params["forHandle"] = username
            else:
                # Son çare: eski forUsername (bazı eski kanallar)
                if "https://www.youtube.com/user/" in url:
                    username = url.split("/user/")[-1].split("/")[0]
                    params["forUsername"] = username

    resp = requests.get("https://www.googleapis.com/youtube/v3/channels", params=params)
    resp.raise_for_status()
    data = resp.json()

    items = data.get("items", [])
    if not items:
        raise YoutubeApiError("Channel not found on YouTube")

    item = items[0]

    youtube_channel_id = item["id"]
    uploads_playlist_id = item["contentDetails"]["relatedPlaylists"]["uploads"]
    total_videos = int(item["statistics"]["videoCount"])

    return {
        "youtube_channel_id": youtube_channel_id,
        "uploads_playlist_id": uploads_playlist_id,
        "total_videos": total_videos,
    }


def fetch_playlist_items_batch(playlist_id: str, page_token: str = None, max_results: int = 50):
    """
    Uploads playlistinden bir batch video çeker.

    Dönen veri:
    {
      "videos": [
        {"video_id": "...", "title": "...", "published_at": "...", "thumbnail_url": "..."},
        ...
      ],
      "next_page_token": "xxxx" veya None
    }
    """

    _require_api_key()

    params = {
        "part": "contentDetails,snippet",
        "playlistId": playlist_id,
        "maxResults": max_results,
        "key": YOUTUBE_API_KEY,
    }
    if page_token:
        params["pageToken"] = page_token

    resp = requests.get("https://www.googleapis.com/youtube/v3/playlistItems", params=params)
    resp.raise_for_status()
    data = resp.json()

    videos = []

    for it in data.get("items", []):
        content = it["contentDetails"]
        snippet = it["snippet"]

        thumb = None
        thumbs = snippet.get("thumbnails") or {}
        if "high" in thumbs:
            thumb = thumbs["high"].get("url")
        elif "default" in thumbs:
            thumb = thumbs["default"].get("url")

        videos.append(
            {
                "video_id": content["videoId"],
                "title": snippet["title"],
                "published_at": content["videoPublishedAt"],
                "thumbnail_url": thumb,
            }
        )

    next_token = data.get("nextPageToken")

    return {
        "videos": videos,
        "next_page_token": next_token,
    }


# ===========================
# Video stats helper
# ===========================
def _parse_duration_iso8601(s: str) -> int:
    """
    PT1H2M3S gibi süreleri saniyeye çevirir.
    """
    if not s:
        return 0
    pattern = r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?"
    m = re.match(pattern, s)
    if not m:
        return 0
    h = int(m.group(1) or 0)
    m_ = int(m.group(2) or 0)
    s_ = int(m.group(3) or 0)
    return h * 3600 + m_ * 60 + s_


def fetch_video_stats(video_ids):
    """
    videos.list ile contentDetails + statistics çeker.
    Dönüş: { videoId: {duration_seconds, view_count, like_count, comment_count} }
    """
    if not video_ids:
        return {}

    stats_map = {}
    chunk_size = 50
    for i in range(0, len(video_ids), chunk_size):
        chunk = video_ids[i:i + chunk_size]
        try:
            data = _youtube_videos_list_response(chunk)
        except requests.RequestException as exc:
            raise YoutubeApiError(f"YouTube stats fetch failed: {exc}") from exc
        items = data.get("items") or []

        for item in items:
            vid = item.get("id")
            if not vid:
                continue
            content_details = item.get("contentDetails", {}) or {}
            statistics = item.get("statistics", {}) or {}

            dur_iso = content_details.get("duration") or ""
            duration_seconds = _parse_duration_iso8601(dur_iso)

            def _to_int(val):
                try:
                    return int(val)
                except Exception:
                    return None

            snippet = item.get("snippet") or {}
            thumbs = snippet.get("thumbnails") or {}
            def _thumb_url_from(obj):
                if isinstance(obj, dict):
                    return obj.get("url")
                return obj
            thumb_url = (
                _thumb_url_from(thumbs.get("maxres")) or
                _thumb_url_from(thumbs.get("standard")) or
                _thumb_url_from(thumbs.get("high")) or
                _thumb_url_from(thumbs.get("medium")) or
                _thumb_url_from(thumbs.get("default"))
            )
            stats_map[vid] = {
                "duration_seconds": duration_seconds or None,
                "view_count": _to_int(statistics.get("viewCount")),
                "like_count": _to_int(statistics.get("likeCount")),
                "comment_count": _to_int(statistics.get("commentCount")),
                "favorite_count": _to_int(statistics.get("favoriteCount")),
                "dislike_count": _to_int(statistics.get("dislikeCount")),
                "thumbnail_url": thumb_url,
            }

    return stats_map


def fetch_video_comments(
    video_id: str,
    max_results: int = 10,
    moderation_status: Optional[str] = None,
    user_id: Optional[str] = None,
):
    """
    Get top-level comment threads for a video.
    If moderation_status is provided (e.g., heldForReview), OAuth is required.
    Otherwise falls back to API key for published comments.
    """
    if not video_id:
        return []
    max_results = max_results or 10
    max_results = min(max_results, 50)
    if max_results <= 0:
        max_results = 10

    if moderation_status:
        return _fetch_video_comments_oauth(video_id, max_results, moderation_status, user_id=user_id)
    if YOUTUBE_API_KEY:
        try:
            return _fetch_video_comments_api_key(video_id, max_results)
        except YoutubeApiError:
            if user_id:
                try:
                    return _fetch_video_comments_oauth(
                        video_id,
                        max_results,
                        None,
                        user_id=user_id,
                    )
                except YoutubeApiError:
                    pass
            raise
    if user_id:
        try:
            return _fetch_video_comments_oauth(
                video_id,
                max_results,
                None,
                user_id=user_id,
            )
        except YoutubeApiError:
            pass
    for token_info in list_stored_refresh_tokens():
        if token_info.get("reauth_required"):
            continue
        candidate_user_id = token_info.get("user_id")
        if not candidate_user_id:
            continue
        try:
            return _fetch_video_comments_oauth(
                video_id,
                max_results,
                None,
                user_id=candidate_user_id,
            )
        except YoutubeApiError:
            continue
    raise YoutubeApiError("Unable to fetch published comments: no API key or valid OAuth token")


def _serialize_comment(
    snippet: Dict[str, Any],
    comment_id: Optional[str],
    video_id: str,
    status_label: Optional[str],
    parent_id: Optional[str] = None,
    thread_id: Optional[str] = None,
):
    comment_url = None
    if comment_id:
        if parent_id:
            comment_url = f"https://www.youtube.com/watch?v={video_id}&lc={comment_id}"
        else:
            comment_url = f"https://www.youtube.com/watch?v={video_id}&lc={comment_id}"
    return {
        "author": snippet.get("authorDisplayName"),
        "text": snippet.get("textDisplay"),
        "published_at": snippet.get("publishedAt"),
        "like_count": snippet.get("likeCount"),
        "comment_id": comment_id,
        "thread_id": thread_id,
        "comment_url": comment_url,
        "status": status_label or snippet.get("moderationStatus") or "unknown",
        "parent_id": parent_id,
        "is_reply": parent_id is not None,
        "video_id": video_id,
    }


def _parse_comments(items, video_id: str, status_label: Optional[str]):
    comments: List[Dict[str, Any]] = []
    for item in items:
        top_level = (item.get("snippet") or {}).get("topLevelComment", {}) or {}
        comment_snippet = top_level.get("snippet") or {}
        comment_id = top_level.get("id") or item.get("id")
        thread_id = item.get("id")
        comments.append(
            _serialize_comment(
                comment_snippet,
                comment_id,
                video_id,
                status_label,
                parent_id=None,
                thread_id=thread_id,
            )
        )
        reply_container = item.get("replies") or {}
        for reply in reply_container.get("comments") or []:
            reply_snippet = reply.get("snippet") or {}
            reply_id = reply.get("id")
            reply_status = reply_snippet.get("moderationStatus") or status_label or "published"
            comments.append(
                _serialize_comment(
                    reply_snippet,
                    reply_id,
                    video_id,
                    reply_status,
                    parent_id=comment_id,
                    thread_id=thread_id,
                )
            )
    return comments




def _fetch_video_comments_api_key(video_id: str, max_results: int):
    _require_api_key()
    params = {
        "part": "snippet,replies",
        "videoId": video_id,
        "maxResults": max_results,
        "textFormat": "plainText",
        "order": "time",
        "key": YOUTUBE_API_KEY,
    }
    try:
        resp = requests.get("https://www.googleapis.com/youtube/v3/commentThreads", params=params, timeout=10)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise YoutubeApiError(f"YouTube comments fetch failed: {exc}")
    items = resp.json().get("items") or []
    return _parse_comments(items, video_id, "published")


def _fetch_video_comments_oauth(
    video_id: str,
    max_results: int,
    moderation_status: Optional[str],
    user_id: Optional[str] = None,
):
    try:
        access_token = get_access_token(user_id=user_id)
    except RefreshError as exc:
        raise YoutubeApiError(f"YouTube OAuth refresh failed: {exc}")
    except requests.RequestException as exc:
        raise YoutubeApiError(f"YouTube OAuth request failed: {exc}")
    except Exception as exc:
        raise YoutubeApiError(f"YouTube OAuth error: {exc}")
    if not access_token:
        raise YoutubeApiError("YouTube OAuth token is not available")
    params = {
        "part": "snippet,replies",
        "videoId": video_id,
        "maxResults": max_results,
        "textFormat": "plainText",
        "order": "time",
    }
    if moderation_status:
        params["moderationStatus"] = moderation_status
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        resp = requests.get(
            "https://www.googleapis.com/youtube/v3/commentThreads",
            params=params,
            headers=headers,
            timeout=10,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise YoutubeApiError(f"YouTube comments fetch failed: {exc}")
    items = resp.json().get("items") or []
    return _parse_comments(items, video_id, moderation_status or "published")
