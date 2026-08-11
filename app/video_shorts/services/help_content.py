from __future__ import annotations

from html import escape
from pathlib import Path
import re


HELP_FAQ_DIR = Path(__file__).resolve().parent.parent / "content" / "help" / "faq"
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")


def _parse_frontmatter(raw: str) -> tuple[dict[str, str], str]:
    text = raw.lstrip()
    if not text.startswith("---\n"):
        return {}, raw

    _, remainder = text.split("---\n", 1)
    if "\n---\n" not in remainder:
        return {}, raw

    frontmatter, body = remainder.split("\n---\n", 1)
    meta: dict[str, str] = {}
    for line in frontmatter.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        meta[key.strip()] = value.strip()
    return meta, body


def _inline_html(text: str) -> str:
    escaped = escape(text.strip())
    return _BOLD_RE.sub(r"<strong>\1</strong>", escaped)


def _parse_blocks(body: str) -> list[dict[str, str]]:
    blocks: list[dict[str, str]] = []
    for chunk in re.split(r"\n\s*\n", body.strip()):
        lines = [line.rstrip() for line in chunk.splitlines() if line.strip()]
        if not lines:
            continue

        if all(line.lstrip().startswith("- ") for line in lines):
            items = [_inline_html(line.lstrip()[2:]) for line in lines]
            blocks.append({"type": "list", "items": items})
            continue

        if len(lines) == 1 and lines[0].startswith("## "):
            blocks.append({"type": "heading", "text": lines[0][3:].strip()})
            continue

        if len(lines) == 1 and lines[0].startswith("**") and lines[0].endswith("**"):
            blocks.append({"type": "question", "text": lines[0][2:-2].strip()})
            continue

        paragraph = " ".join(line.strip() for line in lines)
        blocks.append({"type": "paragraph", "html": _inline_html(paragraph)})

    return blocks


def _load_faq_entry(path: Path) -> dict[str, object]:
    raw = path.read_text(encoding="utf-8")
    meta, body = _parse_frontmatter(raw)
    slug = meta.get("slug") or path.stem
    title = meta.get("title") or slug.replace("-", " ").title()
    description = meta.get("description") or ""
    order_raw = meta.get("order") or "999"
    try:
        order = int(order_raw)
    except ValueError:
        order = 999

    return {
        "slug": slug,
        "title": title,
        "description": description,
        "order": order,
        "blocks": _parse_blocks(body),
    }


def get_faq_entries() -> list[dict[str, object]]:
    if not HELP_FAQ_DIR.exists():
        return []
    entries = [_load_faq_entry(path) for path in HELP_FAQ_DIR.glob("*.md")]
    return sorted(entries, key=lambda item: (int(item["order"]), str(item["title"]).lower()))


def get_faq_entry(slug: str) -> dict[str, object] | None:
    for entry in get_faq_entries():
        if entry["slug"] == slug:
            return entry
    return None
