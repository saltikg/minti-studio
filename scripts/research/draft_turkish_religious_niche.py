#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import requests


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv

load_dotenv(REPO_ROOT / ".env")

from app.video_shorts.config import _openai_client
from app.video_shorts.services.db import get_db
from app.video_shorts.services.youtube_quota import (
    record_youtube_data_api_call,
    require_youtube_quota_available,
    unit_cost,
)
from scripts.research import reclassify_women_hormones as legacy


CHANNEL_ID = "UCpZTTbddOVYPvQj02Zu8x_A"
BRAND_NAME = "Hocaefendiden Kisa Kisa"
SLUG = "turkish_religious_talks"
TARGET_FOCUS = "religious_spiritual_talks"
CACHE_ROOT = REPO_ROOT / "data" / "research" / "cache"
FEATURE = "turkish_religious_talks_research"
YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"


def _cached_titles() -> list[str]:
    titles = []
    seen = set()
    for path in sorted((CACHE_ROOT / "uploads" / CHANNEL_ID).glob("page_*.json")):
        for item in legacy._cache_payload(path).get("items") or []:
            title = " ".join(str((item.get("snippet") or {}).get("title") or "").split())
            if title and title.casefold() not in seen:
                titles.append(title)
                seen.add(title.casefold())
    return titles


def _refresh_titles(cap: int) -> int:
    method = "playlistItems.list"
    cost = unit_cost(method)
    if cost > cap:
        raise RuntimeError(f"YouTube cap {cap} is below the {cost}-unit request cost")
    api_key = str(os.getenv("YOUTUBE_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("YOUTUBE_API_KEY is required to fill the seed cache")
    require_youtube_quota_available(feature=FEATURE, method=method)
    params = {
        "part": "snippet,contentDetails",
        "playlistId": f"UU{CHANNEL_ID[2:]}",
        "maxResults": 50,
    }
    response = requests.get(
        f"{YOUTUBE_API_BASE}/playlistItems",
        params={**params, "key": api_key},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    cache_dir = CACHE_ROOT / "uploads" / CHANNEL_ID
    cache_dir.mkdir(parents=True, exist_ok=True)
    destination = cache_dir / "page_seed_refresh.json"
    temporary = destination.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({"params": params, "payload": payload}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(destination)
    record_youtube_data_api_call(feature=FEATURE, method=method)
    return cost


def _usage(response) -> dict[str, Any]:
    usage = response.usage
    input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": (input_tokens * 2.0 + output_tokens * 8.0) / 1_000_000,
    }


def _draft(titles: list[str]) -> tuple[dict[str, Any], dict[str, Any]]:
    response = _openai_client.chat.completions.create(
        model="gpt-4.1",
        temperature=0.1,
        max_tokens=2200,
        response_format={"type": "json_object"},
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a careful taxonomy researcher. Return one JSON object only. "
                    "Keep the category broad enough for Turkish Islamic sohbet and spiritual talks; "
                    "do not split it into doctrinal sub-groups."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Using the seed channel titles, draft a category definition. Return exactly: "
                    "name_suggestions (3 short Turkish names), description (150-250 words in English), "
                    "core_topics, adjacent_topics, excluded_topics, focus_options, search_queries. "
                    "focus_options must be a JSON list of 5-7 objects with key and short English "
                    "description. It must include target key religious_spiritual_talks plus neighbouring "
                    "focuses such as quran_recitation, religious_news_politics, general_motivation, and "
                    "institution_mosque. search_queries must contain exactly 20 useful Turkish YouTube "
                    "queries for finding similar individual speakers and sohbet channels. Distinguish "
                    "teaching/spiritual talks from channels mainly publishing recitation, political news, "
                    "generic motivation, or institutional broadcasts. Definition language must stay English "
                    "for a consistent classifier prompt. Seed brand: " + BRAND_NAME + ". Titles: "
                    + json.dumps(titles[:50], ensure_ascii=False)
                ),
            },
        ],
    )
    draft = json.loads(response.choices[0].message.content or "{}")
    description = " ".join(str(draft.get("description") or "").split())
    if not 150 <= len(description.split()) <= 250:
        raise RuntimeError("draft description must contain 150-250 words")
    draft["description"] = description
    if not isinstance(draft.get("name_suggestions"), list) or len(draft["name_suggestions"]) != 3:
        raise RuntimeError("draft must contain three name suggestions")
    for key in ("core_topics", "adjacent_topics", "excluded_topics"):
        if not isinstance(draft.get(key), list) or not draft[key]:
            raise RuntimeError(f"draft {key} is invalid")
    options = draft.get("focus_options")
    if not isinstance(options, list) or TARGET_FOCUS not in {
        str(item.get("key") or "") for item in options if isinstance(item, dict)
    }:
        raise RuntimeError("draft focus_options is missing the target focus")
    if not isinstance(draft.get("search_queries"), list) or len(draft["search_queries"]) != 20:
        raise RuntimeError("draft must contain 20 search queries")
    draft["target_focus"] = TARGET_FOCUS
    draft["language"] = "tr"
    draft["status"] = "draft"
    return draft, _usage(response)


def _save(draft: dict[str, Any]) -> int:
    conn = get_db()
    try:
        row = conn.execute(
            """
            INSERT INTO niches (
                slug, name, category, description, core_topics, adjacent_topics,
                excluded_topics, definition_version, language, target_focus,
                focus_options, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, 'draft', CURRENT_TIMESTAMP)
            ON CONFLICT (slug) DO UPDATE SET
                name = EXCLUDED.name,
                category = EXCLUDED.category,
                description = EXCLUDED.description,
                core_topics = EXCLUDED.core_topics,
                adjacent_topics = EXCLUDED.adjacent_topics,
                excluded_topics = EXCLUDED.excluded_topics,
                language = EXCLUDED.language,
                target_focus = EXCLUDED.target_focus,
                focus_options = EXCLUDED.focus_options,
                status = 'draft'
            RETURNING id
            """,
            [
                SLUG, str(draft["name_suggestions"][0]), "Religion & Spirituality",
                draft["description"], json.dumps(draft["core_topics"], ensure_ascii=False),
                json.dumps(draft["adjacent_topics"], ensure_ascii=False),
                json.dumps(draft["excluded_topics"], ensure_ascii=False), "tr", TARGET_FOCUS,
                json.dumps(draft["focus_options"], ensure_ascii=False),
            ],
        ).fetchone()
        conn.commit()
        return int(row[0])
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main() -> int:
    titles = _cached_titles()
    units = 0
    if len(titles) < 50:
        units = _refresh_titles(50)
        titles = _cached_titles()
    if not titles:
        raise RuntimeError("seed channel has no cached titles")
    draft, usage = _draft(titles)
    niche_id = _save(draft)
    print(json.dumps({
        "niche_id": niche_id,
        "slug": SLUG,
        "seed_title_count": len(titles[:50]),
        "youtube_units_used": units,
        "llm_usage": usage,
        "draft": draft,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
