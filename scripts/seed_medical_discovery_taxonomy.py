#!/usr/bin/env python3
"""Seed the clinician-focused discovery taxonomy and retire off-focus queued work."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
load_dotenv(REPO_ROOT / ".env")

from app import create_app
from app.video_shorts.config import OPENAI_MODEL, _openai_client
from app.video_shorts.routes.api import (
    MEDICAL_DISCOVERY_OFF_FOCUS_TERMS,
    MEDICAL_DISCOVERY_SPECIALTIES,
    _filter_medical_category_keyword_items,
    _insert_category_keyword_items_into_queue,
    _normalized_discovery_name,
)
from app.video_shorts.services.db import get_db


MENOPAUSE_KEYWORDS = [
    "perimenopause symptoms explained",
    "menopause hormone therapy explained",
    "HRT risks and benefits women",
    "menopause weight gain belly fat",
    "perimenopause anxiety and sleep",
    "menopause brain fog help",
    "hot flashes night sweats relief",
    "bioidentical hormones explained",
    "testosterone therapy for women",
    "low progesterone symptoms women",
]


def _llm_json(prompt: str, *, temperature: float = 0.25) -> dict:
    if not _openai_client:
        raise RuntimeError("OPENAI_API_KEY missing")
    response = _openai_client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": "You create concise medical YouTube search queries for clinician outreach. Return JSON only."},
            {"role": "user", "content": prompt},
        ],
        temperature=temperature,
    )
    return json.loads((response.choices[0].message.content or "").strip())


def generate_specialty_keywords(specialties: list[str], existing_keywords: list[str]) -> dict[str, list[str]]:
    generated: dict[str, list[str]] = {}
    for attempt in range(3):
        missing = [specialty for specialty in specialties if len(generated.get(specialty, [])) != 3]
        if not missing:
            break
        prompt = (
            "For every medical specialty below, generate exactly 3 distinct YouTube search queries likely to find an individual "
            "physician or credentialed clinician teaching patients on camera. Each query must be 3-6 words. Use patient "
            "problem/symptom phrasing (for example 'knee pain after running' or 'AFib symptoms and treatment') or "
            "specialist-explains phrasing (for example 'orthopedic surgeon explains'). Do not write long video titles. "
            "Do not use hobbies, crafts, food/cooking, gardening, product reviews, or AI/business topics. Do not produce close "
            "variants of existing searched keywords. Return STRICT JSON only as "
            "{\"specialties\":[{\"category\":\"exact supplied specialty\",\"queries\":[\"...\",\"...\",\"...\"]}]}.\n\n"
            f"Specialties: {json.dumps(missing, ensure_ascii=False)}\n\n"
            f"Existing keywords to avoid: {json.dumps(existing_keywords[-500:], ensure_ascii=False)}"
        )
        payload = _llm_json(prompt, temperature=0.2 + attempt * 0.05)
        requested = {_normalized_discovery_name(item): item for item in missing}
        for item in payload.get("specialties") or []:
            supplied = requested.get(_normalized_discovery_name(item.get("category")))
            if not supplied:
                continue
            raw_items = [
                {"category": supplied, "keyword": " ".join(str(query or "").split())}
                for query in (item.get("queries") or [])
            ]
            kept, _rejected = _filter_medical_category_keyword_items(raw_items)
            unique = []
            for entry in kept:
                keyword = entry["keyword"]
                if keyword.lower() not in {value.lower() for value in existing_keywords + unique}:
                    unique.append(keyword)
            if len(unique) == 3:
                generated[supplied] = unique
                existing_keywords.extend(unique)
    missing = [specialty for specialty in specialties if len(generated.get(specialty, [])) != 3]
    if missing:
        raise RuntimeError(f"LLM did not return three valid keywords for: {', '.join(missing)}")
    return generated


def generate_category_sample(existing_categories: list[str], existing_keywords: list[str]) -> list[dict]:
    prompt = (
        "Dry run only: propose exactly 2 new condition-level medical discovery categories for individual credentialed clinicians "
        "who teach patients on camera. Each must name one supplied parent specialty and use the name format "
        "'Parent specialty — subtopic'. Give exactly 3 queries per category; each query must be 3-6 words and use patient "
        "problem/symptom phrasing or specialist-explains phrasing. Return STRICT JSON only as "
        "{\"categories\":[{\"name\":\"...\",\"parent_specialty\":\"...\",\"queries\":[\"...\",\"...\",\"...\"]}]}.\n\n"
        f"Medical taxonomy: {json.dumps(MEDICAL_DISCOVERY_SPECIALTIES, ensure_ascii=False)}\n\n"
        f"Existing categories to avoid: {json.dumps(existing_categories, ensure_ascii=False)}\n\n"
        f"Existing keywords to avoid: {json.dumps(existing_keywords[-500:], ensure_ascii=False)}"
    )
    payload = _llm_json(prompt, temperature=0.4)
    sample = []
    for raw in (payload.get("categories") or [])[:2]:
        name = " ".join(str(raw.get("name") or "").split())
        parent = " ".join(str(raw.get("parent_specialty") or "").split())
        items = [{"category": name, "keyword": " ".join(str(query or "").split())} for query in (raw.get("queries") or [])[:3]]
        kept, rejected = _filter_medical_category_keyword_items(items, blocked_categories=existing_categories)
        if len(kept) == 3 and not rejected and parent:
            sample.append({"name": name, "parent_specialty": parent, "queries": [item["keyword"] for item in kept]})
    if len(sample) != 2:
        raise RuntimeError("Dry-run category sample did not produce 2 valid categories x 3 queries")
    return sample


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    app = create_app()
    with app.app_context():
        read_conn = get_db()
        try:
            category_rows = read_conn.execute("SELECT id, name, status FROM discovery_categories ORDER BY id").fetchall()
            existing_keywords = [str(row[0]) for row in read_conn.execute("SELECT keyword FROM keyword_queue ORDER BY id").fetchall()]
        finally:
            read_conn.close()
        existing_categories = [str(row[1]) for row in category_rows]
        generated = generate_specialty_keywords(
            [item for item in MEDICAL_DISCOVERY_SPECIALTIES if item != "Menopause & perimenopause"],
            list(existing_keywords),
        )
        generated["Menopause & perimenopause"] = list(MENOPAUSE_KEYWORDS)
        sample = generate_category_sample(existing_categories + MEDICAL_DISCOVERY_SPECIALTIES, existing_keywords)
        if not args.apply:
            print(json.dumps({"dry_run": True, "keywords": generated, "category_sample": sample}, indent=2, ensure_ascii=False))
            return 0

        conn = get_db()
        try:
            normalized_existing = {_normalized_discovery_name(row[1]): (int(row[0]), str(row[1])) for row in category_rows}
            category_ids: dict[str, int] = {}
            created_categories: list[str] = []
            skipped_categories: list[str] = []
            for specialty in MEDICAL_DISCOVERY_SPECIALTIES:
                existing = normalized_existing.get(_normalized_discovery_name(specialty))
                if existing:
                    category_ids[specialty] = existing[0]
                    skipped_categories.append(existing[1])
                    continue
                row = conn.execute(
                    "INSERT INTO discovery_categories (name, status, source, created_at) VALUES (?, 'active', 'medical_taxonomy', CURRENT_TIMESTAMP) RETURNING id",
                    [specialty],
                ).fetchone()
                category_ids[specialty] = int(row[0])
                created_categories.append(specialty)

            dropped_categories = []
            dropped_ids = []
            for category_id, name, status in category_rows:
                normalized = _normalized_discovery_name(name)
                if any(_normalized_discovery_name(term) in normalized for term in MEDICAL_DISCOVERY_OFF_FOCUS_TERMS):
                    if str(status) != "dropped":
                        conn.execute("UPDATE discovery_categories SET status = 'dropped', decided_at = CURRENT_TIMESTAMP WHERE id = ?", [category_id])
                    dropped_categories.append(str(name))
                    dropped_ids.append(int(category_id))

            retired_rows = []
            if dropped_ids:
                placeholders = ",".join(["?"] * len(dropped_ids))
                retired_rows = conn.execute(
                    f"SELECT id, keyword FROM keyword_queue WHERE status = 'queued' AND category_id IN ({placeholders}) ORDER BY id",
                    dropped_ids,
                ).fetchall()
                conn.execute(
                    f"UPDATE keyword_queue SET status='retired', next_run_at=NULL, updated_at=CURRENT_TIMESTAMP WHERE status='queued' AND category_id IN ({placeholders})",
                    dropped_ids,
                )
            seo_rows = conn.execute(
                "SELECT id, keyword FROM keyword_queue WHERE status='queued' AND lower(keyword)=lower('AI-powered SEO checklists')"
            ).fetchall()
            if seo_rows:
                conn.execute(
                    "UPDATE keyword_queue SET status='retired', next_run_at=NULL, updated_at=CURRENT_TIMESTAMP WHERE status='queued' AND lower(keyword)=lower('AI-powered SEO checklists')"
                )
                retired_rows.extend(seo_rows)

            inserted_by_category = {}
            duplicates_by_category = {}
            for specialty in MEDICAL_DISCOVERY_SPECIALTIES:
                items = [{"category": specialty, "keyword": keyword} for keyword in generated[specialty]]
                result = _insert_category_keyword_items_into_queue(
                    conn,
                    items,
                    source="medical_taxonomy",
                    default_category_id=category_ids[specialty],
                    priority=0,
                    due_now=False,
                    blocked_categories=[],
                )
                inserted_by_category[specialty] = result["keywords"]
                duplicates_by_category[specialty] = int(result["already_present"])
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        print(json.dumps({
            "created_categories": created_categories,
            "existing_categories_skipped": skipped_categories,
            "keywords_inserted": inserted_by_category,
            "duplicates_skipped": duplicates_by_category,
            "dropped_categories": dropped_categories,
            "retired_keywords": [str(row[1]) for row in retired_rows],
            "category_sample_dry_run": sample,
        }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
