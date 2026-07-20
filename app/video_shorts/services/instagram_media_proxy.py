import base64
import hashlib
import hmac
import json
import os
import time
from typing import Optional


_TOKEN_SALT = b"video_shorts.instagram_media_proxy.v1"


class InstagramMediaProxyError(RuntimeError):
    pass


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode((data + padding).encode("ascii"))


def _secret_bytes(secret: Optional[str] = None) -> bytes:
    raw = (secret or os.getenv("SECRET_KEY") or "").strip()
    if not raw:
        raise InstagramMediaProxyError("SECRET_KEY is not set for Instagram media proxy signing.")
    return raw.encode("utf-8")


def _signature(payload_b64: str, *, secret: Optional[str] = None) -> str:
    mac = hmac.new(_secret_bytes(secret) + _TOKEN_SALT, payload_b64.encode("ascii"), hashlib.sha256)
    return _b64url_encode(mac.digest())


def issue_instagram_media_proxy_token(
    queue_id: str,
    *,
    expires_in_seconds: int = 3600,
    secret: Optional[str] = None,
) -> str:
    now = int(time.time())
    payload = {
        "j": str(queue_id or "").strip(),
        "e": now + max(1, int(expires_in_seconds)),
        "v": 1,
    }
    payload_b64 = _b64url_encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    return f"{payload_b64}.{_signature(payload_b64, secret=secret)}"


def read_instagram_media_proxy_token(token: str, *, secret: Optional[str] = None) -> dict:
    raw = str(token or "").strip()
    if "." not in raw:
        raise InstagramMediaProxyError("Malformed token.")
    payload_b64, sig = raw.split(".", 1)
    expected = _signature(payload_b64, secret=secret)
    if not hmac.compare_digest(sig, expected):
        raise InstagramMediaProxyError("Invalid token signature.")
    try:
        payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
    except Exception as exc:
        raise InstagramMediaProxyError("Invalid token payload.") from exc
    expires_at = int(payload.get("e") or 0)
    queue_id = str(payload.get("j") or "").strip()
    if not queue_id or expires_at <= 0:
        raise InstagramMediaProxyError("Incomplete token payload.")
    if expires_at < int(time.time()):
        raise InstagramMediaProxyError("Expired token.")
    return {
        "queue_id": queue_id,
        "expires_at": expires_at,
        "version": payload.get("v"),
    }
