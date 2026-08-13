from __future__ import annotations

from html import escape
from pathlib import Path
import re


HELP_FAQ_DIR = Path(__file__).resolve().parent.parent / "content" / "help" / "faq"
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_SLUG_RE = re.compile(r"[^a-z0-9]+")

HELP_CATEGORY_DEFS = [
    {
        "slug": "scheduling-publishing",
        "title": "Scheduling & Publishing",
        "description": "Schedule clips, time zones, publishing status and failed posts.",
        "icon": "schedule",
        "topics": ["Scheduling", "Publishing", "YouTube", "Instagram", "Account"],
    },
    {
        "slug": "channels-connections",
        "title": "Channels & Connections",
        "description": "YouTube, Instagram, Facebook and connected accounts.",
        "icon": "hub",
        "topics": ["YouTube", "Instagram", "Facebook", "Connections"],
    },
    {
        "slug": "creating-shorts",
        "title": "Creating Shorts",
        "description": "Generating clips, editor, captions, titles and templates.",
        "icon": "movie",
        "topics": ["Creating", "Captions", "Titles", "Templates"],
    },
    {
        "slug": "account-billing",
        "title": "Account & Billing",
        "description": "Plan, usage, account settings and team members.",
        "icon": "account_circle",
        "topics": ["Account", "Billing", "Plan", "Usage"],
    },
    {
        "slug": "troubleshooting",
        "title": "Troubleshooting",
        "description": "Uploads, transcription, processing and publishing errors.",
        "icon": "build_circle",
        "topics": ["Troubleshooting", "Uploads", "Transcription", "Errors"],
    },
]


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


def _slugify(text: str) -> str:
    normalized = _SLUG_RE.sub("-", text.strip().lower()).strip("-")
    return normalized or "item"


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


def _parse_list_like(value: str | None) -> list[str]:
    if not value:
        return []
    raw = value.strip()
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    return [item.strip().strip("\"'") for item in raw.split(",") if item.strip()]


def _infer_category_slug(slug: str, meta: dict[str, str]) -> str:
    explicit = (meta.get("category_slug") or meta.get("category") or "").strip()
    if explicit:
        normalized = _slugify(explicit)
        for category in HELP_CATEGORY_DEFS:
            if category["slug"] == normalized:
                return normalized
    if slug == "scheduling-and-publishing":
        return "scheduling-publishing"
    return "troubleshooting"


def _question_items_from_blocks(blocks: list[dict[str, object]]) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    intro_blocks: list[dict[str, object]] = []
    question_items: list[dict[str, object]] = []
    current_item: dict[str, object] | None = None

    for block in blocks:
        if block.get("type") == "question":
            if current_item:
                question_items.append(current_item)
            question_text = str(block.get("text") or "").strip()
            current_item = {
                "id": f"faq-{_slugify(question_text)}",
                "question": question_text,
                "blocks": [],
            }
            continue

        if current_item is None:
            intro_blocks.append(block)
        else:
            current_item["blocks"].append(block)

    if current_item:
        question_items.append(current_item)

    return intro_blocks, question_items


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

    blocks = _parse_blocks(body)
    intro_blocks, question_items = _question_items_from_blocks(blocks)
    category_slug = _infer_category_slug(slug, meta)
    topics = _parse_list_like(meta.get("topics"))

    return {
        "slug": slug,
        "title": title,
        "description": description,
        "order": order,
        "blocks": blocks,
        "intro_blocks": intro_blocks,
        "question_items": question_items,
        "question_count": len(question_items),
        "category_slug": category_slug,
        "topics": topics,
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


def get_faq_categories() -> list[dict[str, object]]:
    entries = get_faq_entries()
    by_category: dict[str, list[dict[str, object]]] = {}
    for entry in entries:
        category_slug = str(entry.get("category_slug") or "").strip()
        by_category.setdefault(category_slug, []).append(entry)

    categories: list[dict[str, object]] = []
    for category in HELP_CATEGORY_DEFS:
        category_entries = sorted(
            by_category.get(category["slug"], []),
            key=lambda item: (int(item.get("order", 999)), str(item.get("title", "")).lower()),
        )
        question_count = sum(int(item.get("question_count", 0) or 0) for item in category_entries)
        primary_entry = category_entries[0] if category_entries else None
        categories.append(
            {
                **category,
                "entries": category_entries,
                "article_count": len(category_entries),
                "question_count": question_count,
                "primary_entry": primary_entry,
            }
        )
    return categories


def build_faq_search_index(entries: list[dict[str, object]] | None = None) -> list[dict[str, str]]:
    search_entries: list[dict[str, str]] = []
    for entry in entries or get_faq_entries():
        category_slug = str(entry.get("category_slug") or "").strip()
        category = next((item for item in HELP_CATEGORY_DEFS if item["slug"] == category_slug), None)
        category_title = str(category["title"]) if category else str(entry.get("title") or "")
        for question in entry.get("question_items") or []:
            question_id = str(question.get("id") or "").strip()
            question_text = str(question.get("question") or "").strip()
            if not question_id or not question_text:
                continue
            search_entries.append(
                {
                    "question": question_text,
                    "question_id": question_id,
                    "slug": str(entry.get("slug") or "").strip(),
                    "entry_title": str(entry.get("title") or "").strip(),
                    "category_title": category_title,
                    "href": f"/video_shorts/help/faq/{entry.get('slug')}/?question={question_id}#{question_id}",
                }
            )
    return search_entries
