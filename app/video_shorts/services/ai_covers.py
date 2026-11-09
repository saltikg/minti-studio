"""Tenant-gated Gemini image covers. Invoked by cover workers, never a route."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import os
import re
import subprocess
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4
from zoneinfo import ZoneInfo

from PIL import Image, ImageOps

from app.video_shorts.services.db import get_db, get_db_readonly, table_columns
from app.video_shorts.services.storage import get_media_storage, put_file_for_owner
from app.video_shorts.services.user_preferences import load_user_bool_preference
from app.video_shorts.services.cover_ai_config import BASE_PROMPT, COVER_LOOKS

log = logging.getLogger(__name__)
PROMPT_VERSION = "cover-ai-v1"
DEFAULT_MODEL = "gemini-3.1-flash-image"
CANDIDATE_ALGORITHM_VERSION = "render-layout-v1"
_AI_LOCK = threading.Lock()
_ADVISORY_LOCK_ID = 843715902
_STYLE_REFERENCE = Path(__file__).resolve().parents[1] / "static" / "img" / "cover-ai-style-v1.svg"


class AiCoverLimitReached(RuntimeError):
    pass


class GeminiApiError(RuntimeError):
    def __init__(self, status: int, body: str):
        self.status = int(status)
        self.body = str(body or "")
        super().__init__(f"Gemini API HTTP {self.status}: {self.body[:500]}")


class GeminiSafetyError(RuntimeError):
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
    if "image_count" not in table_columns(conn, "shorts_cover_ai_calls"):
        if getattr(conn, "backend_name", "") == "postgres":
            conn.execute("ALTER TABLE shorts_cover_ai_calls ADD COLUMN image_count INTEGER NOT NULL DEFAULT 0")
        else:
            conn.execute("ALTER TABLE shorts_cover_ai_calls ADD COLUMN image_count INTEGER")
            conn.execute("UPDATE shorts_cover_ai_calls SET image_count=0 WHERE image_count IS NULL")
    json_type = "JSONB" if getattr(conn, "backend_name", "") == "postgres" else "TEXT"
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS shorts_cover_ai_generations (
          id VARCHAR PRIMARY KEY,
          owner_user_id VARCHAR NOT NULL,
          brand_id VARCHAR NOT NULL,
          generated_video_id BIGINT NOT NULL,
          video_pk BIGINT NOT NULL,
          storage_key TEXT NOT NULL,
          prompt TEXT NOT NULL,
          model VARCHAR NOT NULL,
          selected_frames_json {json_type},
          title_text TEXT,
          title_style VARCHAR,
          tone VARCHAR,
          created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_cover_ai_history "
        "ON shorts_cover_ai_generations(owner_user_id, brand_id, generated_video_id, created_at)"
    )
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS shorts_cover_ai_candidate_cache (
          cache_key VARCHAR PRIMARY KEY,
          owner_user_id VARCHAR NOT NULL,
          brand_id VARCHAR NOT NULL,
          generated_video_id BIGINT NOT NULL,
          source_video_id VARCHAR NOT NULL,
          source_ranges_json {json_type} NOT NULL,
          detector_key VARCHAR NOT NULL,
          title_text TEXT,
          duration_seconds DOUBLE PRECISION NOT NULL,
          candidates_json {json_type} NOT NULL,
          face_cache_hit BOOLEAN NOT NULL DEFAULT FALSE,
          created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_cover_ai_candidate_owner "
        "ON shorts_cover_ai_candidate_cache(owner_user_id, brand_id, generated_video_id)"
    )


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
                SELECT COUNT(*)
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
        body = exc.read().decode("utf-8", errors="replace")
        raise GeminiApiError(exc.code, body) from None
    for candidate in payload.get("candidates") or []:
        for part in (candidate.get("content") or {}).get("parts") or []:
            inline = part.get("inlineData") or {}
            if inline.get("data") and str(inline.get("mimeType") or "").startswith("image/"):
                image_bytes = base64.b64decode(inline["data"], validate=True)
                with Image.open(io.BytesIO(image_bytes)) as image:
                    return image.convert("RGB"), payload.get("usageMetadata") or {}
    safety = payload.get("promptFeedback") or {}
    finish_reasons = [str(candidate.get("finishReason") or "") for candidate in payload.get("candidates") or []]
    if safety.get("blockReason") or any(reason in {"SAFETY", "BLOCKLIST", "PROHIBITED_CONTENT"} for reason in finish_reasons):
        raise GeminiSafetyError(
            f"Gemini safety block: {safety.get('blockReason') or ', '.join(finish_reasons)}"
        )
    raise RuntimeError("Gemini image API returned no image")


def _record_call(conn, *, owner: str, brand: str, generated_id: int, model: str,
                 request_kind: str, result_path: str, image_returned: bool,
                 latency_ms: int, usage: dict, error: str = "", image_count: int = 0) -> dict:
    input_tokens = int(usage.get("promptTokenCount") or 0)
    output_tokens = int(usage.get("candidatesTokenCount") or 0)
    recorded_image_count = int(image_count or (1 if image_returned else 0))
    # API reports tokens, not a dollar amount. Google's listed 4K image rate is
    # $0.151; input tokens add a small, separately priced component.
    cost = round(0.151 + input_tokens * 0.50 / 1_000_000, 6) if image_returned else None
    call_id = str(uuid4())
    conn.execute("""
        INSERT INTO shorts_cover_ai_calls
          (id, owner_user_id, brand_id, generated_video_id, model, prompt_version,
           request_kind, result_path, image_returned, latency_ms, cost_usd,
           cost_estimated, input_tokens, output_tokens, error, image_count, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, TRUE, ?, ?, ?, ?, ?)
    """, [call_id, owner, brand, generated_id, model, PROMPT_VERSION,
          request_kind, result_path, image_returned, latency_ms, cost,
          input_tokens or None, output_tokens or None, error[:500] or None, recorded_image_count,
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


def cover_ai_presets() -> dict[str, Any]:
    return {
        "looks": {
            key: {"label": value["label"], "hint": value["hint"]}
            for key, value in COVER_LOOKS.items()
        },
    }


def assemble_phase2_prompt(
    *,
    people_count: int,
    title: str,
    no_title: bool,
    look: str,
    advanced_override: Optional[str] = None,
) -> str:
    if advanced_override is not None and str(advanced_override).strip():
        return str(advanced_override)
    if people_count not in {1, 2}:
        raise ValueError("Select one or two people.")
    if look not in COVER_LOOKS:
        raise ValueError("Choose a valid look.")
    clean_title = " ".join(str(title or "").split())
    text_free = bool(no_title or look == "no_text")
    if not text_free and not clean_title:
        raise ValueError("Enter a cover title or choose No title.")
    people_instruction = (
        "Feature the one selected person as the clear single subject."
        if people_count == 1
        else "Feature both selected people clearly, side by side or facing each other; give both faces equal importance."
    )
    title_instruction = (
        "Do not include any title or other visible text."
        if text_free
        else f'Render this exact title once: "{clean_title}".'
    )
    return BASE_PROMPT.format(
        people_instruction=people_instruction,
        title_instruction=title_instruction,
        look_instruction=COVER_LOOKS[look]["prompt"],
    ).strip()


def _cover_target_metadata(*, owner: str, brand: str, generated_id: int, video_pk: int) -> dict[str, Any]:
    from app.video_shorts.services.clip_covers import _cover_target

    conn = get_db_readonly()
    try:
        return _cover_target(
            conn, generated_id, owner_user_id=owner, brand_id=brand, video_pk=video_pk,
        )
    finally:
        conn.close()


def _cover_source_target(*, owner: str, brand: str, generated_id: int, video_pk: int) -> tuple[dict, Any]:
    target = _cover_target_metadata(
        owner=owner, brand=brand, generated_id=generated_id, video_pk=video_pk,
    )
    source_temp = get_media_storage().download_to_temp(target["source_storage_key"])
    return target, source_temp


def _candidate_source_path(target: dict[str, Any]) -> tuple[Path, bool]:
    """Resolve the same stable source-cache path used by renders when possible."""
    from app.video_shorts.services.media_utils import resolve_s3_source_video_cached

    storage = get_media_storage()
    if getattr(storage, "backend_name", "local") == "s3":
        cached = resolve_s3_source_video_cached(
            str(target["source_video_id"]), str(target["source_storage_key"]),
            storage=storage, log_missing=False,
        )
        if cached and Path(cached).exists():
            return Path(cached), True
    return storage.download_to_temp(target["source_storage_key"]), False


def _cleanup_candidate_source(path: Path, source_cache_lease: bool) -> None:
    if source_cache_lease:
        from app.video_shorts.services.media_utils import _cleanup_resolved_source_video

        _cleanup_resolved_source_video(path, True)
        return
    try:
        path.cleanup()
    except AttributeError:
        Path(path).unlink(missing_ok=True)


def _clip_duration(ranges: list[tuple[float, float]]) -> float:
    return sum(max(0.0, float(end) - float(start)) for start, end in ranges)


def _source_time_for_offset(ranges: list[tuple[float, float]], offset: float) -> float:
    remaining = max(0.0, float(offset))
    for start, end in ranges:
        duration = max(0.0, float(end) - float(start))
        if remaining <= duration:
            return float(start) + remaining
        remaining -= duration
    return float(ranges[-1][1])


def _offset_for_source_time(ranges: list[tuple[float, float]], timestamp: float) -> float:
    elapsed = 0.0
    for start, end in ranges:
        if float(start) <= timestamp <= float(end):
            return elapsed + timestamp - float(start)
        elapsed += max(0.0, float(end) - float(start))
    return elapsed


def _timestamp_allowed(ranges: list[tuple[float, float]], timestamp: float) -> bool:
    return any(float(start) - 0.01 <= timestamp <= float(end) + 0.01 for start, end in ranges)


def _select_face_candidate_records(ranked: list[dict[str, Any]]) -> list[dict[str, Any]]:
    split_faces = [item for item in ranked if item["person_key"] in {"left", "right"}]
    chosen: list[dict[str, Any]] = []
    if split_faces:
        for person_key, label in (("left", "Person 1"), ("right", "Person 2")):
            options = sorted(
                (item for item in split_faces if item["person_key"] == person_key),
                key=lambda item: item["score"], reverse=True,
            )[:1]
            for option in options:
                option = dict(option)
                option["person_label"] = label
                chosen.append(option)
    else:
        options = sorted(ranked, key=lambda item: item["score"], reverse=True)[:1]
        for option in options:
            option = dict(option)
            option["person_label"] = "Person 1"
            chosen.append(option)
    return sorted(chosen, key=lambda item: (item["person_label"], -item["score"]))[:2]


def _load_render_face_track(target: dict[str, Any]) -> list[dict[str, Any]]:
    """Read an existing render track only; never create or detect one here."""
    if len(target.get("source_ranges") or []) != 1:
        return []
    try:
        from app.video_shorts.routes import generation

        start, end = target["source_ranges"][0]
        path = generation._preview_face_track_smooth_path(
            str(target["source_video_id"]), float(start), float(end),
        )
        if not path.exists():
            return []
        rows = json.loads(path.read_text(encoding="utf-8"))
        return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
    except Exception:
        log.warning(
            "AI cover existing face track unavailable video=%s",
            target.get("source_video_id"), exc_info=True,
        )
        return []


def _candidate_face_from_split_crop(crop: dict[str, Any]) -> dict[str, float]:
    width = max(0.01, float(crop.get("w") or 0.25))
    height = max(0.01, float(crop.get("h") or 0.35))
    return {
        "cx_ratio": float(crop.get("cx") or (float(crop.get("x") or 0.0) + width / 2.0)),
        "cy_ratio": float(crop.get("cy") or (float(crop.get("y") or 0.0) + height / 2.0)),
        "w_ratio": min(0.25, width * 0.34),
        "h_ratio": min(0.32, height * 0.38),
    }


def _candidate_records_from_render_data(
    target: dict[str, Any], track_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], bool]:
    """Choose 2–3 timestamps from saved render layout/track without detection."""
    duration = _clip_duration(target["source_ranges"])
    segments = [
        segment for segment in (target.get("auto_layout_segments") or [])
        if isinstance(segment, dict)
    ]
    split_segments = [
        segment for segment in segments
        if str(segment.get("mode") or "").lower() == "split"
        and isinstance(segment.get("split_crops"), dict)
    ]
    if split_segments:
        segment = max(
            split_segments,
            key=lambda item: float(item.get("end") or 0.0) - float(item.get("start") or 0.0),
        )
        clip_t = max(0.0, min(duration, (
            float(segment.get("start") or 0.0) + float(segment.get("end") or 0.0)
        ) / 2.0))
        source_t = _source_time_for_offset(target["source_ranges"], clip_t)
        chosen = []
        for key, person_key, label in (
            ("top", "left", "Person 1"), ("bottom", "right", "Person 2"),
        ):
            crop = (segment.get("split_crops") or {}).get(key)
            if not isinstance(crop, dict):
                continue
            chosen.append({
                "id": f"{source_t:.3f}:{person_key}",
                "person_key": person_key,
                "person_label": label,
                "source_timestamp": round(source_t, 3),
                "clip_timestamp": round(clip_t, 3),
                "face": _candidate_face_from_split_crop(crop),
                "render_crop": crop,
                "score": 0.0,
                "face_sharpness": 0.0,
                "face_count": 2,
            })
        if chosen:
            return chosen, True

    fill_segments = [
        segment for segment in segments
        if str(segment.get("mode") or "").lower() == "fill"
    ]
    if fill_segments:
        segment = max(
            fill_segments,
            key=lambda item: float(item.get("end") or 0.0) - float(item.get("start") or 0.0),
        )
        start = max(0.0, float(segment.get("start") or 0.0))
        end = min(duration, float(segment.get("end") or duration))
        usable_track = []
        for row in track_rows:
            if not row.get("found"):
                continue
            source_t = float(row.get("t") or 0.0)
            clip_t = _offset_for_source_time(target["source_ranges"], source_t)
            if start <= clip_t <= end:
                usable_track.append((clip_t, source_t, row))
        if usable_track:
            clip_t, source_t, row = max(
                usable_track,
                key=lambda item: float(item[2].get("w_ratio") or 0.0) * float(item[2].get("h_ratio") or 0.0),
            )
            face = {
                key: row.get(key)
                for key in ("cx_ratio", "cy_ratio", "w_ratio", "h_ratio")
            }
        else:
            clip_t = (start + end) / 2.0
            source_t = _source_time_for_offset(target["source_ranges"], clip_t)
            face = None
        return [{
            "id": f"{source_t:.3f}:single",
            "person_key": "single",
            "person_label": "Person 1",
            "source_timestamp": round(source_t, 3),
            "clip_timestamp": round(clip_t, 3),
            "face": face,
            "score": 0.0,
            "face_sharpness": 0.0,
            "face_count": 1,
        }], True

    offsets = [duration * ratio for ratio in (0.25, 0.5, 0.75)]
    return ([{
        "id": f"{_source_time_for_offset(target['source_ranges'], offset):.3f}:frame-{index}",
        "person_key": f"frame_{index}",
        "person_label": f"Frame {index}",
        "source_timestamp": round(_source_time_for_offset(target["source_ranges"], offset), 3),
        "clip_timestamp": round(offset, 3),
        "face": None,
        "score": 0.0,
        "face_sharpness": 0.0,
        "face_count": 0,
    } for index, offset in enumerate(offsets, start=1)], False)


def _candidate_render_key(target: dict[str, Any]) -> str:
    snapshot = {
        "version": CANDIDATE_ALGORITHM_VERSION,
        "layout": target.get("auto_layout_segments") or [],
    }
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode("utf-8")).hexdigest()


def _candidate_cache_key(target: dict[str, Any], detector_key: str, *, owner: str, brand: str) -> str:
    identity = {
        "owner_user_id": str(owner),
        "brand_id": str(brand),
        "source_video_id": str(target["source_video_id"]),
        "source_ranges": [
            [round(float(start), 6), round(float(end), 6)]
            for start, end in target["source_ranges"]
        ],
        "detector_key": str(detector_key),
    }
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode("utf-8")).hexdigest()


def _parse_cached_json(value: Any, fallback: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value or "")
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


def _candidate_urls(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    storage = get_media_storage()
    result = []
    for candidate in candidates:
        item = dict(candidate)
        thumbnail_key = str(item.pop("thumbnail_key", "") or "")
        if thumbnail_key:
            item["thumbnail_url"] = storage.public_url(thumbnail_key)
        result.append(item)
    return result


def _load_candidate_cache(
    *, cache_key: str, owner: str, brand: str,
) -> Optional[dict[str, Any]]:
    conn = get_db()
    try:
        ensure_ai_cover_schema(conn)
        row = conn.execute(
            """
            SELECT title_text, duration_seconds, candidates_json, face_cache_hit
              FROM shorts_cover_ai_candidate_cache
             WHERE cache_key=? AND owner_user_id=? AND brand_id=?
            """,
            [cache_key, owner, brand],
        ).fetchone()
        conn.commit()
    finally:
        conn.close()
    if not row:
        return None
    candidates = _parse_cached_json(row[2], [])
    if not isinstance(candidates, list) or not candidates:
        return None
    return {
        "title": str(row[0] or ""),
        "duration": round(float(row[1] or 0.0), 3),
        "candidates": _candidate_urls(candidates),
        "face_cache_hit": bool(row[3]),
    }


def _save_candidate_cache(
    *, cache_key: str, owner: str, brand: str, generated_id: int,
    target: dict[str, Any], detector_key: str, duration: float,
    candidates: list[dict[str, Any]], face_cache_hit: bool,
) -> None:
    conn = get_db()
    try:
        ensure_ai_cover_schema(conn)
        conn.execute(
            """
            INSERT INTO shorts_cover_ai_candidate_cache
              (cache_key, owner_user_id, brand_id, generated_video_id, source_video_id,
               source_ranges_json, detector_key, title_text, duration_seconds,
               candidates_json, face_cache_hit, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT(cache_key) DO UPDATE SET
              owner_user_id=excluded.owner_user_id,
              brand_id=excluded.brand_id,
              generated_video_id=excluded.generated_video_id,
              source_video_id=excluded.source_video_id,
              source_ranges_json=excluded.source_ranges_json,
              detector_key=excluded.detector_key,
              title_text=excluded.title_text,
              duration_seconds=excluded.duration_seconds,
              candidates_json=excluded.candidates_json,
              face_cache_hit=excluded.face_cache_hit,
              updated_at=CURRENT_TIMESTAMP
            """,
            [
                cache_key, owner, brand, int(generated_id), str(target["source_video_id"]),
                json.dumps(target["source_ranges"]), detector_key, target["title"], float(duration),
                json.dumps(candidates, ensure_ascii=False), bool(face_cache_hit),
            ],
        )
        conn.commit()
    finally:
        conn.close()


def phase2_candidate_frames(
    *, owner: str, brand: str, generated_id: int, video_pk: int,
) -> dict[str, Any]:
    """Return render-derived references without face detection on this path."""
    started = time.monotonic()
    target = _cover_target_metadata(
        owner=owner, brand=brand, generated_id=generated_id, video_pk=video_pk,
    )
    render_key = _candidate_render_key(target)
    cache_key = _candidate_cache_key(target, render_key, owner=owner, brand=brand)
    cached = _load_candidate_cache(cache_key=cache_key, owner=owner, brand=brand)
    if cached:
        elapsed_ms = round((time.monotonic() - started) * 1000)
        cached.update({
            "candidate_cache_hit": True,
            "candidate_cache_key": cache_key,
            "elapsed_ms": elapsed_ms,
        })
        log.info(
            "AI cover candidate cache brand=%s clip=%s hit=true face_cache_hit=%s elapsed_ms=%s candidates=%s",
            brand, generated_id, bool(cached.get("face_cache_hit")), elapsed_ms,
            len(cached.get("candidates") or []),
        )
        return cached

    import cv2

    plan_started = time.monotonic()
    track_rows = _load_render_face_track(target)
    chosen, render_data_used = _candidate_records_from_render_data(target, track_rows)
    plan_ms = round((time.monotonic() - plan_started) * 1000)
    source_started = time.monotonic()
    source_temp, source_cache_lease = _candidate_source_path(target)
    source_ms = round((time.monotonic() - source_started) * 1000)
    cap = None
    try:
        cap = cv2.VideoCapture(str(source_temp))
        extract_started = time.monotonic()
        cache_candidates: list[dict[str, Any]] = []
        with tempfile.TemporaryDirectory(prefix="cover_ai_candidates_") as directory:
            directory_path = Path(directory)
            for index, candidate in enumerate(chosen):
                cap.set(cv2.CAP_PROP_POS_MSEC, float(candidate["source_timestamp"]) * 1000.0)
                ok, raw = cap.read()
                if not ok or raw is None:
                    continue
                image = Image.fromarray(cv2.cvtColor(raw, cv2.COLOR_BGR2RGB))
                thumbnail = _generous_person_crop(image, candidate.get("face"))
                thumbnail.thumbnail((480, 480), Image.Resampling.LANCZOS)
                thumbnail_path = directory_path / f"candidate_{index + 1}.jpg"
                thumbnail.save(thumbnail_path, "JPEG", quality=86, optimize=True)
                thumbnail_key = (
                    f"brands/{brand}/private/short-covers/{generated_id}/candidate-cache/"
                    f"{cache_key}-{index + 1}.jpg"
                )
                put_file_for_owner(thumbnail_path, thumbnail_key, owner_user_id=owner)
                cache_candidates.append({**candidate, "thumbnail_key": thumbnail_key})
        extract_ms = round((time.monotonic() - extract_started) * 1000)
        if not cache_candidates:
            raise RuntimeError("Candidate reference frames could not be decoded.")
        duration = round(_clip_duration(target["source_ranges"]), 3)
        _save_candidate_cache(
            cache_key=cache_key, owner=owner, brand=brand, generated_id=generated_id,
            target=target, detector_key=render_key, duration=duration,
            candidates=cache_candidates, face_cache_hit=render_data_used,
        )
        elapsed_ms = round((time.monotonic() - started) * 1000)
        log.info(
            "AI cover candidate cache brand=%s clip=%s hit=false render_data=%s "
            "elapsed_ms=%s plan_ms=%s source_ms=%s extract_ms=%s candidates=%s",
            brand, generated_id, render_data_used, elapsed_ms, plan_ms, source_ms,
            extract_ms, len(cache_candidates),
        )
        return {
            "title": target["title"],
            "duration": duration,
            "candidates": _candidate_urls(cache_candidates),
            "face_cache_hit": render_data_used,
            "render_data_used": render_data_used,
            "candidate_cache_hit": False,
            "candidate_cache_key": cache_key,
            "elapsed_ms": elapsed_ms,
            "timing": {"plan_ms": plan_ms, "source_ms": source_ms, "extract_ms": extract_ms},
        }
    finally:
        if cap is not None:
            cap.release()
        _cleanup_candidate_source(source_temp, source_cache_lease)


def _generous_person_crop(frame: Image.Image, face: Optional[dict[str, Any]]) -> Image.Image:
    if not face:
        return frame.convert("RGB")
    width, height = frame.size
    cx = float(face.get("cx_ratio") or 0.5) * width
    cy = float(face.get("cy_ratio") or 0.5) * height
    fw = max(1.0, float(face.get("w_ratio") or 0.15) * width)
    fh = max(1.0, float(face.get("h_ratio") or 0.20) * height)
    crop_w = min(float(width), max(fw * 4.0, fh * 2.7))
    crop_h = min(float(height), max(fh * 3.8, crop_w * 1.15))
    left = min(max(0.0, cx - crop_w / 2.0), max(0.0, width - crop_w))
    top = min(max(0.0, cy - crop_h * 0.34), max(0.0, height - crop_h))
    return frame.crop((int(left), int(top), int(left + crop_w), int(top + crop_h))).convert("RGB")


def extract_reference_frame(
    *, owner: str, brand: str, generated_id: int, video_pk: int,
    clip_timestamp: float, face: Optional[dict[str, Any]] = None,
    thumbnail: bool = False,
) -> Image.Image:
    import cv2

    target, source_temp = _cover_source_target(
        owner=owner, brand=brand, generated_id=generated_id, video_pk=video_pk,
    )
    try:
        duration = _clip_duration(target["source_ranges"])
        offset = max(0.0, min(float(clip_timestamp), duration))
        source_timestamp = _source_time_for_offset(target["source_ranges"], offset)
        cap = cv2.VideoCapture(str(source_temp))
        cap.set(cv2.CAP_PROP_POS_MSEC, source_timestamp * 1000.0)
        ok, raw = cap.read()
        cap.release()
        if not ok or raw is None:
            raise RuntimeError("Selected source frame could not be decoded.")
        frame = Image.fromarray(cv2.cvtColor(raw, cv2.COLOR_BGR2RGB))
        image = _generous_person_crop(frame, face)
        if thumbnail:
            image.thumbnail((480, 480), Image.Resampling.LANCZOS)
        return image
    finally:
        try:
            source_temp.cleanup()
        except AttributeError:
            Path(source_temp).unlink(missing_ok=True)


def _phase2_api_request(model: str, prompt: str, references: list[Image.Image]) -> tuple[Image.Image, dict]:
    parts: list[dict[str, Any]] = [{"text": prompt}]
    for reference in references:
        parts.append({
            "inlineData": {
                "mimeType": "image/jpeg",
                "data": base64.b64encode(_frame_jpeg(reference)).decode("ascii"),
            }
        })
    body = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "responseModalities": ["TEXT", "IMAGE"],
            "imageConfig": {"aspectRatio": "9:16", "imageSize": "2K"},
        },
    }
    request = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": os.environ["GEMINI_API_KEY"]},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        raise GeminiApiError(exc.code, exc.read().decode("utf-8", errors="replace")) from None
    except TimeoutError as exc:
        raise RuntimeError("Gemini request timed out.") from exc
    for candidate in payload.get("candidates") or []:
        for part in (candidate.get("content") or {}).get("parts") or []:
            inline = part.get("inlineData") or {}
            if inline.get("data") and str(inline.get("mimeType") or "").startswith("image/"):
                with Image.open(io.BytesIO(base64.b64decode(inline["data"], validate=True))) as image:
                    return image.convert("RGB"), payload.get("usageMetadata") or {}
    safety = payload.get("promptFeedback") or {}
    reasons = [str(candidate.get("finishReason") or "") for candidate in payload.get("candidates") or []]
    if safety.get("blockReason") or any(reason in {"SAFETY", "BLOCKLIST", "PROHIBITED_CONTENT"} for reason in reasons):
        raise GeminiSafetyError(f"Gemini safety block: {safety.get('blockReason') or ', '.join(reasons)}")
    raise RuntimeError("Gemini returned no image.")


def suggest_cover_titles(*, clip_title: str, transcript: str = "") -> list[str]:
    model = os.getenv("GEMINI_TEXT_MODEL", "gemini-2.5-flash").strip() or "gemini-2.5-flash"
    prompt = (
        "Return JSON only: an array of exactly three YouTube cover title options. "
        "Each option must be 3 to 5 words, use the same language as the source, and preserve Turkish characters. "
        f"Clip title: {clip_title}\nTranscript excerpt: {str(transcript or '')[:2000]}"
    )
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.7},
    }
    request = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": os.environ["GEMINI_API_KEY"]},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        raise GeminiApiError(exc.code, exc.read().decode("utf-8", errors="replace")) from None
    text = ""
    for candidate in payload.get("candidates") or []:
        for part in (candidate.get("content") or {}).get("parts") or []:
            if part.get("text"):
                text += str(part["text"])
    try:
        values = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Gemini returned invalid title suggestions.") from exc
    clean = [" ".join(str(value).split()) for value in values if 3 <= len(str(value).split()) <= 5]
    if len(clean) < 3:
        raise RuntimeError("Gemini did not return three valid 3–5 word titles.")
    return clean[:3]


def _history_rows(conn: Any, *, owner: str, brand: str, generated_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, storage_key, prompt, model, selected_frames_json, title_text,
               title_style, tone, created_at
          FROM shorts_cover_ai_generations
         WHERE owner_user_id=? AND brand_id=? AND generated_video_id=?
         ORDER BY created_at DESC, id DESC LIMIT 3
        """,
        [owner, brand, generated_id],
    ).fetchall()
    storage = get_media_storage()
    result = []
    for row in rows:
        frames = row[4] if isinstance(row[4], list) else json.loads(row[4] or "[]")
        result.append({
            "id": row[0], "storage_key": row[1], "url": storage.public_url(row[1]),
            "prompt": row[2], "model": row[3], "selected_frames": frames,
            "title": row[5] or "", "look": row[6] or "", "tone": row[7] or "",
            "created_at": row[8].isoformat() if hasattr(row[8], "isoformat") else str(row[8]),
        })
    return result


def ai_cover_history(*, owner: str, brand: str, generated_id: int) -> list[dict[str, Any]]:
    conn = get_db()
    try:
        ensure_ai_cover_schema(conn)
        conn.commit()
        return _history_rows(conn, owner=owner, brand=brand, generated_id=generated_id)
    finally:
        conn.close()


def generate_phase2_cover(
    *, owner: str, brand: str, generated_id: int, video_pk: int,
    selected_frames: list[dict[str, Any]], title: str, no_title: bool,
    look: str, advanced_prompt: Optional[str] = None,
) -> dict[str, Any]:
    if not os.getenv("GEMINI_API_KEY", "").strip():
        raise RuntimeError("Gemini API key is not configured.")
    if not 1 <= len(selected_frames) <= 2:
        raise ValueError("Select one or two people.")
    prompt = assemble_phase2_prompt(
        people_count=len(selected_frames), title=title, no_title=no_title,
        look=look, advanced_override=advanced_prompt,
    )
    used, allowed = ai_cover_allowance(owner, brand)
    if allowed <= 0 or used >= allowed:
        raise AiCoverLimitReached(f"AI cover allowance used up ({used} / {allowed} this month).")
    references = [
        extract_reference_frame(
            owner=owner, brand=brand, generated_id=generated_id, video_pk=video_pk,
            clip_timestamp=float(item.get("clip_timestamp") or 0.0),
            face=item.get("face") if isinstance(item.get("face"), dict) else None,
        )
        for item in selected_frames
    ]
    model = os.getenv("GEMINI_IMAGE_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    started = time.monotonic()
    conn = get_db()
    try:
        ensure_ai_cover_schema(conn)
        conn.commit()
        try:
            image, usage = _phase2_api_request(model, prompt, references)
        except Exception as exc:
            latency_ms = round((time.monotonic() - started) * 1000)
            _record_call(
                conn, owner=owner, brand=brand, generated_id=generated_id, model=model,
                request_kind="phase2_image", result_path="api_error", image_returned=False,
                latency_ms=latency_ms, usage={}, error=f"{type(exc).__name__}: {exc}", image_count=0,
            )
            log.warning(
                "AI cover call brand=%s clip=%s model=%s latency_ms=%s success=false image_count=0 error=%s",
                brand, generated_id, model, latency_ms, exc,
            )
            raise
        image = ImageOps.fit(image.convert("RGB"), (1080, 1920), method=Image.Resampling.LANCZOS)
        generation_id = str(uuid4())
        key = f"brands/{brand}/private/short-covers/{generated_id}/ai-history/{generation_id}.jpg"
        with tempfile.TemporaryDirectory(prefix="cover_ai_phase2_") as directory:
            image_path = Path(directory) / "cover.jpg"
            image.save(image_path, "JPEG", quality=92, optimize=True, progressive=True)
            put_file_for_owner(image_path, key, owner_user_id=owner)
        latency_ms = round((time.monotonic() - started) * 1000)
        conn.execute(
            """
            INSERT INTO shorts_cover_ai_generations
              (id, owner_user_id, brand_id, generated_video_id, video_pk, storage_key,
               prompt, model, selected_frames_json, title_text, title_style, tone, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [generation_id, owner, brand, generated_id, video_pk, key, prompt, model,
             json.dumps(selected_frames, ensure_ascii=False), "" if no_title else title,
             look, None, datetime.now(timezone.utc).replace(tzinfo=None)],
        )
        _record_call(
            conn, owner=owner, brand=brand, generated_id=generated_id, model=model,
            request_kind="phase2_image", result_path=key, image_returned=True,
            latency_ms=latency_ms, usage=usage, image_count=1,
        )
        old_rows = conn.execute(
            """
            SELECT id, storage_key FROM shorts_cover_ai_generations
             WHERE owner_user_id=? AND brand_id=? AND generated_video_id=?
             ORDER BY created_at DESC, id DESC OFFSET 3
            """,
            [owner, brand, generated_id],
        ).fetchall()
        active_cover_row = conn.execute(
            "SELECT storage_key FROM shorts_clip_covers WHERE generated_video_id=? AND owner_user_id=? AND brand_id=?",
            [generated_id, owner, brand],
        ).fetchone()
        active_cover_key = str((active_cover_row[0] if active_cover_row else "") or "")
        for old_id, _old_key in old_rows:
            conn.execute(
                "DELETE FROM shorts_cover_ai_generations WHERE id=? AND owner_user_id=? AND brand_id=?",
                [old_id, owner, brand],
            )
        conn.commit()
        for _old_id, old_key in old_rows:
            if str(old_key) == active_cover_key:
                continue
            try:
                get_media_storage().delete(old_key)
            except Exception:
                log.warning("Could not prune old AI cover history key=%s", old_key, exc_info=True)
        log.info(
            "AI cover call brand=%s clip=%s model=%s latency_ms=%s success=true image_count=1",
            brand, generated_id, model, latency_ms,
        )
        history = _history_rows(conn, owner=owner, brand=brand, generated_id=generated_id)
        return {"generation": history[0], "history": history, "prompt": prompt, "latency_ms": latency_ms}
    finally:
        conn.close()


def use_ai_generation(
    *, owner: str, brand: str, generated_id: int, video_pk: int, generation_id: str,
) -> dict[str, Any]:
    from app.video_shorts.services.clip_covers import _cover_target, ensure_clip_covers_schema
    from app.video_shorts.services import tenant_media
    from app.video_shorts.services.storage import build_storage_reference

    conn = get_db()
    try:
        ensure_ai_cover_schema(conn)
        ensure_clip_covers_schema(conn)
        target = _cover_target(conn, generated_id, owner_user_id=owner, brand_id=brand, video_pk=video_pk)
        row = conn.execute(
            """
            SELECT storage_key FROM shorts_cover_ai_generations
             WHERE id=? AND owner_user_id=? AND brand_id=? AND generated_video_id=? AND video_pk=?
            """,
            [generation_id, owner, brand, generated_id, video_pk],
        ).fetchone()
        if not row:
            raise ValueError("AI cover generation was not found.")
        key = str(row[0])
        if not get_media_storage().exists(key):
            raise FileNotFoundError("AI cover image is missing.")
        temp = get_media_storage().download_to_temp(key)
        try:
            size = Path(temp).stat().st_size
        finally:
            try:
                temp.cleanup()
            except AttributeError:
                Path(temp).unlink(missing_ok=True)
        conn.execute(
            """
            INSERT INTO shorts_clip_covers
              (id, generated_video_id, owner_user_id, brand_id, video_pk, plan_index,
               clip_filename, storage_key, source, generated_storage_key, generated_source,
               status, size_bytes, generated_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ai', ?, 'ai', 'ready', ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT(generated_video_id) DO UPDATE SET
              storage_key=excluded.storage_key, source='ai', generated_storage_key=excluded.storage_key,
              generated_source='ai', status='ready', error=NULL, size_bytes=excluded.size_bytes,
              generated_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
            """,
            [str(uuid4()), generated_id, owner, brand, video_pk, target["plan_index"],
             target["clip_filename"], key, key, size],
        )
        tenant_media.upsert_asset(
            conn, file_key=f"cover:{generated_id}", file_path=build_storage_reference(key),
            file_type="short_cover", size_bytes=size, user_id=owner, brand_id=brand,
        )
        conn.commit()
        return {"source": "ai", "storage_key": key, "size_bytes": size}
    finally:
        conn.close()
