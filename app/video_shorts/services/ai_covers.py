"""Tenant-gated Gemini image covers. Invoked by cover workers, never a route."""
from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import subprocess
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from PIL import Image, ImageOps

from app.video_shorts.services.db import get_db, get_db_readonly, table_columns
from app.video_shorts.services.user_preferences import load_user_bool_preference

log = logging.getLogger(__name__)
PROMPT_VERSION = "cover-ai-v1"
DEFAULT_MODEL = "gemini-3.1-flash-image"
_AI_LOCK = threading.Lock()
_ADVISORY_LOCK_ID = 843715902
_STYLE_REFERENCE = Path(__file__).resolve().parents[1] / "static" / "img" / "cover-ai-style-v1.svg"


class AiCoverLimitReached(RuntimeError):
    pass


def ai_cover_enabled(owner_user_id: str, brand_id: str) -> bool:
    return (
        os.getenv("AI_COVERS_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}
        and bool(os.getenv("GEMINI_API_KEY", "").strip())
        and load_user_bool_preference(owner_user_id, f"ai_covers_enabled:{brand_id}", False)
    )


def ai_cover_monthly_cap() -> int:
    try:
        return max(0, min(1000, int(os.getenv("AI_COVER_MONTHLY_CAP", "60"))))
    except ValueError:
        return 60


def ensure_ai_cover_schema(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS shorts_cover_ai_calls (
          id VARCHAR PRIMARY KEY,
          owner_user_id VARCHAR NOT NULL,
          brand_id VARCHAR NOT NULL,
          generated_video_id BIGINT NOT NULL,
          model VARCHAR NOT NULL,
          prompt_version VARCHAR NOT NULL,
          request_kind VARCHAR NOT NULL,
          result_path VARCHAR NOT NULL,
          image_returned BOOLEAN NOT NULL DEFAULT FALSE,
          latency_ms INTEGER NOT NULL,
          cost_usd DOUBLE PRECISION,
          cost_estimated BOOLEAN NOT NULL DEFAULT TRUE,
          input_tokens INTEGER,
          output_tokens INTEGER,
          error TEXT,
          created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cover_ai_cap ON shorts_cover_ai_calls(brand_id, created_at, image_returned)")


def _month_bounds_utc(now: datetime | None = None) -> tuple[datetime, datetime]:
    now_pt = now or datetime.now(ZoneInfo("America/Los_Angeles"))
    if now_pt.tzinfo is None:
        now_pt = now_pt.replace(tzinfo=timezone.utc).astimezone(ZoneInfo("America/Los_Angeles"))
    else:
        now_pt = now_pt.astimezone(ZoneInfo("America/Los_Angeles"))
    start_pt = now_pt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start_pt.month == 12:
        end_pt = start_pt.replace(year=start_pt.year + 1, month=1)
    else:
        end_pt = start_pt.replace(month=start_pt.month + 1)
    return (
        start_pt.astimezone(timezone.utc).replace(tzinfo=None),
        end_pt.astimezone(timezone.utc).replace(tzinfo=None),
    )


def _month_start_utc() -> datetime:
    return _month_bounds_utc()[0]


def ai_cover_usage(owner_user_id: str, brand_id: str) -> tuple[int, int]:
    conn = get_db_readonly()
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM shorts_cover_ai_calls WHERE owner_user_id=? AND brand_id=? AND image_returned=TRUE AND created_at>=?",
            [owner_user_id, brand_id, _month_start_utc()],
        ).fetchone()
        return int(row[0] or 0), ai_cover_monthly_cap()
    finally:
        conn.close()


def ai_cover_allowance(
    owner_user_id: str,
    brand_id: str,
    *,
    now: datetime | None = None,
) -> tuple[int, int]:
    """Return AI-covered Shorts / rendered Shorts for the current Pacific month."""
    start_utc, end_utc = _month_bounds_utc(now)
    conn = get_db_readonly()
    try:
        if table_columns(conn, "shorts_cover_ai_calls"):
            used = int(conn.execute(
                """
                SELECT COUNT(DISTINCT generated_video_id)
                  FROM shorts_cover_ai_calls
                 WHERE owner_user_id=? AND brand_id=? AND image_returned=TRUE
                   AND created_at>=? AND created_at<?
                """,
                [str(owner_user_id), str(brand_id), start_utc, end_utc],
            ).fetchone()[0] or 0)
        else:
            used = 0
        allowed = int(conn.execute(
            """
            SELECT COUNT(DISTINCT id)
              FROM shorts_generated_videos
             WHERE CAST(user_id AS VARCHAR)=? AND brand_id=?
               AND generation_status='created'
               AND created_at>=? AND created_at<?
            """,
            [str(owner_user_id), str(brand_id), start_utc, end_utc],
        ).fetchone()[0] or 0)
        return used, allowed
    finally:
        conn.close()


def _normalize_ocr(value: str, *, turkish: bool = False) -> str:
    # Turkish I/İ are distinct letters; only case is ignored, not diacritics.
    lowered = value.translate(str.maketrans({"I": "ı", "İ": "i"})).lower() if turkish else value.casefold()
    lowered = unicodedata.normalize("NFC", lowered.replace("i\u0307", "i"))
    return "".join(char for char in lowered if char.isalnum())


def _ocr_matches(image: Image.Image, kicker: str, main: str, highlight: str) -> bool:
    turkish = any(char in kicker + main + highlight for char in "çğıöşüÇĞİÖŞÜ")
    expected = [_normalize_ocr(value, turkish=turkish) for value in (kicker, main, highlight) if value.strip()]
    observed: list[str] = []
    rgb = image.convert("RGB")
    gray = ImageOps.grayscale(rgb)
    if rgb.width < 1600:
        gray = gray.resize((rgb.width * 2, rgb.height * 2), Image.Resampling.LANCZOS)
    variants = ((rgb, "eng+tur", "11"), (gray, "tur+eng", "11"), (gray, "tur+eng", "6"))
    for variant, languages, psm in variants:
        output = io.BytesIO()
        variant.save(output, "PNG")
        result = subprocess.run(
            ["tesseract", "stdin", "stdout", "-l", languages, "--psm", psm],
            input=output.getvalue(), capture_output=True, timeout=20, check=False,
        )
        if result.returncode != 0:
            raise RuntimeError("Cover OCR unavailable")
        observed.append(_normalize_ocr(result.stdout.decode("utf-8", errors="replace"), turkish=turkish))
        if all(any(value in transcript for transcript in observed) for value in expected):
            return True
    return False


def _style_png() -> bytes:
    import cairosvg

    return cairosvg.svg2png(bytestring=_STYLE_REFERENCE.read_bytes(), output_width=1080, output_height=1920)


def _frame_jpeg(frame: Image.Image) -> bytes:
    output = io.BytesIO()
    frame.convert("RGB").save(output, "JPEG", quality=92)
    return output.getvalue()


def _prompt(kicker: str, main: str, highlight: str, *, text_free: bool) -> str:
    base = (
        "Image 1 is a style reference. Image 2 is a frame from my video. "
        "Create a 9:16 YouTube Shorts cover (1080x1920) in the exact style of image 1. "
        "Use the person from image 2. Keep the face EXACTLY the same — same identity, "
        "features, glasses, expression and skin; do not beautify or change the face. "
        "Cut the person out and place them on a dark background with a deep red glow "
        "behind them, studio-like lighting, sharp and clean, head fully visible, "
        "body reaching the bottom edge. "
    )
    if text_free:
        return base + "NO text, no letters, no logos; keep the lower 40% calm (dark chest area)."
    return base + (
        f'Text in the middle-lower area, over the chest, never over the face: '
        f'small top line: "{kicker}"; big main text: "{main}"; '
        f'red box with white text: "{highlight}". Bold condensed font, white text, '
        "large enough to read at thumbnail size. Spell every letter exactly as given, "
        "including Turkish letters (İ, ı, Ş, ş, Ğ, ğ, Ü, ü, Ö, ö, Ç, ç)."
    )


def _api_request(model: str, prompt: str, style: bytes, frame: bytes) -> tuple[Image.Image, dict]:
    body = {
        "contents": [{"role": "user", "parts": [
            {"text": prompt},
            {"inlineData": {"mimeType": "image/png", "data": base64.b64encode(style).decode("ascii")}},
            {"inlineData": {"mimeType": "image/jpeg", "data": base64.b64encode(frame).decode("ascii")}},
        ]}],
        "generationConfig": {"responseModalities": ["TEXT", "IMAGE"],
                             "imageConfig": {"aspectRatio": "9:16", "imageSize": "4K"}},
    }
    request = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": os.environ["GEMINI_API_KEY"]},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Gemini image API HTTP {exc.code}") from None
    for candidate in payload.get("candidates") or []:
        for part in (candidate.get("content") or {}).get("parts") or []:
            inline = part.get("inlineData") or {}
            if inline.get("data") and str(inline.get("mimeType") or "").startswith("image/"):
                image_bytes = base64.b64decode(inline["data"], validate=True)
                with Image.open(io.BytesIO(image_bytes)) as image:
                    return image.convert("RGB"), payload.get("usageMetadata") or {}
    raise RuntimeError("Gemini image API returned no image")


def _record_call(conn, *, owner: str, brand: str, generated_id: int, model: str,
                 request_kind: str, result_path: str, image_returned: bool,
                 latency_ms: int, usage: dict, error: str = "") -> dict:
    input_tokens = int(usage.get("promptTokenCount") or 0)
    output_tokens = int(usage.get("candidatesTokenCount") or 0)
    # API reports tokens, not a dollar amount. Google's listed 4K image rate is
    # $0.151; input tokens add a small, separately priced component.
    cost = round(0.151 + input_tokens * 0.50 / 1_000_000, 6) if image_returned else None
    call_id = str(uuid4())
    conn.execute("""
        INSERT INTO shorts_cover_ai_calls
          (id, owner_user_id, brand_id, generated_video_id, model, prompt_version,
           request_kind, result_path, image_returned, latency_ms, cost_usd,
           cost_estimated, input_tokens, output_tokens, error, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, TRUE, ?, ?, ?, ?)
    """, [call_id, owner, brand, generated_id, model, PROMPT_VERSION,
          request_kind, result_path, image_returned, latency_ms, cost,
          input_tokens or None, output_tokens or None, error[:500] or None,
          datetime.now(timezone.utc).replace(tzinfo=None)])
    conn.commit()
    return {"id": call_id, "model": model, "latency_ms": latency_ms, "path": result_path,
            "cost_usd_estimated": cost, "request_kind": request_kind}


def generate_ai_cover(frame: Image.Image, *, owner: str, brand: str, generated_id: int,
                      kicker: str, main: str, highlight: str) -> tuple[Image.Image, str, list[dict]]:
    if not ai_cover_enabled(owner, brand):
        raise RuntimeError("AI covers are disabled for this brand")
    model = os.getenv("GEMINI_IMAGE_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    if not re.fullmatch(r"[a-zA-Z0-9._-]+", model):
        raise RuntimeError("Invalid Gemini image model configuration")
    frame_bytes, style_bytes = _frame_jpeg(frame), _style_png()
    calls: list[dict] = []
    with _AI_LOCK:
        conn = get_db()
        locked = False
        try:
            if getattr(conn, "backend_name", "") == "postgres":
                conn.execute("SELECT pg_advisory_lock(?)", [_ADVISORY_LOCK_ID])
                locked = True
            ensure_ai_cover_schema(conn)
            conn.commit()
            def call(text_free: bool) -> Image.Image:
                used = conn.execute(
                    "SELECT COUNT(*) FROM shorts_cover_ai_calls WHERE brand_id=? AND image_returned=TRUE AND created_at>=?",
                    [brand, _month_start_utc()],
                ).fetchone()[0]
                if int(used or 0) >= ai_cover_monthly_cap():
                    raise AiCoverLimitReached("AI limit reached this month")
                last_error = None
                for attempt in range(2):
                    started = time.monotonic()
                    try:
                        image, usage = _api_request(model, _prompt(kicker, main, highlight, text_free=text_free), style_bytes, frame_bytes)
                        path = "text_free" if text_free else "generated_text"
                        calls.append(_record_call(conn, owner=owner, brand=brand, generated_id=generated_id,
                                                  model=model, request_kind=path, result_path=path,
                                                  image_returned=True, latency_ms=round((time.monotonic()-started)*1000), usage=usage))
                        return image
                    except Exception as exc:
                        last_error = exc
                        calls.append(_record_call(conn, owner=owner, brand=brand, generated_id=generated_id,
                                                  model=model, request_kind="text_free" if text_free else "text",
                                                  result_path="api_error", image_returned=False,
                                                  latency_ms=round((time.monotonic()-started)*1000), usage={},
                                                  error=f"{type(exc).__name__}: {exc}"))
                        log.warning("Gemini cover call failed brand=%s clip=%s attempt=%s: %s", brand, generated_id, attempt+1, exc)
                raise RuntimeError("Gemini image generation failed") from last_error

            for index in range(2):
                image = call(False)
                try:
                    matched = _ocr_matches(image, kicker, main, highlight)
                except Exception as exc:
                    log.warning("Gemini cover OCR failed brand=%s clip=%s: %s", brand, generated_id, exc)
                    matched = False
                if matched:
                    calls[-1]["path"] = "text_ok" if index == 0 else "regenerated_text_ok"
                    conn.execute("UPDATE shorts_cover_ai_calls SET result_path=? WHERE id=? AND brand_id=? AND generated_video_id=?",
                                 [calls[-1]["path"], calls[-1]["id"], brand, generated_id])
                    conn.commit()
                    return image, calls[-1]["path"], calls
                calls[-1]["path"] = "ocr_mismatch"
                conn.execute("UPDATE shorts_cover_ai_calls SET result_path='ocr_mismatch' WHERE id=?", [calls[-1]["id"]])
                conn.commit()
                log.info("Gemini cover OCR mismatch brand=%s clip=%s pass=%s", brand, generated_id, index+1)
            image = call(True)
            from app.video_shorts.services.clip_covers import _draw_bold_text
            image = image.resize((1080, 1920), Image.Resampling.LANCZOS)
            # The text-free prompt explicitly reserves a quiet lower area.
            _draw_bold_text(image, kicker=kicker, main=main, highlight=highlight, cover_face=None)
            calls[-1]["path"] = "text_free_bold_overlay"
            conn.execute("UPDATE shorts_cover_ai_calls SET result_path='text_free_bold_overlay' WHERE id=?", [calls[-1]["id"]])
            conn.commit()
            return image, "text_free_bold_overlay", calls
        finally:
            if locked:
                conn.execute("SELECT pg_advisory_unlock(?)", [_ADVISORY_LOCK_ID])
            conn.close()
