from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Iterable, Optional
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request


logger = logging.getLogger(__name__)

TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"

_RATE_LIMIT_LOCK = threading.Lock()
_RATE_LIMIT_BUCKETS: dict[str, deque[float]] = defaultdict(deque)


@dataclass(frozen=True)
class RateLimitRule:
    limit: int
    window_seconds: int


def turnstile_site_key() -> str:
    return (os.getenv("TURNSTILE_SITE_KEY") or "").strip()


def turnstile_enabled() -> bool:
    return bool(turnstile_site_key())


def verify_turnstile_token(*, token: str, remote_ip: str = "") -> bool:
    secret = (os.getenv("TURNSTILE_SECRET_KEY") or "").strip()
    if not secret:
        logger.error("Turnstile verification aborted: TURNSTILE_SECRET_KEY is not set")
        return False
    if not token:
        return False
    payload = {
        "secret": secret,
        "response": token,
    }
    if remote_ip:
        payload["remoteip"] = remote_ip
    body = urllib_parse.urlencode(payload).encode("utf-8")
    req = urllib_request.Request(
        TURNSTILE_VERIFY_URL,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": "MintiStudio/1.0",
        },
    )
    try:
        with urllib_request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib_error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        logger.exception("Turnstile verification failed: status=%s body=%s", exc.code, detail)
        return False
    except Exception:
        logger.exception("Turnstile verification failed")
        return False
    return bool(data.get("success"))


def check_rate_limits(bucket: str, key_parts: Iterable[str], rules: Iterable[RateLimitRule]) -> tuple[bool, int]:
    key = ":".join(part.strip() for part in key_parts if part and part.strip())
    if not key:
        key = "anonymous"
    now = time.time()
    max_retry_after = 0
    with _RATE_LIMIT_LOCK:
        for rule in rules:
            bucket_key = f"{bucket}:{key}:{rule.limit}:{rule.window_seconds}"
            entries = _RATE_LIMIT_BUCKETS[bucket_key]
            cutoff = now - rule.window_seconds
            while entries and entries[0] <= cutoff:
                entries.popleft()
            if len(entries) >= rule.limit:
                retry_after = max(1, int(rule.window_seconds - (now - entries[0])))
                max_retry_after = max(max_retry_after, retry_after)
            else:
                max_retry_after = max(max_retry_after, 0)
        if max_retry_after:
            return False, max_retry_after
        for rule in rules:
            bucket_key = f"{bucket}:{key}:{rule.limit}:{rule.window_seconds}"
            _RATE_LIMIT_BUCKETS[bucket_key].append(now)
    return True, 0
