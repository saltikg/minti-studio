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
)

EXTRACTION_SYSTEM_PROMPT = """You extract competitor pricing and feature facts for a blog reference file.

Return strict JSON only:
{
  "name": "Competitor name",
  "source_url": "https://...",
  "last_fetched": "YYYY-MM-DD",
  "pricing_tiers": [
    {"name": "Tier name", "monthly_price": "$... or null", "price_unverified": false, "included": ["clearly stated item"], "limits": ["clearly stated limit"]}
  ],
  "key_features": ["clearly stated feature"],
  "notes": ["important clearly stated caveat"]
}

Rules:
- Extract ONLY what is clearly stated in the supplied page text.
- Use verified_price_candidates for tier prices. If no verified candidate fits a tier, monthly_price must be null and price_unverified must be true.
- If a field is not clearly present, use null or an empty list. Never guess, infer, calculate, or normalize a number.
- Preserve currency, billing words, and limits exactly enough to be fact-checkable.
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


def _price_number(value: str | None) -> Decimal | None:
    match = re.search(r"\$?\s*(\d+(?:\.\d+)?)", str(value or ""))
    if not match:
        return None
    try:
        return Decimal(match.group(1))
    except Exception:
        return None


def _candidate(tier: str, display: str, *, billing: str = "monthly", source: str = "raw-html", context: str = "") -> dict[str, str]:
    return {
        "tier": tier,
        "display": display,
        "billing": billing,
        "source": source,
        "context": re.sub(r"\s+", " ", context).strip()[:240],
    }


def _script_bodies(raw_html: str) -> list[str]:
    return re.findall(r"<script[^>]*>(.*?)</script>", raw_html or "", flags=re.I | re.S)


def _object_numbers(script_text: str, const_name: str) -> dict[str, str]:
    match = re.search(rf"\b{re.escape(const_name)}\s*=\s*\{{(?P<body>.*?)\}}", script_text, flags=re.S)
    if not match:
        return {}
    body = match.group("body")
    pairs = re.findall(r"\b([A-Za-z0-9_]+)\s*:\s*(\d+(?:\.\d+)?)", body)
    return {key: value for key, value in pairs}


def _raw_price_context(raw_html: str, pattern: str) -> str:
    match = re.search(pattern, raw_html, flags=re.I | re.S)
    if not match:
        return ""
    start = max(0, match.start() - 120)
    end = min(len(raw_html), match.end() + 120)
    return html.unescape(re.sub(r"<[^>]+>", " ", raw_html[start:end]))


def _extract_opusclip_prices(raw_html: str) -> list[dict[str, str]]:
    candidates: list[dict[str, str]] = []
    if re.search(r"\$0\s*USD", raw_html, flags=re.I):
        candidates.append(_candidate("Free", "$0/mo", source="raw-html", context=_raw_price_context(raw_html, r"\$0\s*USD")))
    if re.search(r"\$15\s+billed\s+monthly|\$15/mo", raw_html, flags=re.I):
        candidates.append(_candidate("Starter", "$15/mo", source="raw-html", context=_raw_price_context(raw_html, r"\$15\s+billed\s+monthly|\$15/mo")))
    for body in _script_bodies(raw_html):
        pro_base = _object_numbers(body, "PRO_BASE_PRICE")
        pro_yearly = _object_numbers(body, "PRO_YEARLY_PRICE")
        if pro_base.get("TIER1"):
            display = f"${pro_base['TIER1']}/mo"
            if pro_yearly.get("TIER1"):
                yearly = Decimal(pro_yearly["TIER1"]) * Decimal("12")
                yearly_text = str(yearly.normalize()) if yearly == yearly.to_integral() else str(yearly)
                display = f"{display}; ${pro_yearly['TIER1']}/mo billed yearly (${yearly_text}/year)"
            candidates.append(_candidate("Pro", display, source="inline-script", context="PRO_BASE_PRICE and PRO_YEARLY_PRICE"))
        for tier_key, value in pro_base.items():
            if tier_key == "TIER1":
                continue
            candidates.append(_candidate(f"Pro {tier_key}", f"${value}/mo", source="inline-script", context="PRO_BASE_PRICE"))
    if re.search(r"\bCustom\b", raw_html, flags=re.I):
        candidates.append(_candidate("Business", "Custom", billing="custom", source="raw-html", context="Custom"))
    return candidates


def _extract_klap_prices(raw_html: str) -> list[dict[str, str]]:
    text = html.unescape(raw_html)
    tier_names = ["Basic", "Pro", "Pro+"]
    prices = re.findall(r"<span>\$(14|39|94)</span>\s*<span[^>]*>\s*/mo\s*</span>", text, flags=re.I)
    candidates: list[dict[str, str]] = []
    for tier, price in zip(tier_names, prices):
        candidates.append(_candidate(tier, f"${price}/mo", source="raw-html", context=_raw_price_context(raw_html, rf"\${price}</span>\s*<span[^>]*>\s*/mo")))
    if not candidates:
        for tier, price in zip(tier_names, re.findall(r"\$(14|39|94)\b", text)):
            candidates.append(_candidate(tier, f"${price}/mo", source="raw-html"))
    return candidates


def _extract_vizard_prices(raw_html: str) -> list[dict[str, str]]:
    candidates: list[dict[str, str]] = []
    if re.search(r"\$0\b", raw_html):
        candidates.append(_candidate("Free", "$0", source="raw-html", context=_raw_price_context(raw_html, r"\$0\b")))
    return candidates


def extract_price_candidates(name: str, raw_html: str) -> list[dict[str, str]]:
    lower = name.lower()
    if lower == "opusclip":
        candidates = _extract_opusclip_prices(raw_html)
    elif lower == "klap":
        candidates = _extract_klap_prices(raw_html)
    elif lower == "vizard":
        candidates = _extract_vizard_prices(raw_html)
    else:
        candidates = []
    seen: set[tuple[str, str]] = set()
    unique: list[dict[str, str]] = []
    for item in candidates:
        key = (item["tier"].lower(), item["display"].lower())
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def _tier_matches(candidate_tier: str, tier_name: str) -> bool:
    cand = re.sub(r"[^a-z0-9+]+", " ", candidate_tier.lower()).strip()
    tier = re.sub(r"[^a-z0-9+]+", " ", tier_name.lower()).strip()
    if not cand or not tier:
        return False
    if cand == tier or cand in tier or tier in cand:
        return True
    return cand.startswith(tier.split()[0]) if tier.split() else False


def _tier_match_rank(candidate_tier: str, tier_name: str) -> tuple[int, int]:
    cand = re.sub(r"[^a-z0-9+]+", " ", candidate_tier.lower()).strip()
    tier = re.sub(r"[^a-z0-9+]+", " ", tier_name.lower()).strip()
    return (1 if cand == tier else 0, len(cand))


def _positive_numbers(text: str) -> set[Decimal]:
    values: set[Decimal] = set()
    for match in re.findall(r"\$?\s*(\d+(?:\.\d+)?)", text or ""):
        try:
            value = Decimal(match)
        except Exception:
            continue
        if value > 0:
            values.add(value)
    return values


def _strip_zero_price_items(values: Any) -> list[Any]:
    cleaned = []
    for value in _as_list(values):
        text = str(value or "").strip()
        if not text:
            continue
        if re.search(r"\$0(?:\b|/)", text):
            continue
        cleaned.append(value)
    return cleaned


def validate_tier_prices(facts: dict[str, Any], price_candidates: list[dict[str, str]]) -> dict[str, Any]:
    tiers = _as_list(facts.get("pricing_tiers"))
    normalized: list[dict[str, Any]] = []
    for tier in tiers:
        if not isinstance(tier, dict):
            continue
        current = dict(tier)
        tier_name = str(current.get("name") or "").strip()
        matches = sorted(
            [item for item in price_candidates if _tier_matches(item.get("tier", ""), tier_name)],
            key=lambda item: _tier_match_rank(item.get("tier", ""), tier_name),
            reverse=True,
        )
        verified_positive = [item for item in matches if _positive_numbers(item.get("display", ""))]
        is_free = "free" in tier_name.lower()
        price_text = str(current.get("monthly_price") or "").strip()
        price_numbers = _positive_numbers(price_text)
        if is_free and any((item.get("display") or "").strip().startswith("$0") for item in matches):
            current["monthly_price"] = "$0"
            current["price_unverified"] = False
        elif verified_positive:
            candidate = verified_positive[0]
            current["monthly_price"] = candidate["display"]
            current["price_unverified"] = False
        else:
            current["monthly_price"] = None
            current["price_unverified"] = True
            current["included"] = _strip_zero_price_items(current.get("included"))
            current["limits"] = _strip_zero_price_items(current.get("limits"))
            current["notes"] = _strip_zero_price_items(current.get("notes"))
        normalized.append(current)
    facts["pricing_tiers"] = normalized
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
        "### Pricing tiers",
    ]
    tiers = _as_list(facts.get("pricing_tiers"))
    if tiers:
        for tier in tiers:
            if not isinstance(tier, dict):
                continue
            lines.extend(
                [
                    "",
                    f"#### {_string_or_null(tier.get('name'))}",
                    f"- Price: {_string_or_null(tier.get('monthly_price')) if tier.get('monthly_price') else 'not available from the pricing page (verify on site)'}",
                    f"- Price unverified: {'yes' if tier.get('price_unverified') else 'no'}",
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
        "Writers may use competitor pricing, features, limits, and comparison claims only when they are present here.",
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
    raw_html = data.decode("utf-8", "ignore")
    price_candidates = extract_price_candidates(competitor["name"], raw_html)
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
                "verified_price_candidates": price_candidates,
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
    facts = validate_tier_prices(facts, price_candidates)
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
    missing = [_section_key(item["name"]) for item in COMPETITORS if _section_key(item["name"]) not in sections]
    if missing:
        detail = "; ".join(failures) if failures else "no failure details"
        raise RuntimeError("Refusing to write partial competitor_facts.md; missing sections: " + ", ".join(missing) + f" ({detail})")
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
