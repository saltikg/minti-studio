# src/one_time/rewrite_titles.py
"""
Rewrite unblogged idea titles with SEO-optimized versions.
- Finds ideas that don't have a published blog_post
- Sends the old title to LLM for optimization
- Updates ideas.idea_title in DB
"""

import duckdb
import os
import argparse
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()
DB_PATH = "warehouse/blog_factory.duckdb"
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL_GPT = os.getenv("OPENAI_MODEL_GPT", "gpt-4o-mini")

client = OpenAI(api_key=OPENAI_API_KEY)

PROMPT_TMPL = """
You are an SEO strategist for affiliate marketing blogs. 
Rewrite the blog title below into a **sales-driven, SEO-optimized headline** 
that maximizes clicks and buying intent.

Rules:
- Always make the title **purchase-oriented** (focus on products, buying guides, reviews, comparisons).
- Use high-CTR words: "Best", "Top", "Review", "Guide".
- For **comparison / roundup** → Use "Best", "Top 10", and include year (e.g., 2025).
- For **buyer guide** → Use "Guide" or "How to Choose", but do NOT include year.
- For **review** → Use "Review" in a natural way.
- For **troubleshooting / use case** → Problem-solution phrasing, NO year.
- If the original title is abstract (e.g., rituals, techniques, tips), 
  rewrite it to include the **actual product type** people would buy 
  (e.g., curling irons, balsam products, whitening kits).
- Avoid redundant wording (never say "Best Top 5").
- Keep it concise, clear, and human-friendly.
- Do not change the main product/topic.
- Return only the rewritten title, no commentary.

Original: "{title}"
"""



def connect_db():
    return duckdb.connect(DB_PATH, read_only=False)

def fetch_unblogged_ideas(limit=5):
    con = connect_db()
    query = """
        SELECT i.idea_id, i.idea_title
        FROM ideas i
        LEFT JOIN blog_posts b ON b.idea_id = i.idea_id
        WHERE b.idea_id IS NULL OR b.status != 'published'
        ORDER BY random()
        LIMIT ?
    """
    return con.execute(query, [limit]).fetchdf()

def update_title_in_db(idea_id, new_title):
    con = connect_db()
    con.execute(
        "UPDATE ideas SET idea_title = ? WHERE idea_id = ?",
        [new_title, idea_id]
    )

def rewrite_title(old_title: str) -> str:
    resp = client.chat.completions.create(
        model=OPENAI_MODEL_GPT,
        messages=[
            {"role": "system", "content": "You are an SEO title generator."},
            {"role": "user", "content": PROMPT_TMPL.format(title=old_title)},
        ],
        max_tokens=60,
        temperature=0.4,
    )
    return resp.choices[0].message.content.strip()

def main(limit: int):
    df = fetch_unblogged_ideas(limit=limit)
    if df.empty:
        print("ℹ️ No unblogged ideas found to rewrite.")
        return

    for _, row in df.iterrows():
        idea_id = row["idea_id"]
        old_title = row["idea_title"]

        print(f"\n🔎 Original: {old_title}")
        new_title = rewrite_title(old_title)
        print(f"✨ Rewritten: {new_title}")

        update_title_in_db(idea_id, new_title)
        print(f"💾 Updated in DB (idea_id={idea_id})")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=5, help="How many ideas to rewrite")
    args = parser.parse_args()

    main(limit=args.limit)
