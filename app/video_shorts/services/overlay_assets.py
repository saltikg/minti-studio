from __future__ import annotations

import io
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

from PIL import Image, UnidentifiedImageError

from app.video_shorts.services import tenant_media
from app.video_shorts.services.db import get_db, get_db_readonly
from app.video_shorts.services.storage import (
    ManagedTempPath,
    build_storage_reference,
    get_media_storage,
    put_bytes_for_owner,
)


OVERLAY_IMAGE_MAX_BYTES = 2 * 1024 * 1024
OVERLAY_IMAGE_MAX_SIDE = 1080
OVERLAY_IMAGE_ASSET_KIND = "overlay"
_ALLOWED_UPLOAD_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".svg"}
_RASTER_FORMATS = {"PNG", "JPEG", "WEBP"}


class OverlayAssetError(ValueError):
    pass


def _safe_label(raw: Any, fallback: str = "Overlay image") -> str:
    clean = re.sub(r"\s+", " ", str(raw or "").strip())[:100]
    return clean or fallback


def _is_svg(data: bytes) -> bool:
    head = data[:4096].lstrip().lower()
    if head.startswith(b"<?xml"):
        marker = head.find(b"<svg")
        return 0 <= marker < 2048
    return head.startswith(b"<svg")


def _rasterize_svg(data: bytes) -> bytes:
    lowered = data.lower()
    blocked_tokens = (b"<!doctype", b"<!entity", b"<script", b"<foreignobject")
    if any(token in lowered for token in blocked_tokens):
        raise OverlayAssetError("The SVG contains unsupported content.")
    for value in re.findall(rb"(?:href|xlink:href)\s*=\s*['\"]([^'\"]+)", data, flags=re.I):
        clean = value.strip().lower()
        if clean and not clean.startswith((b"#", b"data:image/")):
            raise OverlayAssetError("The SVG may not reference external files.")
    if re.search(rb"url\s*\(\s*['\"]?(?:https?:|//|file:)", data, flags=re.I):
        raise OverlayAssetError("The SVG may not reference external files.")
    try:
        import cairosvg

        root = ET.fromstring(data)
        if str(root.tag).split("}")[-1].lower() != "svg":
            raise OverlayAssetError("The uploaded file is not a valid SVG.")

        def _dimension(value: Any) -> float | None:
            match = re.match(r"^\s*([0-9]+(?:\.[0-9]+)?)", str(value or ""))
            return float(match.group(1)) if match else None

        width = _dimension(root.attrib.get("width"))
        height = _dimension(root.attrib.get("height"))
        if not width or not height:
            view_box = [part for part in re.split(r"[\s,]+", str(root.attrib.get("viewBox") or "").strip()) if part]
            if len(view_box) == 4:
                width = _dimension(view_box[2])
                height = _dimension(view_box[3])
        width = max(1.0, min(10000.0, width or 512.0))
        height = max(1.0, min(10000.0, height or 512.0))
        scale = min(1.0, OVERLAY_IMAGE_MAX_SIDE / max(width, height))
        output_width = max(1, int(round(width * scale)))
        output_height = max(1, int(round(height * scale)))

        return cairosvg.svg2png(
            bytestring=data,
            output_width=output_width,
            output_height=output_height,
            unsafe=False,
        )
    except OverlayAssetError:
        raise
    except Exception as exc:
        raise OverlayAssetError("The SVG could not be converted.") from exc


def normalize_overlay_image_upload(data: bytes, filename: str) -> tuple[bytes, int, int]:
    if not data:
        raise OverlayAssetError("Choose an image to upload.")
    if len(data) > OVERLAY_IMAGE_MAX_BYTES:
        raise OverlayAssetError("Image must be 2 MB or smaller.")
    extension = Path(str(filename or "")).suffix.lower()
    if extension not in _ALLOWED_UPLOAD_EXTENSIONS:
        raise OverlayAssetError("Use a PNG, JPG, WEBP or SVG image.")

    svg = _is_svg(data)
    if svg != (extension == ".svg"):
        raise OverlayAssetError("The image contents do not match its file type.")
    source = _rasterize_svg(data) if svg else data
    try:
        with Image.open(io.BytesIO(source)) as probe:
            actual_format = str(probe.format or "").upper()
            if actual_format not in _RASTER_FORMATS:
                raise OverlayAssetError("Use a PNG, JPG, WEBP or SVG image.")
            if getattr(probe, "is_animated", False):
                probe.seek(0)
            image = probe.convert("RGBA")
    except OverlayAssetError:
        raise
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise OverlayAssetError("The uploaded file is not a valid image.") from exc

    if image.width < 1 or image.height < 1:
        raise OverlayAssetError("The uploaded image has invalid dimensions.")
    if max(image.size) > OVERLAY_IMAGE_MAX_SIDE:
        image.thumbnail((OVERLAY_IMAGE_MAX_SIDE, OVERLAY_IMAGE_MAX_SIDE), Image.Resampling.LANCZOS)
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue(), image.width, image.height


def _storage_key(user_id: str, filename: str) -> str:
    return f"user_images/{user_id}/{filename}"


def _asset_file_key(asset_id: str) -> str:
    return f"overlay_image:{asset_id}"


def create_overlay_image_asset(
    *,
    owner_user_id: str,
    brand_id: str | None,
    data: bytes,
    filename: str,
    label: str | None = None,
    normalized_upload: tuple[bytes, int, int] | None = None,
) -> dict[str, Any]:
    normalized, width, height = normalized_upload or normalize_overlay_image_upload(data, filename)
    asset_id = str(uuid4())
    stored_name = f"overlay_{asset_id}.png"
    key = _storage_key(owner_user_id, stored_name)
    display_label = _safe_label(label, Path(filename).stem or "Overlay image")
    put_bytes_for_owner(normalized, key, content_type="image/png", owner_user_id=owner_user_id)
    conn = get_db()
    try:
        conn.execute(
            """
            INSERT INTO shorts_static_images
              (id, user_id, brand_id, asset_kind, use_as_background, label, filename,
               file_size, file_ext, is_active, updated_at)
            VALUES (?, ?, ?, ?, false, ?, ?, ?, 'png', true, now())
            """,
            [asset_id, owner_user_id, brand_id, OVERLAY_IMAGE_ASSET_KIND, display_label, stored_name, len(normalized)],
        )
        tenant_media.upsert_asset(
            conn,
            file_key=_asset_file_key(asset_id),
            file_path=build_storage_reference(key),
            file_type="overlay_image",
            size_bytes=len(normalized),
            user_id=owner_user_id,
            brand_id=brand_id,
        )
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        get_media_storage().delete(key)
        raise
    finally:
        conn.close()
    return {
        "id": asset_id,
        "label": display_label,
        "url": get_media_storage().public_url(key),
        "width": width,
        "height": height,
        "size_bytes": len(normalized),
    }


def list_overlay_image_assets(*, owner_user_id: str, brand_id: str | None) -> list[dict[str, Any]]:
    conn = get_db_readonly()
    try:
        rows = conn.execute(
            """
            SELECT id, label, filename, file_size, created_at
            FROM shorts_static_images
            WHERE user_id = ?
              AND brand_id IS NOT DISTINCT FROM ?
              AND asset_kind = ?
              AND COALESCE(is_active, true) = true
            ORDER BY created_at DESC
            """,
            [owner_user_id, brand_id, OVERLAY_IMAGE_ASSET_KIND],
        ).fetchall()
    finally:
        conn.close()
    storage = get_media_storage()
    return [
        {
            "id": str(row[0]),
            "label": str(row[1] or "Overlay image"),
            "url": storage.public_url(_storage_key(owner_user_id, str(row[2]))),
            "size_bytes": int(row[3] or 0),
            "created_at": row[4].isoformat() if hasattr(row[4], "isoformat") else str(row[4] or ""),
        }
        for row in rows
    ]


def validate_overlay_image_asset_ids(
    asset_ids: Iterable[str], *, owner_user_id: str, brand_id: str | None
) -> set[str]:
    clean_ids = sorted({str(value or "").strip() for value in asset_ids if str(value or "").strip()})
    if not clean_ids:
        return set()
    placeholders = ",".join("?" for _ in clean_ids)
    conn = get_db_readonly()
    try:
        rows = conn.execute(
            f"""
            SELECT id FROM shorts_static_images
            WHERE id IN ({placeholders})
              AND user_id = ?
              AND brand_id IS NOT DISTINCT FROM ?
              AND asset_kind = ?
              AND COALESCE(is_active, true) = true
            """,
            [*clean_ids, owner_user_id, brand_id, OVERLAY_IMAGE_ASSET_KIND],
        ).fetchall()
    finally:
        conn.close()
    return {str(row[0]) for row in rows}


def resolve_overlay_image_asset(
    asset_id: str, *, owner_user_id: str, brand_id: str | None
) -> tuple[Path, ManagedTempPath | None]:
    conn = get_db_readonly()
    try:
        row = conn.execute(
            """
            SELECT filename FROM shorts_static_images
            WHERE id = ? AND user_id = ? AND brand_id IS NOT DISTINCT FROM ?
              AND asset_kind = ? AND COALESCE(is_active, true) = true
            LIMIT 1
            """,
            [asset_id, owner_user_id, brand_id, OVERLAY_IMAGE_ASSET_KIND],
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise OverlayAssetError("Overlay image is unavailable.")
    key = _storage_key(owner_user_id, str(row[0]))
    resolved = get_media_storage().download_to_temp(key)
    managed = resolved if isinstance(resolved, ManagedTempPath) else None
    return Path(resolved), managed


def _payload_uses_asset(payload: Any, asset_id: str) -> bool:
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            return False
    if isinstance(payload, dict):
        payload = payload.get("overlay_shapes", [])
    if not isinstance(payload, list):
        return False
    return any(
        isinstance(item, dict)
        and str(item.get("type") or "shape") == "image"
        and str(item.get("asset_id") or "") == asset_id
        for item in payload
    )


def count_overlay_image_uses(*, asset_id: str, owner_user_id: str, brand_id: str | None) -> int:
    conn = get_db_readonly()
    try:
        videos = conn.execute(
            """
            SELECT overlay_shapes FROM youtube_videos
            WHERE owner_user_id = ? AND brand_id IS NOT DISTINCT FROM ?
              AND deleted_at IS NULL AND overlay_shapes IS NOT NULL
            """,
            [owner_user_id, brand_id],
        ).fetchall()
        preferences = conn.execute(
            """
            SELECT preference_value FROM shorts_user_preferences
            WHERE user_id = ? AND preference_key LIKE 'short_editor_defaults%'
            """,
            [owner_user_id],
        ).fetchall()
    finally:
        conn.close()
    return sum(_payload_uses_asset(row[0], asset_id) for row in [*videos, *preferences])


def delete_overlay_image_asset(*, asset_id: str, owner_user_id: str, brand_id: str | None) -> int:
    uses = count_overlay_image_uses(asset_id=asset_id, owner_user_id=owner_user_id, brand_id=brand_id)
    if uses:
        return uses
    conn = get_db()
    try:
        row = conn.execute(
            """
            SELECT filename FROM shorts_static_images
            WHERE id = ? AND user_id = ? AND brand_id IS NOT DISTINCT FROM ?
              AND asset_kind = ? AND COALESCE(is_active, true) = true
            LIMIT 1
            """,
            [asset_id, owner_user_id, brand_id, OVERLAY_IMAGE_ASSET_KIND],
        ).fetchone()
        if not row:
            raise OverlayAssetError("Overlay image was not found.")
        conn.execute(
            "UPDATE shorts_static_images SET is_active = false, updated_at = now() WHERE id = ?",
            [asset_id],
        )
        conn.execute(
            "UPDATE shorts_storage_assets SET status = 'deleted', updated_at = now() WHERE file_key = ? AND user_id = ? AND brand_id IS NOT DISTINCT FROM ?",
            [_asset_file_key(asset_id), owner_user_id, brand_id],
        )
        conn.commit()
    finally:
        conn.close()
    get_media_storage().delete(_storage_key(owner_user_id, str(row[0])))
    return 0
