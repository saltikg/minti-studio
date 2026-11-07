from __future__ import annotations

import json
import hashlib
import logging
import math
import os
import resource
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

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
MOUTH_VISIBILITY_DARK_RATIO_MIN = 0.012
LIGHT_BOX_HIGHLIGHT_FALLBACK = "#E11D48"
DARK_BOX_HIGHLIGHT_FALLBACK = "#FFD400"
CANDIDATE_RANK_VERSION = 2
COVER_TEMPLATE_PREFERENCE_PREFIX = "cover_template"
COVER_TEMPLATES = {"clean", "bold"}
METIN_BRAND_ID = "37594f93-7692-49b2-b559-2e0841fe0268"
SELFIE_SEGMENTER_URL = (
    "https://storage.googleapis.com/mediapipe-models/image_segmenter/"
    "selfie_segmenter/float16/latest/selfie_segmenter.tflite"
)
SELFIE_SEGMENTER_SHA256 = "191ac9529ae506ee0beefa6b2c945a172dab9d07d1e802a290a4e4038226658b"
_SEGMENTER = None
_SEGMENTER_LOCK = threading.Lock()


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
          cover_template VARCHAR NOT NULL DEFAULT 'clean',
          kicker_text TEXT,
          main_text TEXT,
          bold_highlight_text TEXT,
          candidate_frames_json {json_type},
          selected_timestamp DOUBLE,
          used_face_crop BOOLEAN NOT NULL DEFAULT FALSE,
          status VARCHAR NOT NULL DEFAULT 'queued',
          error TEXT,
          generation_ms INTEGER,
          segmentation_ms INTEGER,
          segmentation_confidence DOUBLE PRECISION,
          peak_memory_mb DOUBLE PRECISION,
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
    additions = {
        "cover_template": "VARCHAR NOT NULL DEFAULT 'clean'",
        "kicker_text": "TEXT",
        "main_text": "TEXT",
        "bold_highlight_text": "TEXT",
        "segmentation_ms": "INTEGER",
        "segmentation_confidence": "DOUBLE PRECISION",
        "peak_memory_mb": "DOUBLE PRECISION",
    }
    if getattr(conn, "backend_name", "") == "postgres":
        for name, definition in additions.items():
            conn.execute(f"ALTER TABLE shorts_clip_covers ADD COLUMN IF NOT EXISTS {name} {definition}")
    else:
        existing = {str(row[1]) for row in conn.execute("PRAGMA table_info(shorts_clip_covers)").fetchall()}
        for name, definition in additions.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE shorts_clip_covers ADD COLUMN {name} {definition}")


def _cover_template_preference_key(brand_id: str) -> str:
    return f"{COVER_TEMPLATE_PREFERENCE_PREFIX}:{str(brand_id).strip()}"


def normalize_cover_template(value: Any, *, brand_id: str = "") -> str:
    clean = str(value or "").strip().lower()
    if clean in COVER_TEMPLATES:
        return clean
    return "bold" if str(brand_id).strip() == METIN_BRAND_ID else "clean"


def get_brand_cover_template(owner_user_id: str, brand_id: str) -> str:
    from app.video_shorts.services.user_preferences import load_user_preference

    stored = load_user_preference(owner_user_id, _cover_template_preference_key(brand_id))
    return normalize_cover_template(stored, brand_id=brand_id)


def save_brand_cover_template(owner_user_id: str, brand_id: str, template: str) -> str:
    from app.video_shorts.services.user_preferences import save_user_preference

    resolved = normalize_cover_template(template, brand_id=brand_id)
    save_user_preference(owner_user_id, _cover_template_preference_key(brand_id), resolved)
    return resolved


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
                   cover_text, highlight_phrase, cover_template, kicker_text, main_text, bold_highlight_text,
                   candidate_frames_json, selected_timestamp, used_face_crop, status, error,
                   generation_ms, segmentation_ms, segmentation_confidence, peak_memory_mb,
                   size_bytes, generated_at
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
            "cover_template": normalize_cover_template(row[8], brand_id=brand_id),
            "kicker": row[9] or "", "main": row[10] or "", "bold_highlight": row[11] or "",
            "candidates": _parse_json(row[12]) if row[12] else [],
            "selected_timestamp": row[13], "used_face_crop": bool(row[14]),
            "status": row[15], "error": row[16], "generation_ms": row[17],
            "segmentation_ms": row[18], "segmentation_confidence": row[19],
            "peak_memory_mb": row[20], "size_bytes": row[21], "generated_at": row[22],
        }
    finally:
        conn.close()


def list_covers_for_video(*, owner_user_id: str, brand_id: str, video_pk: int) -> dict[int, dict[str, Any]]:
    conn = get_db_readonly()
    try:
        rows = conn.execute(
            """
            SELECT id, generated_video_id, video_pk, plan_index, clip_filename, storage_key,
                   cover_text, highlight_phrase, cover_template, kicker_text, main_text, bold_highlight_text,
                   candidate_frames_json, selected_timestamp, used_face_crop, status, error,
                   generation_ms, segmentation_ms, segmentation_confidence, peak_memory_mb,
                   size_bytes, generated_at
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
            "cover_template": normalize_cover_template(row[8], brand_id=brand_id),
            "kicker": row[9] or "", "main": row[10] or "", "bold_highlight": row[11] or "",
            "candidates": _parse_json(row[12]) if row[12] else [],
            "selected_timestamp": row[13], "used_face_crop": bool(row[14]),
            "status": row[15], "error": row[16], "generation_ms": row[17],
            "segmentation_ms": row[18], "segmentation_confidence": row[19],
            "peak_memory_mb": row[20], "size_bytes": row[21], "generated_at": row[22],
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


def _mouth_region_is_obscured(mouth_state: str, dark_ratio: float) -> bool:
    return mouth_state != "unknown" and float(dark_ratio) < MOUTH_VISIBILITY_DARK_RATIO_MIN


def _speaker_feature(image: Any, face: dict[str, Any], cv2: Any) -> Any:
    """Small appearance descriptor used only to reject brief b-roll faces."""
    import numpy as np

    x, y, width, height = (int(face[key]) for key in ("x", "y", "w", "h"))
    roi = image[max(0, y):max(0, y) + height, max(0, x):max(0, x) + width]
    if roi.size == 0:
        return None
    resized = cv2.resize(roi, (32, 32), interpolation=cv2.INTER_AREA)
    hsv = cv2.cvtColor(resized, cv2.COLOR_BGR2HSV)
    histogram = cv2.calcHist([hsv], [0, 1], None, [12, 8], [0, 180, 0, 256]).flatten()
    histogram = histogram / max(float(np.linalg.norm(histogram)), 1e-6)
    gray = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY).astype("float32")
    gray = (gray - float(gray.mean())) / max(float(gray.std()), 1.0)
    appearance = cv2.resize(gray, (12, 12), interpolation=cv2.INTER_AREA).flatten()
    appearance = appearance / max(float(np.linalg.norm(appearance)), 1e-6)
    return np.concatenate((histogram * 0.72, appearance * 0.28))


def _tag_main_speaker(candidates: list[dict[str, Any]]) -> None:
    """Mark the face identity that recurs most throughout the clip."""
    import numpy as np

    with_features = [item for item in candidates if item.get("_speaker_feature") is not None]
    clusters: list[list[dict[str, Any]]] = []
    for item in with_features:
        feature = item["_speaker_feature"]
        best_cluster = None
        best_similarity = -1.0
        for cluster in clusters:
            centroid = np.mean([member["_speaker_feature"] for member in cluster], axis=0)
            centroid = centroid / max(float(np.linalg.norm(centroid)), 1e-6)
            similarity = float(np.dot(feature, centroid))
            if similarity > best_similarity:
                best_similarity, best_cluster = similarity, cluster
        if best_cluster is not None and best_similarity >= 0.64:
            best_cluster.append(item)
        else:
            clusters.append([item])

    main_cluster: list[dict[str, Any]] = []
    if clusters:
        main_cluster = max(
            clusters,
            key=lambda cluster: (
                len(cluster),
                sum(float(item.get("face_sharpness") or 0.0) for item in cluster),
            ),
        )
    # A single isolated detection is not enough evidence to call all other faces b-roll.
    has_identity_evidence = len(main_cluster) >= 2
    main_ids = {id(item) for item in main_cluster}
    for item in candidates:
        item.pop("_speaker_feature", None)
        if not item.get("face"):
            item["main_speaker"] = False
            continue
        is_main = not has_identity_evidence or id(item) in main_ids
        item["main_speaker"] = is_main
        item["speaker_cluster_size"] = len(main_cluster) if is_main else 0
        if not is_main:
            reasons = list(item.get("rejection_reasons") or [])
            if "different_speaker" not in reasons:
                reasons.append("different_speaker")
            item["rejection_reasons"] = reasons
            item["eligible"] = False
            item["score"] = round(float(item.get("score") or 0.0) - 10000.0, 3)
        else:
            item["score"] = round(float(item.get("score") or 0.0) + min(80.0, len(main_cluster) * 4.0), 3)


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
        mouth_obscured = False
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
            mouth_obscured = _mouth_region_is_obscured(mouth_state, mouth_dark_ratio)
            face_occluded = (
                float(face.get("confidence") or 0.0) < FACE_CONFIDENCE_MIN
                or not _landmark_geometry_is_visible(face)
                or mouth_obscured
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
            "rank_version": CANDIDATE_RANK_VERSION,
            "timestamp": round(float(ts), 3), "source_timestamp": round(float(ts), 3),
            "clip_timestamp": round(float(clip_ts), 3), "score": round(score, 3),
            "frame_sharpness": round(frame_sharpness, 2), "face_sharpness": round(face_sharpness, 2),
            "eyes_open": eyes_open, "eye_count": eye_count, "mouth_state": mouth_state,
            "mouth_dark_ratio": round(mouth_dark_ratio, 4), "mouth_obscured": mouth_obscured,
            "face_occluded": face_occluded,
            "eligible": eligible, "rejection_reasons": rejection_reasons, "face": stored_face,
            "_speaker_feature": _speaker_feature(analysis, face, cv2) if face else None,
        })
    cap.release()
    _tag_main_speaker(scored)
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
        ranked = [{"rank_version": CANDIDATE_RANK_VERSION,
                   "timestamp": round(middle, 3), "source_timestamp": round(middle, 3),
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
    box_height = text_height + vertical_padding * 2
    block_height = box_height * len(lines) + line_gap * (len(lines) - 1)
    face_safe_bottom = 0.0
    if cover_face:
        # Mouth is normally around 70–80% down the face box. Keeping the
        # text below 88% leaves a visible buffer around both eyes and mouth.
        face_safe_bottom = float(cover_face["y"]) + float(cover_face["h"]) * 0.88
    y = int(max(COVER_HEIGHT * (2.0 / 3.0), face_safe_bottom + 64.0))
    y = min(y, COVER_HEIGHT - block_height - 92)
    highlight_indexes = _highlight_word_indexes(rendered_text, highlight)
    global_word_index = 0
    bg_rgb = _rgb(bg_color)
    for line, line_width in zip(lines, line_widths):
        box_width = min(COVER_WIDTH - 72, line_width + horizontal_padding * 2)
        x = int((COVER_WIDTH - box_width) / 2)
        bbox = (x, y, x + box_width, y + box_height)
        draw.rounded_rectangle(bbox, radius=20, fill=(*bg_rgb, int(255 * bg_alpha / 100)))
        cursor = x + (box_width - line_width) / 2.0
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


def _model_path() -> Path:
    configured = str(os.getenv("COVER_SEGMENTATION_MODEL_PATH") or "").strip()
    path = Path(configured) if configured else Path(tempfile.gettempdir()) / "minti_models" / "selfie_segmenter.tflite"
    if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == SELFIE_SEGMENTER_SHA256:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".download")
    with urllib.request.urlopen(SELFIE_SEGMENTER_URL, timeout=15) as response:
        data = response.read()
    if hashlib.sha256(data).hexdigest() != SELFIE_SEGMENTER_SHA256:
        raise RuntimeError("Selfie segmentation model checksum mismatch")
    temporary.write_bytes(data)
    temporary.replace(path)
    return path


def _get_segmenter() -> Any:
    global _SEGMENTER
    if _SEGMENTER is not None:
        return _SEGMENTER
    with _SEGMENTER_LOCK:
        if _SEGMENTER is not None:
            return _SEGMENTER
        import mediapipe as mp

        options = mp.tasks.vision.ImageSegmenterOptions(
            base_options=mp.tasks.BaseOptions(model_asset_path=str(_model_path())),
            output_confidence_masks=True,
            output_category_mask=False,
        )
        _SEGMENTER = mp.tasks.vision.ImageSegmenter.create_from_options(options)
        return _SEGMENTER


def _red_glow_background() -> Image.Image:
    background = Image.new("RGBA", (COVER_WIDTH, COVER_HEIGHT), (11, 11, 13, 255))
    glow = Image.new("RGBA", background.size, (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow)
    glow_draw.ellipse((-50, 170, COVER_WIDTH + 50, 1410), fill=(179, 18, 27, 205))
    glow = glow.filter(ImageFilter.GaussianBlur(185))
    background = Image.alpha_composite(background, glow)

    vignette = Image.new("L", background.size, 0)
    vignette_draw = ImageDraw.Draw(vignette)
    vignette_draw.ellipse((-180, -100, COVER_WIDTH + 180, COVER_HEIGHT + 170), fill=215)
    vignette = vignette.filter(ImageFilter.GaussianBlur(150))
    darkness = Image.new("RGBA", background.size, (0, 0, 0, 170))
    background = Image.composite(background, darkness, vignette)
    return background


def _segment_subject(image: Image.Image, cover_face: Optional[dict[str, float]]) -> tuple[Optional[Image.Image], float, int]:
    import mediapipe as mp
    import numpy as np

    started = time.monotonic()
    analysis = image.convert("RGB").resize((288, 512), Image.Resampling.LANCZOS)
    array = np.ascontiguousarray(np.asarray(analysis, dtype=np.uint8))
    result = _get_segmenter().segment(mp.Image(image_format=mp.ImageFormat.SRGB, data=array))
    if not result.confidence_masks:
        return None, 0.0, int(round((time.monotonic() - started) * 1000))
    mask_array = np.asarray(result.confidence_masks[0].numpy_view(), dtype="float32").squeeze()
    foreground_ratio = float((mask_array >= 0.45).mean())
    confidence_region = mask_array
    if cover_face:
        x0 = max(0, int(float(cover_face["x"]) / COVER_WIDTH * mask_array.shape[1]))
        y0 = max(0, int(float(cover_face["y"]) / COVER_HEIGHT * mask_array.shape[0]))
        x1 = min(mask_array.shape[1], int((float(cover_face["x"]) + float(cover_face["w"])) / COVER_WIDTH * mask_array.shape[1]))
        y1 = min(mask_array.shape[0], int((float(cover_face["y"]) + float(cover_face["h"])) / COVER_HEIGHT * mask_array.shape[0]))
        if x1 > x0 and y1 > y0:
            confidence_region = mask_array[y0:y1, x0:x1]
    confidence = float(confidence_region.mean()) if confidence_region.size else 0.0
    if confidence < 0.58 or foreground_ratio < 0.055:
        return None, confidence, int(round((time.monotonic() - started) * 1000))
    mask = Image.fromarray(np.uint8(np.clip(mask_array, 0.0, 1.0) * 255), mode="L")
    mask = mask.resize((COVER_WIDTH, COVER_HEIGHT), Image.Resampling.BILINEAR).filter(ImageFilter.GaussianBlur(3.0))
    subject = ImageEnhance.Contrast(image.convert("RGB")).enhance(1.08)
    subject = ImageEnhance.Brightness(subject).enhance(1.05).convert("RGBA")
    subject.putalpha(mask)
    return subject, confidence, int(round((time.monotonic() - started) * 1000))


def _compose_bold_background(
    image: Image.Image,
    cover_face: Optional[dict[str, float]],
) -> tuple[Image.Image, bool, float, int]:
    background = _red_glow_background()
    try:
        subject, confidence, elapsed_ms = _segment_subject(image, cover_face)
    except Exception:
        log.exception("Bold cover person segmentation failed; using darkened-frame fallback")
        subject, confidence, elapsed_ms = None, 0.0, 0
    if subject is not None:
        return Image.alpha_composite(background, subject).convert("RGB"), True, confidence, elapsed_ms
    darkened = ImageEnhance.Brightness(image.convert("RGB")).enhance(0.55).convert("RGBA")
    # Keep the glow visible while retaining the real frame when confidence is low.
    fallback = Image.blend(darkened, background, 0.34)
    return fallback.convert("RGB"), False, confidence, elapsed_ms


def _locale_upper(value: str, *, turkish: bool) -> str:
    if turkish:
        value = value.translate(str.maketrans({"i": "İ", "ı": "I"}))
    return value.upper()


def _fit_single_line(draw: Any, text: str, font_path: str, maximum: int, minimum: int, width: int) -> tuple[Any, int]:
    from app.video_shorts.services.compositor import _load_title_font

    for size in range(maximum, minimum - 1, -2):
        font = _load_title_font(font_path, size)
        if font and float(draw.textlength(text, font=font)) <= width:
            return font, size
    font = _load_title_font(font_path, minimum)
    if font is None:
        raise RuntimeError("Bold cover font could not be loaded")
    return font, minimum


def _fit_bold_main(draw: Any, text: str, font_path: str) -> tuple[Any, int, list[str]]:
    from app.video_shorts.services.compositor import _load_title_font

    maximum_width = int(COVER_WIDTH * 0.88)
    for size in range(260, 199, -2):
        font = _load_title_font(font_path, size)
        if font and float(draw.textlength(text, font=font)) <= maximum_width:
            return font, size, [text]
    words = text.split()
    if len(words) == 2:
        for size in range(260, 199, -2):
            font = _load_title_font(font_path, size)
            if font and max(float(draw.textlength(word, font=font)) for word in words) <= maximum_width:
                return font, size, words
        font = _load_title_font(font_path, 200)
        if font:
            return font, 200, words
    # Preserve the requested visual height and condense an exceptionally long
    # word horizontally at draw time instead of clipping it or making it small.
    font = _load_title_font(font_path, 200)
    if font is None:
        raise RuntimeError("Bold cover font could not be loaded")
    return font, 200, [text]


def _draw_centered_scaled_text(
    image: Image.Image,
    text: str,
    font: Any,
    y: float,
    *,
    maximum_width: int,
) -> None:
    stroke_width = 3
    bbox = font.getbbox(text, stroke_width=stroke_width)
    width = max(1, int(math.ceil(bbox[2] - bbox[0])))
    height = max(1, int(math.ceil(bbox[3] - bbox[1])))
    layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    layer_draw = ImageDraw.Draw(layer)
    layer_draw.text(
        (-bbox[0], -bbox[1]), text, font=font, fill=(255, 255, 255, 255),
        stroke_width=stroke_width, stroke_fill=(0, 0, 0, 170),
    )
    if width > maximum_width:
        layer = layer.resize((maximum_width, height), Image.Resampling.LANCZOS)
        width = maximum_width
    image.paste(layer, (int((COVER_WIDTH - width) / 2), int(y)), layer)


def _draw_letter_spaced(draw: Any, text: str, font: Any, y: float, fill: tuple[int, int, int, int], spacing: int = 7) -> None:
    widths = [float(draw.textlength(character, font=font)) for character in text]
    total = sum(widths) + max(0, len(widths) - 1) * spacing
    x = (COVER_WIDTH - total) / 2.0
    for character, width in zip(text, widths):
        draw.text((x, y), character, font=font, fill=fill, stroke_width=1, stroke_fill=(0, 0, 0, 145))
        x += width + spacing


def _draw_bold_text(
    image: Image.Image,
    *,
    kicker: str,
    main: str,
    highlight: str,
    cover_face: Optional[dict[str, float]],
) -> dict[str, Any]:
    from app.video_shorts.services.compositor import _title_font_path_for_text

    all_text = " ".join(value for value in (kicker, main, highlight) if value).strip()
    turkish = any(character in all_text for character in "çğıöşüÇĞİÖŞÜ")
    kicker = _locale_upper(kicker, turkish=turkish)
    main = _locale_upper(main, turkish=turkish)
    highlight = _locale_upper(highlight, turkish=turkish)
    font_path = _title_font_path_for_text(
        all_text, str((TITLE_FONTS.get("oswald_bold") or TITLE_FONTS[DEFAULT_EDITOR_TITLE_FONT_KEY])["path"]),
    )
    draw = ImageDraw.Draw(image, "RGBA")
    main_font, main_size, main_lines = _fit_bold_main(draw, main, font_path)
    highlight_font, highlight_size = _fit_single_line(draw, highlight, font_path, 150, 110, int(COVER_WIDTH * 0.82))
    kicker_font, kicker_size = _fit_single_line(draw, kicker or " ", font_path, 64, 52, int(COVER_WIDTH * 0.82))
    main_boxes = [draw.textbbox((0, 0), line, font=main_font) for line in main_lines]
    highlight_box = draw.textbbox((0, 0), highlight, font=highlight_font)
    kicker_box = draw.textbbox((0, 0), kicker or " ", font=kicker_font)
    main_line_heights = [box[3] - box[1] for box in main_boxes]
    main_line_gap = -18 if len(main_lines) > 1 else 0
    main_height = sum(main_line_heights) + main_line_gap * max(0, len(main_lines) - 1)
    highlight_height = highlight_box[3] - highlight_box[1]
    kicker_height = kicker_box[3] - kicker_box[1] if kicker else 0
    total_height = main_height + highlight_height + kicker_height + (45 if kicker else 24) + 76
    face_safe_bottom = (
        float(cover_face["y"]) + float(cover_face["h"]) * 0.92 + 50.0 if cover_face else 760.0
    )
    y = min(max(800.0, face_safe_bottom), COVER_HEIGHT - total_height - 115.0)
    if kicker:
        _draw_letter_spaced(draw, kicker, kicker_font, y - kicker_box[1], (255, 255, 255, 255))
        y += kicker_height + 28
    main_y = y
    for index, (line, box_value, line_height) in enumerate(zip(main_lines, main_boxes, main_line_heights)):
        _draw_centered_scaled_text(
            image, line, main_font, main_y,
            maximum_width=int(COVER_WIDTH * 0.88),
        )
        main_y += line_height + (main_line_gap if index < len(main_lines) - 1 else 0)
    y += main_height + 24
    highlight_width = float(draw.textlength(highlight, font=highlight_font))
    padding_x, padding_y = 30, 16
    box = (
        (COVER_WIDTH - highlight_width) / 2.0 - padding_x,
        y,
        (COVER_WIDTH + highlight_width) / 2.0 + padding_x,
        y + highlight_height + padding_y * 2,
    )
    draw.rounded_rectangle(box, radius=10, fill=(225, 29, 46, 255))
    draw.text(
        ((COVER_WIDTH - highlight_width) / 2.0, y + padding_y - highlight_box[1]),
        highlight, font=highlight_font, fill=(255, 255, 255, 255),
    )
    return {
        "kicker": kicker,
        "main": main,
        "highlight": highlight,
        "main_font_size": main_size,
        "highlight_font_size": highlight_size,
        "kicker_font_size": kicker_size,
    }


def _peak_memory_mb() -> float:
    value = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    if os.uname().sysname == "Darwin":
        value /= 1024.0
    return round(value / 1024.0, 1)


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
            """
            SELECT id, cover_text, highlight_phrase, candidate_frames_json,
                   cover_template, kicker_text, main_text, bold_highlight_text
              FROM shorts_clip_covers
             WHERE generated_video_id = ?
            """,
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
            and all(int(candidate.get("rank_version") or 0) == CANDIDATE_RANK_VERSION for candidate in existing_candidates)
            and (
                payload.get("cover_text")
                or payload.get("highlight_phrase")
                or payload.get("kicker")
                or payload.get("main")
                or payload.get("bold_highlight")
                or payload.get("cover_template")
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
        existing_kicker = str((existing[5] if existing else "") or "").strip()
        existing_main = str((existing[6] if existing else "") or "").strip()
        existing_bold_highlight = str((existing[7] if existing else "") or "").strip()
        template = normalize_cover_template(
            payload.get("cover_template") or get_brand_cover_template(owner, brand), brand_id=brand,
        )
        cover_text = " ".join(str(payload.get("cover_text") or existing_text).strip().split())
        highlight = " ".join(str(payload.get("highlight_phrase") or existing_highlight).strip().split())
        kicker = " ".join(str(payload.get("kicker") if "kicker" in payload else existing_kicker).strip().split())
        main = " ".join(str(payload.get("main") or existing_main).strip().split())
        bold_highlight = " ".join(
            str(payload.get("bold_highlight") or existing_bold_highlight).strip().split()
        )
        copy_call_used = False
        if not cover_text or not main or not bold_highlight:
            copy = generate_clip_cover_copy(target["transcript"], clip_title=target["title"])
            copy_call_used = True
            cover_text = cover_text or copy["cover_text"]
            highlight = highlight or copy["highlight_phrase"]
            kicker = kicker or copy["kicker"]
            main = main or copy["main"]
            bold_highlight = bold_highlight or copy["highlight"]
        if len(cover_text.split()) > 4:
            raise ValueError("Clean cover text must be at most 4 words")
        if not highlight or highlight.casefold() not in cover_text.casefold():
            highlight = " ".join(cover_text.split()[:2])
        bold_word_count = sum(len(value.split()) for value in (kicker, main, bold_highlight))
        if len(kicker.split()) > 3 or not 1 <= len(main.split()) <= 2 or not 1 <= len(bold_highlight.split()) <= 3:
            raise ValueError("Bold cover requires an optional 3-word kicker, 1-2 main words, and 1-3 highlight words")
        if bold_word_count > 6:
            raise ValueError("Bold cover copy must be at most 6 words total")

        from app.video_shorts.routes import generation
        design = generation._load_short_editor_defaults_json(owner, brand)
        segmentation_used = False
        segmentation_confidence = 0.0
        segmentation_ms = 0
        if template == "bold":
            image, segmentation_used, segmentation_confidence, segmentation_ms = _compose_bold_background(
                image, cover_face,
            )
            _draw_bold_text(
                image, kicker=kicker, main=main, highlight=bold_highlight, cover_face=cover_face,
            )
        else:
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
        peak_memory_mb = _peak_memory_mb()
        conn = get_db()
        try:
            tenant_media.upsert_asset(
                conn, file_key=f"cover:{generated_id}", file_path=build_storage_reference(cover_key),
                file_type="short_cover", size_bytes=size, user_id=owner, brand_id=brand,
            )
            conn.execute(
                """
                UPDATE shorts_clip_covers
                   SET storage_key=?, cover_text=?, highlight_phrase=?, cover_template=?,
                       kicker_text=?, main_text=?, bold_highlight_text=?, candidate_frames_json=?,
                       selected_timestamp=?, used_face_crop=?, status='ready', error=NULL,
                       generation_ms=?, segmentation_ms=?, segmentation_confidence=?, peak_memory_mb=?,
                       size_bytes=?, generated_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
                 WHERE generated_video_id=? AND owner_user_id=? AND brand_id=?
                """,
                [cover_key, cover_text, highlight, template, kicker, main, bold_highlight,
                 _json_value(conn, stored_candidates), selected_ts, used_face_crop, elapsed_ms,
                 segmentation_ms, segmentation_confidence, peak_memory_mb, size, generated_id, owner, brand],
            )
            conn.commit()
        finally:
            conn.close()
        return {
            "generated_video_id": generated_id, "storage_key": cover_key,
            "cover_text": cover_text, "highlight_phrase": highlight,
            "cover_template": template, "kicker": kicker, "main": main,
            "bold_highlight": bold_highlight, "segmentation_used": segmentation_used,
            "segmentation_ms": segmentation_ms, "segmentation_confidence": segmentation_confidence,
            "selected_timestamp": selected_ts, "candidate_count": len(stored_candidates),
            "used_face_crop": used_face_crop, "generation_ms": elapsed_ms,
            "peak_memory_mb": peak_memory_mb, "size_bytes": size,
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
