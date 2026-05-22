#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, re, argparse
from datetime import datetime
from dotenv import load_dotenv
import time
from app.db import connect_rw

# --- ENV ---
# optional: load .env if you use one
load_dotenv("/home/ubuntu/blog-factory/.env")  # gerekirse yolunu değiştirin

# --- OpenAI (new SDK) ---
from openai import OpenAI
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL   = os.getenv("OPENAI_MODEL_GPT", "gpt-4o-mini")
client = OpenAI(api_key=OPENAI_API_KEY)  # veya OpenAI() -> ENV kullanır

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def slugify(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"-+", "-", s)
    return s.strip("-")

def titlecase(s: str) -> str:
    return " ".join(w.capitalize() for w in (s or "").split())

def detect_default_category(trend_text: str) -> str:
    t = (trend_text or "").lower()
    if any(x in t for x in ["annabelle", "haunted", "halloween"]):
        return "halloween-2025"
    return "seasonal"


def ensure_trend(con, trend_text: str, desc: str | None, given_slug: str | None):
    slug = slugify(given_slug or trend_text)
    row = con.execute(
        "SELECT trend_id, trend, slug FROM trend_topics WHERE slug = ?", [slug]
    ).fetchone()
    if row:
        print(f"✅ Trend exists: {row[1]} (slug={row[2]}, id={row[0]})")
        return row[0], row[1], row[2]

    # 🔑 Python tarafında güvenli ID üret (ms timestamp) ve UNIQUE slug tut
    trend_id = int(time.time() * 1000)
    trend_title = titlecase(trend_text)

    # slug zaten varsa unique ihlali olmasın diye ekstra kontrol
    # (üstte SELECT yaptık, ama yarış olursa diye TRY/EXCEPT iyi olur)
    con.execute("""
      INSERT INTO trend_topics (trend_id, trend, description, slug, created_at)
      VALUES (?, ?, ?, ?, now())
    """, [trend_id, trend_title, desc, slug])

    print(f"🆕 Trend created: {trend_title} (slug={slug}, id={trend_id})")
    return trend_id, trend_title, slug


def llm_affiliate_title(trend_text: str, lang: str, extra_hint: str | None) -> str:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not set in environment.")

    sys_msg = "You are a concise ecommerce blog planner."
    user_prompt = f"""
Generate ONE clean, skimmable, affiliate-style blog title in { 'English' if lang=='en' else 'Turkish' }.
Topic: "{trend_text}"
Rules:
- Audience: shoppers (affiliate intent).
- Be factual, avoid clickbait, ≤ 120 chars.
- Prefer a structure like: “{trend_text.title()}: What Happens? Story + Replicas & Display Ideas (2025)”
- If Halloween/Annabelle vibe, keep tone tasteful, not sensational.
- Return ONLY the title text, no quotes, no JSON.
""".strip()
    if extra_hint:
        user_prompt += f"\nExtra notes: {extra_hint}"

    try:
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": sys_msg},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.5,
            max_tokens=80,
        )
    except Exception as e:
        raise RuntimeError(f"OpenAI request failed: {e}")

    title = (resp.choices[0].message.content or "").strip()
    return title.strip('"').strip("'")


def insert_affiliate_idea(con, trend_id: int, title: str, category_slug: str, dry_run: bool=False):
    if dry_run:
        print(f"💡 [DRY-RUN] Would insert idea: '{title}' (affiliate, {category_slug}) for trend_id={trend_id}")
        return

    # id'yi kendimiz üretelim — benzersiz ve artan
    idea_id = int(time.time() * 1000)

    con.execute("""
        INSERT INTO trend_ideas (idea_id, trend_id, title, idea_type, category_slug, status, created_at, updated_at)
        VALUES (?, ?, ?, 'affiliate', ?, 'pending', now(), now())
    """, [idea_id, trend_id, title, category_slug])

    print(f"✅ Idea inserted: id={idea_id} • '{title}' • category={category_slug}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug", required=True, help='Free text like "haunted dolls annabelle" (used to build/find slug).')
    ap.add_argument("--desc", default=None, help="Optional description to guide LLM.")
    ap.add_argument("--category-slug", default=None, help="Override target category (default auto-detect).")
    ap.add_argument("--lang", default="en", choices=["en","tr"], help="Language for generated title.")
    ap.add_argument("--dry-run", type=int, default=0, help="1 = don’t write to DB.")
    args = ap.parse_args()

    con = connect_rw()

    trend_id, trend_title, slug = ensure_trend(con, args.slug, args.desc, given_slug=None)
    category_slug = args.category_slug or detect_default_category(args.slug)

    title = llm_affiliate_title(trend_title, args.lang, args.desc)
    print(f"📝 LLM title: {title}")

    insert_affiliate_idea(con, trend_id, title, category_slug, dry_run=bool(args.dry_run))
    con.commit()
    con.close()

if __name__ == "__main__":
    main()
