from __future__ import annotations

import json
import logging
import math
import tempfile
import time
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from PIL import Image, ImageDraw

from app.video_shorts.config import (
    DEFAULT_EDITOR_TITLE_BG_ALPHA,
    DEFAULT_EDITOR_TITLE_BG_COLOR,
    DEFAULT_EDITOR_TITLE_FONT_KEY,
    DEFAULT_EDITOR_TITLE_TEXT_COLOR,
    TITLE_FONTS,
)
from app.video_shorts.services import tenant_media
from app.video_shorts.services.clip_title import generate_clip_cover_copy
from app.video_shorts.services.db import get_db, get_db_readonly
from app.video_shorts.services.storage import build_storage_reference, get_media_storage, put_file_for_owner


log = logging.getLogger(__name__)
COVER_JOB_TYPE = "generate_cover"
COVER_WIDTH = 1080
COVER_HEIGHT = 1920
MAX_COVER_BYTES = 2 * 1024 * 1024


def _json_value(conn: Any, value: Any) -> Any:
    return json.dumps(value, ensure_ascii=False)


def _parse_json(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value or "{}")
    except Exception:
        return {}


def ensure_clip_covers_schema(conn: Any) -> None:
    json_type = "JSONB" if getattr(conn, "backend_name", "") == "postgres" else "TEXT"
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS shorts_clip_covers (
          id VARCHAR PRIMARY KEY,
          generated_video_id BIGINT NOT NULL UNIQUE,
          owner_user_id VARCHAR NOT NULL,
          brand_id VARCHAR NOT NULL,
          video_pk BIGINT NOT NULL,
          plan_index INTEGER NOT NULL,
          clip_filename VARCHAR NOT NULL,
          storage_key TEXT,
          cover_text TEXT,
          highlight_phrase TEXT,
          candidate_frames_json {json_type},
          selected_timestamp DOUBLE,
          used_face_crop BOOLEAN NOT NULL DEFAULT FALSE,
          status VARCHAR NOT NULL DEFAULT 'queued',
          error TEXT,
          generation_ms INTEGER,
          size_bytes BIGINT,
          generated_at TIMESTAMP,
          created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_shorts_clip_covers_tenant ON shorts_clip_covers(owner_user_id, brand_id, video_pk)"
    )


def _cover_target(
    conn: Any,
    generated_video_id: int,
    *,
    owner_user_id: str,
    brand_id: str,
    video_pk: int,
) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT gv.id, gv.source_video_id, gv.clip_filename, gv.output_filename,
               gv.storage_file_key, gv.generated_title, gv.generated_transcript_full,
               gv.raw_plan_entry_json, yv.id
          FROM shorts_generated_videos gv
          JOIN youtube_videos yv
            ON yv.video_id = gv.source_video_id
           AND CAST(yv.owner_user_id AS VARCHAR) = CAST(gv.user_id AS VARCHAR)
           AND yv.brand_id IS NOT DISTINCT FROM gv.brand_id
           AND yv.id = ?
         WHERE gv.id = ?
           AND CAST(gv.user_id AS VARCHAR) = ?
           AND gv.brand_id = ?
           AND yv.deleted_at IS NULL
        """,
        [int(video_pk), int(generated_video_id), str(owner_user_id), str(brand_id)],
    ).fetchall()
    if len(row) != 1:
        raise tenant_media.TenantMediaConflict("Cover target does not belong to this tenant video")
    item = row[0]
    raw = _parse_json(item[7])
    filename = str(item[3] or item[2] or "").strip()
    plan_index = int(raw.get("plan_index") or filename.split("_", 1)[0])
    video_pk = int(item[8])
    tenant_media.assert_clip_write_owner(
        conn, filename, owner_user_id, brand_id, video_pk=video_pk, source_id=str(item[1])
    )
    asset = conn.execute(
        "SELECT file_path FROM shorts_storage_assets WHERE file_key = ? AND CAST(user_id AS VARCHAR) = ? AND brand_id = ?",
        [item[4] or tenant_media.asset_key(filename), str(owner_user_id), str(brand_id)],
    ).fetchone()
    if not asset:
        raise tenant_media.TenantMediaConflict("Rendered clip asset is missing from this tenant")
    location = tenant_media.storage_location(asset[0])
    return {
        "generated_video_id": int(item[0]),
        "source_video_id": str(item[1]),
        "clip_filename": filename,
        "title": str(item[5] or raw.get("title") or "Short").strip(),
        "transcript": str(item[6] or raw.get("transcript_full") or raw.get("excerpt") or "").strip(),
        "plan_index": plan_index,
        "video_pk": video_pk,
        "clip_storage_key": location["storage_key"],
    }


def find_generated_video_id(
    *, owner_user_id: str, brand_id: str, video_pk: int, source_video_id: str, clip_filename: str
) -> Optional[int]:
    conn = get_db_readonly()
    try:
        rows = conn.execute(
            """
            SELECT gv.id
              FROM shorts_generated_videos gv
              JOIN youtube_videos yv ON yv.id = ?
             WHERE gv.source_video_id = ?
               AND CAST(gv.user_id AS VARCHAR) = ?
               AND gv.brand_id = ?
               AND (gv.clip_filename = ? OR gv.output_filename = ?)
               AND yv.video_id = gv.source_video_id
               AND CAST(yv.owner_user_id AS VARCHAR) = CAST(gv.user_id AS VARCHAR)
               AND yv.brand_id IS NOT DISTINCT FROM gv.brand_id
            """,
            [int(video_pk), str(source_video_id), str(owner_user_id), str(brand_id), clip_filename, clip_filename],
        ).fetchall()
        return int(rows[0][0]) if len(rows) == 1 else None
    finally:
        conn.close()


def get_cover_for_generated(
    generated_video_id: int, *, owner_user_id: str, brand_id: str, video_pk: Optional[int] = None
) -> Optional[dict[str, Any]]:
    conn = get_db_readonly()
    try:
        sql = """
            SELECT id, generated_video_id, video_pk, plan_index, clip_filename, storage_key,
                   cover_text, highlight_phrase, candidate_frames_json, selected_timestamp,
                   used_face_crop, status, error, generation_ms, size_bytes, generated_at
              FROM shorts_clip_covers
             WHERE generated_video_id = ? AND owner_user_id = ? AND brand_id = ?
        """
        params: list[Any] = [int(generated_video_id), str(owner_user_id), str(brand_id)]
        if video_pk is not None:
            sql += " AND video_pk = ?"
            params.append(int(video_pk))
        row = conn.execute(sql, params).fetchone()
        if not row:
            return None
        return {
            "id": row[0], "generated_video_id": int(row[1]), "video_pk": int(row[2]),
            "plan_index": int(row[3]), "clip_filename": row[4], "storage_key": row[5],
            "cover_text": row[6] or "", "highlight_phrase": row[7] or "",
            "candidates": _parse_json(row[8]) if row[8] else [],
            "selected_timestamp": row[9], "used_face_crop": bool(row[10]),
            "status": row[11], "error": row[12], "generation_ms": row[13],
            "size_bytes": row[14], "generated_at": row[15],
        }
    finally:
        conn.close()


def list_covers_for_video(*, owner_user_id: str, brand_id: str, video_pk: int) -> dict[int, dict[str, Any]]:
    conn = get_db_readonly()
    try:
        rows = conn.execute(
            """
            SELECT id, generated_video_id, video_pk, plan_index, clip_filename, storage_key,
                   cover_text, highlight_phrase, candidate_frames_json, selected_timestamp,
                   used_face_crop, status, error, generation_ms, size_bytes, generated_at
              FROM shorts_clip_covers
             WHERE owner_user_id = ? AND brand_id = ? AND video_pk = ?
            """, [str(owner_user_id), str(brand_id), int(video_pk)]
        ).fetchall()
    except Exception:
        return {}
    finally:
        conn.close()
    result: dict[int, dict[str, Any]] = {}
    for row in rows:
        cover = {
            "id": row[0], "generated_video_id": int(row[1]), "video_pk": int(row[2]),
            "plan_index": int(row[3]), "clip_filename": row[4], "storage_key": row[5],
            "cover_text": row[6] or "", "highlight_phrase": row[7] or "",
            "candidates": _parse_json(row[8]) if row[8] else [],
            "selected_timestamp": row[9], "used_face_crop": bool(row[10]),
            "status": row[11], "error": row[12], "generation_ms": row[13],
            "size_bytes": row[14], "generated_at": row[15],
        }
        result[int(cover["plan_index"])] = cover
    return result


def _hex(value: Any, fallback: str) -> str:
    raw = str(value or "").strip()
    return raw if len(raw) == 7 and raw.startswith("#") else fallback


def _frame_at(cap: Any, cv2: Any, timestamp: float) -> Any:
    cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, timestamp) * 1000.0)
    ok, frame = cap.read()
    return frame if ok else None


def _candidate_frames(clip_path: Path) -> tuple[list[dict[str, Any]], float]:
    import cv2
    from app.video_shorts.routes import generation

    cap = cv2.VideoCapture(str(clip_path))
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 30.0)
    frame_count = float(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0)
    duration = frame_count / fps if fps > 0 else 0.0
    start = 0.5
    end = max(start, duration - 0.5)
    samples = [start + (end - start) * i / 19.0 for i in range(20)] if end > start else [duration / 2.0]
    scored: list[dict[str, Any]] = []
    detector = None
    for ts in samples:
        frame = _frame_at(cap, cv2, ts)
        if frame is None:
            continue
        h, w = frame.shape[:2]
        try:
            if detector is None:
                detector = cv2.FaceDetectorYN_create(
                    str(generation._YUNET_FACE_MODEL_PATH), "", (int(w), int(h)),
                    float(generation._YUNET_FACE_SCORE_THRESHOLD), 0.3, 5000,
                )
            faces = generation._yunet_face_detection_ratios(cv2, frame, w, h, detector=detector)
        except Exception:
            faces = []
        face = max(faces, key=lambda item: float(item["w"]) * float(item["h"]), default=None)
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        eyes_open = False
        mouth_closed = True
        if face:
            x, y, fw, fh = (int(face[k]) for k in ("x", "y", "w", "h"))
            roi = gray[max(0, y):min(h, y + fh), max(0, x):min(w, x + fw)]
            try:
                eye_detector = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_eye_tree_eyeglasses.xml")
                smile_detector = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_smile.xml")
                eyes_open = len(eye_detector.detectMultiScale(roi[: max(1, int(len(roi) * 0.65))], 1.1, 4)) >= 1
                mouth_closed = len(smile_detector.detectMultiScale(roi[max(0, int(len(roi) * 0.45)):], 1.4, 20)) == 0
            except Exception:
                pass
        face_area = (float(face["w"]) * float(face["h"]) / float(max(1, w * h))) if face else 0.0
        score = (face_area * 10000.0) + min(sharpness, 1200.0) / 12.0
        score += 35.0 if eyes_open else 0.0
        score += 15.0 if mouth_closed else -15.0
        scored.append({
            "timestamp": round(float(ts), 3), "score": round(score, 3),
            "sharpness": round(sharpness, 2), "eyes_open": eyes_open,
            "mouth_mostly_closed": mouth_closed, "face": dict(face) if face else None,
        })
    cap.release()
    faces = [item for item in scored if item.get("face")]
    ranked = sorted(faces or scored, key=lambda item: float(item["score"]), reverse=True)[:5]
    if not ranked:
        ranked = [{"timestamp": round(max(0.0, duration / 2.0), 3), "score": 0, "face": None}]
    return ranked, duration


def _crop_frame(frame: Any, face: Optional[dict[str, Any]]) -> tuple[Image.Image, bool]:
    import cv2
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    image = Image.fromarray(rgb)
    width, height = image.size
    if not face:
        return image.resize((COVER_WIDTH, COVER_HEIGHT), Image.Resampling.LANCZOS), False
    fx, fy, fw, fh = (float(face[k]) for k in ("x", "y", "w", "h"))
    crop_w = min(float(width), max(fw * 2.8, float(height) * 9.0 / 16.0 * 0.52))
    crop_h = min(float(height), crop_w * 16.0 / 9.0)
    if crop_h >= height:
        crop_h = float(height)
        crop_w = crop_h * 9.0 / 16.0
    cx = fx + fw / 2.0
    # Keep the face center around 32% of the cover so the full face is in the upper 55%.
    left = min(max(0.0, cx - crop_w / 2.0), max(0.0, width - crop_w))
    top = min(max(0.0, fy + fh / 2.0 - crop_h * 0.32), max(0.0, height - crop_h))
    box = (int(left), int(top), int(left + crop_w), int(top + crop_h))
    return image.crop(box).resize((COVER_WIDTH, COVER_HEIGHT), Image.Resampling.LANCZOS), True


def _wrap_cover_text(text: str, font: Any, draw: ImageDraw.ImageDraw, max_width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current: list[str] = []
    for word in words:
        candidate = " ".join(current + [word])
        if current and draw.textbbox((0, 0), candidate, font=font)[2] > max_width:
            lines.append(" ".join(current))
            current = [word]
        else:
            current.append(word)
    if current:
        lines.append(" ".join(current))
    if len(lines) == 1 and len(words) >= 2:
        midpoint = int(math.ceil(len(words) / 2.0))
        lines = [" ".join(words[:midpoint]), " ".join(words[midpoint:])]
    if len(lines) > 3:
        chunk = int(math.ceil(len(words) / 3.0))
        lines = [" ".join(words[index:index + chunk]) for index in range(0, len(words), chunk)]
    return [line for line in lines[:3] if line]


def _draw_cover_text(image: Image.Image, *, text: str, highlight: str, design: dict[str, Any]) -> None:
    from app.video_shorts.services.compositor import _load_title_font, _title_font_path_for_text

    font_key = str(design.get("font") or DEFAULT_EDITOR_TITLE_FONT_KEY)
    font_cfg = TITLE_FONTS.get(font_key) or TITLE_FONTS[DEFAULT_EDITOR_TITLE_FONT_KEY]
    font_path = _title_font_path_for_text(text, str(font_cfg["path"]))
    saved_size = int(design.get("title_font_size") or 40)
    font_size = max(78, min(150, int(saved_size * 2.25)))
    font = _load_title_font(font_path, font_size)
    if font is None:
        raise RuntimeError("Brand title font could not be loaded")
    draw = ImageDraw.Draw(image, "RGBA")
    lines = _wrap_cover_text(text, font, draw, 920)
    line_height = int(font_size * 1.15)
    block_h = line_height * len(lines)
    y = max(1040, min(1580 - block_h, int(COVER_HEIGHT * 0.63)))
    text_color = _hex(design.get("title_text_color"), DEFAULT_EDITOR_TITLE_TEXT_COLOR)
    highlight_color = _hex(
        design.get("subtitle_highlight_color") or design.get("highlight_color"), "#FFD83D"
    )
    bg_color = _hex(design.get("title_bg_color"), DEFAULT_EDITOR_TITLE_BG_COLOR)
    bg_alpha = int(max(0, min(100, float(design.get("title_bg_alpha", DEFAULT_EDITOR_TITLE_BG_ALPHA)))))
    for line in lines:
        line_w = draw.textbbox((0, 0), line, font=font)[2]
        x = int((COVER_WIDTH - line_w) / 2)
        bbox = (x - 30, y - 15, x + line_w + 30, y + line_height - 4)
        if bg_alpha:
            rgb = tuple(int(bg_color[i:i + 2], 16) for i in (1, 3, 5))
            draw.rounded_rectangle(bbox, radius=20, fill=(*rgb, int(255 * bg_alpha / 100)))
        # Word-by-word drawing preserves the compositor font while emphasizing the requested phrase.
        cursor = x
        highlight_cf = highlight.casefold()
        for word in line.split():
            token = word + " "
            clean = word.strip(".,!?;:'\"()[]{}").casefold()
            is_highlight = bool(clean and clean in highlight_cf)
            color = highlight_color if is_highlight else text_color
            draw.text((cursor, y), word, font=font, fill=color, stroke_width=2, stroke_fill="#101010")
            cursor += draw.textbbox((0, 0), token, font=font)[2]
        y += line_height


def _save_jpeg_under_limit(image: Image.Image, path: Path) -> int:
    for quality in (92, 88, 84, 80, 76, 72):
        image.convert("RGB").save(path, "JPEG", quality=quality, optimize=True, progressive=True)
        size = path.stat().st_size
        if size < MAX_COVER_BYTES:
            return size
    raise RuntimeError("Cover image could not be compressed below 2 MB")


def generate_cover(payload: dict[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    generated_id = int(payload["generated_video_id"])
    owner = str(payload.get("owner_user_id") or "").strip()
    brand = str(payload.get("brand_id") or "").strip()
    if not owner or not brand:
        raise tenant_media.TenantMediaConflict("Cover job has no complete tenant scope")
    conn = get_db()
    try:
        requested_video_pk = int(payload.get("video_pk") or 0)
        if not requested_video_pk:
            raise tenant_media.TenantMediaConflict("Cover job has no video identity")
        target = _cover_target(
            conn, generated_id, owner_user_id=owner, brand_id=brand, video_pk=requested_video_pk,
        )
        existing = conn.execute(
            "SELECT id, cover_text, highlight_phrase, candidate_frames_json FROM shorts_clip_covers WHERE generated_video_id = ?",
            [generated_id],
        ).fetchone()
        cover_id = str(existing[0]) if existing else str(uuid4())
        conn.execute(
            """
            INSERT INTO shorts_clip_covers
              (id, generated_video_id, owner_user_id, brand_id, video_pk, plan_index, clip_filename, status, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'processing', CURRENT_TIMESTAMP)
            ON CONFLICT(generated_video_id) DO UPDATE SET status='processing', error=NULL, updated_at=CURRENT_TIMESTAMP
            """,
            [cover_id, generated_id, owner, brand, target["video_pk"], target["plan_index"], target["clip_filename"]],
        )
        conn.commit()
        conn.close()
    except Exception:
        conn.close()
        raise

    storage = get_media_storage()
    clip_temp = storage.download_to_temp(target["clip_storage_key"])
    try:
        existing_candidates = _parse_json(existing[3]) if existing and existing[3] else []
        is_cover_edit = bool(
            existing_candidates
            and (
                payload.get("cover_text")
                or payload.get("highlight_phrase")
                or payload.get("selected_timestamp") is not None
            )
        )
        if is_cover_edit:
            candidates = existing_candidates
            import cv2
            probe = cv2.VideoCapture(str(clip_temp))
            probe_fps = float(probe.get(cv2.CAP_PROP_FPS) or 30.0)
            duration = float(probe.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0) / probe_fps
            probe.release()
        else:
            candidates, duration = _candidate_frames(Path(clip_temp))
        selected_ts = float(payload.get("selected_timestamp") if payload.get("selected_timestamp") is not None else candidates[0]["timestamp"])
        if selected_ts < 0.5 or (duration > 1.0 and selected_ts > duration - 0.5):
            selected_ts = max(0.5, min(selected_ts, max(0.5, duration - 0.5)))

        import cv2
        cap = cv2.VideoCapture(str(clip_temp))
        frame = _frame_at(cap, cv2, selected_ts)
        cap.release()
        if frame is None:
            raise RuntimeError("Selected cover frame could not be decoded")
        chosen = min(candidates, key=lambda item: abs(float(item["timestamp"]) - selected_ts))
        face = chosen.get("face") if abs(float(chosen["timestamp"]) - selected_ts) < 0.08 else None
        image, used_face_crop = _crop_frame(frame, face)

        existing_text = str((existing[1] if existing else "") or "").strip()
        existing_highlight = str((existing[2] if existing else "") or "").strip()
        cover_text = " ".join(str(payload.get("cover_text") or existing_text).strip().split())
        highlight = " ".join(str(payload.get("highlight_phrase") or existing_highlight).strip().split())
        if not cover_text:
            copy = generate_clip_cover_copy(target["transcript"], clip_title=target["title"])
            cover_text = copy["cover_text"]
            highlight = copy["highlight_phrase"]
        if len(cover_text.split()) > 6:
            raise ValueError("Cover text must be at most 6 words")
        if not highlight or highlight.casefold() not in cover_text.casefold():
            highlight = " ".join(cover_text.split()[:2])

        from app.video_shorts.routes import generation
        design = generation._load_short_editor_defaults_json(owner, brand)
        _draw_cover_text(image, text=cover_text, highlight=highlight, design=design)

        with tempfile.TemporaryDirectory(prefix="short_cover_") as temp_dir:
            temp_root = Path(temp_dir)
            cover_path = temp_root / "cover.jpg"
            size = _save_jpeg_under_limit(image, cover_path)
            prefix = f"brands/{brand}/private/short-covers/{generated_id}"
            cover_key = f"{prefix}/cover.jpg"
            put_file_for_owner(cover_path, cover_key, owner_user_id=owner)

            # Candidate previews are tenant-private assets; timestamps remain the source of truth.
            cap = cv2.VideoCapture(str(clip_temp))
            stored_candidates = []
            for candidate in candidates:
                candidate_frame = _frame_at(cap, cv2, float(candidate["timestamp"]))
                if candidate_frame is None:
                    continue
                preview_image, _ = _crop_frame(candidate_frame, candidate.get("face"))
                preview_image.thumbnail((270, 480), Image.Resampling.LANCZOS)
                ms = int(round(float(candidate["timestamp"]) * 1000))
                candidate_path = temp_root / f"candidate-{ms}.jpg"
                preview_image.convert("RGB").save(candidate_path, "JPEG", quality=82, optimize=True)
                candidate_key = f"{prefix}/candidates/{ms}.jpg"
                put_file_for_owner(candidate_path, candidate_key, owner_user_id=owner)
                stored_candidates.append({**candidate, "storage_key": candidate_key})
            cap.release()

        elapsed_ms = int(round((time.monotonic() - started) * 1000))
        conn = get_db()
        try:
            tenant_media.upsert_asset(
                conn, file_key=f"cover:{generated_id}", file_path=build_storage_reference(cover_key),
                file_type="short_cover", size_bytes=size, user_id=owner, brand_id=brand,
            )
            conn.execute(
                """
                UPDATE shorts_clip_covers
                   SET storage_key=?, cover_text=?, highlight_phrase=?, candidate_frames_json=?,
                       selected_timestamp=?, used_face_crop=?, status='ready', error=NULL,
                       generation_ms=?, size_bytes=?, generated_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
                 WHERE generated_video_id=? AND owner_user_id=? AND brand_id=?
                """,
                [cover_key, cover_text, highlight, _json_value(conn, stored_candidates), selected_ts,
                 used_face_crop, elapsed_ms, size, generated_id, owner, brand],
            )
            conn.commit()
        finally:
            conn.close()
        return {
            "generated_video_id": generated_id, "storage_key": cover_key,
            "cover_text": cover_text, "highlight_phrase": highlight,
            "selected_timestamp": selected_ts, "candidate_count": len(stored_candidates),
            "used_face_crop": used_face_crop, "generation_ms": elapsed_ms, "size_bytes": size,
        }
    except Exception as exc:
        conn = get_db()
        try:
            conn.execute(
                "UPDATE shorts_clip_covers SET status='failed', error=?, updated_at=CURRENT_TIMESTAMP WHERE generated_video_id=? AND owner_user_id=? AND brand_id=?",
                [str(exc)[:1000], generated_id, owner, brand],
            )
            conn.commit()
        finally:
            conn.close()
        raise
    finally:
        try:
            clip_temp.cleanup()  # ManagedTempPath
        except AttributeError:
            Path(clip_temp).unlink(missing_ok=True)


def signed_cover_payload(cover: dict[str, Any]) -> dict[str, Any]:
    storage = get_media_storage()
    result = dict(cover)
    result["cover_url"] = storage.public_url(cover["storage_key"]) if cover.get("storage_key") else ""
    candidates = []
    for candidate in cover.get("candidates") or []:
        item = dict(candidate)
        item["url"] = storage.public_url(item["storage_key"]) if item.get("storage_key") else ""
        candidates.append(item)
    result["candidates"] = candidates
    return result
