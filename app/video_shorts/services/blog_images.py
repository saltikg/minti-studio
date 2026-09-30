from __future__ import annotations

import base64
import os
import re
import shutil
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import quote
from urllib import request

from app.video_shorts.services.blog_llm import (
    BLOG_IMAGE_COVER,
    BLOG_IMAGE_COVER_QUALITY,
    BLOG_IMAGE_INLINE,
    BLOG_IMAGE_INLINE_QUALITY,
    LLMResult,
    compute_image_cost,
    log_usage,
    _check_budget,
)

try:
    from openai import OpenAI
except ImportError:  # pragma: no cover
    OpenAI = None


ROOT = Path(__file__).resolve().parents[3]
STATIC_BLOG_ROOT = ROOT / "app" / "video_shorts" / "static" / "img" / "blog"
IMAGE_STYLE_PATH = ROOT / "app" / "video_shorts" / "blog_pipeline" / "image_style.md"
COVER_STYLE_PATH = ROOT / "app" / "video_shorts" / "blog_pipeline" / "style" / "cover_style.md"
COVER_REFERENCE_PATH = ROOT / "app" / "video_shorts" / "blog_pipeline" / "style" / "cover_reference.png"
COVER_REFERENCE_OBJECTS_PATH = ROOT / "app" / "video_shorts" / "blog_pipeline" / "style" / "cover_reference_objects.png"
LOGO_BADGE_PATHS = (
    ROOT / "app" / "video_shorts" / "static" / "img" / "brand" / "minti_badge.png",
)
BLOG_IMAGE_SIZE = os.getenv("BLOG_IMAGE_SIZE", "1536x1024")
COVER_DIRECTIVES_MARKER = "\n\nCover directives:\n"
COVER_REFERENCE_SCOPE = (
    "The reference images define ONLY the rendering style (soft 3D material, lighting, palette, "
    "background motifs). Do NOT reuse their characters, clothing, composition, layout, or objects."
)


@dataclass
class BlogImageResult:
    kind: str
    marker: str | None
    filename: str
    url: str
    alt: str
    prompt: str
    model: str
    quality: str
    cost_usd: Decimal
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    attempts: list[dict[str, str]]


def image_style_suffix(kind: str = "inline") -> str:
    if kind == "cover" and COVER_STYLE_PATH.is_file():
        return f"{COVER_REFERENCE_SCOPE}\n\n{COVER_STYLE_PATH.read_text(encoding='utf-8').strip()}"
    return IMAGE_STYLE_PATH.read_text(encoding="utf-8").strip()


def _cover_reference_paths() -> list[Path]:
    return [path for path in (COVER_REFERENCE_PATH, COVER_REFERENCE_OBJECTS_PATH) if path.is_file()]


def article_image_dir(slug: str) -> Path:
    safe_slug = re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(slug or "article")).strip("-") or "article"
    return STATIC_BLOG_ROOT / safe_slug


def article_image_url(slug: str, filename: str) -> str:
    return f"/video_shorts/static/img/blog/{quote(str(slug))}/{quote(filename)}"


def _usage_value(usage: Any, *names: str) -> int:
    for name in names:
        value = getattr(usage, name, None)
        if value is not None:
            try:
                return int(value or 0)
            except (TypeError, ValueError):
                return 0
    return 0


def _cached_tokens(usage: Any) -> int:
    details = getattr(usage, "input_tokens_details", None) or getattr(usage, "prompt_tokens_details", None)
    if not details:
        return 0
    return _usage_value(details, "cached_tokens", "cached_input_tokens")


def _cache_write_tokens(usage: Any) -> int:
    details = getattr(usage, "input_tokens_details", None) or getattr(usage, "prompt_tokens_details", None)
    if not details:
        return 0
    return _usage_value(details, "cache_write_tokens", "cache_write_input_tokens", "cache_creation_tokens", "cache_creation_input_tokens")


def _decode_image_response(response: Any) -> bytes:
    data = getattr(response, "data", None) or []
    if not data:
        raise RuntimeError("image response did not include data")
    first = data[0]
    b64_value = getattr(first, "b64_json", None)
    if not b64_value and isinstance(first, dict):
        b64_value = first.get("b64_json")
    if not b64_value:
        image_url = getattr(first, "url", None)
        if not image_url and isinstance(first, dict):
            image_url = first.get("url")
        if image_url:
            with request.urlopen(str(image_url), timeout=30) as resp:
                return resp.read()
        raise RuntimeError("image response did not include b64_json or url")
    return base64.b64decode(b64_value)


_FORBIDDEN_COVER_PROMPT_RE = re.compile(
    r"\b(?:text|words?|headline|title|caption|typography|font|logo|watermark|ui|interface|screenshot|"
    r"youtube|instagram|tiktok|realistic people|photorealistic people|photo-realistic people|portrait|"
    r"brand colors?|mint|teal|palette|style)\b",
    re.I,
)


def clean_cover_scene_prompt(prompt: str) -> str:
    sentences = re.split(r"(?<=[.!?])\s+", str(prompt or "").strip())
    kept = [sentence for sentence in sentences if sentence and not _FORBIDDEN_COVER_PROMPT_RE.search(sentence)]
    cleaned = " ".join(kept).strip()
    if not cleaned:
        cleaned = _FORBIDDEN_COVER_PROMPT_RE.sub("", str(prompt or "")).strip()
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,;:-")
    return cleaned or "A simple editorial scene with two to four symbolic objects."


def _clean_cover_prompt(prompt: str) -> str:
    raw = str(prompt or "")
    if COVER_DIRECTIVES_MARKER in raw:
        scene, directives = raw.split(COVER_DIRECTIVES_MARKER, 1)
        return f"{clean_cover_scene_prompt(scene)}{COVER_DIRECTIVES_MARKER}{directives.strip()}"
    return clean_cover_scene_prompt(raw)


def _logo_badge_path() -> Path | None:
    return next((path for path in LOGO_BADGE_PATHS if path.is_file()), None)


def _add_logo_badge(image_path: Path, *, badge_ratio: float = 0.18, margin_ratio: float = 0.02) -> None:
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover
        return
    logo_path = _logo_badge_path()
    if not logo_path:
        return
    with Image.open(image_path).convert("RGBA") as base:
        with Image.open(logo_path).convert("RGBA") as logo:
            badge_width = max(1, round(base.width * badge_ratio))
            badge_width = min(badge_width, logo.width)
            badge_height = max(1, round(logo.height * (badge_width / logo.width)))
            resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
            badge = logo.resize((badge_width, badge_height), resample)
            margin = max(1, round(base.width * margin_ratio))
            base.alpha_composite(badge, (base.width - badge.width - margin, base.height - badge.height - margin))
        base.convert("RGB").save(image_path)


def _image_request(client: Any, *, kind: str, model: str, prompt: str, size: str, quality: str) -> Any:
    reference_paths = _cover_reference_paths() if kind == "cover" else []
    if reference_paths:
        references = [path.open("rb") for path in reference_paths]
        try:
            return client.images.edit(
                model=model,
                image=references if len(references) > 1 else references[0],
                prompt=prompt,
                size=size,
                quality=quality,
                n=1,
            )
        finally:
            for reference in references:
                reference.close()
    return client.images.generate(
        model=model,
        prompt=prompt,
        size=size,
        quality=quality,
        n=1,
    )


def generate_blog_image(
    *,
    prompt: str,
    slug: str,
    filename: str,
    kind: str,
    marker: str | None = None,
    alt: str = "",
    model: str | None = None,
    quality: str | None = None,
    topic_id: int | None = None,
    run_id: int | None = None,
    overwrite: bool = True,
    dry_run: bool = False,
) -> BlogImageResult:
    _check_budget()
    if OpenAI is None:
        raise RuntimeError("openai package is not installed")
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is not configured")
    selected_model = model or (BLOG_IMAGE_COVER if kind == "cover" else BLOG_IMAGE_INLINE)
    selected_quality = quality or (BLOG_IMAGE_COVER_QUALITY if kind == "cover" else BLOG_IMAGE_INLINE_QUALITY)
    scene_prompt = _clean_cover_prompt(prompt) if kind == "cover" else str(prompt or "").strip()
    full_prompt = f"{scene_prompt}\n\n{image_style_suffix(kind)}".strip()
    target_dir = article_image_dir(slug)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / filename
    attempts: list[dict[str, str]] = []
    last_error: Exception | None = None
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    for attempt_index in range(2):
        try:
            response = _image_request(
                client,
                kind=kind,
                model=selected_model,
                prompt=full_prompt,
                size=BLOG_IMAGE_SIZE,
                quality=selected_quality,
            )
            image_bytes = _decode_image_response(response)
            if target.exists() and overwrite:
                previous = target.with_name(f"{target.stem}-prev{target.suffix}")
                shutil.copy2(target, previous)
            target.write_bytes(image_bytes)
            if kind in {"cover", "inline"}:
                _add_logo_badge(target, badge_ratio=0.18, margin_ratio=0.02)
            usage = getattr(response, "usage", None)
            input_tokens = _usage_value(usage, "input_tokens", "prompt_tokens")
            cached_tokens = _cached_tokens(usage)
            cache_write_tokens = _cache_write_tokens(usage)
            output_tokens = _usage_value(usage, "output_tokens", "image_tokens")
            cost = compute_image_cost(selected_model, input_tokens, cached_tokens, output_tokens)
            usage_result = LLMResult(
                content="",
                provider="openai",
                model=selected_model,
                input_tokens=input_tokens,
                cached_input_tokens=cached_tokens,
                cache_write_input_tokens=cache_write_tokens,
                output_tokens=output_tokens,
                reasoning_tokens=0,
                cost_usd=cost,
            )
            log_usage("image_dry" if dry_run else "image", usage_result, topic_id=topic_id, run_id=run_id, images=1, quality=selected_quality)
            return BlogImageResult(
                kind=kind,
                marker=marker,
                filename=filename,
                url=article_image_url(slug, filename),
                alt=alt,
                prompt=scene_prompt,
                model=selected_model,
                quality=selected_quality,
                cost_usd=cost,
                input_tokens=input_tokens,
                cached_input_tokens=cached_tokens,
                output_tokens=output_tokens,
                attempts=attempts,
            )
        except Exception as exc:
            last_error = exc
            attempts.append({"attempt": str(attempt_index + 1), "error_class": exc.__class__.__name__, "message": str(exc)[:300]})
    raise RuntimeError(f"image generation failed after retry: {last_error}") from last_error


def generated_visual_filename(visual: dict[str, Any], index: int) -> str:
    raw = str(visual.get("filename") or "").strip()
    if raw:
        safe = re.sub(r"[^a-zA-Z0-9_.-]+", "-", raw).strip("-")
        if safe.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
            return safe
    return f"image-{index}.png"


def render_markdown_image(visual: dict[str, Any], result: BlogImageResult) -> str:
    alt = str(visual.get("alt") or result.alt or result.marker or "Blog illustration").strip()
    caption = str(visual.get("caption") or alt or "").strip()
    caption = caption.replace('"', "'")
    title = f' "{caption}"' if caption else ""
    markdown = f"![{alt}]({result.url}{title})"
    if caption:
        return markdown
    return markdown
