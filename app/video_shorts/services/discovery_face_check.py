import base64
import io
import json
import math
from typing import Any, Dict, Iterable, List

import requests
from PIL import Image, ImageDraw, ImageOps

from app.video_shorts.config import OPENAI_MODEL, _openai_client


FACE_CHECK_PROMPT = """Inspect this collage from one YouTube channel. Each numbered row is one recent long-form video: the left cell is its custom thumbnail and the right cell is an auto-generated in-video frame. A blank cell means that image was unavailable.

Identify whether the same real human presenter is visibly presenting in either image in each row. Return STRICT JSON only:
{"videos":[{"video_index":1,"same_presenter_visible":true}],"presenter_type":"real_person|stock_or_random_people|illustration_or_animation|ai_avatar|text_or_slides|no_person|mixed","reason":"..."}

Rules:
- Include exactly one result for every numbered usable row.
- A video counts when the same real person is visible as presenter in EITHER image in that row.
- Do not count a logo, body part without a visible presenter, illustration, avatar, slide, or unrelated stock person.
- Keep the reason concise and grounded only in what is visible."""

_VALID_PRESENTER_TYPES = {
    "real_person", "stock_or_random_people", "illustration_or_animation", "ai_avatar",
    "text_or_slides", "no_person", "mixed",
}
_FAIL_PRESENTER_TYPES = {
    "stock_or_random_people", "illustration_or_animation", "ai_avatar", "text_or_slides", "no_person",
}


def _vision_model(model: str) -> str:
    normalized = str(model or "").strip().lower()
    return str(model).strip() if normalized.startswith(("gpt-4o", "gpt-4.1", "gpt-4.5", "gpt-5")) else "gpt-4o-mini"


def _unsure(reason: str, *, usable_videos: int = 0, presenter_type: str = "mixed") -> Dict[str, Any]:
    return {
        "face_check_verdict": "face_unsure",
        "face_check_same_person_count": 0,
        "face_check_presenter_type": presenter_type if presenter_type in _VALID_PRESENTER_TYPES else "mixed",
        "face_check_reason": str(reason or "Face check could not be completed.")[:500],
        "face_check_usable_video_count": max(0, int(usable_videos or 0)),
    }


def _get_image(url: str, *, timeout_seconds: float) -> Image.Image | None:
    if not url:
        return None
    try:
        response = requests.get(str(url), timeout=timeout_seconds)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return Image.open(io.BytesIO(response.content)).convert("RGB")
    except Exception:
        return None


def _normalize_samples(samples: Iterable[Any]) -> List[Dict[str, str]]:
    normalized: List[Dict[str, str]] = []
    for item in samples:
        if isinstance(item, dict):
            video_id = str(item.get("video_id") or "").strip()
            thumbnail_url = str(item.get("thumbnail_url") or item.get("custom_thumbnail_url") or "").strip()
        else:
            video_id = ""
            thumbnail_url = str(item or "").strip()
        if video_id or thumbnail_url:
            normalized.append({"video_id": video_id, "thumbnail_url": thumbnail_url})
        if len(normalized) >= 6:
            break
    return normalized


def _download_video_rows(samples: List[Dict[str, str]], *, timeout_seconds: float) -> tuple[List[Dict[str, Any]], Dict[str, int]]:
    rows: List[Dict[str, Any]] = []
    stats = {"custom_available": 0, "hq1_available": 0, "hq2_available": 0, "hq3_available": 0}
    timeout = min(timeout_seconds, 10.0)
    for sample in samples[:6]:
        custom = _get_image(sample.get("thumbnail_url") or "", timeout_seconds=timeout)
        if custom is not None:
            stats["custom_available"] += 1
        frame = None
        video_id = sample.get("video_id") or ""
        if video_id:
            for frame_name in ("hq2", "hq1", "hq3"):
                candidate = _get_image(f"https://i.ytimg.com/vi/{video_id}/{frame_name}.jpg", timeout_seconds=timeout)
                if candidate is not None:
                    stats[f"{frame_name}_available"] += 1
                    frame = candidate
                    break
        if custom is not None or frame is not None:
            rows.append({"custom": custom, "frame": frame, "video_id": video_id})
    return rows, stats


def _collage_data_url(rows: List[Dict[str, Any]]) -> str:
    cell_size = (480, 270)
    canvas = Image.new("RGB", (cell_size[0] * 2, cell_size[1] * len(rows)), "white")
    draw = ImageDraw.Draw(canvas)
    for index, row in enumerate(rows):
        y = index * cell_size[1]
        for column, key in enumerate(("custom", "frame")):
            image = row.get(key)
            if image is not None:
                canvas.paste(ImageOps.fit(image, cell_size, method=Image.Resampling.LANCZOS), (column * cell_size[0], y))
        draw.rectangle((0, y, 48, y + 34), fill="black")
        draw.text((12, y + 8), str(index + 1), fill="white")
    buffer = io.BytesIO()
    canvas.save(buffer, format="JPEG", quality=85, optimize=True)
    return f"data:image/jpeg;base64,{base64.b64encode(buffer.getvalue()).decode('ascii')}"


def _code_verdict(*, presenter_type: str, same_presenter_count: int, usable_videos: int) -> str:
    if usable_videos < 3:
        return "face_unsure"
    if same_presenter_count >= max(2, int(math.ceil(usable_videos * 0.5))):
        return "face_pass"
    if presenter_type in _FAIL_PRESENTER_TYPES and same_presenter_count <= 1:
        return "face_fail"
    return "face_unsure"


def check_face_on_camera(
    video_samples: Iterable[Any], *, client: Any = None, model: str = "", timeout_seconds: float = 30.0,
) -> Dict[str, Any]:
    samples = _normalize_samples(video_samples)
    if len(samples) < 3:
        return _unsure(f"Only {len(samples)} usable long-form video sample(s); at least 3 are required.")
    try:
        rows, frame_stats = _download_video_rows(samples, timeout_seconds=timeout_seconds)
        usable_videos = len(rows)
        if usable_videos < 3:
            result = _unsure(f"Only {usable_videos} video rows could be loaded; at least 3 are required.", usable_videos=usable_videos)
            result["_face_check_frame_stats"] = frame_stats
            return result
        selected_client = client or _openai_client
        if selected_client is None:
            return _unsure("OpenAI client is not configured.", usable_videos=usable_videos)
        selected_model = _vision_model(model or OPENAI_MODEL)
        response = selected_client.with_options(timeout=timeout_seconds).chat.completions.create(
            model=selected_model,
            messages=[{"role": "user", "content": [
                {"type": "text", "text": FACE_CHECK_PROMPT},
                {"type": "image_url", "image_url": {"url": _collage_data_url(rows), "detail": "low"}},
            ]}],
            response_format={"type": "json_object"}, temperature=0,
        )
        payload = json.loads(str(response.choices[0].message.content or "").strip())
        presenter_type = str(payload.get("presenter_type") or "")
        if presenter_type not in _VALID_PRESENTER_TYPES:
            raise ValueError(f"invalid presenter_type: {presenter_type!r}")
        visible_indices = set()
        for item in payload.get("videos") or []:
            try:
                index = int(item.get("video_index"))
            except (TypeError, ValueError, AttributeError):
                continue
            if 1 <= index <= usable_videos and item.get("same_presenter_visible") is True:
                visible_indices.add(index)
        count = min(usable_videos, len(visible_indices))
        return {
            "face_check_verdict": _code_verdict(
                presenter_type=presenter_type, same_presenter_count=count, usable_videos=usable_videos,
            ),
            "face_check_same_person_count": count,
            "face_check_presenter_type": presenter_type,
            "face_check_reason": str(payload.get("reason") or "")[:500],
            "face_check_usable_video_count": usable_videos,
            "_face_check_response": response,
            "_face_check_model": selected_model,
            "_face_check_frame_stats": frame_stats,
        }
    except Exception as exc:
        return _unsure(f"Face check error: {type(exc).__name__}: {exc}")
