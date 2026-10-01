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
_DIAGRAM_TYPES = {"flow", "compare"}
_DIAGRAM_ICONS = {"video", "clips", "scissors", "calendar", "clock", "eye", "users", "chart", "mic", "upload", "check", "sparkles"}
_CHECK_CELL_RE = re.compile(r"(<td[^>]*>)(.*?)(</td>)", re.I | re.S)
_IMAGE_WITH_EM_CAPTION_RE = re.compile(r"<p>\s*(<img\b[^>]*>)\s*</p>\s*<p>\s*<em>(.*?)</em>\s*</p>", re.I | re.S)
_IMAGE_RE = re.compile(r"<p>\s*(<img\b[^>]*>)\s*</p>", re.I)
_TABLE_RE = re.compile(r"(<table\b[^>]*>.*?</table>)", re.I | re.S)
_SPECIMEN_TAGS = {"short": "Short", "long": "Long-form"}
_PLACEHOLDER_ALT_VALUES = {"alt text", "image", "placeholder"}

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
        "video": '<path d="M15 10.5 21 7v10l-6-3.5V17a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v3.5Z"/>',
        "clips": '<rect x="4" y="5" width="14" height="10" rx="2"/><path d="M8 19h10a2 2 0 0 0 2-2V9"/><path d="m10 8 4 2.5-4 2.5V8Z"/>',
        "scissors": '<circle cx="6" cy="7" r="3"/><circle cx="6" cy="17" r="3"/><path d="M8.6 8.6 19 19"/><path d="M8.6 15.4 19 5"/>',
        "calendar": '<path d="M8 2v4"/><path d="M16 2v4"/><rect x="3" y="4" width="18" height="18" rx="2"/><path d="M3 10h18"/>',
        "clock": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
        "eye": '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/>',
        "users": '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>',
        "chart": '<path d="M3 3v18h18"/><path d="m7 15 4-4 3 3 5-7"/>',
        "mic": '<rect x="9" y="2" width="6" height="12" rx="3"/><path d="M5 10a7 7 0 0 0 14 0"/><path d="M12 17v5"/><path d="M8 22h8"/>',
        "upload": '<path d="M12 16V3"/><path d="m7 8 5-5 5 5"/><path d="M20 16v3a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2v-3"/>',
        "check": '<path d="m20 6-11 11-5-5"/>',
        "up": '<path d="m5 15 7-7 7 7"/><path d="M12 8v13"/>',
        "down": '<path d="m19 9-7 7-7-7"/><path d="M12 3v13"/>',
        "flat": '<path d="M5 12h14"/>',
    }
    if name not in paths:
        name = "sparkles"
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
    if caption.lower() in _PLACEHOLDER_ALT_VALUES:
        return ""
    if not caption and alt.lower().strip() in _PLACEHOLDER_ALT_VALUES:
        return ""
    return caption or alt


def _figure_html(match: re.Match[str]) -> str:
    tag = match.group(1)
    attrs = _attrs_from_tag(tag)
    src = attrs.get("src", "")
    alt = attrs.get("alt", "")
    title = attrs.get("title", "")
    if alt.lower().strip() in _PLACEHOLDER_ALT_VALUES:
        alt = "Blog image"
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


def _limit_words(text: str, limit: int) -> str:
    words = re.findall(r"\S+", re.sub(r"\s+", " ", text or "").strip())
    return " ".join(words[:limit])


def _diagram_figure(inner_html: str, caption: str) -> str:
    safe_caption = escape((caption or "").strip())
    caption_html = f"<figcaption>{safe_caption}</figcaption>" if safe_caption else ""
    return f'<figure class="diagram-figure">{inner_html}{caption_html}</figure>'


def _flow_html(body: str, caption: str) -> str:
    steps: list[tuple[str, str, str]] = []
    for line in (body or "").splitlines():
        parts = [part.strip() for part in line.split("|")]
        if len(parts) != 3:
            continue
        icon, title, subtitle = parts
        icon = icon.lower() if icon.lower() in _DIAGRAM_ICONS else "sparkles"
        steps.append((icon, _limit_words(title, 4), _limit_words(subtitle, 8)))
    if len(steps) < 2:
        return ""
    steps = steps[:5]
    items = []
    for index, (icon, title, subtitle) in enumerate(steps, start=1):
        items.append(
            '<div class="flow-step">'
            f'<div class="flow-icon">{_svg_icon(icon)}</div>'
            f'<div class="flow-num">{index}</div>'
            '<div>'
            f'<div class="flow-title">{escape(title)}</div>'
            f'<div class="flow-subtitle">{escape(subtitle)}</div>'
            '</div>'
            '</div>'
        )
    return _diagram_figure(f'<div class="diagram-card flow-card">{"".join(items)}</div>', caption)


def _compare_direction(direction: str) -> str:
    direction = (direction or "").strip().lower()
    return direction if direction in {"up", "down", "flat"} else "flat"


def _compare_html(body: str, caption: str) -> str:
    metric_lines: list[list[str]] = []
    note = ""
    for line in (body or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.lower().startswith("note:"):
            note = stripped.split(":", 1)[1].strip()
            continue
        parts = [part.strip() for part in stripped.split("|")]
        if len(parts) == 4:
            metric_lines.append(parts)
    if len(metric_lines) < 2:
        return ""
    sides = []
    for icon, value, label, direction in metric_lines[:2]:
        icon = icon.lower() if icon.lower() in _DIAGRAM_ICONS else "chart"
        direction = _compare_direction(direction)
        direction_label = {"up": "Up", "down": "Down", "flat": "Flat"}[direction]
        sides.append(
            f'<div class="compare-side compare-side--{direction}">'
            f'<div class="compare-icon">{_svg_icon(icon)}</div>'
            f'<div class="compare-value">{escape(_limit_words(value, 3))}</div>'
            f'<div class="compare-label">{escape(_limit_words(label, 6))}</div>'
            f'<div class="compare-trend"><span>{_svg_icon(direction)}</span>{direction_label}</div>'
            '</div>'
        )
    note_html = f'<div class="compare-note">{escape(note)}</div>' if note else ""
    card = f'<div class="diagram-card compare-card">{sides[0]}<div class="compare-mark">≠</div>{sides[1]}{note_html}</div>'
    return _diagram_figure(card, caption)


def _component_html(component_type: str, title: str, body: str) -> str:
    safe_title = escape((title or "").strip())
    if component_type == "flow":
        return _flow_html(body, title)
    if component_type == "compare":
        return _compare_html(body, title)
    body_html = _render_markdown(body, expand_components=False)
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
        if component_type not in _CALLOUT_TYPES | _DIAGRAM_TYPES | {"key", "action", "steps"} | set(_SPECIMEN_TAGS):
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
