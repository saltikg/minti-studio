import base64
import io
import json
from typing import Any, Dict, Iterable, List

import requests
from PIL import Image, ImageOps

from app.video_shorts.config import OPENAI_MODEL, _openai_client


FACE_CHECK_PROMPT = """Inspect this collage of up to 6 thumbnails from one YouTube channel's most recent long-form videos. Read the cells left-to-right, top-to-bottom.

Return STRICT JSON only, with exactly this shape:
{"verdict":"face_pass|face_fail|face_unsure","same_person_count":0-6,"presenter_type":"real_person|stock_or_random_people|illustration_or_animation|ai_avatar|text_or_slides|no_person|mixed","reason":"..."}

Rules:
- face_pass only if the SAME real human appears as the presenter in at least 4 thumbnails and looks like they are talking to camera.
- Stock photos, different people each time, illustrations, animations, slides/text-only, and AI avatars must be face_fail.
- If identity, realism, or presenter role is ambiguous, return face_unsure.
- Count only thumbnails where that same real presenter is visibly present in same_person_count.
- Keep reason concise and grounded only in what is visible in the collage."""

_VALID_VERDICTS = {"face_pass", "face_fail", "face_unsure"}
_VALID_PRESENTER_TYPES = {
    "real_person",
    "stock_or_random_people",
    "illustration_or_animation",
    "ai_avatar",
    "text_or_slides",
    "no_person",
    "mixed",
}


def _vision_model(model: str) -> str:
    normalized = str(model or "").strip().lower()
    vision_prefixes = ("gpt-4o", "gpt-4.1", "gpt-4.5", "gpt-5")
    return str(model).strip() if normalized.startswith(vision_prefixes) else "gpt-4o-mini"


def _unsure(reason: str) -> Dict[str, Any]:
    return {
        "face_check_verdict": "face_unsure",
        "face_check_same_person_count": 0,
        "face_check_presenter_type": "mixed",
        "face_check_reason": str(reason or "Face check could not be completed.")[:500],
    }


def _download_images(urls: Iterable[str], *, timeout_seconds: float) -> List[Image.Image]:
    images: List[Image.Image] = []
    for url in list(urls)[:6]:
        response = requests.get(str(url), timeout=timeout_seconds)
        response.raise_for_status()
        image = Image.open(io.BytesIO(response.content)).convert("RGB")
        images.append(image)
    return images


def _collage_data_url(images: List[Image.Image]) -> str:
    cell_size = (480, 270)
    canvas = Image.new("RGB", (cell_size[0] * 3, cell_size[1] * 2), "white")
    for index, image in enumerate(images[:6]):
        fitted = ImageOps.fit(image, cell_size, method=Image.Resampling.LANCZOS)
        canvas.paste(fitted, ((index % 3) * cell_size[0], (index // 3) * cell_size[1]))
    buffer = io.BytesIO()
    canvas.save(buffer, format="JPEG", quality=85, optimize=True)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def check_face_on_camera(
    thumbnail_urls: Iterable[str],
    *,
    client: Any = None,
    model: str = "",
    timeout_seconds: float = 30.0,
) -> Dict[str, Any]:
    urls = [str(url or "").strip() for url in thumbnail_urls if str(url or "").strip()][:6]
    if len(urls) < 3:
        return _unsure(f"Only {len(urls)} usable long-form thumbnail URL(s); at least 3 are required.")
    try:
        images = _download_images(urls, timeout_seconds=min(timeout_seconds, 10.0))
        if len(images) < 3:
            return _unsure(f"Only {len(images)} thumbnails could be loaded; at least 3 are required.")
        selected_client = client or _openai_client
        if selected_client is None:
            return _unsure("OpenAI client is not configured.")
        selected_model = _vision_model(model or OPENAI_MODEL)
        response = selected_client.with_options(timeout=timeout_seconds).chat.completions.create(
            model=selected_model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": FACE_CHECK_PROMPT},
                        {"type": "image_url", "image_url": {"url": _collage_data_url(images), "detail": "low"}},
                    ],
                }
            ],
            response_format={"type": "json_object"},
            temperature=0,
        )
        payload = json.loads(str(response.choices[0].message.content or "").strip())
        verdict = str(payload.get("verdict") or "")
        presenter_type = str(payload.get("presenter_type") or "")
        count = int(payload.get("same_person_count"))
        if verdict not in _VALID_VERDICTS:
            raise ValueError(f"invalid verdict: {verdict!r}")
        if presenter_type not in _VALID_PRESENTER_TYPES:
            raise ValueError(f"invalid presenter_type: {presenter_type!r}")
        if count < 0 or count > len(images):
            raise ValueError(f"invalid same_person_count: {count}")
        if verdict == "face_pass" and count < 4:
            raise ValueError("face_pass requires same_person_count >= 4")
        return {
            "face_check_verdict": verdict,
            "face_check_same_person_count": count,
            "face_check_presenter_type": presenter_type,
            "face_check_reason": str(payload.get("reason") or "")[:500],
            "_face_check_response": response,
            "_face_check_model": selected_model,
        }
    except Exception as exc:
        return _unsure(f"Face check error: {type(exc).__name__}: {exc}")
