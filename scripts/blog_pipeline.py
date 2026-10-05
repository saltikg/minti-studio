#!/home/ubuntu/apps/minti_studio/.venv/bin/python
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import quote
from urllib import request
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.video_shorts.services.blog_articles import _slugify  # noqa: E402
from app.video_shorts.services.blog_llm import (  # noqa: E402
    BLOG_MODEL_DESIGNER,
    BLOG_MODEL_REVIEWER,
    BLOG_MODEL_WRITER,
    call_json,
    log_usage,
)
from app.video_shorts.services.blog_images import (  # noqa: E402
    BLOG_IMAGE_COVER,
    BLOG_IMAGE_COVER_QUALITY,
    BLOG_IMAGE_INLINE,
    BLOG_IMAGE_INLINE_QUALITY,
    BlogImageResult,
    _add_logo_badge,
    generate_blog_image,
    generated_visual_filename,
    render_markdown_image,
)
from app.video_shorts.services.blog_articles import update_blog_article_status  # noqa: E402
from app.video_shorts.services.blog_notifications import BLOG_NOTIFY_EMAIL, send_blog_run_notification  # noqa: E402
from app.video_shorts.services.blog_pipeline import current_month_spend, json_dumps_compact  # noqa: E402
from app.video_shorts.services.blog_pipeline_runs import (  # noqa: E402
    create_run,
    finish_run,
    mark_run_notified,
    record_stage,
    run_was_notified,
    set_current_stage,
)
from app.video_shorts.services.db import get_db, get_db_readonly, table_columns  # noqa: E402


BLOG_REVIEW_PASS = int(os.getenv("BLOG_REVIEW_PASS", "85") or "85")
BLOG_RUN_MAX_USD = Decimal(os.getenv("BLOG_RUN_MAX_USD", "1.50") or "1.50")
BLOG_AUTO_PUBLISH = str(os.getenv("BLOG_AUTO_PUBLISH", "true")).strip().lower() not in {"0", "false", "no", "off"}
BLOG_AUTO_CATEGORIES = tuple(
    item.strip().lower()
    for item in os.getenv("BLOG_AUTO_CATEGORIES", "persona,craft,workflow").split(",")
    if item.strip()
)
STATIC_BLOG_ROOT = ROOT / "app" / "video_shorts" / "static" / "img" / "blog"
LIBRARY_ROOT = STATIC_BLOG_ROOT / "library"
MANIFEST_PATH = LIBRARY_ROOT / "screenshot_manifest.json"
CONTEXT_ROOT = ROOT / "app" / "video_shorts" / "blog_pipeline"
COVER_ARCHETYPES_PATH = CONTEXT_ROOT / "style" / "cover_archetypes.md"
CTA_URL = "https://mintistudio.com/video_shorts/register"
BASE_URL = (os.getenv("BLOG_PUBLIC_BASE_URL") or "https://mintistudio.com").rstrip("/")
COMPETITOR_FACTS_PATHS = (
    CONTEXT_ROOT / "competitor_facts.md",
    CONTEXT_ROOT / "comparison_facts.md",
)
SELF_DISCLAIMER_RE = re.compile(
    r"(not a claim that MintiStudio|do not assume Autopilot|should not be inferred from the price|"
    r"not a promised MintiStudio|confirm .*MintiStudio|MintiStudio .*does not (?:claim|promise|specify))",
    re.I,
)
IMAGE_MARKERS = ("IMAGE_1", "IMAGE_2", "IMAGE_3")
VISUAL_TYPES = {"screenshot", "flow", "compare", "generate"}
IMAGE_VISUAL_TYPES = {"screenshot", "generate"}
FLOW_BLOCK_RE = re.compile(r"(?ms)^:::flow(?:\s+[^\n]+)?\n(?P<body>.*?)\n:::\s*$")
COMPARE_BLOCK_RE = re.compile(r"(?ms)^:::compare(?:\s+[^\n]+)?\n(?P<body>.*?)\n:::\s*$")
IMAGE_COMMENT_RE = re.compile(r"<!--\s*IMAGE_([123])\s*-->")
MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]\n]*\]\([^\)\n]+\)")
PROTECTED_TOKEN_RE = re.compile(r"\[\[BLOCK_(\d+)\]\]")
IMAGE_PLACEHOLDER_VARIANT_RE = re.compile(
    r"<!--\s*IMAGE_([123])\s*-->|"
    r"\{\{\s*IMAGE_([123])\s*\}\}|"
    r"\[\[\s*IMAGE_([123])\s*\]\]|"
    r"\[\s*IMAGE_([123])\s*\]|"
    r"(?<![A-Za-z0-9_])IMAGE_([123])(?![A-Za-z0-9_])"
)
COVER_ARCHETYPE_LABELS = {
    "A": "Object still life, no people",
    "B": "Workflow landscape, no people",
    "C": "Metaphor object, no people",
    "D": "Phone close-up, hands only",
    "E": "Solo creator at work",
    "F": "Educator teaching",
    "G": "Coach on a video call",
    "H": "Podcast duo",
}
PEOPLE_COVER_ARCHETYPES = {"E", "F", "G", "H"}
CHARACTER_GENDERS = ("woman", "man", "nonbinary creator")
CHARACTER_AGES = ("25-34", "35-44", "45-54", "55-60")
CHARACTER_HAIR = ("short dark hair", "curly black hair", "silver cropped hair", "shoulder-length brown hair", "tied-back dark hair")
CHARACTER_OUTFITS = ("charcoal sweater", "soft white shirt", "deep mint overshirt", "warm gray jacket", "black studio tee", "burgundy cardigan")


@dataclass
class PipelineState:
    topic: dict[str, Any]
    run_id: int | None
    dry_run: bool
    run_cost: Decimal = Decimal("0")
    seq: int = 0


@dataclass(frozen=True)
class MaskedBlock:
    token: str
    content: str
    start: int
    end: int


def _row_to_dict(description, row) -> dict[str, Any]:
    return {description[index][0]: value for index, value in enumerate(row)}


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _word_count(markdown_text: str) -> int:
    text = re.sub(r"<!--\s*IMAGE_[123]\s*-->", " ", markdown_text or "")
    text = re.sub(r"\[[^\]]+\]\([^)]+\)", " ", text)
    text = re.sub(r"[#>*_`|:-]+", " ", text)
    return len(re.findall(r"\b[\w'-]+\b", text))


def _coerce_reading_time_minutes(value: Any, content_md: str) -> int:
    if isinstance(value, int) and value > 0:
        return value
    if value is not None:
        match = re.match(r"\s*(\d+)", str(value))
        if match:
            parsed = int(match.group(1))
            if parsed > 0:
                return parsed
    return max(1, (_word_count(content_md) + 199) // 200)


def _trim_at_word_boundary(value: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    trimmed = text[:limit].rstrip()
    boundary = max(trimmed.rfind(" "), trimmed.rfind("-"))
    if boundary >= max(1, int(limit * 0.6)):
        trimmed = trimmed[:boundary].rstrip()
    return trimmed.rstrip(" ,;:-")


def _normalize_marker(value: Any) -> str:
    text = str(value or "").strip()
    match = re.search(r"IMAGE_[123]", text)
    return match.group(0) if match else text


def _image_comment(marker: str) -> str:
    return f"<!-- {marker} -->"


def _normalize_image_placeholder_syntax(content: str) -> str:
    def replace(match: re.Match[str]) -> str:
        number = next(group for group in match.groups() if group)
        return _image_comment(f"IMAGE_{number}")

    return IMAGE_PLACEHOLDER_VARIANT_RE.sub(replace, content or "")


def _remove_duplicate_image_placeholders(content: str) -> str:
    seen: set[str] = set()

    def replace(match: re.Match[str]) -> str:
        marker = f"IMAGE_{match.group(1)}"
        if marker in seen:
            return ""
        seen.add(marker)
        return _image_comment(marker)

    content = IMAGE_COMMENT_RE.sub(replace, content or "")
    return re.sub(r"\n{3,}", "\n\n", content).strip()


def _is_h2(line: str) -> bool:
    return bool(re.match(r"^##\s+\S", line or ""))


def _is_final_section_heading(line: str) -> bool:
    if not _is_h2(line):
        return False
    heading = re.sub(r"^##\s+", "", line).strip().lower()
    return any(term in heading for term in ("final", "conclusion", "cta", "next step", "related"))


def _is_safe_paragraph_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    return not (
        stripped.startswith(("#", "|", ">", ":::", "<!--", "!["))
        or re.match(r"^([-*+]\s+|\d+\.\s+)", stripped)
    )


def _candidate_h2_indexes(lines: list[str]) -> list[int]:
    indexes = [index for index, line in enumerate(lines) if _is_h2(line) and not _is_final_section_heading(line)]
    return indexes or [index for index, line in enumerate(lines) if _is_h2(line)]


def _insert_position_after_first_paragraph(lines: list[str], start_index: int) -> int:
    index = start_index + 1
    while index < len(lines):
        line = lines[index]
        if _is_h2(line) or _is_final_section_heading(line):
            return index
        if _is_safe_paragraph_line(line):
            while index + 1 < len(lines) and _is_safe_paragraph_line(lines[index + 1]):
                index += 1
            return index + 1
        index += 1
    return len(lines)


def _fallback_insert_position(lines: list[str]) -> int:
    for index, line in enumerate(lines):
        if _is_final_section_heading(line):
            return index
    return len(lines)


def _insert_missing_image_placeholders(content: str, expected_markers: tuple[str, ...] = IMAGE_MARKERS) -> str:
    lines = (content or "").splitlines()
    present = {f"IMAGE_{match.group(1)}" for match in IMAGE_COMMENT_RE.finditer(content or "")}
    missing = [marker for marker in expected_markers if marker not in present]
    if not missing:
        return content

    h2_indexes = _candidate_h2_indexes(lines)
    fallback_index = _fallback_insert_position(lines)
    slot_positions = [_insert_position_after_first_paragraph(lines, index) for index in h2_indexes]
    if fallback_index not in slot_positions:
        slot_positions.append(fallback_index)
    slot_positions = sorted(set(slot_positions)) or [len(lines)]
    planned: dict[int, list[str]] = {}
    for marker in missing:
        marker_index = IMAGE_MARKERS.index(marker)
        slot_index = min(round(marker_index * (len(slot_positions) - 1) / 2), len(slot_positions) - 1)
        planned.setdefault(slot_positions[slot_index], []).append(marker)
    for insert_at in sorted(planned, reverse=True):
        insertion: list[str] = [""]
        for marker in sorted(planned[insert_at], key=IMAGE_MARKERS.index):
            insertion.extend([_image_comment(marker), ""])
        lines[insert_at:insert_at] = insertion
    return "\n".join(lines).strip()


def _previous_h2_and_first_paragraph(content: str, marker: str) -> tuple[str, str]:
    lines = (content or "").splitlines()
    marker_line = next((index for index, line in enumerate(lines) if _image_comment(marker) in line), len(lines))
    heading = ""
    heading_index = 0
    for index in range(marker_line, -1, -1):
        if index < len(lines) and _is_h2(lines[index]):
            heading = re.sub(r"^##\s+", "", lines[index]).strip()
            heading_index = index
            break
    paragraph = ""
    for line in lines[heading_index + 1 : marker_line]:
        if _is_safe_paragraph_line(line):
            paragraph = line.strip()
            break
    return heading or "MintiStudio workflow", paragraph


def _section_context_for_marker(content: str, marker: str) -> tuple[str, str]:
    if _image_comment(marker) in (content or ""):
        return _previous_h2_and_first_paragraph(content, marker)
    marker_index = IMAGE_MARKERS.index(marker) if marker in IMAGE_MARKERS else 0
    headings = list(re.finditer(r"(?m)^##\s+(.+?)\s*$", content or ""))
    if not headings:
        return "MintiStudio workflow", ""
    heading_match = headings[min(marker_index, len(headings) - 1)]
    heading = heading_match.group(1).strip()
    end = headings[min(marker_index + 1, len(headings) - 1)].start() if marker_index + 1 < len(headings) else len(content or "")
    paragraph = ""
    for line in (content or "")[heading_match.end() : end].splitlines():
        if _is_safe_paragraph_line(line):
            paragraph = line.strip()
            break
    return heading or "MintiStudio workflow", paragraph


def _generated_visual_for_marker(content: str, marker: str) -> dict[str, Any]:
    heading, paragraph = _section_context_for_marker(content, marker)
    prompt_detail = f" Section context: {paragraph[:320]}" if paragraph else ""
    return {
        "marker": marker,
        "type": "generate",
        "alt": f"{heading} illustration",
        "prompt": f"Landscape editorial visual for the section '{heading}'.{prompt_detail}",
    }


def _valid_flow_blocks(content: str) -> list[str]:
    valid: list[str] = []
    for match in FLOW_BLOCK_RE.finditer(content or ""):
        rows = []
        for line in match.group("body").splitlines():
            parts = [part.strip() for part in line.split("|")]
            if len(parts) == 3 and all(parts):
                rows.append(parts)
        if 2 <= len(rows) <= 5:
            valid.append(match.group(0))
    return valid


def _valid_compare_blocks(content: str) -> list[str]:
    valid: list[str] = []
    for match in COMPARE_BLOCK_RE.finditer(content or ""):
        rows = []
        for line in match.group("body").splitlines():
            stripped = line.strip()
            if not stripped or stripped.lower().startswith("note:"):
                continue
            parts = [part.strip() for part in stripped.split("|")]
            if len(parts) == 4 and parts[3].lower() in {"up", "down", "flat"} and all(parts[:3]):
                rows.append(parts)
        if len(rows) == 2:
            valid.append(match.group(0))
    return valid


def _expected_image_placeholders(visuals: list[dict[str, Any]]) -> list[str]:
    return [_image_comment(str(visual.get("marker"))) for visual in visuals if visual.get("type") in IMAGE_VISUAL_TYPES and visual.get("marker") in IMAGE_MARKERS]


def _compare_metric_values(content: str) -> list[str]:
    values: list[str] = []
    for match in COMPARE_BLOCK_RE.finditer(content or ""):
        for line in match.group("body").splitlines():
            stripped = line.strip()
            if not stripped or stripped.lower().startswith("note:"):
                continue
            parts = [part.strip() for part in stripped.split("|")]
            if len(parts) == 4:
                values.append(parts[1])
    return values


def _missing_compare_numbers(content: str) -> list[str]:
    article_text = COMPARE_BLOCK_RE.sub(" ", content or "")
    normalized_article = re.sub(r"[^a-z0-9+.%]+", " ", article_text.lower())
    missing: list[str] = []
    for value in _compare_metric_values(content):
        tokens = [token.lower().replace(",", "") for token in re.findall(r"[+]?\d[\d,.]*(?:k|m|%|x)?", value, flags=re.I)]
        if tokens and not any(token in normalized_article for token in tokens):
            missing.append(value)
    return missing


def _component_type_for_marker(content: str, marker: str) -> str | None:
    lines = (content or "").splitlines()
    marker_line = next((index for index, line in enumerate(lines) if _image_comment(marker) in line), None)
    if marker_line is not None:
        start = max(0, marker_line - 4)
        end = min(len(lines), marker_line + 5)
        window = "\n".join(lines[start:end])
        if _valid_flow_blocks(window):
            return "flow"
        if _valid_compare_blocks(window):
            return "compare"
    marker_index = IMAGE_MARKERS.index(marker) if marker in IMAGE_MARKERS else 0
    components: list[tuple[int, str]] = []
    for match in FLOW_BLOCK_RE.finditer(content or ""):
        components.append((match.start(), "flow"))
    for match in COMPARE_BLOCK_RE.finditer(content or ""):
        components.append((match.start(), "compare"))
    components.sort()
    if marker_line is None and marker_index < len(components):
        return components[marker_index][1]
    return None


def _token_set(text: str) -> set[str]:
    stop = {"a", "an", "and", "for", "from", "how", "into", "of", "on", "or", "the", "to", "with", "your"}
    return {token for token in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(token) > 2 and token not in stop}


def _best_screenshot_for_section(screenshots: list[dict[str, Any]], heading: str) -> dict[str, Any] | None:
    heading_tokens = _token_set(heading)
    best: tuple[int, dict[str, Any]] | None = None
    for item in screenshots:
        use_for = item.get("use_for") or []
        use_text = " ".join(str(value) for value in use_for)
        score = len(heading_tokens & _token_set(use_text))
        if score <= 0:
            continue
        if best is None or score > best[0]:
            best = (score, item)
    return best[1] if best else None


def _visual_section_heading(content: str, visual: dict[str, Any]) -> str:
    marker = str(visual.get("marker") or "")
    caption = str(visual.get("caption") or visual.get("alt") or "")
    heading, _paragraph = _section_context_for_marker(content, marker)
    return " ".join(part for part in (heading, caption) if part).strip()


def _strip_visual_marker(content: str, marker: str) -> str:
    marker = _normalize_marker(marker)
    if marker not in IMAGE_MARKERS:
        return content or ""
    content = re.sub(r"\n{0,2}<!--\s*" + re.escape(marker) + r"\s*-->\n{0,2}", "\n\n", content or "")
    return re.sub(r"\n{3,}", "\n\n", content).strip()


def _remove_invalid_markdown_images(content: str) -> tuple[str, list[str]]:
    notes: list[str] = []

    def replace(match: re.Match[str]) -> str:
        image = match.group(0)
        url_match = re.search(r"\]\(([^)\s]+)", image)
        url = str(url_match.group(1) if url_match else "")
        if re.search(r"/\.(?:png|jpe?g|webp)(?:[\"')\s]|$)", url, flags=re.I):
            notes.append(f"removed invalid markdown image URL: {url}")
            return ""
        if url.startswith("/video_shorts/static/img/blog/") and "/library/" not in url:
            notes.append(f"removed pre-rendered article image markdown: {url}")
            return ""
        return image

    cleaned = MARKDOWN_IMAGE_RE.sub(replace, content or "")
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip(), notes


def _limit_component_blocks(content: str, component_type: str, keep_count: int) -> tuple[str, list[str]]:
    pattern = FLOW_BLOCK_RE if component_type == "flow" else COMPARE_BLOCK_RE
    seen = 0
    notes: list[str] = []

    def replace(match: re.Match[str]) -> str:
        nonlocal seen
        seen += 1
        if seen <= keep_count:
            return match.group(0)
        notes.append(f"removed extra :::{component_type} block beyond visual plan")
        return ""

    cleaned = pattern.sub(replace, content or "")
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip(), notes


def _repair_visual_slots(content: str, visuals: list[dict[str, Any]], screenshots: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    available_by_id = {str(item.get("id") or ""): item for item in screenshots if item.get("id")}
    repaired: list[dict[str, Any]] = []
    notes: list[str] = []
    screenshot_count = 0
    for visual in visuals:
        current = dict(visual)
        marker = str(current.get("marker") or "")
        if current.get("type") == "screenshot":
            screenshot_id = str(current.get("screenshot_id") or "").strip()
            if screenshot_id in available_by_id and screenshot_count < 2:
                screenshot_count += 1
                repaired.append(current)
                continue
            component_type = _component_type_for_marker(content, marker)
            if component_type:
                current["type"] = component_type
                current.pop("screenshot_id", None)
                notes.append(f"{marker}: repaired screenshot slot with invalid id '{screenshot_id}' to {component_type}")
                repaired.append(current)
                continue
            heading, _paragraph = _section_context_for_marker(content, marker)
            candidate = _best_screenshot_for_section(screenshots, heading) if screenshot_count < 2 else None
            if candidate:
                current["screenshot_id"] = candidate["id"]
                current.setdefault("alt", candidate.get("alt") or heading)
                screenshot_count += 1
                notes.append(f"{marker}: repaired screenshot slot with invalid id '{screenshot_id}' to screenshot_id '{candidate['id']}'")
                repaired.append(current)
                continue
            replacement = _generated_visual_for_marker(content, marker)
            replacement["caption"] = current.get("caption") or replacement["alt"]
            notes.append(f"{marker}: repaired screenshot slot with invalid id '{screenshot_id}' to generate")
            repaired.append(replacement)
            continue
        if current.get("type") == "screenshot":
            screenshot_count += 1
        repaired.append(current)
    flow_count = 0
    compare_count = 0
    valid_flow_count = len(_valid_flow_blocks(content))
    valid_compare_count = len(_valid_compare_blocks(content))
    component_checked: list[dict[str, Any]] = []
    for visual in repaired:
        current = dict(visual)
        visual_type = current.get("type")
        if visual_type == "flow":
            flow_count += 1
            if flow_count > valid_flow_count:
                replacement = _generated_visual_for_marker(content, str(current.get("marker") or ""))
                replacement["caption"] = current.get("caption") or replacement["alt"]
                notes.append(f"{current.get('marker')}: repaired flow slot without valid :::flow block to generate")
                current = replacement
        elif visual_type == "compare":
            compare_count += 1
            if compare_count > valid_compare_count:
                replacement = _generated_visual_for_marker(content, str(current.get("marker") or ""))
                replacement["caption"] = current.get("caption") or replacement["alt"]
                notes.append(f"{current.get('marker')}: repaired compare slot without valid :::compare block to generate")
                current = replacement
        component_checked.append(current)
    capped: list[dict[str, Any]] = []
    screenshot_count = 0
    for visual in component_checked:
        current = dict(visual)
        if current.get("type") == "screenshot":
            screenshot_count += 1
            if screenshot_count > 2:
                replacement = _generated_visual_for_marker(content, str(current.get("marker") or ""))
                replacement["caption"] = current.get("caption") or replacement["alt"]
                notes.append(f"{current.get('marker')}: repaired screenshot cap overflow to generate")
                current = replacement
        capped.append(current)
    final: list[dict[str, Any]] = []
    screenshot_count = sum(1 for visual in capped if visual.get("type") == "screenshot")
    generate_seen = False
    used_screenshot_ids = {str(visual.get("screenshot_id") or "") for visual in capped if visual.get("type") == "screenshot"}
    for visual in capped:
        current = dict(visual)
        if current.get("type") != "generate":
            final.append(current)
            continue
        marker = str(current.get("marker") or "")
        if not generate_seen:
            generate_seen = True
            final.append(current)
            continue
        heading = _visual_section_heading(content, current)
        candidate = _best_screenshot_for_section(
            [item for item in screenshots if str(item.get("id") or "") not in used_screenshot_ids],
            heading,
        ) if screenshot_count < 2 else None
        if candidate:
            replacement = {
                **current,
                "type": "screenshot",
                "screenshot_id": candidate["id"],
                "alt": current.get("alt") or candidate.get("alt") or heading,
                "caption": current.get("caption") or candidate.get("alt") or heading,
            }
            replacement.pop("prompt", None)
            screenshot_count += 1
            used_screenshot_ids.add(str(candidate["id"]))
            notes.append(f"{marker}: repaired extra generate slot to screenshot_id '{candidate['id']}'")
            final.append(replacement)
        else:
            notes.append(f"{marker}: removed extra generate slot and marker")
    return final, notes


def _remove_unneeded_image_placeholders(content: str, keep_markers: set[str]) -> str:
    def replace(match: re.Match[str]) -> str:
        marker = f"IMAGE_{match.group(1)}"
        return _image_comment(marker) if marker in keep_markers else ""

    return re.sub(r"\n{3,}", "\n\n", IMAGE_COMMENT_RE.sub(replace, content or "")).strip()


def _protected_block_spans(content: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for pattern in (FLOW_BLOCK_RE, COMPARE_BLOCK_RE, IMAGE_COMMENT_RE, MARKDOWN_IMAGE_RE):
        spans.extend((match.start(), match.end()) for match in pattern.finditer(content or ""))
    spans.sort()
    filtered: list[tuple[int, int]] = []
    last_end = -1
    for start, end in spans:
        if start < last_end:
            continue
        filtered.append((start, end))
        last_end = end
    return filtered


def _mask_protected_blocks(content: str) -> tuple[str, list[MaskedBlock]]:
    source = content or ""
    spans = _protected_block_spans(source)
    if not spans:
        return source, []
    pieces: list[str] = []
    blocks: list[MaskedBlock] = []
    cursor = 0
    for index, (start, end) in enumerate(spans, start=1):
        token = f"[[BLOCK_{index}]]"
        pieces.append(source[cursor:start])
        pieces.append(token)
        blocks.append(MaskedBlock(token=token, content=source[start:end], start=start, end=end))
        cursor = end
    pieces.append(source[cursor:])
    return "".join(pieces), blocks


def _dedupe_mask_token(text: str, token: str) -> tuple[str, bool]:
    first = text.find(token)
    if first < 0:
        return text, False
    before = text[: first + len(token)]
    after = text[first + len(token) :]
    cleaned = after.replace(token, "")
    return before + cleaned, cleaned != after


def _reinsert_missing_token(source_masked: str, candidate: str, token: str, ordered_tokens: list[str]) -> str:
    source_index = source_masked.find(token)
    previous_tokens = [item for item in ordered_tokens if source_masked.find(item) < source_index and item in candidate]
    next_tokens = [item for item in ordered_tokens if source_masked.find(item) > source_index and item in candidate]
    if previous_tokens:
        previous = previous_tokens[-1]
        insert_at = candidate.find(previous) + len(previous)
        return candidate[:insert_at] + f"\n\n{token}" + candidate[insert_at:]
    if next_tokens:
        next_token = next_tokens[0]
        insert_at = candidate.find(next_token)
        return candidate[:insert_at] + f"{token}\n\n" + candidate[insert_at:]
    return (candidate.rstrip() + f"\n\n{token}").strip()


def _restore_masked_blocks(source_masked: str, candidate_masked: str, blocks: list[MaskedBlock]) -> tuple[str, list[str]]:
    candidate = str(candidate_masked or "")
    repairs: list[str] = []
    ordered_tokens = [block.token for block in blocks]
    for token in ordered_tokens:
        candidate, removed_duplicate = _dedupe_mask_token(candidate, token)
        if removed_duplicate:
            repairs.append(f"{token}: removed duplicate protected token")
    for token in ordered_tokens:
        if token not in candidate:
            candidate = _reinsert_missing_token(source_masked, candidate, token, ordered_tokens)
            repairs.append(f"{token}: reinserted missing protected token")
    restored = candidate
    for block in blocks:
        restored = restored.replace(block.token, block.content, 1)
    return restored, repairs


def _masked_article(article: dict[str, Any]) -> tuple[dict[str, Any], str, list[MaskedBlock]]:
    masked_article = dict(article or {})
    masked_content, blocks = _mask_protected_blocks(str(masked_article.get("content_md") or ""))
    masked_article["content_md"] = masked_content
    return masked_article, masked_content, blocks


def _restore_candidate_article(candidate: dict[str, Any], source_masked: str, blocks: list[MaskedBlock]) -> tuple[dict[str, Any], list[str]]:
    restored = dict(candidate or {})
    content = restored.get("content_md") or restored.get("content") or restored.get("content_markdown") or ""
    restored_content, repairs = _restore_masked_blocks(source_masked, str(content), blocks)
    restored["content_md"] = restored_content
    return restored, repairs


def _normalize_image_placeholders_and_visuals(article: dict[str, Any]) -> dict[str, Any]:
    content = _normalize_image_placeholder_syntax(str(article.get("content_md") or ""))
    content = _remove_duplicate_image_placeholders(content)
    content, image_repair_notes = _remove_invalid_markdown_images(content)
    screenshots = _load_manifest()

    visual_by_marker: dict[str, dict[str, Any]] = {}
    for visual in article.get("visuals") or []:
        marker = _normalize_marker(visual.get("marker") or visual.get("id"))
        if marker not in IMAGE_MARKERS or marker in visual_by_marker:
            continue
        normalized = dict(visual)
        normalized["marker"] = marker
        if normalized.get("type") not in VISUAL_TYPES:
            normalized["type"] = "generate"
        if normalized.get("type") == "generate" and not normalized.get("prompt"):
            generated = _generated_visual_for_marker(content, marker)
            normalized["prompt"] = normalized.get("brief") or normalized.get("description") or generated["prompt"]
            normalized.setdefault("alt", generated["alt"])
        visual_by_marker[marker] = normalized

    normalized_visuals: list[dict[str, Any]] = []
    for marker in IMAGE_MARKERS:
        visual = dict(visual_by_marker.get(marker) or _generated_visual_for_marker(content, marker))
        normalized_visuals.append(visual)
    normalized_visuals, repair_notes = _repair_visual_slots(content, normalized_visuals, screenshots)
    repair_notes = image_repair_notes + repair_notes
    if repair_notes:
        existing_repairs = list(article.get("visual_repairs") or [])
        for note in repair_notes:
            if note not in existing_repairs:
                existing_repairs.append(note)
        article["visual_repairs"] = existing_repairs
    article["visuals"] = normalized_visuals
    flow_keep = sum(1 for visual in normalized_visuals if visual.get("type") == "flow")
    compare_keep = sum(1 for visual in normalized_visuals if visual.get("type") == "compare")
    content, flow_notes = _limit_component_blocks(content, "flow", flow_keep)
    content, compare_notes = _limit_component_blocks(content, "compare", compare_keep)
    if flow_notes or compare_notes:
        existing_repairs = list(article.get("visual_repairs") or [])
        for note in flow_notes + compare_notes:
            if note not in existing_repairs:
                existing_repairs.append(note)
        article["visual_repairs"] = existing_repairs
    image_markers = tuple(visual["marker"] for visual in normalized_visuals if visual.get("type") in IMAGE_VISUAL_TYPES)
    content = _remove_unneeded_image_placeholders(content, set(image_markers))
    for marker in set(IMAGE_MARKERS) - set(image_markers):
        content = _strip_visual_marker(content, marker)
    content = _insert_missing_image_placeholders(content, image_markers)
    article["content_md"] = content
    return article


def _normalize_article_payload(article: dict[str, Any]) -> dict[str, Any]:
    article = dict(article or {})
    if not article.get("content_md"):
        article["content_md"] = article.get("content") or article.get("content_markdown")
    if not article.get("summary") and article.get("excerpt"):
        article["summary"] = article.get("excerpt")
    article["slug"] = _slugify(article.get("slug") or article.get("title") or "article")
    content = str(article.get("content_md") or "")
    visuals: list[dict[str, Any]] = []
    for visual in article.get("visuals") or []:
        normalized = dict(visual)
        marker = _normalize_marker(normalized.get("marker") or normalized.get("id"))
        normalized["marker"] = marker
        if normalized.get("type") not in VISUAL_TYPES:
            normalized["type"] = "generate"
        if normalized.get("type") == "generate" and not normalized.get("prompt"):
            normalized["prompt"] = normalized.get("brief") or normalized.get("description") or ""
        visuals.append(normalized)
    article["visuals"] = visuals
    article["content_md"] = content
    article = _normalize_image_placeholders_and_visuals(article)
    if not article.get("meta_title"):
        article["meta_title"] = article.get("title") or ""
    if not article.get("meta_description"):
        article["meta_description"] = article.get("summary") or ""
    article["meta_title"] = _trim_at_word_boundary(article.get("meta_title"), 60)
    article["meta_description"] = _trim_at_word_boundary(article.get("meta_description"), 155)
    article["reading_time"] = _coerce_reading_time_minutes(article.get("reading_time"), str(article.get("content_md") or ""))
    return article


def _review_score(review: dict[str, Any]) -> int:
    for key in ("total", "score", "overall_score"):
        value = review.get(key)
        if value is not None:
            try:
                parsed = int(value)
                return parsed * 10 if key == "overall_score" and parsed <= 10 else parsed
            except (TypeError, ValueError):
                pass
    scores = review.get("scores")
    if isinstance(scores, dict):
        total = 0
        for value in scores.values():
            try:
                total += int(value)
            except (TypeError, ValueError):
                pass
        return total
    return 0


def _h2_headings(markdown_text: str) -> list[str]:
    headings = []
    for match in re.finditer(r"(?m)^##\s+(.+?)\s*$", markdown_text or ""):
        heading = re.sub(r"\s+", " ", match.group(1)).strip().lower()
        if heading:
            headings.append(heading)
    return headings


def _revision_rejection_reason(previous: dict[str, Any], candidate: dict[str, Any]) -> str | None:
    previous_content, _previous_blocks = _mask_protected_blocks(str(previous.get("content_md") or ""))
    candidate_content, _candidate_blocks = _mask_protected_blocks(str(candidate.get("content_md") or ""))
    previous_words = _word_count(previous_content)
    candidate_words = _word_count(candidate_content)
    if previous_words and candidate_words < previous_words * Decimal("0.75"):
        return f"word count dropped more than 25% ({previous_words} -> {candidate_words})"
    if CTA_URL in previous_content and CTA_URL not in candidate_content:
        return "CTA link disappeared"
    previous_headings = _h2_headings(previous_content)
    if previous_headings:
        candidate_headings = set(_h2_headings(candidate_content))
        survived = sum(1 for heading in previous_headings if heading in candidate_headings)
        if survived / len(previous_headings) < 0.8:
            return f"fewer than 80% of previous H2 headings survived ({survived}/{len(previous_headings)})"
    return None


def _load_manifest() -> list[dict[str, Any]]:
    raw = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    items: list[dict[str, Any]] = []
    for item in raw:
        filename = str(item.get("filename") or "")
        exists = bool(filename and (LIBRARY_ROOT / filename).is_file())
        normalized = dict(item)
        normalized["exists"] = exists
        normalized["hold"] = bool(item.get("hold"))
        if exists and not normalized["hold"]:
            items.append(normalized)
    return items


def _all_manifest_entries() -> list[dict[str, Any]]:
    raw = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    for item in raw:
        filename = str(item.get("filename") or "")
        item["exists"] = bool(filename and (LIBRARY_ROOT / filename).is_file())
        item["hold"] = bool(item.get("hold"))
    return raw


def _published_articles(limit: int | None = None, *, include_content: bool = False) -> list[dict[str, Any]]:
    conn = get_db_readonly()
    try:
        content_sql = ", content" if include_content else ""
        sql = f"""
            SELECT title, slug, summary{content_sql}
            FROM blog_articles
            WHERE status = 'published'
            ORDER BY published_at DESC NULLS LAST, created_at DESC
        """
        params: list[Any] = []
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        rows = conn.execute(sql, params).fetchall()
        articles = []
        for row in rows:
            item = {"title": row[0], "slug": row[1], "summary": row[2], "url": _canonical_blog_url(str(row[1] or ""))}
            if include_content:
                item["content"] = row[3]
            articles.append(item)
        return articles
    finally:
        conn.close()


def _recent_cover_metadata(limit: int = 6) -> list[dict[str, Any]]:
    conn = get_db_readonly()
    try:
        columns = table_columns(conn, "blog_articles")
        if "cover_archetype" not in columns:
            return []
        character_sql = "cover_character_json" if "cover_character_json" in columns else "NULL AS cover_character_json"
        rows = conn.execute(
            f"""
            SELECT cover_archetype, {character_sql}
            FROM blog_articles
            WHERE status = 'published'
              AND cover_archetype IS NOT NULL
              AND cover_archetype <> ''
            ORDER BY published_at DESC NULLS LAST, created_at DESC
            LIMIT ?
            """,
            [int(limit)],
        ).fetchall()
    finally:
        conn.close()
    items: list[dict[str, Any]] = []
    for archetype, character_json in rows:
        character: dict[str, Any] = {}
        if character_json:
            try:
                character = json.loads(character_json) if isinstance(character_json, str) else dict(character_json)
            except Exception:
                character = {}
        items.append({"archetype": str(archetype or "").upper(), "character": character})
    return items


def _stable_index(seed: str, length: int) -> int:
    if length <= 0:
        return 0
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % length


def _cover_archetype_proposals(article: dict[str, Any], topic: dict[str, Any] | None = None) -> list[str]:
    cover = article.get("cover") if isinstance(article.get("cover"), dict) else {}
    raw = cover.get("archetypes") or cover.get("archetype_proposals") or cover.get("top_archetypes") or []
    if isinstance(raw, str):
        raw = re.findall(r"\b[A-H]\b", raw.upper())
    proposals: list[str] = []
    for item in raw if isinstance(raw, list) else []:
        code = str(item or "").strip().upper()[:1]
        if code in COVER_ARCHETYPE_LABELS and code not in proposals:
            proposals.append(code)
    if proposals:
        return proposals[:3]
    text = " ".join(str(value or "") for value in (article.get("title"), (topic or {}).get("title"), (topic or {}).get("category"), (topic or {}).get("brief"))).lower()
    if any(word in text for word in ("coach", "client", "objection", "sales call")):
        return ["G", "C", "A"]
    if any(word in text for word in ("podcast", "interview", "hosts")):
        return ["H", "B", "A"]
    if any(word in text for word in ("teacher", "educator", "lesson", "course", "webinar")):
        return ["F", "B", "A"]
    if any(word in text for word in ("workflow", "calendar", "publish", "batch")):
        return ["B", "A", "C"]
    if any(word in text for word in ("metric", "views", "subscriber", "conversion")):
        return ["C", "A", "B"]
    return ["A", "C", "B"]


def _choose_cover_archetype(proposals: list[str]) -> str:
    recent = _recent_cover_metadata(6)
    last_three = {item["archetype"] for item in recent[:3]}
    people_count = sum(1 for item in recent if item["archetype"] in PEOPLE_COVER_ARCHETYPES)
    valid = [item for item in proposals if item in COVER_ARCHETYPE_LABELS]
    candidates = list(dict.fromkeys(valid + ["A", "C", "B"]))
    for code in candidates:
        if code in last_three:
            continue
        if code in PEOPLE_COVER_ARCHETYPES and people_count >= 3 and any(item not in PEOPLE_COVER_ARCHETYPES for item in candidates):
            continue
        return code
    return candidates[0]


def _choose_cover_character(article: dict[str, Any], archetype: str) -> dict[str, str] | None:
    if archetype not in PEOPLE_COVER_ARCHETYPES:
        return None
    seed = f"{article.get('slug') or article.get('title') or ''}:{archetype}"
    recent = _recent_cover_metadata(3)
    recent_burgundy = sum(
        1
        for item in recent
        if "burgundy" in json.dumps(item.get("character") or {}, ensure_ascii=False).lower()
    )
    outfits = [item for item in CHARACTER_OUTFITS if "burgundy" not in item or recent_burgundy < 1]
    outfit = outfits[_stable_index(seed + ":outfit", len(outfits))]
    return {
        "gender": CHARACTER_GENDERS[_stable_index(seed + ":gender", len(CHARACTER_GENDERS))],
        "age_range": CHARACTER_AGES[_stable_index(seed + ":age", len(CHARACTER_AGES))],
        "hair": CHARACTER_HAIR[_stable_index(seed + ":hair", len(CHARACTER_HAIR))],
        "outfit": outfit,
        "face_style": "featureless face",
    }


def _prepare_cover_plan(article: dict[str, Any], topic: dict[str, Any] | None = None) -> dict[str, Any]:
    article = dict(article or {})
    cover = dict(article.get("cover") if isinstance(article.get("cover"), dict) else {})
    proposals = _cover_archetype_proposals(article, topic)
    archetype = str(cover.get("archetype") or "").strip().upper()[:1]
    if archetype not in COVER_ARCHETYPE_LABELS:
        archetype = _choose_cover_archetype(proposals)
    character = cover.get("character") if isinstance(cover.get("character"), dict) else None
    if archetype in PEOPLE_COVER_ARCHETYPES and not character:
        character = _choose_cover_character(article, archetype)
    cover["archetypes"] = proposals
    cover["archetype"] = archetype
    cover["archetype_label"] = COVER_ARCHETYPE_LABELS[archetype]
    if character:
        cover["character"] = character
    else:
        cover.pop("character", None)
    article["cover"] = cover
    article["cover_archetype"] = archetype
    article["cover_character"] = character
    return article


def _competitor_facts_exists() -> bool:
    return any(path.is_file() and path.read_text(encoding="utf-8").strip() for path in COMPETITOR_FACTS_PATHS)


def _stable_prefix() -> dict[str, Any]:
    return {
        "facts": _read_text(CONTEXT_ROOT / "minti_facts.md"),
        "style_guide": _read_text(CONTEXT_ROOT / "style_guide.md"),
        "cover_archetypes": _read_text(COVER_ARCHETYPES_PATH) if COVER_ARCHETYPES_PATH.is_file() else "",
        "example_articles": _published_articles(1, include_content=True),
        "screenshots": _load_manifest(),
        "allowed_components": {
            "callouts": [":::key\\nOne key sentence.\\n:::", ":::info\\nBody\\n:::", ":::tip\\nBody\\n:::", ":::warning\\nBody\\n:::", ":::action\\nCTA sentence.\\n:::"],
            "steps": ":::steps\\n### Step title\\nOne or two sentences.\\n\\n### Next step title\\nOne or two sentences.\\n:::",
            "flow": ":::flow Optional short caption\\nvideo | Long video | Your full recording\\nclips | 5 Shorts | Best moments, captioned\\ncalendar | Weekly calendar | Mon, Wed, Fri\\n:::",
            "compare": ":::compare Optional short caption\\neye | 100K | Views on Shorts | up\\nusers | +120 | New subscribers | flat\\nnote: Views are great. Growth comes from turning viewers into subscribers.\\n:::",
            "specimens": [":::short\\nShort example text.\\n:::", ":::long\\nLong-form example text.\\n:::"],
            "tables": "Markdown pipe tables are allowed and encouraged whenever options are compared.",
            "youtube": "[youtube: https://www.youtube.com/watch?v=VIDEO_ID]",
            "images": "Markdown images only with short title captions: ![alt](/video_shorts/static/img/blog/slug/file.png \"Caption under 12 words\")",
        },
    }


def _screenshot_prompt_list(screenshots: list[dict[str, Any]]) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for item in screenshots:
        use_for = ", ".join(str(value) for value in (item.get("use_for") or [])[:4])
        description = str(item.get("alt") or item.get("title") or use_for or item.get("filename") or "").strip()
        items.append({"id": str(item.get("id") or ""), "description": description[:160]})
    return items


def _writer_schema_violations(article: dict[str, Any], screenshots: list[dict[str, Any]]) -> list[str]:
    violations: list[str] = []
    visuals = article.get("visuals") if isinstance(article.get("visuals"), list) else []
    available_ids = {str(item.get("id") or "") for item in screenshots if item.get("id")}
    if len(visuals) != 3:
        violations.append(f"visuals must contain exactly 3 items before repair; got {len(visuals)}")
    seen_markers: set[str] = set()
    for index, visual in enumerate(visuals, start=1):
        if not isinstance(visual, dict):
            violations.append(f"visual {index} is not an object")
            continue
        marker = _normalize_marker(visual.get("marker") or visual.get("id"))
        if marker not in IMAGE_MARKERS:
            violations.append(f"visual {index} marker must be one of {', '.join(IMAGE_MARKERS)}")
        elif marker in seen_markers:
            violations.append(f"{marker} is duplicated")
        seen_markers.add(marker)
        visual_type = str(visual.get("type") or "")
        if visual_type not in VISUAL_TYPES:
            violations.append(f"{marker or index}: type must be one of {', '.join(sorted(VISUAL_TYPES))}")
            continue
        if visual_type == "screenshot":
            screenshot_id = str(visual.get("screenshot_id") or "").strip()
            if screenshot_id not in available_ids:
                violations.append(f"{marker}: screenshot_id must be a listed available id, got {screenshot_id!r}")
        alt_text = str(visual.get("alt") or "").strip().lower()
        caption_text = str(visual.get("caption") or "").strip().lower()
        if alt_text in {"alt text", "image", "placeholder"}:
            violations.append(f"{marker}: alt must be a real descriptive phrase, not {visual.get('alt')!r}")
        if caption_text in {"alt text", "image", "placeholder"}:
            violations.append(f"{marker}: caption must be real or omitted, not {visual.get('caption')!r}")
    generate_count = sum(1 for visual in visuals if isinstance(visual, dict) and visual.get("type") == "generate")
    if generate_count > 1:
        violations.append(f"generate may be used at most once before repair; got {generate_count}")
    return violations


def _pop_topic(conn, topic_id: int | None, *, dry_run: bool = False) -> dict[str, Any] | None:
    columns = table_columns(conn, "blog_topics")
    source_summary_sql = "source_summary" if "source_summary" in columns else "NULL AS source_summary"
    fit_breakdown_sql = "fit_breakdown" if "fit_breakdown" in columns else "NULL AS fit_breakdown"
    adapted_from_sql = "adapted_from" if "adapted_from" in columns else "NULL AS adapted_from"
    if topic_id:
        row = conn.execute(
            f"""
            SELECT id, title, primary_keyword, category, intent, brief, source_type, source_name,
                   source_url, source_title, fit_score, judge_reason, {source_summary_sql},
                   {fit_breakdown_sql}, {adapted_from_sql}
            FROM blog_topics
            WHERE id = ?
              AND status = 'queued'
            FOR UPDATE SKIP LOCKED
            LIMIT 1
            """,
            [int(topic_id)],
        ).fetchone()
    else:
        auto_categories = BLOG_AUTO_CATEGORIES or ("persona", "craft", "workflow")
        placeholders = ", ".join(["?"] * len(auto_categories))
        row = conn.execute(
            f"""
            SELECT id, title, primary_keyword, category, intent, brief, source_type, source_name,
                   source_url, source_title, fit_score, judge_reason, {source_summary_sql},
                   {fit_breakdown_sql}, {adapted_from_sql}
            FROM blog_topics
            WHERE status = 'queued'
              AND created_at >= CURRENT_TIMESTAMP - INTERVAL '45 days'
              AND LOWER(COALESCE(category, '')) IN ({placeholders})
            ORDER BY fit_score DESC NULLS LAST, created_at ASC
            FOR UPDATE SKIP LOCKED
            LIMIT 1
            """,
            list(auto_categories),
        ).fetchone()
    if not row:
        return None
    topic = _row_to_dict(conn.description, row)
    if not dry_run:
        conn.execute("UPDATE blog_topics SET status = 'in_production' WHERE id = ?", [int(topic["id"])])
    return topic


def _check_run_budget(state: PipelineState) -> None:
    if state.run_cost >= BLOG_RUN_MAX_USD:
        raise RuntimeError(f"BLOG_RUN_MAX_USD reached: spent ${state.run_cost} of ${BLOG_RUN_MAX_USD}")


def _call_stage(state: PipelineState, stage: str, model: str, system_prompt: str, payload: dict[str, Any]) -> dict[str, Any]:
    _check_run_budget(state)
    usage_stage = f"{stage}_dry" if state.dry_run else stage
    result = call_json(usage_stage, model=model, system_prompt=system_prompt, user_prompt=json_dumps_compact(payload))
    state.run_cost += result.cost_usd
    log_usage(usage_stage, result, topic_id=int(state.topic["id"]), run_id=state.run_id)
    return json.loads(result.content), result.cost_usd


def _internal_link_urls(articles: list[dict[str, Any]]) -> set[str]:
    return {str(article["url"]) for article in articles}


def _canonical_blog_url(slug: str) -> str:
    from app import create_app
    from flask import url_for

    app = create_app()
    with app.test_request_context(base_url=BASE_URL):
        return url_for("video_shorts_bp.blog_article", slug=slug, _external=True)


def _article_links(markdown_text: str) -> list[str]:
    return re.findall(r"\[[^\]]+\]\((https?://[^)]+)\)", markdown_text or "")


def _http_status(url: str, *, timeout: int = 5) -> int | None:
    headers = {"User-Agent": "MintiStudioBlogPipeline/1.0 (+https://mintistudio.com)"}
    for method in ("HEAD", "GET"):
        try:
            req = request.Request(url, method=method, headers=headers)
            with request.urlopen(req, timeout=timeout) as resp:
                return int(resp.status)
        except HTTPError as exc:
            return int(exc.code)
        except Exception:
            if method == "GET":
                return None
    return None


def _indexability_status(url: str, *, timeout: int = 8) -> dict[str, Any]:
    headers = {"User-Agent": "MintiStudioBlogPipeline/1.0 (+https://mintistudio.com)"}
    result: dict[str, Any] = {
        "url": url,
        "status": None,
        "final_url": None,
        "x_robots_tag": None,
        "meta_robots": None,
        "canonical": None,
        "ok": False,
        "reasons": [],
    }
    try:
        req = request.Request(url, headers=headers)
        with request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(400_000).decode("utf-8", "ignore")
            result["status"] = int(resp.status)
            result["final_url"] = resp.geturl()
            result["x_robots_tag"] = resp.headers.get("X-Robots-Tag")
    except HTTPError as exc:
        result["status"] = int(exc.code)
        result["final_url"] = exc.geturl()
        result["x_robots_tag"] = exc.headers.get("X-Robots-Tag")
        body = exc.read(400_000).decode("utf-8", "ignore")
    except Exception as exc:
        result["reasons"].append(f"fetch failed: {str(exc)[:200]}")
        return result

    meta_match = re.search(
        r"<meta[^>]+name=[\"']robots[\"'][^>]*content=[\"']([^\"']+)[\"'][^>]*>",
        body,
        flags=re.I,
    )
    if not meta_match:
        meta_match = re.search(
            r"<meta[^>]+content=[\"']([^\"']+)[\"'][^>]*name=[\"']robots[\"'][^>]*>",
            body,
            flags=re.I,
        )
    canonical_match = re.search(
        r"<link[^>]+rel=[\"']canonical[\"'][^>]*href=[\"']([^\"']+)[\"'][^>]*>",
        body,
        flags=re.I,
    )
    if not canonical_match:
        canonical_match = re.search(
            r"<link[^>]+href=[\"']([^\"']+)[\"'][^>]*rel=[\"']canonical[\"'][^>]*>",
            body,
            flags=re.I,
        )
    result["meta_robots"] = meta_match.group(1).strip() if meta_match else None
    result["canonical"] = canonical_match.group(1).strip() if canonical_match else None

    if result["status"] != 200:
        result["reasons"].append(f"HTTP status {result['status'] or 'error'}")
    x_robots = str(result.get("x_robots_tag") or "").lower()
    meta_robots = str(result.get("meta_robots") or "").lower()
    if "noindex" in x_robots:
        result["reasons"].append(f"X-Robots-Tag contains noindex: {result['x_robots_tag']}")
    if "noindex" in meta_robots:
        result["reasons"].append(f"meta robots contains noindex: {result['meta_robots']}")
    if result.get("canonical") != url:
        result["reasons"].append(f"canonical mismatch: {result.get('canonical') or 'missing'}")
    result["ok"] = not result["reasons"]
    return result


def _public_blog_url(slug: str) -> str:
    return f"{BASE_URL}/video_shorts/blog/{quote(str(slug).strip())}/"


def _admin_edit_url(article_id: int | None) -> str | None:
    return f"{BASE_URL}/video_shorts/admin/blog/{int(article_id)}/edit" if article_id else None


def _run_detail_url(run_id: int | None) -> str | None:
    return f"{BASE_URL}/video_shorts/admin/blog-pipeline/runs/{int(run_id)}" if run_id else None


def _sitemap_status(public_url: str) -> dict[str, Any]:
    sitemap_url = f"{BASE_URL}/sitemap.xml"
    try:
        req = request.Request(sitemap_url, headers={"User-Agent": "MintiStudioBlogPipeline/1.0"})
        with request.urlopen(req, timeout=5) as resp:
            body = resp.read(2_000_000).decode("utf-8", "ignore")
            return {"url": sitemap_url, "status": int(resp.status), "contains_article": public_url in body}
    except HTTPError as exc:
        return {"url": sitemap_url, "status": int(exc.code), "contains_article": False}
    except Exception as exc:
        return {"url": sitemap_url, "status": None, "contains_article": False, "error": str(exc)[:200]}


def _slug_exists(slug: str) -> bool:
    conn = get_db_readonly()
    try:
        row = conn.execute("SELECT 1 FROM blog_articles WHERE slug = ? LIMIT 1", [slug]).fetchone()
        return bool(row)
    finally:
        conn.close()


def _unique_slug(slug: str) -> str:
    base = _slugify(slug)
    candidate = base
    counter = 2
    while _slug_blocks_new_draft(candidate):
        candidate = f"{base}-{counter}"
        counter += 1
    return candidate


def _slug_blocks_new_draft(slug: str) -> bool:
    conn = get_db_readonly()
    try:
        row = conn.execute(
            """
            SELECT status, import_source
            FROM blog_articles
            WHERE slug = ?
            LIMIT 1
            """,
            [slug],
        ).fetchone()
        if not row:
            return False
        return not (str(row[0] or "") == "archived" and str(row[1] or "") == "blog_pipeline")
    finally:
        conn.close()


def _rename_archived_pipeline_slug_conflict(conn, slug: str) -> str | None:
    row = conn.execute(
        """
        SELECT id
        FROM blog_articles
        WHERE slug = ?
          AND status = 'archived'
          AND import_source = 'blog_pipeline'
        LIMIT 1
        """,
        [slug],
    ).fetchone()
    if not row:
        return None
    archived_id = int(row[0])
    archived_slug = f"{slug}-archived-{archived_id}"
    conn.execute(
        """
        UPDATE blog_articles
        SET slug = ?
        WHERE id = ?
        """,
        [archived_slug, archived_id],
    )
    return archived_slug


def _code_checks(article: dict[str, Any], screenshots: list[dict[str, Any]], published: list[dict[str, Any]]) -> dict[str, Any]:
    normalized_article = _normalize_article_payload(article)
    article.clear()
    article.update(normalized_article)
    blocking_issues: list[str] = []
    fix_items: list[str] = []
    fixed: dict[str, Any] = {}
    slug = _unique_slug(article.get("slug") or article.get("title") or "article")
    if slug != article.get("slug"):
        fixed["slug"] = slug
        article["slug"] = slug
    content = str(article.get("content_md") or "")
    word_count = _word_count(content)
    if word_count < 100:
        blocking_issues.append("article body is missing or too short")
    if CTA_URL not in content:
        blocking_issues.append("CTA link is missing")
    if re.search(r"(?m)^\s*IMAGE_[123]\s*$", content):
        fixed["image_placeholders"] = "normalized bare IMAGE_n placeholders to exact HTML comments"
    visuals = article.get("visuals") or []
    expected_placeholders = sorted(_expected_image_placeholders(visuals))
    placeholders = sorted(re.findall(r"<!--\s*IMAGE_[123]\s*-->", content))
    if placeholders != expected_placeholders:
        fixed["image_placeholders"] = "normalized image placeholders to match screenshot/generate visual slots"
    if re.search(r"<(iframe|script|style|div|span|img)\b", content, flags=re.I):
        blocking_issues.append("raw HTML is not allowed except IMAGE comments")
    word_count = _word_count(content)
    if word_count < 1100 or word_count > 2000:
        fix_items.append(f"word count {word_count} is outside 1100-2000")
    allowed_urls = _internal_link_urls(published) | {CTA_URL}
    for url in _article_links(content):
        if "mintistudio.com" in url and url not in allowed_urls:
            blocking_issues.append(f"internal link not in published list: {url}")
        if "mintistudio.com" in url and "/video_shorts/blog/" in url:
            status = _http_status(url)
            if status != 200:
                blocking_issues.append(f"internal blog link returned {status or 'error'}: {url}")
    if SELF_DISCLAIMER_RE.search(content):
        blocking_issues.append("MintiStudio self-disclaimer is not allowed")
    if len(visuals) > 3:
        fixed["visuals"] = "trimmed visuals to at most 3 items"
    generate_count = sum(1 for visual in visuals if visual.get("type") == "generate")
    if generate_count > 1:
        fixed["visuals"] = f"repaired generate visual cap ({generate_count})"
    flow_count = sum(1 for visual in visuals if visual.get("type") == "flow")
    compare_count = sum(1 for visual in visuals if visual.get("type") == "compare")
    valid_flow_count = len(_valid_flow_blocks(content))
    valid_compare_count = len(_valid_compare_blocks(content))
    if valid_flow_count < flow_count:
        fixed["visuals"] = f"repaired flow visual block count ({valid_flow_count}/{flow_count})"
    if valid_compare_count < compare_count:
        fixed["visuals"] = f"repaired compare visual block count ({valid_compare_count}/{compare_count})"
    missing_compare = _missing_compare_numbers(content)
    if missing_compare:
        blocking_issues.append("compare metric numbers not found in article text: " + ", ".join(missing_compare[:4]))
    screenshot_by_id = {item["id"]: item for item in screenshots}
    for visual in visuals:
        if visual.get("type") not in VISUAL_TYPES:
            fixed["visuals"] = f"repaired unknown visual type: {visual.get('type')}"
        if visual.get("type") == "screenshot":
            screenshot_id = str(visual.get("screenshot_id") or "")
            if screenshot_id not in screenshot_by_id:
                fixed["visuals"] = f"repaired screenshot_id is unavailable or on hold: {screenshot_id}"
    repair_notes = list(article.get("visual_repairs") or [])
    if repair_notes:
        fixed["visual_repairs"] = repair_notes
    issues = blocking_issues + fix_items
    return {"ok": not blocking_issues, "issues": issues, "blocking_issues": blocking_issues, "fixes": fix_items, "fixed": fixed, "word_count": word_count}


def _checks_notes(checks: dict[str, Any]) -> str:
    notes = list(checks.get("issues") or [])
    repairs = ((checks.get("fixed") or {}).get("visual_repairs") or [])
    notes.extend(f"visual repair: {item}" for item in repairs)
    return "; ".join(str(item) for item in notes)


def _join_notes(*groups: Any) -> str | None:
    notes: list[str] = []
    for group in groups:
        if not group:
            continue
        if isinstance(group, str):
            notes.append(group)
        else:
            notes.extend(str(item) for item in group if item)
    return "; ".join(notes) if notes else None


def _blocking_issues(review: dict[str, Any], checks: dict[str, Any] | None = None) -> list[Any]:
    items: list[Any] = []
    for key in ("blocking_issues", "required_fixes"):
        value = review.get(key)
        if isinstance(value, list):
            items.extend(value)
    for item in review.get("issues") or []:
        if isinstance(item, dict) and str(item.get("severity") or "").lower() == "blocking":
            items.append(item)
    if checks and checks.get("blocking_issues"):
        items.extend([{"severity": "blocking", "issue": issue} for issue in checks.get("blocking_issues") or []])
    return items


def _pre_save_visual_guard(article: dict[str, Any], screenshots: list[dict[str, Any]], published: list[dict[str, Any]]) -> None:
    checks = _code_checks(article, screenshots, published)
    if not checks.get("ok"):
        raise RuntimeError("pre-save blog checks failed: " + "; ".join(checks.get("issues") or []))


def _strip_design_syntax(text: str) -> str:
    text = re.sub(r"^:::[A-Za-z]+.*$|^:::$", "", text or "", flags=re.M)
    text = re.sub(r"[#>*_`|:-]+", " ", text)
    text = re.sub(r"\[[^\]]+\]\(([^)]+)\)", " ", text)
    return " ".join(re.findall(r"\b[\w'-]+\b", text.lower()))


def _strip_designer_wrappers(text: str) -> str:
    text = re.sub(r"(?m)^:::[A-Za-z][^\n]*$", "", text or "")
    text = re.sub(r"(?m)^:::\s*$", "", text)
    text = re.sub(r"<!--\s*IMAGE_[123]\s*-->", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _guard_designer(before: str, after: str) -> tuple[bool, str]:
    before_links = _article_links(before)
    after_links = _article_links(after)
    if before_links != after_links:
        return False, "designer changed links"
    before_prose = _strip_designer_wrappers(before)
    after_prose = _strip_designer_wrappers(after)
    if before_prose != after_prose:
        ratio = difflib.SequenceMatcher(None, before_prose, after_prose).ratio()
        return False, f"designer altered approved prose: similarity {ratio:.3f}"
    return True, "prose preserved"


def _replace_screenshot_placeholders(article: dict[str, Any], screenshots: list[dict[str, Any]], *, copy_files: bool = True) -> dict[str, Any]:
    slug = str(article["slug"])
    content = str(article.get("content_md") or "")
    screenshot_by_id = {item["id"]: item for item in screenshots}
    target_dir = STATIC_BLOG_ROOT / slug
    if copy_files:
        target_dir.mkdir(parents=True, exist_ok=True)
    placed: list[dict[str, str]] = []
    for visual in article.get("visuals") or []:
        marker = _normalize_marker(visual.get("marker") or visual.get("id"))
        if visual.get("type") != "screenshot" or marker not in {"IMAGE_1", "IMAGE_2", "IMAGE_3"}:
            continue
        screenshot = screenshot_by_id.get(str(visual.get("screenshot_id") or ""))
        if not screenshot:
            continue
        source = LIBRARY_ROOT / str(screenshot["filename"])
        target_name = source.name
        if copy_files:
            target = target_dir / source.name
            shutil.copy2(source, target)
            _add_logo_badge(target, badge_ratio=0.14, margin_ratio=0.02)
            target_name = target.name
        url = f"/video_shorts/static/img/blog/{quote(slug)}/{quote(target_name)}"
        alt = str(screenshot.get("alt") or visual.get("alt") or "").replace('"', "'")
        caption = str(visual.get("caption") or alt or "MintiStudio workflow screenshot").strip().replace('"', "'")
        content = content.replace(f"<!-- {marker} -->", f"![{alt}]({url} \"{caption} [screenshot]\")")
        placed.append({"marker": marker, "filename": target_name, "url": url, "alt": alt})
    article["content_md"] = content
    article["screenshots_placed"] = placed
    return article


def _image_prompt_for_cover(article: dict[str, Any]) -> str:
    cover = article.get("cover") if isinstance(article.get("cover"), dict) else {}
    prompt = str(cover.get("prompt") or cover.get("description") or "").strip()
    if not prompt:
        prompt = f"Two to four symbolic objects showing the article idea: {article.get('title') or 'MintiStudio guide'}"
    archetype = str(cover.get("archetype") or article.get("cover_archetype") or "").strip().upper()[:1]
    label = COVER_ARCHETYPE_LABELS.get(archetype)
    additions = [
        "The reference images define ONLY the rendering style (soft 3D material, lighting, palette, background motifs). Do NOT reuse their characters, clothing, composition, layout, or objects."
    ]
    if label:
        additions.append(f"Use cover archetype {archetype}: {label}.")
    character = cover.get("character") if isinstance(cover.get("character"), dict) else article.get("cover_character")
    if isinstance(character, dict) and character:
        additions.append(
            "Character attributes: "
            + ", ".join(
                str(character.get(key))
                for key in ("gender", "age_range", "hair", "outfit", "face_style")
                if character.get(key)
            )
            + "."
        )
    return f"{prompt}\n\nCover directives:\n" + "\n".join(additions)


def _image_result_payload(result: BlogImageResult) -> dict[str, Any]:
    payload = dict(result.__dict__)
    payload["cost_usd"] = str(result.cost_usd)
    return payload


def _replace_generated_placeholder(content: str, marker: str, markdown: str) -> str:
    return re.sub(r"<!--\s*" + re.escape(marker) + r"\s*-->", markdown, content, count=1)


def _ensure_cover_metadata_columns(conn) -> None:
    desired = {
        "blog_articles": {
            "cover_archetype": "VARCHAR",
            "cover_character_json": "TEXT",
        },
        "blog_pipeline_runs": {
            "cover_archetype": "VARCHAR",
        },
    }
    for table, columns in desired.items():
        existing = table_columns(conn, table)
        for column, column_type in columns.items():
            if column in existing:
                continue
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")


def _article_image_urls(article: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    cover = str(article.get("cover_image_url") or "").strip()
    if cover:
        urls.append(cover)
    urls.extend(re.findall(r"!\[[^\]]*\]\((/video_shorts/static/img/blog/[^)]+)\)", str(article.get("content_md") or "")))
    absolute: list[str] = []
    for url in urls:
        absolute.append(url if url.startswith("http") else f"{BASE_URL}{url}")
    return absolute


def _no_publish_reason(*, run_status: str, article: dict[str, Any], checks: dict[str, Any], final_score: int, image_status: str) -> str | None:
    content = str(article.get("content_md") or "")
    if run_status != "draft_ready":
        if image_status != "done":
            return "image failure"
        if final_score < BLOG_REVIEW_PASS:
            return f"reviewer score {final_score} below pass threshold {BLOG_REVIEW_PASS}"
        if _blocking_issues({}, checks):
            return "blocking check issues"
        return run_status
    if "FACT-CHECK REQUIRED" in content:
        return "FACT-CHECK REQUIRED marker present"
    if re.search(r"<!--\s*IMAGE_[123]\s*-->", content):
        return "remaining IMAGE placeholder"
    if not article.get("cover_image_url"):
        return "missing cover_image_url"
    if not checks.get("ok"):
        return "final checks failed: " + "; ".join(checks.get("issues") or [])
    return None


def _notify_run(
    *,
    state: PipelineState | None,
    status_label: str,
    title: str,
    article: dict[str, Any] | None = None,
    article_id: int | None = None,
    reviewer_score: int | None = None,
    total_cost: str | None = None,
    reason: str | None = None,
    published: bool = False,
) -> dict[str, Any]:
    article = article or {}
    if state and state.run_id and not state.dry_run:
        check_conn = get_db()
        try:
            if run_was_notified(check_conn, state.run_id):
                return {"status": "skipped", "notes": "already notified", "payload": {}}
        finally:
            check_conn.close()
    if status_label == "published":
        subject = f"[Minti Blog] Published — {title}"
    elif status_label == "published_not_indexable":
        subject = f"[Minti Blog] Published but NOT indexable — {title}"
    elif status_label == "failed":
        subject = f"[Minti Blog] Run failed — {title}"
    else:
        subject = f"[Minti Blog] Needs your review — {title} ({reason or 'review needed'})"
    try:
        result = send_blog_run_notification(
            subject=subject,
            status_label=status_label,
            title=title,
            public_url=_public_blog_url(str(article.get("slug") or "")) if published and article.get("slug") else None,
            admin_edit_url=_admin_edit_url(article_id),
            run_detail_url=_run_detail_url(state.run_id if state else None),
            reviewer_score=reviewer_score,
            total_cost=total_cost,
            images=_article_image_urls(article),
            reason=reason,
            to_email=BLOG_NOTIFY_EMAIL,
        )
        payload = {"email": result}
        status = "done"
        notes = "SMTP accepted"
    except Exception as exc:
        payload = {"error_class": exc.__class__.__name__, "message": str(exc)[:500]}
        status = "failed"
        notes = str(exc)[:1000]
    if state and state.run_id and not state.dry_run:
        conn = get_db()
        try:
            row = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM blog_pipeline_stages WHERE run_id = ?", [state.run_id]).fetchone()
            record_stage(conn, run_id=state.run_id, seq=int(row[0] or 0) + 1, stage="notification", status=status, output=payload, notes=notes)
            if status == "done":
                mark_run_notified(conn, state.run_id)
            conn.commit()
        finally:
            conn.close()
    return {"status": status, "notes": notes, "payload": payload}


def _publish_if_clean(
    *,
    article_id: int,
    article: dict[str, Any],
    checks: dict[str, Any],
    run_status: str,
    final_score: int,
    image_status: str,
) -> tuple[bool, str | None, dict[str, Any]]:
    reason = _no_publish_reason(run_status=run_status, article=article, checks=checks, final_score=final_score, image_status=image_status)
    verification: dict[str, Any] = {}
    if not BLOG_AUTO_PUBLISH:
        return False, "BLOG_AUTO_PUBLISH disabled", verification
    if reason:
        return False, reason, verification
    updated = update_blog_article_status(article_id, "published")
    if not updated:
        return False, "publish helper returned no update", verification
    public_url = _public_blog_url(str(article["slug"]))
    verification["public_url"] = public_url
    verification["http_status"] = _http_status(public_url)
    verification["indexability"] = _indexability_status(public_url)
    verification["sitemap"] = _sitemap_status(public_url)
    return True, None, verification


def _run_image_stage(
    *,
    state: PipelineState,
    article: dict[str, Any],
    article_id: int | None = None,
    low_medium_test: bool = False,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    set_current = state.run_id and not state.dry_run
    conn = get_db() if set_current else None
    try:
        if conn:
            set_current_stage(conn, state.run_id, "images")
            conn.commit()
    finally:
        if conn:
            conn.close()
    output: dict[str, Any] = {"cover": None, "visuals": [], "errors": [], "test_variants": []}
    run_status = "done"
    slug = str(article["slug"])
    try:
        _check_run_budget(state)
        cover_result = generate_blog_image(
            prompt=_image_prompt_for_cover(article),
            slug=slug,
            filename="cover.png",
            kind="cover",
            marker=None,
            alt=str(article.get("title") or "Blog cover"),
            model=BLOG_IMAGE_COVER,
            quality=BLOG_IMAGE_COVER_QUALITY,
            topic_id=int(state.topic["id"]),
            run_id=state.run_id,
            overwrite=True,
            dry_run=state.dry_run,
        )
        article["cover_image_url"] = cover_result.url
        output["cover"] = _image_result_payload(cover_result)
        state.run_cost += cover_result.cost_usd
    except Exception as exc:
        run_status = "needs_you"
        output["errors"].append({"kind": "cover", "error_class": exc.__class__.__name__, "message": str(exc)[:300]})

    content = str(article.get("content_md") or "")
    generate_index = 1
    for visual in article.get("visuals") or []:
        marker = _normalize_marker(visual.get("marker") or visual.get("id"))
        if visual.get("type") != "generate" or marker not in {"IMAGE_1", "IMAGE_2", "IMAGE_3"}:
            continue
        prompt = str(visual.get("prompt") or visual.get("description") or "").strip()
        filename = generated_visual_filename(visual, generate_index)
        generate_index += 1
        try:
            _check_run_budget(state)
            result = generate_blog_image(
                prompt=prompt,
                slug=slug,
                filename=filename,
                kind="inline",
                marker=marker,
                alt=str(visual.get("alt") or marker),
                model=BLOG_IMAGE_INLINE,
                quality=BLOG_IMAGE_INLINE_QUALITY,
                topic_id=int(state.topic["id"]),
                run_id=state.run_id,
                overwrite=True,
                dry_run=state.dry_run,
            )
            output["visuals"].append(_image_result_payload(result))
            state.run_cost += result.cost_usd
            content = _replace_generated_placeholder(content, marker, render_markdown_image(visual, result))
            if low_medium_test and marker == "IMAGE_1":
                medium_filename = filename.rsplit(".", 1)[0] + "-medium.png"
                _check_run_budget(state)
                medium = generate_blog_image(
                    prompt=prompt,
                    slug=slug,
                    filename=medium_filename,
                    kind="inline",
                    marker=marker,
                    alt=str(visual.get("alt") or marker),
                    model=BLOG_IMAGE_INLINE,
                    quality="medium",
                    topic_id=int(state.topic["id"]),
                    run_id=state.run_id,
                    overwrite=True,
                    dry_run=state.dry_run,
                )
                state.run_cost += medium.cost_usd
                output["test_variants"].append({"low": _image_result_payload(result), "medium": _image_result_payload(medium)})
        except Exception as exc:
            run_status = "needs_you"
            output["errors"].append({"kind": "inline", "marker": marker, "error_class": exc.__class__.__name__, "message": str(exc)[:300]})
    article["content_md"] = content
    if article_id and not state.dry_run:
        conn = get_db()
        try:
            _ensure_cover_metadata_columns(conn)
            conn.execute(
                """
                UPDATE blog_articles
                SET content = ?,
                    cover_image_url = ?,
                    cover_archetype = ?,
                    cover_character_json = ?,
                    content_updated_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                [
                    article.get("content_md"),
                    article.get("cover_image_url"),
                    article.get("cover_archetype"),
                    json.dumps(article.get("cover_character") or {}, ensure_ascii=False),
                    int(article_id),
                ],
            )
            conn.commit()
        finally:
            conn.close()
    return article, output, run_status


def _save_draft(conn, *, state: PipelineState, article: dict[str, Any], visuals_plan: dict[str, Any], run_status: str) -> int:
    _ensure_cover_metadata_columns(conn)
    _rename_archived_pipeline_slug_conflict(conn, str(article["slug"]))
    article["reading_time"] = _coerce_reading_time_minutes(article.get("reading_time"), str(article.get("content_md") or ""))
    row = conn.execute(
        """
        INSERT INTO blog_articles (
            title, slug, summary, content, cover_image_url, meta_title, meta_description,
            author_name, reading_time, view_count, status, published_at, content_updated_at, import_source, import_source_id
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, 'MintiStudio Team', ?, 0, 'draft', NULL, CURRENT_TIMESTAMP, 'blog_pipeline', ?)
        RETURNING id
        """,
        [
            article["title"],
            article["slug"],
            article.get("summary"),
            article.get("content_md"),
            article.get("cover_image_url"),
            article.get("meta_title"),
            article.get("meta_description"),
            article.get("reading_time"),
            str(state.run_id or "dry-run"),
        ],
    ).fetchone()
    article_id = int(row[0])
    conn.execute(
        """
        UPDATE blog_articles
        SET cover_archetype = ?, cover_character_json = ?
        WHERE id = ?
        """,
        [
            article.get("cover_archetype"),
            json.dumps(article.get("cover_character") or {}, ensure_ascii=False),
            article_id,
        ],
    )
    conn.execute(
        """
        UPDATE blog_pipeline_runs
        SET cover_archetype = ?
        WHERE id = ?
        """,
        [article.get("cover_archetype"), int(state.run_id)],
    )
    topic_status = "draft_ready" if run_status == "draft_ready" else "in_production"
    conn.execute("UPDATE blog_topics SET status = ? WHERE id = ?", [topic_status, int(state.topic["id"])])
    record_stage(
        conn,
        run_id=state.run_id,
        seq=state.seq + 1,
        stage="final",
        status="done",
        output={"article_id": article_id, "cover": article.get("cover"), "visuals": visuals_plan},
    )
    return article_id


def _stage_output_from_run(conn, run_id: int, stage_names: tuple[str, ...]) -> dict[str, Any]:
    placeholders = ",".join(["?"] * len(stage_names))
    row = conn.execute(
        f"""
        SELECT output
        FROM blog_pipeline_stages
        WHERE run_id = ?
          AND stage IN ({placeholders})
        ORDER BY seq DESC, id DESC
        LIMIT 1
        """,
        [int(run_id), *stage_names],
    ).fetchone()
    if not row or not row[0]:
        return {}
    try:
        return json.loads(row[0]) if isinstance(row[0], str) else dict(row[0])
    except Exception:
        return {}


def _article_for_existing_draft(conn, *, article_id: int, run_id: int) -> tuple[dict[str, Any], dict[str, Any]]:
    row = conn.execute(
        """
        SELECT id, title, slug, summary, content, cover_image_url, meta_title, meta_description, reading_time
        FROM blog_articles
        WHERE id = ?
        LIMIT 1
        """,
        [int(article_id)],
    ).fetchone()
    if not row:
        raise RuntimeError(f"blog article {article_id} was not found")
    topic_row = conn.execute(
        """
        SELECT t.id, t.title, t.primary_keyword, t.category, t.intent, t.brief
        FROM blog_pipeline_runs r
        LEFT JOIN blog_topics t ON t.id = r.topic_id
        WHERE r.id = ?
        LIMIT 1
        """,
        [int(run_id)],
    ).fetchone()
    if not topic_row:
        raise RuntimeError(f"blog pipeline run {run_id} was not found")
    article = {
        "id": int(row[0]),
        "title": row[1],
        "slug": row[2],
        "summary": row[3],
        "content_md": row[4],
        "cover_image_url": row[5],
        "meta_title": row[6],
        "meta_description": row[7],
        "reading_time": row[8],
    }
    final_output = _stage_output_from_run(conn, run_id, ("final",))
    designer_output = _stage_output_from_run(conn, run_id, ("designer",))
    writer_output = _stage_output_from_run(conn, run_id, ("revision_2", "revision_1", "writer"))
    plan = final_output.get("visuals") if isinstance(final_output.get("visuals"), dict) else {}
    visuals = plan.get("visuals") if isinstance(plan, dict) else None
    if not visuals:
        visuals = designer_output.get("visuals") or writer_output.get("visuals") or []
    article["visuals"] = visuals
    article["cover"] = (plan or {}).get("cover") or writer_output.get("cover") or {}
    topic = {
        "id": int(topic_row[0]),
        "title": topic_row[1],
        "primary_keyword": topic_row[2],
        "category": topic_row[3],
        "intent": topic_row[4],
        "brief": topic_row[5],
    }
    return article, topic


def run_images_for_existing_draft(*, run_id: int, article_id: int, low_medium_test: bool = False) -> dict[str, Any]:
    conn = get_db()
    try:
        article, topic = _article_for_existing_draft(conn, article_id=article_id, run_id=run_id)
        state = PipelineState(topic=topic, run_id=int(run_id), dry_run=False)
        row = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM blog_pipeline_stages WHERE run_id = ?", [int(run_id)]).fetchone()
        state.seq = int(row[0] or 0)
        conn.commit()
    finally:
        conn.close()
    article = _prepare_cover_plan(article, topic)
    article, image_output, image_status = _run_image_stage(
        state=state,
        article=article,
        article_id=article_id,
        low_medium_test=low_medium_test,
    )
    image_cost = sum((Decimal(str((item or {}).get("cost_usd") or "0")) for item in ([image_output.get("cover")] + list(image_output.get("visuals") or []))), Decimal("0"))
    for variant in image_output.get("test_variants") or []:
        image_cost += Decimal(str((variant.get("medium") or {}).get("cost_usd") or "0"))
    conn = get_db()
    try:
        record_stage(
            conn,
            run_id=run_id,
            seq=state.seq + 1,
            stage="images",
            status=image_status,
            model=f"{BLOG_IMAGE_COVER}/{BLOG_IMAGE_INLINE}",
            output=image_output,
            notes="; ".join(f"{item.get('kind')} {item.get('marker') or ''}: {item.get('message')}" for item in image_output.get("errors") or [])[:1000],
            cost_usd=image_cost,
        )
        if image_status != "done":
            finish_run(conn, run_id, status="needs_you", article_id=article_id, error="Image generation needs review")
        else:
            finish_run(conn, run_id, status="draft_ready", article_id=article_id)
        conn.commit()
    finally:
        conn.close()
    return {"status": image_status, "article_id": article_id, "run_id": run_id, "images": image_output, "image_cost_usd": str(image_cost)}


def _writer_prompt() -> str:
    return """You are the MintiStudio blog Writer. Return strict JSON only. Write original, practical long-form blog content for the supplied topic. Use the stable context as binding instructions.

Required JSON keys: title, slug, summary, content_md, meta_title, meta_description, reading_time, cover, visuals.
reading_time must be an integer number of minutes only, such as 8. Never return "8 min read" or any other phrase; the renderer adds the label.
cover.prompt must describe only the scene: 2-4 objects, one visual idea, story, and mood. Never include style, colors, text, logos, UI, screenshots, third-party brands, or realistic people in cover.prompt.
cover.archetypes must be the top 3 archetype letters from stable_context.cover_archetypes that fit the topic. Code chooses the final archetype for variety; do not force one in the prose.
visuals must contain exactly 3 items with marker values IMAGE_1, IMAGE_2, IMAGE_3. Valid visual types are screenshot, flow, compare, generate.
For type=screenshot, screenshot_id is required and must exactly match one of requirements.available_screenshots ids. Never leave screenshot_id empty. If none of the listed screenshots fits the section, choose flow, compare, or generate instead.
Use flow for a process/workflow and compare for metric or option comparisons. Use screenshots for product features. Use generate at most once per article, only when flow, compare, and screenshot do not fit.
For screenshot or generate visuals, content_md must include that marker as a standalone HTML comment such as <!-- IMAGE_1 -->. Bare IMAGE_1 text is forbidden.
For flow or compare visuals, put the full :::flow or :::compare block directly in content_md where that visual belongs; do not also include an IMAGE comment for that slot.
Flow syntax is 2-5 lines of: icon | title max 4 words | subtitle max 8 words. Allowed icons: video, clips, scissors, calendar, clock, eye, users, chart, mic, upload, check, sparkles.
Compare syntax is exactly two metric lines of: icon | value | label | direction. Direction must be up, down, or flat. Any number in the value must already appear in the article text. Add an optional note: line.
Each visual must include a short caption, maximum 12 words, suitable for the markdown image title or component caption.
Never use placeholder image text such as "Alt text", "Image", or "Placeholder"; every visual alt must describe the actual visual.
Use only the supplied published_articles URLs for internal links. Do not invent blog URLs.
Never claim anything about MintiStudio unless it is in minti_facts.md.
Never write sentences that disclaim, hedge, or caution about MintiStudio itself. If a Minti detail is not in minti_facts.md, omit it. Make an honest, clear case for Autopilot where it genuinely fits and tie Minti features to the reader's problem.
Use the primary keyword naturally, with correct hyphenation such as "done-for-you"; never place it as a standalone bolded SEO phrase.
Do not force the exact-match keyword phrase awkwardly into a sentence or repeat it for SEO. One natural use is enough. If the exact phrase reads stiffly, rephrase it for a human, such as "short-form video can be such a useful channel for consultants" instead of "youtube shorts for consultants can be such a useful channel."
In body prose, spell out small quantities as words: one, two, three. Never write constructions like "a 1 clear idea" or "a 1 private story"; write "one clear idea" or "one private story." Digits are allowed only for real data/statistics such as "10 million views", "15 videos/month", percentages, years, or component/infographic specs, not ordinary prose.
When MintiStudio features are mentioned, tie each one to the reader's task in the same sentence; never use a comma-separated feature list.
Screenshot placement: if the article covers Autopilot, prefer placing a relevant screenshot in or near the Autopilot or "Where MintiStudio helps" section when the screenshot library has a suitable image."""


def _reviewer_prompt() -> str:
    return """You are the MintiStudio blog Reviewer. Return strict JSON only. Score the article against the supplied facts, checks, screenshots, published URLs, and existing titles. Be concrete and conservative.

Return JSON with total, scores, blocking_issues, fixes.
Any deterministic check issue must be copied into blocking_issues.
Blocking issues regardless of total score: MintiStudio self-disclaimers or hedges; any internal link not exactly in published_articles or the CTA URL; bare IMAGE_n placeholder text; missing IMAGE comment placeholders for screenshot/generate visuals; IMAGE comment placeholders for flow/compare visuals; more than 3 visual items after code repair; invalid flow/compare syntax; compare metric numbers not present in the article prose outside the compare block.
Updated component rule: screenshot and generate visuals use IMAGE comment placeholders; flow and compare visuals use their :::flow / :::compare blocks directly and must not have IMAGE comment placeholders. Do not require IMAGE placeholders for flow or compare visuals.
Facts rule: flow/compare text may only describe MintiStudio features that appear in minti_facts.md. Do not allow invented metrics, features, platform logos, or third-party brand claims.
Non-blocking fix: a MintiStudio section that reads as a feature list without tying features to the reader's problem."""


def _revision_prompt() -> str:
    return """You are the MintiStudio blog Reviser. Return strict JSON only. Apply only the listed reviewer fixes and blocking issues. Return the full article, not a patch or excerpt. Never shorten, summarize, delete, or rewrite sections that are not mentioned in the fixes. Preserve valid metadata, links, cover, visuals, and exact [[BLOCK_N]] protected tokens unless a fix explicitly requires moving the surrounding paragraph. Keep every [[BLOCK_N]] token exactly once and in the same relative position. Never drop the visuals array."""


def _designer_prompt() -> str:
    return """You are the MintiStudio blog Designer. Return strict JSON only with content_md. You are given final, approved prose. Do not change any wording. Only wrap existing passages in ::: component blocks and insert image markers. Return the same text with components/markers added. Never reword, rewrite, re-summarize, regenerate, add, or delete sentences. Never change links, metadata, or [[BLOCK_N]] protected tokens. Keep every [[BLOCK_N]] token exactly once and in the same relative position.

Use :::steps for any sequential workflow, at least one :::key, and :::tip or :::warning where useful. If the writer already included :::flow or :::compare, preserve it exactly. Keep the preservation guard passing by preserving approved prose exactly."""


def run_pipeline(*, topic_id: int | None = None, dry_run: bool = False) -> dict[str, Any]:
    prefix = _stable_prefix()
    published = _published_articles()
    screenshots = prefix["screenshots"]
    conn = get_db()
    state: PipelineState | None = None
    article: dict[str, Any] | None = None
    try:
        topic = _pop_topic(conn, topic_id, dry_run=dry_run)
        if not topic:
            conn.rollback()
            print("BLOG_PIPELINE_NO_TOPIC")
            return {"status": "no_topic"}
        run_id = create_run(conn, topic_id=int(topic["id"]), dry_run=dry_run)
        state = PipelineState(topic=topic, run_id=run_id, dry_run=dry_run)
        conn.rollback() if dry_run else conn.commit()

        user_base = {
            "stable_context": prefix,
            "topic": topic,
            "published_articles": published,
            "requirements": {
                "visual_markers": ["IMAGE_1", "IMAGE_2", "IMAGE_3"],
                "cta_url": CTA_URL,
                "writer_model": BLOG_MODEL_WRITER,
                "reviewer_model": BLOG_MODEL_REVIEWER,
                "designer_model": BLOG_MODEL_DESIGNER,
                "available_screenshots": _screenshot_prompt_list(screenshots),
            },
        }
        article, cost = _call_stage(state, "writer", BLOG_MODEL_WRITER, _writer_prompt(), user_base)
        writer_schema_violations = _writer_schema_violations(article, screenshots)
        writer_retry_notes: list[str] = []
        if writer_schema_violations:
            retry_payload = {
                **user_base,
                "previous_output": article,
                "schema_violations": writer_schema_violations,
                "instruction": (
                    "Return the complete corrected article JSON. Change only the visuals and matching "
                    "IMAGE/component placements needed to satisfy these schema violations."
                ),
            }
            article, retry_cost = _call_stage(state, "writer_retry", BLOG_MODEL_WRITER, _writer_prompt(), retry_payload)
            cost += retry_cost
            retry_violations = _writer_schema_violations(article, screenshots)
            writer_retry_notes = [f"writer schema retry: {item}" for item in writer_schema_violations]
            if retry_violations:
                writer_retry_notes.extend(f"writer schema still repaired by code: {item}" for item in retry_violations)
        article = _prepare_cover_plan(_normalize_article_payload(article), topic)
        conn = get_db()
        state.seq += 1
        set_current_stage(conn, state.run_id, "writer")
        record_stage(conn, run_id=state.run_id, seq=state.seq, stage="writer", status="done", model=BLOG_MODEL_WRITER, output=article, notes=_join_notes(writer_retry_notes), cost_usd=cost)
        conn.commit()

        checks = _code_checks(article, screenshots, published)
        state.seq += 1
        record_stage(conn, run_id=state.run_id, seq=state.seq, stage="checks", status="done" if checks["ok"] else "failed", output=checks, notes=_checks_notes(checks))
        conn.commit()

        reviewer_payload = {**user_base, "article": article, "checks": checks, "existing_titles": [item["title"] for item in published]}
        review_1, cost = _call_stage(state, "reviewer_1", BLOG_MODEL_REVIEWER, _reviewer_prompt(), reviewer_payload)
        state.seq += 1
        review_1_score = _review_score(review_1)
        record_stage(conn, run_id=state.run_id, seq=state.seq, stage="reviewer_1", status="done", model=BLOG_MODEL_REVIEWER, output=review_1, score=review_1_score, cost_usd=cost)
        conn.commit()

        final_review = review_1
        if review_1_score < BLOG_REVIEW_PASS or _blocking_issues(review_1, checks):
            previous_article = dict(article)
            masked_previous, previous_masked_content, previous_blocks = _masked_article(previous_article)
            revision_payload = {**user_base, "article": masked_previous, "review": review_1}
            previous_visuals = list(previous_article.get("visuals") or [])
            candidate_article, cost = _call_stage(state, "revision_1", BLOG_MODEL_WRITER, _revision_prompt(), revision_payload)
            candidate_article, token_repairs = _restore_candidate_article(candidate_article, previous_masked_content, previous_blocks)
            if not candidate_article.get("visuals") and previous_visuals:
                candidate_article["visuals"] = previous_visuals
            candidate_article = _prepare_cover_plan(_normalize_article_payload(candidate_article), topic)
            rejection_reason = _revision_rejection_reason(previous_article, candidate_article)
            if rejection_reason:
                article = previous_article
                revision_status = "rejected"
                revision_notes = _join_notes(rejection_reason, token_repairs)
                revision_output = {"rejected_reason": rejection_reason, "token_repairs": token_repairs, "candidate": candidate_article, "kept_previous": True}
            else:
                article = candidate_article
                revision_status = "done"
                revision_notes = _join_notes(token_repairs)
                revision_output = {**article, "token_repairs": token_repairs} if token_repairs else article
            state.seq += 1
            record_stage(conn, run_id=state.run_id, seq=state.seq, stage="revision_1", status=revision_status, model=BLOG_MODEL_WRITER, output=revision_output, notes=revision_notes, cost_usd=cost)
            checks = _code_checks(article, screenshots, published)
            state.seq += 1
            record_stage(conn, run_id=state.run_id, seq=state.seq, stage="checks", status="done" if checks["ok"] else "failed", output=checks, notes=_checks_notes(checks))
            reviewer_payload = {**user_base, "article": article, "checks": checks, "existing_titles": [item["title"] for item in published]}
            review_2, cost = _call_stage(state, "reviewer_2", BLOG_MODEL_REVIEWER, _reviewer_prompt(), reviewer_payload)
            final_review = review_2
            state.seq += 1
            review_2_score = _review_score(review_2)
            record_stage(conn, run_id=state.run_id, seq=state.seq, stage="reviewer_2", status="done", model=BLOG_MODEL_REVIEWER, output=review_2, score=review_2_score, cost_usd=cost)
            conn.commit()
            if review_2_score < BLOG_REVIEW_PASS or _blocking_issues(review_2, checks):
                previous_article = dict(article)
                masked_previous, previous_masked_content, previous_blocks = _masked_article(previous_article)
                previous_visuals = list(previous_article.get("visuals") or [])
                candidate_article, cost = _call_stage(state, "revision_2", BLOG_MODEL_WRITER, _revision_prompt(), {**user_base, "article": masked_previous, "review": review_2})
                candidate_article, token_repairs = _restore_candidate_article(candidate_article, previous_masked_content, previous_blocks)
                if not candidate_article.get("visuals") and previous_visuals:
                    candidate_article["visuals"] = previous_visuals
                candidate_article = _prepare_cover_plan(_normalize_article_payload(candidate_article), topic)
                rejection_reason = _revision_rejection_reason(previous_article, candidate_article)
                if rejection_reason:
                    article = previous_article
                    revision_status = "rejected"
                    revision_notes = _join_notes(rejection_reason, token_repairs)
                    revision_output = {"rejected_reason": rejection_reason, "token_repairs": token_repairs, "candidate": candidate_article, "kept_previous": True}
                else:
                    article = candidate_article
                    revision_status = "done"
                    revision_notes = _join_notes(token_repairs)
                    revision_output = {**article, "token_repairs": token_repairs} if token_repairs else article
                state.seq += 1
                record_stage(conn, run_id=state.run_id, seq=state.seq, stage="revision_2", status=revision_status, model=BLOG_MODEL_WRITER, output=revision_output, notes=revision_notes, cost_usd=cost)
                checks = _code_checks(article, screenshots, published)
                state.seq += 1
                record_stage(conn, run_id=state.run_id, seq=state.seq, stage="checks", status="done" if checks["ok"] else "failed", output=checks, notes=_checks_notes(checks))
                conn.commit()

        before_design = str(article.get("content_md") or "")
        masked_design_content, design_blocks = _mask_protected_blocks(before_design)
        designer_payload = {
            "stable_context": {
                "allowed_components": prefix["allowed_components"],
                "style_guide": prefix["style_guide"],
            },
            "content_md": masked_design_content,
        }
        designer_output, cost = _call_stage(state, "designer", BLOG_MODEL_DESIGNER, _designer_prompt(), designer_payload)
        after_design, token_repairs = _restore_masked_blocks(masked_design_content, str(designer_output.get("content_md") or ""), design_blocks)
        after_design = _normalize_image_placeholder_syntax(after_design)
        guard_ok, guard_note = _guard_designer(before_design, after_design)
        if token_repairs:
            guard_note = _join_notes(guard_note, token_repairs) or guard_note
        if guard_ok:
            article["content_md"] = after_design
            designer_status = "done"
        else:
            designer_status = "failed"
        state.seq += 1
        record_stage(conn, run_id=state.run_id, seq=state.seq, stage="designer", status=designer_status, model=BLOG_MODEL_DESIGNER, output={"guard": guard_note, "source_content_md": before_design, "content_md": after_design, "visuals": article.get("visuals"), "token_repairs": token_repairs}, notes=guard_note, cost_usd=cost)
        conn.commit()

        checks = _code_checks(article, screenshots, published)
        state.seq += 1
        record_stage(conn, run_id=state.run_id, seq=state.seq, stage="checks", status="done" if checks["ok"] else "failed", output=checks, notes=_checks_notes(checks))
        conn.commit()
        if not checks.get("ok"):
            raise RuntimeError("pre-save blog checks failed: " + "; ".join(checks.get("issues") or []))

        article = _prepare_cover_plan(article, topic)
        article = _replace_screenshot_placeholders(article, screenshots, copy_files=not dry_run)
        article, image_output, image_status = _run_image_stage(state=state, article=article)
        image_cost = sum((Decimal(str((item or {}).get("cost_usd") or "0")) for item in ([image_output.get("cover")] + list(image_output.get("visuals") or []))), Decimal("0"))
        for variant in image_output.get("test_variants") or []:
            image_cost += Decimal(str((variant.get("medium") or {}).get("cost_usd") or "0"))
        state.seq += 1
        record_stage(
            conn,
            run_id=state.run_id,
            seq=state.seq,
            stage="images",
            status=image_status,
            model=f"{BLOG_IMAGE_COVER}/{BLOG_IMAGE_INLINE}",
            output=image_output,
            notes="; ".join(f"{item.get('kind')} {item.get('marker') or ''}: {item.get('message')}" for item in image_output.get("errors") or [])[:1000],
            cost_usd=image_cost,
        )
        conn.commit()
        run_status = "draft_ready"
        final_score = _review_score(final_review)
        if designer_status != "done" or image_status != "done" or final_score < BLOG_REVIEW_PASS or _blocking_issues(final_review, checks) or not checks.get("ok"):
            run_status = "needs_you"

        result = {
            "status": run_status,
            "topic": topic,
            "article": article,
            "review": final_review,
            "checks": checks,
            "designer_guard": guard_note,
            "images": image_output,
            "total_cost_usd": str(state.run_cost),
        }
        if dry_run:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return result

        article_id = _save_draft(conn, state=state, article=article, visuals_plan={"cover": article.get("cover"), "visuals": article.get("visuals"), "screenshots_placed": article.get("screenshots_placed")}, run_status=run_status)
        finish_run(conn, state.run_id, status=run_status, article_id=article_id, final_review_score=final_score)
        conn.commit()
        result["article_id"] = article_id
        published, publish_reason, publish_verification = _publish_if_clean(
            article_id=article_id,
            article=article,
            checks=checks,
            run_status=run_status,
            final_score=final_score,
            image_status=image_status,
        )
        result["published"] = published
        result["publish_reason"] = publish_reason
        result["publish_verification"] = publish_verification
        published_not_indexable = bool(
            published and not (publish_verification.get("indexability") or {}).get("ok")
        )
        if published_not_indexable:
            publish_reason = "Published but NOT indexable: " + "; ".join(
                (publish_verification.get("indexability") or {}).get("reasons") or ["indexability check failed"]
            )
            result["publish_reason"] = publish_reason
        conn = get_db()
        try:
            row = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM blog_pipeline_stages WHERE run_id = ?", [state.run_id]).fetchone()
            record_stage(
                conn,
                run_id=state.run_id,
                seq=int(row[0] or 0) + 1,
                stage="publish",
                status="done" if published else "skipped",
                output={"published": published, "reason": publish_reason, "verification": publish_verification},
                notes=publish_reason,
            )
            conn.commit()
        finally:
            conn.close()
        if published:
            conn = get_db()
            try:
                finish_run(conn, state.run_id, status="published", article_id=article_id, final_review_score=final_score)
                conn.commit()
            finally:
                conn.close()
            run_status = "published"
            result["status"] = "published"
        notification = _notify_run(
            state=state,
            status_label="published_not_indexable" if published_not_indexable else ("published" if published else "draft_kept"),
            title=str(article.get("title") or topic.get("title") or "Untitled"),
            article=article,
            article_id=article_id,
            reviewer_score=final_score,
            total_cost=str(state.run_cost),
            reason=publish_reason,
            published=published,
        )
        result["notification"] = notification
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return result
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        if state and not state.dry_run:
            try:
                fail_conn = get_db()
                try:
                    finish_run(fail_conn, state.run_id, status="failed", error=str(exc)[:1000])
                    failed_runs = fail_conn.execute(
                        "SELECT COUNT(*) FROM blog_pipeline_runs WHERE topic_id = ? AND status = 'failed'",
                        [int(state.topic["id"])],
                    ).fetchone()
                    topic_status = "failed" if int((failed_runs or [0])[0] or 0) >= 2 else "queued"
                    fail_conn.execute(
                        "UPDATE blog_topics SET status = ?, judge_reason = COALESCE(judge_reason, '') || ? WHERE id = ?",
                        [topic_status, f"\nPipeline failed: {str(exc)[:500]}", int(state.topic["id"])],
                    )
                    fail_conn.commit()
                finally:
                    fail_conn.close()
            finally:
                _notify_run(
                    state=state,
                    status_label="failed",
                    title=str(state.topic.get("title") or "Untitled topic"),
                    reviewer_score=None,
                    total_cost=str(state.run_cost),
                    reason=str(exc)[:500],
                )
        raise
    finally:
        try:
            conn.close()
        except Exception:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one MintiStudio blog production pipeline.")
    parser.add_argument("--dry-run", action="store_true", help="Do not save the draft or run rows.")
    parser.add_argument("--topic-id", type=int, default=None, help="Run a specific queued topic.")
    parser.add_argument("--images-only", action="store_true", help="Run the image stage for an existing pipeline draft.")
    parser.add_argument("--run-id", type=int, default=None, help="Existing blog_pipeline_runs id for --images-only.")
    parser.add_argument("--article-id", type=int, default=None, help="Existing blog_articles id for --images-only.")
    parser.add_argument("--low-medium-test", action="store_true", help="Generate IMAGE_1 low and medium variants for comparison.")
    parser.add_argument("--manifest-report", action="store_true", help="Print screenshot manifest availability and exit.")
    args = parser.parse_args()
    if args.manifest_report:
        print(json.dumps(_all_manifest_entries(), ensure_ascii=False, indent=2))
        return 0
    if args.images_only:
        if not args.run_id or not args.article_id:
            raise SystemExit("--images-only requires --run-id and --article-id")
        result = run_images_for_existing_draft(run_id=args.run_id, article_id=args.article_id, low_medium_test=args.low_medium_test)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0
    run_pipeline(topic_id=args.topic_id, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
