from __future__ import annotations

from html import escape
import re
from urllib.parse import parse_qs, urlparse

import bleach
import markdown


_YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_YOUTUBE_SHORTCODE_RE = re.compile(r"^\[youtube:\s*(?P<value>[^\]]+?)\s*\]$", re.IGNORECASE)
_YOUTUBE_BARE_URL_RE = re.compile(r"^https?://(?:www\.)?(?:youtube\.com|youtu\.be)/\S+$", re.IGNORECASE)
_YOUTUBE_EMBED_TOKEN_RE = re.compile(r"(?:<p>)?YOUTUBE_EMBED_([A-Za-z0-9_-]{11})(?:</p>)?")
_COMPONENT_START_RE = re.compile(r"^:::(?P<type>[A-Za-z]+)(?:\s+(?P<title>.*))?$")
_COMPONENT_TOKEN_RE = re.compile(r"(?:<p>)?BLOG_COMPONENT_(\d+)(?:</p>)?")
_CALLOUT_TYPES = {"warning", "tip", "info"}
_SPECIMEN_TAGS = {"short": "Short", "long": "Long-form"}

_ALLOWED_TAGS = [
    "a",
    "blockquote",
    "br",
    "code",
    "em",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "hr",
    "img",
    "li",
    "ol",
    "p",
    "pre",
    "strong",
    "table",
    "tbody",
    "td",
    "th",
    "thead",
    "tr",
    "ul",
]

_ALLOWED_ATTRIBUTES = {
    "a": ["href", "title", "rel", "target"],
    "img": ["src", "alt", "title", "loading"],
}


def _extract_youtube_video_id(value: str) -> str | None:
    candidate = (value or "").strip()
    if _YOUTUBE_ID_RE.fullmatch(candidate):
        return candidate

    parsed = urlparse(candidate)
    host = (parsed.hostname or "").lower()
    if host in {"youtu.be", "www.youtu.be"}:
        video_id = parsed.path.strip("/").split("/", 1)[0]
        return video_id if _YOUTUBE_ID_RE.fullmatch(video_id) else None
    if host not in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        return None

    if parsed.path == "/watch":
        video_id = (parse_qs(parsed.query).get("v") or [""])[0]
    elif parsed.path.startswith("/shorts/") or parsed.path.startswith("/embed/"):
        video_id = parsed.path.strip("/").split("/", 2)[1]
    else:
        return None
    return video_id if _YOUTUBE_ID_RE.fullmatch(video_id) else None


def _youtube_embed_html(video_id: str) -> str:
    return (
        '<div class="yt-embed">'
        f'<iframe src="https://www.youtube-nocookie.com/embed/{video_id}" '
        'title="YouTube video" loading="lazy" frameborder="0" '
        'allow="accelerator; encrypted-media; picture-in-picture" allowfullscreen></iframe>'
        "</div>"
    )


def _expand_youtube_embeds(markdown_text: str) -> str:
    lines: list[str] = []
    for line in (markdown_text or "").splitlines():
        stripped = line.strip()
        shortcode_match = _YOUTUBE_SHORTCODE_RE.fullmatch(stripped)
        video_id = None
        if shortcode_match:
            video_id = _extract_youtube_video_id(shortcode_match.group("value"))
        elif _YOUTUBE_BARE_URL_RE.fullmatch(stripped):
            video_id = _extract_youtube_video_id(stripped)
        lines.append(f"YOUTUBE_EMBED_{video_id}" if video_id else line)
    return "\n".join(lines)


def _restore_youtube_embeds(clean_html: str) -> str:
    return _YOUTUBE_EMBED_TOKEN_RE.sub(lambda match: _youtube_embed_html(match.group(1)), clean_html)


def _component_html(component_type: str, title: str, body: str) -> str:
    body_html = _render_markdown(body, expand_components=False)
    safe_title = escape((title or "").strip())
    if component_type in _CALLOUT_TYPES:
        head = f'<span class="vs-callout__head">{safe_title}</span>' if safe_title else ""
        return f'<div class="vs-callout vs-callout--{component_type}">{head}{body_html}</div>'
    if component_type == "key":
        return f'<div class="vs-key">{body_html}</div>'
    if component_type == "action":
        head_text = safe_title or "Next step"
        return f'<div class="vs-action"><span class="vs-action__head">🎯 {head_text}</span>{body_html}</div>'
    if component_type in _SPECIMEN_TAGS:
        tag = _SPECIMEN_TAGS[component_type]
        return (
            f'<div class="vs-specimen vs-specimen--{component_type}">'
            f'<span class="vs-specimen__tag">{tag}</span>'
            '<span class="vs-specimen__play">▶</span>'
            f"{body_html}</div>"
        )
    return ""


def _extract_component_blocks(markdown_text: str) -> tuple[str, list[str]]:
    lines = (markdown_text or "").splitlines()
    output: list[str] = []
    components: list[str] = []
    index = 0
    while index < len(lines):
        start_match = _COMPONENT_START_RE.fullmatch(lines[index].strip())
        if not start_match:
            output.append(lines[index])
            index += 1
            continue

        component_type = start_match.group("type").lower()
        if component_type not in _CALLOUT_TYPES | {"key", "action"} | set(_SPECIMEN_TAGS):
            output.append(lines[index])
            index += 1
            continue

        end_index = index + 1
        while end_index < len(lines) and lines[end_index].strip() != ":::":
            end_index += 1
        if end_index >= len(lines):
            output.append(lines[index])
            index += 1
            continue

        body = "\n".join(lines[index + 1 : end_index]).strip()
        title = start_match.group("title") or ""
        components.append(_component_html(component_type, title, body))
        output.append(f"BLOG_COMPONENT_{len(components) - 1}")
        index = end_index + 1

    return "\n".join(output), components


def _restore_component_blocks(clean_html: str, components: list[str]) -> str:
    def replace(match: re.Match[str]) -> str:
        index = int(match.group(1))
        return components[index] if 0 <= index < len(components) else match.group(0)

    return _COMPONENT_TOKEN_RE.sub(replace, clean_html)


def _render_markdown(markdown_text: str, *, expand_components: bool = True) -> str:
    component_html: list[str] = []
    source = markdown_text or ""
    if expand_components:
        source, component_html = _extract_component_blocks(source)

    raw_html = markdown.markdown(
        _expand_youtube_embeds(source),
        extensions=["extra", "sane_lists", "tables"],
        output_format="html5",
    )
    clean_html = bleach.clean(
        raw_html,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRIBUTES,
        protocols=["http", "https", "mailto"],
        strip=True,
    )
    restored_html = _restore_youtube_embeds(bleach.linkify(clean_html))
    if expand_components:
        restored_html = _restore_component_blocks(restored_html, component_html)
    return restored_html


def render_markdown(markdown_text: str) -> str:
    return _render_markdown(markdown_text)
