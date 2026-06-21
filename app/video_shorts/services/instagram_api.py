from datetime import datetime, timezone
import json
import requests
from typing import Dict, List, Optional

from app.video_shorts.config import IG_GRAPH_API_BASE
from app.video_shorts.services.comment_moderation import moderate_text_entries
from app.video_shorts.services.comment_store import upsert_comment_records
from app.video_shorts.services.instagram_queue import (
    fetch_instagram_media_jobs,
    get_instagram_queue_entry,
    update_instagram_metrics,
    upsert_instagram_comment_cache,
)
from src.trends.instagram_tokens import get_instagram_credentials


class InstagramActionError(RuntimeError):
    pass


def _extract_graph_error_message(resp: requests.Response) -> str:
    body = (resp.text or "").strip()
    try:
        payload = resp.json() if body else {}
    except ValueError:
        payload = {}

    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return body or f"Instagram Graph API request failed with status {resp.status_code}."

    message = (error.get("message") or "").strip()
    code = error.get("code")
    error_type = error.get("type")

    if code == 10:
        return (
            "Instagram insight veya yorum izni yok. Instagram hesabini "
            "`instagram_manage_insights` ve `instagram_manage_comments` izinleriyle yeniden baglayin."
        )

    if message:
        return message

    return json.dumps(
        {
            "error": {
                "message": message or "Instagram Graph API request failed.",
                "code": code,
                "type": error_type,
            }
        },
        ensure_ascii=False,
    )


def _extract_instagram_replies(comment: Dict[str, object]) -> List[Dict[str, object]]:
    replies = comment.get("replies") if comment else None
    if isinstance(replies, dict):
        return replies.get("data") or []
    if isinstance(replies, list):
        return replies
    return []


def _moderate_instagram_comments(
    comments: List[Dict[str, object]],
    user_id: Optional[str],
) -> Dict[str, Dict[str, object]]:
    entries: List[Dict[str, str]] = []
    for comment in comments or []:
        comment_id = comment.get("id")
        text = (comment.get("text") or "").strip()
        if comment_id and text:
            entries.append({"id": str(comment_id), "text": text})
        for reply in _extract_instagram_replies(comment):
            reply_id = reply.get("id")
            reply_text = (reply.get("text") or "").strip()
            if reply_id and reply_text:
                entries.append({"id": str(reply_id), "text": reply_text})

    if not entries:
        return {}

    return moderate_text_entries(entries, user_id or "")


def _apply_comment_moderation(
    comments: List[Dict[str, object]],
    moderation_map: Dict[str, Dict[str, object]],
) -> None:
    for comment in comments or []:
        comment_id = comment.get("id")
        if comment_id and str(comment_id) in moderation_map:
            comment["moderation"] = moderation_map[str(comment_id)]
        for reply in _extract_instagram_replies(comment):
            reply_id = reply.get("id")
            if reply_id and str(reply_id) in moderation_map:
                reply["moderation"] = moderation_map[str(reply_id)]


def _build_instagram_comment_records(
    entry: Dict[str, object],
    media_id: str,
    comments: List[Dict[str, object]],
) -> List[Dict[str, object]]:
    records: List[Dict[str, object]] = []
    now = datetime.now(timezone.utc)
    for comment in comments or []:
        moderation = comment.get("moderation") or {}
        comment_id = comment.get("id")
        if comment_id:
            records.append(
                {
                    "platform": "instagram",
                    "comment_id": str(comment_id),
                    "parent_id": None,
                    "thread_id": str(comment_id),
                    "video_id": entry.get("video_id"),
                    "instagram_media_id": media_id,
                    "queue_id": entry.get("id"),
                    "owner_user_id": entry.get("user_id"),
                    "video_title": entry.get("plan_title"),
                    "author": comment.get("username"),
                    "text": comment.get("text"),
                    "status": "published",
                    "comment_url": entry.get("permalink"),
                    "published_at": comment.get("timestamp"),
                    "like_count": comment.get("like_count"),
                    "moderation_flagged": moderation.get("flagged")
                    if moderation
                    else None,
                    "moderation_reason": moderation.get("reason") if moderation else None,
                    "moderation_checked_at": now if moderation else None,
                }
            )
        for reply in _extract_instagram_replies(comment):
            reply_moderation = reply.get("moderation") or {}
            reply_id = reply.get("id")
            if not reply_id:
                continue
            records.append(
                {
                    "platform": "instagram",
                    "comment_id": str(reply_id),
                    "parent_id": str(comment_id) if comment_id else None,
                    "thread_id": str(comment_id) if comment_id else None,
                    "video_id": entry.get("video_id"),
                    "instagram_media_id": media_id,
                    "queue_id": entry.get("id"),
                    "owner_user_id": entry.get("user_id"),
                    "video_title": entry.get("plan_title"),
                    "author": reply.get("username"),
                    "text": reply.get("text"),
                    "status": "published",
                    "comment_url": entry.get("permalink"),
                    "published_at": reply.get("timestamp"),
                    "like_count": reply.get("like_count"),
                    "moderation_flagged": reply_moderation.get("flagged")
                    if reply_moderation
                    else None,
                    "moderation_reason": reply_moderation.get("reason")
                    if reply_moderation
                    else None,
                    "moderation_checked_at": now if reply_moderation else None,
                }
            )
    return records


def _extract_insight_value(payload: Dict[str, object], metric: str) -> Optional[int]:
    data = payload.get("data") if payload else None
    if not data:
        return None
    for item in data:
        if item.get("name") != metric:
            continue
        values = item.get("values") or []
        if values:
            value = values[0].get("value")
        else:
            value = item.get("value")
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    return None


def _get_entry_and_token(queue_id: str):
    entry = get_instagram_queue_entry(queue_id)
    if not entry:
        raise InstagramActionError("Instagram kuyruğu kaydı bulunamadı.")
    user_id = entry.get("user_id")
    if not user_id:
        raise InstagramActionError("Instagram kullanıcısı bulunamadı.")
    creds = get_instagram_credentials(user_id)
    if not creds:
        raise InstagramActionError("Instagram erişim bilgisi bulunamadı.")
    token = creds.get("page_access_token")
    if not token:
        raise InstagramActionError("Instagram page token eksik.")
    return entry, token


def _graph_request(method: str, path: str, token: str, *, params=None, data=None):
    url = f"{IG_GRAPH_API_BASE.rstrip('/')}/{path.lstrip('/')}"
    params = params or {}
    params["access_token"] = token
    resp = requests.request(method, url, params=params, data=data, timeout=20)
    if resp.status_code >= 400:
        raise InstagramActionError(_extract_graph_error_message(resp))
    return resp.json() if resp.text else {}


def delete_instagram_comment(queue_id: str, comment_id: str) -> None:
    _, token = _get_entry_and_token(queue_id)
    _graph_request("DELETE", comment_id, token)


def reply_instagram_comment(queue_id: str, comment_id: str, message: str) -> Dict[str, str]:
    if not message:
        raise InstagramActionError("Yanıt metni boş olamaz.")
    _, token = _get_entry_and_token(queue_id)
    return _graph_request(
        "POST",
        f"{comment_id}/replies",
        token,
        data={"message": message},
    )


def set_instagram_comment_like(queue_id: str, comment_id: str, like: bool = True) -> None:
    _, token = _get_entry_and_token(queue_id)
    method = "POST" if like else "DELETE"
    path = f"{comment_id}/likes"
    _graph_request(method, path, token)


def refresh_instagram_media(queue_id: str, comments_limit: int = 25) -> None:
    entry, token = _get_entry_and_token(queue_id)
    media_id = entry.get("instagram_media_id")
    if not media_id:
        raise InstagramActionError("Instagram media_id kaydedilmemiş.")
    details = _graph_request(
        "GET",
        media_id,
        token,
        params={
            "fields": "id,media_type,permalink,like_count,comments_count,timestamp",
        },
    )
    views = None
    reach = None
    saved = None
    shares = None
    try:
        insights = _graph_request(
            "GET",
            f"{media_id}/insights",
            token,
            params={"metric": "views,reach,likes,comments,saved,shares,total_interactions"},
        )
        views = _extract_insight_value(insights, "views")
        reach = _extract_insight_value(insights, "reach")
        saved = _extract_insight_value(insights, "saved")
        shares = _extract_insight_value(insights, "shares")
    except InstagramActionError as exc:
        media_type = (details.get("media_type") or "").lower()
        print(f"Instagram insights failed media_id={media_id} media_type={media_type}: {exc}")
    update_instagram_metrics(
        queue_id,
        like_count=details.get("like_count"),
        comment_count=details.get("comments_count"),
        permalink=details.get("permalink"),
        impressions=views,
        reach=reach,
        saved=saved,
        shares=shares,
    )
    comments: List[Dict[str, object]] = []
    if comments_limit > 0:
        try:
            data = _graph_request(
                "GET",
                f"{media_id}/comments",
                token,
                params={
                    "fields": "id,text,username,like_count,timestamp,replies{id,text,username,like_count,timestamp}",
                    "limit": comments_limit,
                },
            )
            comments = data.get("data") or []
            moderation_map = _moderate_instagram_comments(comments, entry.get("user_id"))
            if moderation_map:
                _apply_comment_moderation(comments, moderation_map)
        except InstagramActionError as exc:
            # Comment fetching failure shouldn't stop metrics update
            raise InstagramActionError(f"Comments fetch failed: {exc}") from exc
    if comments:
        records = _build_instagram_comment_records(entry, media_id, comments)
        upsert_comment_records(records)
    upsert_instagram_comment_cache(
        media_id,
        details.get("like_count"),
        details.get("comments_count"),
        comments,
    )


def fetch_instagram_follower_count(business_account_id: str, token: str) -> Dict[str, Optional[int]]:
    if not business_account_id:
        raise InstagramActionError("Instagram business account id eksik.")
    if not token:
        raise InstagramActionError("Instagram access token eksik.")
    data = _graph_request(
        "GET",
        business_account_id,
        token,
        params={"fields": "followers_count,username"},
    )
    try:
        followers_count = int(data.get("followers_count"))
    except (TypeError, ValueError):
        followers_count = None
    return {
        "followers_count": followers_count,
        "username": data.get("username"),
    }
