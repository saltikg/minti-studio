from __future__ import annotations

from html import escape, unescape
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
_CHECK_CELL_RE = re.compile(r"(<td[^>]*>)(.*?)(</td>)", re.I | re.S)
_IMAGE_WITH_EM_CAPTION_RE = re.compile(r"<p>\s*(<img\b[^>]*>)\s*</p>\s*<p>\s*<em>(.*?)</em>\s*</p>", re.I | re.S)
_IMAGE_RE = re.compile(r"<p>\s*(<img\b[^>]*>)\s*</p>", re.I)
_TABLE_RE = re.compile(r"(<table\b[^>]*>.*?</table>)", re.I | re.S)
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


def _svg_icon(name: str) -> str:
    paths = {
        "bulb": '<path d="M9 18h6"/><path d="M10 22h4"/><path d="M8.6 15.5a6 6 0 1 1 6.8 0c-.8.6-1.4 1.5-1.4 2.5h-4c0-1-.6-1.9-1.4-2.5Z"/>',
        "sparkles": '<path d="M12 3l1.8 4.2L18 9l-4.2 1.8L12 15l-1.8-4.2L6 9l4.2-1.8L12 3Z"/><path d="M5 15l.9 2.1L8 18l-2.1.9L5 21l-.9-2.1L2 18l2.1-.9L5 15Z"/><path d="M19 14l.8 1.7 1.7.8-1.7.8L19 19l-.8-1.7-1.7-.8 1.7-.8L19 14Z"/>',
        "info": '<path d="M12 9h.01"/><path d="M11 12h1v4h1"/><circle cx="12" cy="12" r="9"/>',
        "alert": '<path d="M12 9v4"/><path d="M12 17h.01"/><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/>',
        "arrow": '<path d="M5 12h14"/><path d="m13 6 6 6-6 6"/>',
        "zoom": '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
        "x": '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
    }
    return (
        '<svg aria-hidden="true" viewBox="0 0 24 24" fill="none" '
        'stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'
        f'{paths[name]}</svg>'
    )


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


def _attrs_from_tag(tag: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for match in re.finditer(r'([A-Za-z_:][-A-Za-z0-9_:.]*)\s*=\s*("([^"]*)"|\'([^\']*)\')', tag):
        attrs[match.group(1).lower()] = unescape(match.group(3) if match.group(3) is not None else match.group(4) or "")
    return attrs


def _is_screenshot_image(src: str, title: str) -> bool:
    title_lower = title.lower()
    return (
        "/video_shorts/static/img/blog/library/" in src
        or "[screenshot]" in title_lower
        or "data-screenshot" in title_lower
    )


def _caption_from_image(alt: str, title: str) -> str:
    caption = re.sub(r"\s*(?:\|\|\||\[)\s*(?:screenshot|data-screenshot)\s*\]?\s*$", "", title, flags=re.I).strip()
    return caption or alt


def _figure_html(match: re.Match[str]) -> str:
    tag = match.group(1)
    attrs = _attrs_from_tag(tag)
    src = attrs.get("src", "")
    alt = attrs.get("alt", "")
    title = attrs.get("title", "")
    safe_src = escape(src, quote=True)
    safe_alt = escape(alt, quote=True)
    caption = _caption_from_image(alt, title)
    safe_caption = escape(caption)
    screenshot_tag = (
        '<span class="tag">Screenshot from MintiStudio</span>'
        if _is_screenshot_image(src, title)
        else ""
    )
    return (
        '<figure class="figure">'
        '<div class="media" tabindex="0" role="button" aria-label="Open image in lightbox">'
        f'<img src="{safe_src}" alt="{safe_alt}" loading="lazy">'
        f"{screenshot_tag}"
        f'<button class="zoom" type="button" aria-label="Enlarge image">{_svg_icon("zoom")}</button>'
        "</div>"
        f"<figcaption>{safe_caption}</figcaption>"
        "</figure>"
    )


def _figure_with_em_caption_html(match: re.Match[str]) -> str:
    tag = match.group(1)
    caption = re.sub(r"<[^>]+>", "", match.group(2)).strip()
    if caption:
        tag = re.sub(r"\s+title=(\"[^\"]*\"|'[^']*')", "", tag, flags=re.I)
        safe_caption = escape(unescape(caption), quote=True)
        if tag.rstrip().endswith("/>"):
            tag = re.sub(r"\s*/>$", f' title="{safe_caption}" />', tag)
        else:
            tag = re.sub(r">$", f' title="{safe_caption}">', tag)
    return _figure_html(re.match(r"(.*)", tag, flags=re.S))


def _add_table_checks(table_html: str) -> str:
    def replace_cell(match: re.Match[str]) -> str:
        open_tag, cell_html, close_tag = match.groups()
        text = re.sub(r"<[^>]+>", "", cell_html).strip()
        if re.match(r"^(Handled for you|Handled|Yes)\b", unescape(text), flags=re.I):
            return f'{open_tag}<span class="check" aria-hidden="true">✓</span>{cell_html}{close_tag}'
        return match.group(0)

    return _CHECK_CELL_RE.sub(replace_cell, table_html)


def _wrap_tables(html: str) -> str:
    return _TABLE_RE.sub(lambda match: f'<div class="table-shell">{_add_table_checks(match.group(1))}</div>', html)


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


def _steps_html(body: str) -> str:
    steps: list[tuple[str, str]] = []
    current_title = ""
    current_body: list[str] = []
    for line in (body or "").splitlines():
        heading = re.match(r"^###\s+(.+?)\s*$", line)
        if heading:
            if current_title:
                steps.append((current_title, "\n".join(current_body).strip()))
            current_title = heading.group(1).strip()
            current_body = []
        else:
            current_body.append(line)
    if current_title:
        steps.append((current_title, "\n".join(current_body).strip()))
    if not steps:
        return ""
    items = []
    for title, step_body in steps:
        body_html = _render_markdown(step_body, expand_components=False)
        items.append(
            '<div class="step"><div class="step-num"></div><div>'
            f"<h3>{escape(title)}</h3>{body_html}</div></div>"
        )
    return f'<div class="steps">{"".join(items)}</div>'


def _component_html(component_type: str, title: str, body: str) -> str:
    body_html = _render_markdown(body, expand_components=False)
    safe_title = escape((title or "").strip())
    if component_type in _CALLOUT_TYPES:
        labels = {"tip": "Tip", "info": "Note", "warning": "Watch out"}
        icons = {"tip": "sparkles", "info": "info", "warning": "alert"}
        label = safe_title or labels[component_type]
        return (
            f'<div class="callout callout--{component_type}">'
            f'<div class="icon">{_svg_icon(icons[component_type])}</div>'
            f'<div><div class="label">{label}</div>{body_html}</div></div>'
        )
    if component_type == "key":
        return (
            f'<div class="callout callout--key"><div class="icon">{_svg_icon("bulb")}</div>'
            f'<div><div class="label">Key takeaway</div>{body_html}</div></div>'
        )
    if component_type == "action":
        head_text = safe_title or "Next step"
        return (
            f'<div class="callout callout--action"><div class="icon">{_svg_icon("arrow")}</div>'
            f'<div><div class="label">{head_text}</div>{body_html}</div></div>'
        )
    if component_type == "steps":
        return _steps_html(body)
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
        if component_type not in _CALLOUT_TYPES | {"key", "action", "steps"} | set(_SPECIMEN_TAGS):
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
    restored_html = _wrap_tables(restored_html)
    restored_html = _IMAGE_WITH_EM_CAPTION_RE.sub(_figure_with_em_caption_html, restored_html)
    restored_html = _IMAGE_RE.sub(_figure_html, restored_html)
    return restored_html


def render_markdown(markdown_text: str) -> str:
    return _render_markdown(markdown_text)
