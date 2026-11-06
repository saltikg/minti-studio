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
MIN_COVER_FONT_SIZE = 110
MAX_COVER_FONT_SIZE = 174
COVER_TEXT_MAX_WIDTH = int(COVER_WIDTH * 0.86)
FACE_SHARPNESS_MIN = 45.0
FACE_CONFIDENCE_MIN = 0.86
LIGHT_BOX_HIGHLIGHT_FALLBACK = "#E11D48"
DARK_BOX_HIGHLIGHT_FALLBACK = "#FFD400"


class CoverCopyDoesNotFit(ValueError):
    pass


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
    clip_location = tenant_media.storage_location(asset[0])
    locations = tenant_media.source_locations(str(item[1]), video_pk=video_pk, conn=conn)
    if len(locations) != 1:
        raise tenant_media.TenantMediaConflict("Cover source video is missing or ambiguous")
    edit_ranges = raw.get("edit_keep_ranges") if isinstance(raw.get("edit_keep_ranges"), list) else []
    source_ranges = []
    for value in edit_ranges:
        try:
            range_start, range_end = float(value["start"]), float(value["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if range_end > range_start:
            source_ranges.append((range_start, range_end))
    if not source_ranges:
        try:
            range_start, range_end = float(raw["start"]), float(raw["end"])
        except (KeyError, TypeError, ValueError):
            raise tenant_media.TenantMediaConflict("Cover target has no source time range")
        if range_end <= range_start:
            raise tenant_media.TenantMediaConflict("Cover target source time range is invalid")
        source_ranges = [(range_start, range_end)]
    return {
        "generated_video_id": int(item[0]),
        "source_video_id": str(item[1]),
        "clip_filename": filename,
        "title": str(item[5] or raw.get("title") or "Short").strip(),
        "transcript": str(item[6] or raw.get("transcript_full") or raw.get("excerpt") or "").strip(),
        "plan_index": plan_index,
        "video_pk": video_pk,
        "source_storage_key": locations[0]["storage_key"],
        "clip_storage_key": clip_location["storage_key"],
        "clip_local_path": clip_location["local_path"],
        "source_ranges": source_ranges,
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
    return raw.upper() if len(raw) == 7 and raw.startswith("#") else fallback.upper()


def _rgb(value: str) -> tuple[int, int, int]:
    clean = _hex(value, "#000000")
    return tuple(int(clean[index:index + 2], 16) for index in (1, 3, 5))


def _relative_luminance(value: str) -> float:
    channels = []
    for channel in _rgb(value):
        normalized = channel / 255.0
        channels.append(normalized / 12.92 if normalized <= 0.04045 else ((normalized + 0.055) / 1.055) ** 2.4)
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def _contrast_ratio(first: str, second: str) -> float:
    light, dark = sorted((_relative_luminance(first), _relative_luminance(second)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


def _resolve_cover_colors(design: dict[str, Any]) -> tuple[str, str, str]:
    """Return readable base, highlight, and box colors without outline text."""
    box = _hex(design.get("title_bg_color"), DEFAULT_EDITOR_TITLE_BG_COLOR)
    base = _hex(design.get("title_text_color"), DEFAULT_EDITOR_TITLE_TEXT_COLOR)
    is_light_box = _relative_luminance(box) >= 0.55
    if _contrast_ratio(base, box) < 4.5:
        base = "#1F2937" if is_light_box else "#FFFFFF"

    accent = _hex(
        design.get("subtitle_highlight_color")
        or design.get("highlight_color")
        or design.get("accent_color"),
        DARK_BOX_HIGHLIGHT_FALLBACK if not is_light_box else LIGHT_BOX_HIGHLIGHT_FALLBACK,
    )
    accent_is_usable = accent != base and _contrast_ratio(accent, box) >= 4.5
    if not accent_is_usable:
        accent = LIGHT_BOX_HIGHLIGHT_FALLBACK if is_light_box else DARK_BOX_HIGHLIGHT_FALLBACK
    if accent == base:
        alternate = "#1F2937" if is_light_box else "#FFFFFF"
        accent = alternate if alternate != base else LIGHT_BOX_HIGHLIGHT_FALLBACK
    return base, accent, box


def _frame_at(cap: Any, cv2: Any, timestamp: float) -> Any:
    cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, timestamp) * 1000.0)
    ok, frame = cap.read()
    return frame if ok else None


def _source_time_for_clip_offset(ranges: list[tuple[float, float]], offset: float) -> float:
    remaining = max(0.0, float(offset))
    for start, end in ranges:
        duration = max(0.0, end - start)
        if remaining <= duration:
            return start + remaining
        remaining -= duration
    return ranges[-1][1]


def _yunet_faces(detector: Any, image: Any) -> list[dict[str, Any]]:
    height, width = image.shape[:2]
    detector.setInputSize((int(width), int(height)))
    _, detections = detector.detect(image)
    if detections is None:
        return []
    faces: list[dict[str, Any]] = []
    landmark_names = ("right_eye", "left_eye", "nose", "right_mouth", "left_mouth")
    for detection in detections:
        if len(detection) < 15:
            continue
        confidence = float(detection[-1])
        if confidence < 0.60:
            continue
        face = {
            "x": max(0, int(round(float(detection[0])))),
            "y": max(0, int(round(float(detection[1])))),
            "w": max(1, int(round(float(detection[2])))),
            "h": max(1, int(round(float(detection[3])))),
            "confidence": round(confidence, 4),
            "landmarks": {},
        }
        for index, name in enumerate(landmark_names):
            face["landmarks"][name] = {
                "x": round(float(detection[4 + index * 2]), 2),
                "y": round(float(detection[5 + index * 2]), 2),
            }
        if face["x"] < width and face["y"] < height:
            face["w"] = min(face["w"], width - face["x"])
            face["h"] = min(face["h"], height - face["y"])
            faces.append(face)
    return faces


def _landmark_geometry_is_visible(face: dict[str, Any]) -> bool:
    landmarks = face.get("landmarks") or {}
    if not all(name in landmarks for name in ("right_eye", "left_eye", "nose", "right_mouth", "left_mouth")):
        return False
    x, y, width, height = (float(face[key]) for key in ("x", "y", "w", "h"))
    if width <= 0 or height <= 0:
        return False
    points = list(landmarks.values())
    if any(not (x - 0.05 * width <= float(point["x"]) <= x + 1.05 * width
                   and y - 0.05 * height <= float(point["y"]) <= y + 1.05 * height) for point in points):
        return False
    right_eye, left_eye = landmarks["right_eye"], landmarks["left_eye"]
    nose = landmarks["nose"]
    right_mouth, left_mouth = landmarks["right_mouth"], landmarks["left_mouth"]
    eye_distance = abs(float(left_eye["x"]) - float(right_eye["x"])) / width
    eye_level_delta = abs(float(left_eye["y"]) - float(right_eye["y"])) / height
    eye_y = (float(left_eye["y"]) + float(right_eye["y"])) / 2.0
    mouth_y = (float(left_mouth["y"]) + float(right_mouth["y"])) / 2.0
    mouth_width = abs(float(left_mouth["x"]) - float(right_mouth["x"])) / width
    return (
        0.18 <= eye_distance <= 0.72
        and eye_level_delta <= 0.20
        and eye_y + 0.06 * height <= float(nose["y"]) <= eye_y + 0.52 * height
        and float(nose["y"]) + 0.04 * height <= mouth_y <= y + 0.95 * height
        and 0.12 <= mouth_width <= 0.72
    )


def _scale_face(face: dict[str, Any], scale: float) -> dict[str, Any]:
    if scale == 1.0:
        return dict(face)
    result = dict(face)
    for key in ("x", "y", "w", "h"):
        result[key] = int(round(float(face[key]) / scale))
    result["landmarks"] = {
        name: {"x": round(float(point["x"]) / scale, 2), "y": round(float(point["y"]) / scale, 2)}
        for name, point in (face.get("landmarks") or {}).items()
    }
    return result


def _mouth_state(gray: Any, face: dict[str, Any], smile_detector: Any) -> tuple[str, float]:
    x, y, width, height = (int(face[key]) for key in ("x", "y", "w", "h"))
    landmarks = face.get("landmarks") or {}
    right_mouth, left_mouth = landmarks.get("right_mouth"), landmarks.get("left_mouth")
    if not right_mouth or not left_mouth:
        return "neutral", 0.0
    mouth_center_x = (float(right_mouth["x"]) + float(left_mouth["x"])) / 2.0
    mouth_center_y = (float(right_mouth["y"]) + float(left_mouth["y"])) / 2.0
    mouth_width = max(abs(float(right_mouth["x"]) - float(left_mouth["x"])) * 1.35, width * 0.22)
    mouth_height = max(height * 0.17, 8.0)
    x0 = max(x, int(round(mouth_center_x - mouth_width / 2.0)))
    x1 = min(x + width, int(round(mouth_center_x + mouth_width / 2.0)))
    y0 = max(y, int(round(mouth_center_y - mouth_height * 0.35)))
    y1 = min(y + height, int(round(mouth_center_y + mouth_height * 0.65)))
    mouth_roi = gray[y0:y1, x0:x1]
    face_roi = gray[y:y + height, x:x + width]
    if mouth_roi.size == 0 or face_roi.size == 0:
        return "neutral", 0.0
    dark_threshold = max(20.0, float(face_roi.mean()) - 42.0)
    dark_ratio = float((mouth_roi < dark_threshold).mean())
    lower_face = face_roi[int(height * 0.45):]
    smiles = smile_detector.detectMultiScale(lower_face, scaleFactor=1.35, minNeighbors=22) if lower_face.size else []
    if dark_ratio >= 0.24:
        return "open", dark_ratio
    return ("slight_smile" if len(smiles) else "neutral"), dark_ratio


def _candidate_frames(
    source_path: Path,
    source_ranges: list[tuple[float, float]],
) -> tuple[list[dict[str, Any]], float]:
    import cv2
    from app.video_shorts.routes import generation

    cap = cv2.VideoCapture(str(source_path))
    duration = sum(max(0.0, end - start) for start, end in source_ranges)
    start = 0.5
    end = max(start, duration - 0.5)
    # Sample enough of the clip that hard eye/blur/occlusion filters can still
    # yield five genuinely usable choices instead of promoting rejected frames.
    clip_offsets = [start + (end - start) * i / 47.0 for i in range(48)] if end > start else [duration / 2.0]
    samples = [(_source_time_for_clip_offset(source_ranges, value), value) for value in clip_offsets]
    scored: list[dict[str, Any]] = []
    detector = None
    eye_detector = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_eye_tree_eyeglasses.xml")
    smile_detector = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_smile.xml")
    for ts, clip_ts in samples:
        frame = _frame_at(cap, cv2, ts)
        if frame is None:
            continue
        h, w = frame.shape[:2]
        analysis_scale = min(1.0, 540.0 / float(max(1, w)))
        analysis = frame
        if analysis_scale < 1.0:
            analysis = cv2.resize(
                frame,
                (max(1, int(round(w * analysis_scale))), max(1, int(round(h * analysis_scale)))),
                interpolation=cv2.INTER_AREA,
            )
        analysis_h, analysis_w = analysis.shape[:2]
        try:
            if detector is None:
                detector = cv2.FaceDetectorYN_create(
                    str(generation._YUNET_FACE_MODEL_PATH), "", (int(analysis_w), int(analysis_h)),
                    0.60, 0.3, 5000,
                )
            faces = _yunet_faces(detector, analysis)
        except Exception:
            faces = []
        face = max(faces, key=lambda item: float(item["w"]) * float(item["h"]), default=None)
        gray = cv2.cvtColor(analysis, cv2.COLOR_BGR2GRAY)
        frame_sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        eyes_open = False
        eye_count = 0
        face_sharpness = 0.0
        mouth_state = "unknown"
        mouth_dark_ratio = 0.0
        face_occluded = False
        rejection_reasons: list[str] = []
        if face:
            x, y, fw, fh = (int(face[k]) for k in ("x", "y", "w", "h"))
            roi = gray[max(0, y):min(analysis_h, y + fh), max(0, x):min(analysis_w, x + fw)]
            face_sharpness = float(cv2.Laplacian(roi, cv2.CV_64F).var()) if roi.size else 0.0
            try:
                upper_face = roi[: max(1, int(len(roi) * 0.62))]
                eyes = eye_detector.detectMultiScale(upper_face, scaleFactor=1.08, minNeighbors=4)
                eye_count = len(eyes)
                # YuNet supplies stable eye landmarks; Haar supplies the open-eye
                # evidence. At least one detected open eye rejects fully closed-eye
                # frames without falsely discarding three-quarter/profile poses.
                eyes_open = eye_count >= 1
                mouth_state, mouth_dark_ratio = _mouth_state(gray, face, smile_detector)
            except Exception:
                pass
            face_occluded = (
                float(face.get("confidence") or 0.0) < FACE_CONFIDENCE_MIN
                or not _landmark_geometry_is_visible(face)
            )
            if not eyes_open:
                rejection_reasons.append("eyes_closed_or_not_visible")
            if face_sharpness < FACE_SHARPNESS_MIN:
                rejection_reasons.append("face_motion_blur")
            if face_occluded:
                rejection_reasons.append("face_occluded")
        else:
            rejection_reasons.append("no_face")
        stored_face = _scale_face(face, analysis_scale) if face else None
        face_area = (
            float(stored_face["w"]) * float(stored_face["h"]) / float(max(1, w * h))
            if stored_face else 0.0
        )
        quality_score = (face_area * 10000.0) + min(face_sharpness, 1200.0) / 8.0
        quality_score += float((face or {}).get("confidence") or 0.0) * 40.0
        quality_score += 30.0 if eyes_open else -30.0
        quality_score += 40.0 if mouth_state == "slight_smile" else (30.0 if mouth_state == "neutral" else -120.0)
        eligible = bool(face and not rejection_reasons)
        score = quality_score if eligible else quality_score - 10000.0
        scored.append({
            "timestamp": round(float(ts), 3), "source_timestamp": round(float(ts), 3),
            "clip_timestamp": round(float(clip_ts), 3), "score": round(score, 3),
            "frame_sharpness": round(frame_sharpness, 2), "face_sharpness": round(face_sharpness, 2),
            "eyes_open": eyes_open, "eye_count": eye_count, "mouth_state": mouth_state,
            "mouth_dark_ratio": round(mouth_dark_ratio, 4), "face_occluded": face_occluded,
            "eligible": eligible, "rejection_reasons": rejection_reasons, "face": stored_face,
        })
    cap.release()
    faces = [item for item in scored if item.get("face")]
    eligible = [item for item in faces if item.get("eligible")]
    if eligible:
        ranked = sorted(eligible, key=lambda item: float(item["score"]), reverse=True)[:5]
        if len(ranked) < 5:
            log.info(
                "Cover quality filters yielded fewer than five usable frames source=%s usable=%s",
                source_path.name, len(ranked),
            )
    elif faces:
        fallback = max(faces, key=lambda item: float(item.get("face_sharpness") or 0.0))
        fallback["selection_fallback"] = "all_face_candidates_rejected; selected sharpest face"
        reason_counts: dict[str, int] = {}
        for item in faces:
            for reason in item.get("rejection_reasons") or []:
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
        log.warning(
            "All cover face candidates failed quality filters source=%s reasons=%s selected=%.3f",
            source_path.name, reason_counts, float(fallback["timestamp"]),
        )
        rest = sorted(
            (item for item in faces if item is not fallback),
            key=lambda item: float(item.get("face_sharpness") or 0.0), reverse=True,
        )
        ranked = [fallback, *rest][:5]
    else:
        middle = _source_time_for_clip_offset(source_ranges, duration / 2.0)
        ranked = [{"timestamp": round(middle, 3), "source_timestamp": round(middle, 3),
                   "clip_timestamp": round(duration / 2.0, 3), "score": 0, "face": None,
                   "eligible": False, "rejection_reasons": ["no_face"],
                   "selection_fallback": "no_face_detected; selected middle frame"}]
        log.warning("No face detected for cover source=%s; selected middle frame", source_path.name)
    return ranked, duration


def _crop_frame(
    frame: Any,
    face: Optional[dict[str, Any]],
) -> tuple[Image.Image, bool, Optional[dict[str, float]]]:
    import cv2
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    image = Image.fromarray(rgb)
    width, height = image.size
    if not face:
        return image.resize((COVER_WIDTH, COVER_HEIGHT), Image.Resampling.LANCZOS), False, None
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
    scale_x, scale_y = COVER_WIDTH / crop_w, COVER_HEIGHT / crop_h
    cover_face = {
        "x": (fx - left) * scale_x,
        "y": (fy - top) * scale_y,
        "w": fw * scale_x,
        "h": fh * scale_y,
    }
    return (
        image.crop(box).resize((COVER_WIDTH, COVER_HEIGHT), Image.Resampling.LANCZOS),
        True,
        cover_face,
    )


def _line_partitions(words: list[str], line_count: int) -> list[list[list[str]]]:
    if line_count <= 1:
        return [[words]] if words else []
    if len(words) < line_count:
        return []
    partitions: list[list[list[str]]] = []
    if line_count == 2:
        for first in range(1, len(words)):
            partitions.append([words[:first], words[first:]])
    elif line_count == 3:
        for first in range(1, len(words) - 1):
            for second in range(first + 1, len(words)):
                partitions.append([words[:first], words[first:second], words[second:]])
    return partitions


def _fit_cover_layout(
    text: str,
    *,
    font_path: str,
    preferred_size: int,
) -> tuple[str, Any, int, list[str]]:
    from app.video_shorts.services.compositor import _load_title_font

    words = text.split()
    if not words:
        raise ValueError("Cover text is empty")
    clean_text = " ".join(words)
    for size in range(min(MAX_COVER_FONT_SIZE, preferred_size), MIN_COVER_FONT_SIZE - 1, -2):
        font = _load_title_font(font_path, size)
        if font is None:
            raise RuntimeError("Brand title font could not be loaded")
        measure = ImageDraw.Draw(Image.new("RGB", (8, 8)))
        line_counts = (2, 3) if len(words) >= 3 else ((2,) if len(words) == 2 else (1,))
        options: list[tuple[float, list[str]]] = []
        for line_count in line_counts:
            for partition in _line_partitions(words, line_count):
                lines = [" ".join(part) for part in partition]
                widths = [float(measure.textlength(line, font=font)) for line in lines]
                if max(widths, default=0.0) > COVER_TEXT_MAX_WIDTH:
                    continue
                imbalance = max(widths) - min(widths)
                options.append((imbalance + (line_count - 2) * 24.0, lines))
        if options:
            lines = min(options, key=lambda item: item[0])[1]
            return clean_text, font, size, lines
    raise CoverCopyDoesNotFit("Cover text cannot fit at the minimum readable size")


def _thumbnail_copy_fallback(text: str) -> str:
    """Keep source words while producing a last-resort, readable short phrase."""
    words = text.split()
    if len(words) <= 3:
        value = " ".join(words)
        return value if len(value) <= 36 else value[:35].rstrip() + "…"
    candidates = (
        [words[0], *words[-2:]],
        [words[0], words[-1]],
        words[:3],
        words[:2],
    )
    for candidate in candidates:
        value = " ".join(candidate)
        if len(value) <= 36:
            return value
    return words[0]


def _highlight_word_indexes(text: str, highlight: str) -> set[int]:
    words = text.split()
    phrase_words = highlight.split()
    if not phrase_words:
        return set()
    folded_words = [word.casefold().strip(".,!?;:'\"()[]{}") for word in words]
    folded_phrase = [word.casefold().strip(".,!?;:'\"()[]{}") for word in phrase_words]
    for start in range(0, len(folded_words) - len(folded_phrase) + 1):
        if folded_words[start:start + len(folded_phrase)] == folded_phrase:
            return set(range(start, start + len(folded_phrase)))
    return set()


def _draw_cover_text(
    image: Image.Image,
    *,
    text: str,
    highlight: str,
    design: dict[str, Any],
    cover_face: Optional[dict[str, float]] = None,
) -> dict[str, Any]:
    from app.video_shorts.services.compositor import _title_font_path_for_text

    font_key = str(design.get("font") or DEFAULT_EDITOR_TITLE_FONT_KEY)
    font_cfg = TITLE_FONTS.get(font_key) or TITLE_FONTS[DEFAULT_EDITOR_TITLE_FONT_KEY]
    font_path = _title_font_path_for_text(text, str(font_cfg["path"]))
    saved_size = int(design.get("title_font_size") or 40)
    preferred_size = max(140, min(MAX_COVER_FONT_SIZE, int(saved_size * 3.2)))
    rendered_text, font, font_size, lines = _fit_cover_layout(
        text, font_path=font_path, preferred_size=preferred_size,
    )
    if highlight.casefold() not in rendered_text.casefold():
        highlight = " ".join(rendered_text.split()[:2])
    draw = ImageDraw.Draw(image, "RGBA")
    text_color, highlight_color, bg_color = _resolve_cover_colors(design)
    bg_alpha = int(max(82, min(100, float(design.get("title_bg_alpha", DEFAULT_EDITOR_TITLE_BG_ALPHA)))))
    line_widths = [int(math.ceil(draw.textlength(line, font=font))) for line in lines]
    text_bbox = draw.textbbox((0, 0), "Ag", font=font)
    text_height = max(1, int(text_bbox[3] - text_bbox[1]))
    horizontal_padding, vertical_padding, line_gap = 34, 22, 14
    box_width = min(COVER_WIDTH - 72, max(line_widths) + horizontal_padding * 2)
    box_height = text_height + vertical_padding * 2
    block_height = box_height * len(lines) + line_gap * (len(lines) - 1)
    face_safe_bottom = 0.0
    if cover_face:
        # Mouth is normally around 70–80% down the face box. Keeping the
        # text below 88% leaves a visible buffer around both eyes and mouth.
        face_safe_bottom = float(cover_face["y"]) + float(cover_face["h"]) * 0.88
    y = int(max(COVER_HEIGHT * (2.0 / 3.0), face_safe_bottom + 64.0))
    y = min(y, COVER_HEIGHT - block_height - 92)
    x = int((COVER_WIDTH - box_width) / 2)
    highlight_indexes = _highlight_word_indexes(rendered_text, highlight)
    global_word_index = 0
    bg_rgb = _rgb(bg_color)
    for line, line_width in zip(lines, line_widths):
        bbox = (x, y, x + box_width, y + box_height)
        draw.rounded_rectangle(bbox, radius=20, fill=(*bg_rgb, int(255 * bg_alpha / 100)))
        cursor = x + horizontal_padding
        baseline_y = y + vertical_padding - text_bbox[1]
        words = line.split()
        for index, word in enumerate(words):
            is_highlight = global_word_index in highlight_indexes
            draw.text(
                (cursor, baseline_y), word, font=font,
                fill=highlight_color if is_highlight else text_color,
            )
            cursor += float(draw.textlength(word, font=font))
            if index < len(words) - 1:
                cursor += float(draw.textlength(" ", font=font))
            global_word_index += 1
        y += box_height + line_gap
    return {
        "cover_text": rendered_text,
        "highlight_phrase": highlight,
        "font_size": font_size,
        "lines": lines,
        "text_color": text_color,
        "highlight_color": highlight_color,
        "box_color": bg_color,
    }


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
    if not Path(target["clip_local_path"]).is_file() and not storage.exists(target["clip_storage_key"]):
        missing_error = "Rendered clip object is missing"
        failed_conn = get_db()
        try:
            failed_conn.execute(
                "UPDATE shorts_clip_covers SET status='failed', error=?, updated_at=CURRENT_TIMESTAMP WHERE generated_video_id=? AND owner_user_id=? AND brand_id=?",
                [missing_error, generated_id, owner, brand],
            )
            failed_conn.commit()
        finally:
            failed_conn.close()
        raise FileNotFoundError(missing_error)
    source_temp = storage.download_to_temp(target["source_storage_key"])
    try:
        existing_candidates = _parse_json(existing[3]) if existing and existing[3] else []
        is_cover_edit = bool(
            existing_candidates
            and all(candidate.get("source_timestamp") is not None for candidate in existing_candidates)
            and (
                payload.get("cover_text")
                or payload.get("highlight_phrase")
                or payload.get("selected_timestamp") is not None
            )
        )
        if is_cover_edit:
            candidates = existing_candidates
            duration = sum(max(0.0, end - start) for start, end in target["source_ranges"])
        else:
            candidates, duration = _candidate_frames(Path(source_temp), target["source_ranges"])
        selected_ts = float(payload.get("selected_timestamp") if payload.get("selected_timestamp") is not None else candidates[0]["timestamp"])
        if not any(start <= selected_ts <= end for start, end in target["source_ranges"]):
            selected_ts = float(candidates[0]["timestamp"])

        import cv2
        cap = cv2.VideoCapture(str(source_temp))
        frame = _frame_at(cap, cv2, selected_ts)
        cap.release()
        if frame is None:
            raise RuntimeError("Selected cover frame could not be decoded")
        chosen = min(candidates, key=lambda item: abs(float(item["timestamp"]) - selected_ts))
        face = chosen.get("face") if abs(float(chosen["timestamp"]) - selected_ts) < 0.08 else None
        image, used_face_crop, cover_face = _crop_frame(frame, face)

        existing_text = str((existing[1] if existing else "") or "").strip()
        existing_highlight = str((existing[2] if existing else "") or "").strip()
        cover_text = " ".join(str(payload.get("cover_text") or existing_text).strip().split())
        highlight = " ".join(str(payload.get("highlight_phrase") or existing_highlight).strip().split())
        copy_call_used = False
        if not cover_text:
            copy = generate_clip_cover_copy(target["transcript"], clip_title=target["title"])
            copy_call_used = True
            cover_text = copy["cover_text"]
            highlight = copy["highlight_phrase"]
        if len(cover_text.split()) > 6:
            raise ValueError("Cover text must be at most 6 words")
        if not highlight or highlight.casefold() not in cover_text.casefold():
            highlight = " ".join(cover_text.split()[:2])

        from app.video_shorts.routes import generation
        design = generation._load_short_editor_defaults_json(owner, brand)
        try:
            text_layout = _draw_cover_text(
                image, text=cover_text, highlight=highlight, design=design, cover_face=cover_face,
            )
        except CoverCopyDoesNotFit:
            original_text = cover_text
            if not copy_call_used:
                copy = generate_clip_cover_copy(target["transcript"], clip_title=target["title"])
                copy_call_used = True
                cover_text = copy["cover_text"]
                highlight = copy["highlight_phrase"]
            try:
                text_layout = _draw_cover_text(
                    image, text=cover_text, highlight=highlight, design=design, cover_face=cover_face,
                )
            except CoverCopyDoesNotFit:
                cover_text = _thumbnail_copy_fallback(original_text)
                if not highlight or highlight.casefold() not in cover_text.casefold():
                    highlight = " ".join(cover_text.split()[:2])
                text_layout = _draw_cover_text(
                    image, text=cover_text, highlight=highlight, design=design, cover_face=cover_face,
                )
            log.info(
                "Cover copy shortened for thumbnail layout generated_video_id=%s original=%r rendered=%r",
                generated_id, original_text, text_layout["cover_text"],
            )
        cover_text = str(text_layout["cover_text"])
        highlight = str(text_layout["highlight_phrase"])

        with tempfile.TemporaryDirectory(prefix="short_cover_") as temp_dir:
            temp_root = Path(temp_dir)
            cover_path = temp_root / "cover.jpg"
            size = _save_jpeg_under_limit(image, cover_path)
            prefix = f"brands/{brand}/private/short-covers/{generated_id}"
            cover_key = f"{prefix}/cover.jpg"
            put_file_for_owner(cover_path, cover_key, owner_user_id=owner)

            # Candidate previews are tenant-private assets; timestamps remain the source of truth.
            cap = cv2.VideoCapture(str(source_temp))
            stored_candidates = []
            for candidate in candidates:
                candidate_frame = _frame_at(cap, cv2, float(candidate["timestamp"]))
                if candidate_frame is None:
                    continue
                preview_image, _, _ = _crop_frame(candidate_frame, candidate.get("face"))
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
            source_temp.cleanup()  # ManagedTempPath
        except AttributeError:
            Path(source_temp).unlink(missing_ok=True)


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
