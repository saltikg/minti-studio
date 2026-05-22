#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
LLM Internal Link Enricher (single blog mode)
---------------------------------------------
Kullanım:
    python3 blog_internal_links.py <idea_id>
"""

import os, sys, json, duckdb
from openai import OpenAI
from dotenv import load_dotenv

# --- ENV ---
load_dotenv("/home/ubuntu/blog-factory/.env")
DB_PATH = os.getenv("DB_PATH", "/home/ubuntu/blog-factory/warehouse/blog_factory.duckdb")
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
MODEL = "gpt-4o-mini"

BASE_URL = os.getenv("BASE_URL", "https://mintiproduct.com").rstrip("/")

client = OpenAI(api_key=OPENAI_KEY)


def db_connect():
    return duckdb.connect(DB_PATH, read_only=False)

def get_blog(con, idea_id):
    row = con.execute("""
        SELECT idea_id, title, category_slug, introduction
        FROM blog_contents
        WHERE idea_id = ?
    """, [idea_id]).fetchone()
    return row

# --- ekle ---
def get_season_info(con, idea_id):
    """idea_id → (season_id, season_name) döndürür; yoksa (None, None)."""
    row = con.execute("""
        SELECT s.id, s.season_name
        FROM season_phrases sp
        JOIN seasons s ON s.id = sp.season_id
        WHERE sp.phrase = ?
        LIMIT 1
    """, [idea_id]).fetchone()
    return (row[0], row[1]) if row else (None, None)


# --- mevcut fonksiyonu sezon filtresiyle değiştir ---
def get_related_blogs(con, category_slug, exclude_idea, season_id=None):
    """
    Aynı kategorideki yazılar arasından, (varsa) aynı season_id'ye ait olanları döndür.
    """
    if season_id is not None:
        return con.execute("""
            SELECT bc.idea_id, bc.title, bc.slug
            FROM blog_contents bc
            JOIN season_phrases sp ON sp.phrase = bc.idea_id
            WHERE bc.category_slug = ?
              AND bc.idea_id != ?
              AND sp.season_id = ?
            LIMIT 20
        """, [category_slug, exclude_idea, season_id]).fetchall()
    else:
        # Fallback: season eşleşmesi bulunamadıysa eski davranış
        return con.execute("""
            SELECT idea_id, title, slug
            FROM blog_contents
            WHERE category_slug = ? AND idea_id != ?
            LIMIT 20
        """, [category_slug, exclude_idea]).fetchall()


def enrich_overview(title, overview, related_blogs):

    prompt = f"""
You are an expert SEO content editor and digital copywriter.

Your task:
- Rewrite the overview section below to make it more **engaging, conversational, and conversion-friendly**.
- Add **one natural internal link** (Markdown format) to a relevant post from the list provided.
- Keep the style short and dynamic (3–5 sentences). Start with a **hook** — a question, an emotional statement, or a relatable observation.
- Preserve factual accuracy and category relevance.

---

Current blog title: "{title}"

Overview to edit:
---
{overview}
---

Other published blogs in the same category (use one of them for linking):
{json.dumps([{"title":t,"slug":s} for _,t,s in related_blogs], indent=2)}

---

When you rewrite:
- Begin with a **strong hook** such as:
  - “Looking for the perfect {title.lower()} to complete your setup?”
  - “It’s 2025 — and {title.lower()} are trending again for all the right reasons.”
  - “Ever wondered what makes the best {title.lower()} stand out?”
- Integrate the internal link naturally, e.g.:
  “... see our [Best Halloween Lights 2025](/seasonal/halloween-lights-2025/) guide for inspiration.”
- Use **Markdown** emphasis for key ideas: **bold** or _italic_ where it improves readability.
- End with a sentence that leads naturally into the “Quick Take” section.

---

Return ONLY valid JSON in this exact format:
{{
  "overview_updated": "<rewritten markdown overview with one embedded link>",
  "overview_link": {{"title": "...", "slug": "..."}},
  "buyers_guide": {{"title": "...", "slug": "..."}},
  "related_links": [
    {{"title": "...", "slug": "..."}},
    {{"title": "...", "slug": "..."}},
    {{"title": "...", "slug": "..."}}
  ]
}}

**Important Rules:**
- The rewritten overview must be under 100 words.
- Include exactly one internal link from the list above.
- Use Markdown syntax for the link: `[Title](/category/slug/)`
- Return JSON only — no explanations or extra text.
"""


    resp = client.chat.completions.create(
        model=MODEL,
        messages=[{"role":"user","content":prompt}],
        temperature=0.6,
        max_tokens=800,
    )

    raw = resp.choices[0].message.content.strip()
    start, end = raw.find("{"), raw.rfind("}")
    return json.loads(raw[start:end+1]) if start != -1 else {}

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 blog_internal_links.py <idea_id>")
        sys.exit(1)

    idea_id = sys.argv[1]
    con = db_connect()

    con.execute("ALTER TABLE blog_contents ADD COLUMN IF NOT EXISTS overview_updated TEXT;")
    con.execute("ALTER TABLE blog_contents ADD COLUMN IF NOT EXISTS related_links_json TEXT;")

    row = get_blog(con, idea_id)
    if not row:
        print(f"❌ Blog not found for idea_id={idea_id}")
        sys.exit(1)

    idea_id, title, category_slug, overview = row
    print(f"🟩 Processing blog: {title} (category={category_slug})")

    # 🔑 Burada sezon bilgisini bul
    season_id, season_name = get_season_info(con, idea_id)

    # 🔗 Artık aynı sezon içinden ilgili yazıları seç
    related = get_related_blogs(con, category_slug, idea_id, season_id=season_id)


    if not related:
        print("⚠️ No related blogs found in this category.")
        sys.exit(0)

    data = enrich_overview(title, overview, related)
    # Force-replace placeholder domain if model ignored BASE_URL
    # 🔧 Force-replace placeholder domains with real BASE_URL
    if data.get("overview_updated"):
        for placeholder in [
            "https://yourwebsite.com",
            "http://yourwebsite.com",
            "https://example.com",
            "http://example.com",
            "https://www.example.com",
            "http://www.example.com",
        ]:
            data["overview_updated"] = data["overview_updated"].replace(placeholder, BASE_URL)

    con.execute("""
        UPDATE blog_contents
        SET overview_updated = ?, related_links_json = ?, updated_at = now()
        WHERE idea_id = ?
    """, [data.get("overview_updated",""), json.dumps(data, ensure_ascii=False), idea_id])

    con.close()

    print(f"✅ Updated overview & links for: {title}")
    print("\n--- New Overview ---\n")
    print(data.get("overview_updated","(empty)"))


if __name__ == "__main__":
    main()
