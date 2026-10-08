#!/home/ubuntu/apps/minti_studio/.venv/bin/python
from __future__ import annotations

import html
import json
import os
import re
import sys
from datetime import datetime, timezone
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

ROOT = Path(os.getenv("MINTI_ROOT") or Path(__file__).resolve().parents[1])
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.video_shorts.services.blog_llm import BLOG_MODEL_REVIEWER, call_json, log_usage  # noqa: E402
from app.video_shorts.services.blog_pipeline import fetch_url, json_dumps_compact, robots_allows  # noqa: E402


CONTEXT_ROOT = ROOT / "app" / "video_shorts" / "blog_pipeline"
FACTS_PATH = CONTEXT_ROOT / "competitor_facts.md"
MAX_PAGE_TEXT_CHARS = 30000

COMPETITORS: tuple[dict[str, str], ...] = (
    {"name": "OpusClip", "source_url": "https://www.opus.pro/pricing"},
    {"name": "Klap", "source_url": "https://klap.app/pricing"},
    {"name": "Vizard", "source_url": "https://vizard.ai/pricing"},
    {"name": "Quso", "source_url": "https://quso.ai/pricing"},
    {"name": "2Short", "source_url": "https://2short.ai/pricing"},
    {"name": "Submagic", "source_url": "https://www.submagic.co/pricing"},
)

EXTRACTION_SYSTEM_PROMPT = """You extract competitor feature and workflow facts for a blog reference file.

Return strict JSON only:
{
  "name": "Competitor name",
  "source_url": "https://...",
  "last_fetched": "YYYY-MM-DD",
  "tiers": [
    {"name": "Tier name", "included": ["clearly stated feature or capability"], "limits": ["clearly stated non-price limit"]}
  ],
  "key_features": ["clearly stated feature"],
  "notes": ["important clearly stated caveat"]
}

Rules:
- Extract ONLY what is clearly stated in the supplied page text.
- Do not extract, copy, infer, or mention competitor prices, dollar amounts, discounts, billing periods, annual totals, or per-seat costs.
- Keep plan/tier names, feature differences, workflow capabilities, platform destinations, watermark/storage/export limits, credits/clips/minutes quotas, team/seats/API availability, and other stable non-price facts.
- If a field is not clearly present, use null or an empty list. Never guess, infer, calculate, or normalize a number.
- Do not include marketing claims unless the page clearly states them as product capabilities.
- Do not compare products. Extract this competitor only.
"""


class _TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style", "noscript", "svg"}:
            self._skip_depth += 1
            return
        if tag.lower() in {"p", "br", "li", "tr", "h1", "h2", "h3", "h4", "section", "div"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript", "svg"} and self._skip_depth:
            self._skip_depth -= 1
            return
        if tag.lower() in {"p", "li", "tr", "h1", "h2", "h3", "h4", "section"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)

    def text(self) -> str:
        text = html.unescape(" ".join(self.parts))
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        text = re.sub(r"\n\s+", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def html_to_text(data: bytes) -> str:
    source = data.decode("utf-8", "ignore")
    source = re.sub(r"(?is)<(script|style|noscript|svg)\b.*?</\1>", " ", source)
    parser = _TextParser()
    parser.feed(source)
    text = parser.text()
    lines = []
    seen: set[str] = set()
    for line in text.splitlines():
        cleaned = re.sub(r"\s+", " ", line).strip()
        if len(cleaned) < 2:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        lines.append(cleaned)
    return "\n".join(lines)[:MAX_PAGE_TEXT_CHARS]


def _section_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _existing_sections() -> dict[str, str]:
    if not FACTS_PATH.is_file():
        return {}
    text = FACTS_PATH.read_text(encoding="utf-8")
    sections: dict[str, str] = {}
    pattern = re.compile(
        r"<!-- competitor-facts:(?P<key>[a-z0-9-]+):start -->\n(?P<body>.*?)\n<!-- competitor-facts:(?P=key):end -->",
        re.S,
    )
    for match in pattern.finditer(text):
        sections[match.group("key")] = match.group(0).strip()
    return sections


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _string_or_null(value: Any) -> str:
    text = str(value or "").strip()
    return text if text else "Not clearly stated"


def _bullet_lines(values: list[Any], *, fallback: str = "Not clearly stated") -> list[str]:
    lines = []
    for value in values:
        text = str(value or "").strip()
        if text:
            lines.append(f"- {text}")
    return lines or [f"- {fallback}"]


def _strip_price_items(values: Any) -> list[Any]:
    cleaned = []
    for value in _as_list(values):
        text = str(value or "").strip()
        if not text:
            continue
        if re.search(r"\$\s*\d|(?:billed|billing|discount|save\s+\d+%|off\b|price|pricing|cost)", text, flags=re.I):
            continue
        cleaned.append(value)
    return cleaned


def remove_competitor_prices(facts: dict[str, Any]) -> dict[str, Any]:
    tiers = _as_list(facts.get("tiers") or facts.get("pricing_tiers"))
    normalized: list[dict[str, Any]] = []
    for tier in tiers:
        if not isinstance(tier, dict):
            continue
        current = dict(tier)
        for key in ("monthly_price", "price", "price_unverified", "billing", "annual_price", "yearly_price"):
            current.pop(key, None)
        current["included"] = _strip_price_items(current.get("included"))
        current["limits"] = _strip_price_items(current.get("limits"))
        current["notes"] = _strip_price_items(current.get("notes"))
        normalized.append(current)
    facts["tiers"] = normalized
    facts.pop("pricing_tiers", None)
    facts["key_features"] = _strip_price_items(facts.get("key_features"))
    facts["notes"] = _strip_price_items(facts.get("notes"))
    return facts


def render_competitor_section(facts: dict[str, Any]) -> str:
    name = str(facts.get("name") or "").strip()
    key = _section_key(name)
    source_url = str(facts.get("source_url") or "").strip()
    last_fetched = str(facts.get("last_fetched") or "").strip()
    lines = [
        f"<!-- competitor-facts:{key}:start -->",
        f"## {name}",
        "",
        f"- Source: {source_url}",
        f"- Last fetched: {last_fetched}",
        "",
        "### Plan / tier structure",
    ]
    tiers = _as_list(facts.get("tiers"))
    if tiers:
        for tier in tiers:
            if not isinstance(tier, dict):
                continue
            lines.extend(
                [
                    "",
                    f"#### {_string_or_null(tier.get('name'))}",
                    "- Included:",
                    *_bullet_lines(_as_list(tier.get("included"))),
                    "- Limits:",
                    *_bullet_lines(_as_list(tier.get("limits"))),
                ]
            )
    else:
        lines.append("- Not clearly stated")
    lines.extend(["", "### Key features", *_bullet_lines(_as_list(facts.get("key_features")))])
    notes = _as_list(facts.get("notes"))
    if notes:
        lines.extend(["", "### Notes", *_bullet_lines(notes)])
    lines.append(f"<!-- competitor-facts:{key}:end -->")
    return "\n".join(lines).strip()


def render_file(sections: dict[str, str]) -> str:
    today = datetime.now(timezone.utc).date().isoformat()
    lines = [
        "# Competitor Facts",
        "",
        f"Auto-generated on {today}. This file may be stale; treat it as a reference and verify before publishing.",
        "Competitor prices are intentionally omitted. Use this file for feature, workflow, and limit comparisons only.",
        "",
    ]
    for competitor in COMPETITORS:
        key = _section_key(competitor["name"])
        section = sections.get(key)
        if section:
            lines.extend([section.strip(), ""])
    return "\n".join(lines).rstrip() + "\n"


def extract_competitor(competitor: dict[str, str]) -> tuple[dict[str, Any], Any]:
    source_url = competitor["source_url"]
    if not robots_allows(source_url):
        raise RuntimeError("robots.txt disallows source URL")
    status, final_url, content_type, data = fetch_url(source_url, accept="text/html,application/xhtml+xml")
    if not status or status >= 400:
        raise RuntimeError(f"fetch failed with status {status or 'error'}")
    page_text = html_to_text(data)
    if len(page_text) < 200:
        raise RuntimeError(f"page text too short ({len(page_text)} chars, content_type={content_type})")
    last_fetched = datetime.now(timezone.utc).date().isoformat()
    result = call_json(
        "competitor_facts",
        model=BLOG_MODEL_REVIEWER,
        system_prompt=EXTRACTION_SYSTEM_PROMPT,
        user_prompt=json_dumps_compact(
            {
                "name": competitor["name"],
                "source_url": final_url or source_url,
                "last_fetched": last_fetched,
                "page_text": page_text,
            }
        ),
    )
    payload = json.loads(result.content)
    facts = payload.get("competitor") if isinstance(payload.get("competitor"), dict) else payload
    if not isinstance(facts, dict):
        raise RuntimeError("extractor returned non-object JSON")
    facts["name"] = competitor["name"]
    facts["source_url"] = final_url or source_url
    facts["last_fetched"] = last_fetched
    facts = remove_competitor_prices(facts)
    return facts, result


def write_atomic(text: str) -> None:
    FACTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = FACTS_PATH.with_suffix(".md.tmp")
    tmp_path.write_text(text, encoding="utf-8")
    tmp_path.replace(FACTS_PATH)


def refresh() -> dict[str, Any]:
    sections = _existing_sections()
    failures: list[str] = []
    updated: list[str] = []
    total_cost = Decimal("0")
    for competitor in COMPETITORS:
        key = _section_key(competitor["name"])
        try:
            facts, usage = extract_competitor(competitor)
            log_usage("competitor_facts", usage)
            total_cost += usage.cost_usd
            sections[key] = render_competitor_section(facts)
            updated.append(competitor["name"])
        except Exception as exc:
            if key in sections:
                failures.append(f"{competitor['name']}: {exc}; kept previous section")
                continue
            failures.append(f"{competitor['name']}: {exc}; no previous section")
    if not sections:
        detail = "; ".join(failures) if failures else "no failure details"
        raise RuntimeError("Refusing to write empty competitor_facts.md; no sections available (" + detail + ")")
    write_atomic(render_file(sections))
    return {"updated": updated, "failures": failures, "path": str(FACTS_PATH), "cost_usd": str(total_cost)}


def main() -> int:
    result = refresh()
    print("BLOG_COMPETITOR_FACTS_DONE")
    print("path=" + result["path"])
    print("updated=" + ", ".join(result["updated"]))
    if result["failures"]:
        print("failures:")
        for failure in result["failures"]:
            print("- " + failure)
    print("cost_usd=" + result["cost_usd"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
