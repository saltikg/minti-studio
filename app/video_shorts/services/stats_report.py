"""Single-use report grants and bounded Playwright PDF rendering."""
from __future__ import annotations

import fcntl
import hashlib
import os
import secrets
import time
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Dict, Iterator
from urllib.parse import urlencode

from flask import current_app
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.video_shorts.config import PRIVATE_STATE_DIR


REPORT_TOKEN_MAX_AGE = 300
REPORT_TOKEN_SALT = "stats-report-render-v1"
REPORT_RENDER_ROUTE = "/video_shorts/stats/report/render"
_TOKEN_DIR = PRIVATE_STATE_DIR / "stats_report_tokens"
_LOCK_PATH = PRIVATE_STATE_DIR / "stats_report_render.lock"


class ReportGrantError(ValueError):
    pass


class ReportRenderBusy(RuntimeError):
    pass


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(current_app.secret_key, salt=REPORT_TOKEN_SALT)


def _marker_path(nonce: str) -> Path:
    digest = hashlib.sha256(nonce.encode("utf-8")).hexdigest()
    return _TOKEN_DIR / digest


def issue_report_grant(*, owner_user_id: str, brand_id: str, start_date: date, end_date: date) -> str:
    nonce = secrets.token_urlsafe(32)
    _TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    marker = _marker_path(nonce)
    marker.write_text("unused", encoding="utf-8")
    try:
        return _serializer().dumps(
            {
                "purpose": REPORT_TOKEN_SALT,
                "route": REPORT_RENDER_ROUTE,
                "owner_user_id": owner_user_id,
                "brand_id": brand_id,
                "start": start_date.isoformat(),
                "end": end_date.isoformat(),
                "nonce": nonce,
            }
        )
    except Exception:
        marker.unlink(missing_ok=True)
        raise


def consume_report_grant(
    token: str,
    *,
    brand_id: str,
    start: str,
    end: str,
    route: str,
) -> Dict[str, str]:
    try:
        claims = _serializer().loads(token, max_age=REPORT_TOKEN_MAX_AGE)
    except (BadSignature, SignatureExpired) as exc:
        raise ReportGrantError("The report link is invalid or expired") from exc
    expected = {
        "purpose": REPORT_TOKEN_SALT,
        "route": route,
        "brand_id": brand_id,
        "start": start,
        "end": end,
    }
    if any(str(claims.get(key) or "") != value for key, value in expected.items()):
        raise ReportGrantError("The report link does not match this brand, range, or route")
    owner_user_id = str(claims.get("owner_user_id") or "").strip()
    nonce = str(claims.get("nonce") or "").strip()
    if not owner_user_id or not nonce:
        raise ReportGrantError("The report link is incomplete")
    marker = _marker_path(nonce)
    try:
        marker.unlink()
    except FileNotFoundError as exc:
        raise ReportGrantError("The report link has already been used") from exc
    return {
        "owner_user_id": owner_user_id,
        "brand_id": brand_id,
        "start": start,
        "end": end,
    }


@contextmanager
def _render_lock() -> Iterator[None]:
    _LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK_PATH.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ReportRenderBusy("Another report is rendering. Please try again shortly.") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def render_report_pdf(*, token: str, brand_id: str, start_date: date, end_date: date) -> tuple[bytes, float]:
    from playwright.sync_api import sync_playwright

    base_url = str(
        current_app.config.get("REPORT_RENDER_BASE_URL")
        or os.getenv("REPORT_RENDER_BASE_URL")
        or "http://127.0.0.1:8000"
    ).rstrip("/")
    query = urlencode(
        {
            "token": token,
            "brand_id": brand_id,
            "start": start_date.isoformat(),
            "end": end_date.isoformat(),
        }
    )
    started = time.monotonic()
    with _render_lock(), sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        try:
            page = browser.new_page(viewport={"width": 816, "height": 1056}, device_scale_factor=1)
            page.set_default_timeout(60_000)
            response = page.goto(f"{base_url}{REPORT_RENDER_ROUTE}?{query}", wait_until="networkidle", timeout=60_000)
            if response is None or not response.ok:
                raise RuntimeError(f"Report view returned HTTP {response.status if response else 'unknown'}")
            page.wait_for_function("window.__MINTI_REPORT_READY__ === true", timeout=60_000)
            pdf = page.pdf(
                format="Letter",
                print_background=True,
                prefer_css_page_size=True,
                margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
            )
        finally:
            browser.close()
    return pdf, time.monotonic() - started
