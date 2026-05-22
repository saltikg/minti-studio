# app/admin/youtube_idea_expander.py

import os
import json
from openai import OpenAI

DB_PATH = os.getenv("DB_PATH", "/home/ubuntu/blog-factory/warehouse/blog_factory.duckdb")
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


def expand_trend_idea(idea_text: str, caption_summary: str | None = None) -> dict:
    """
    Bir trend fikrini blog intro ve eBay ürünlerine bağlanacak
    arama ipuçları ile genişletir.

    Output:
      {
        "blog_intro": "...",
        "ebay_search_query": "...",
        "products_angle": "how to connect to products"
      }
    """
    summary_part = f"\nRelated video summary:\n{caption_summary}\n" if caption_summary else ""

    prompt = f"""
You are a content planner for an affiliate blog.

Trend idea:
{idea_text}
{summary_part}

Task:
1) Write a short blog style introduction for this idea, 2 to 3 short paragraphs.
   Tone: practical, shopping focused, not fluffy.
2) Suggest one concise search query string that could be used to find products on eBay.
3) Briefly describe how to connect this idea to specific product types
   like categories, gift bundles, or comparison lists.

Return JSON only with keys:
- blog_intro
- ebay_search_query
- products_angle
"""

    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "You create ecommerce focused content plans."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.4,
    )

    content = resp.choices[0].message.content
    try:
        data = json.loads(content)
    except Exception:
        # başarısız olursa minimum yapı döndür
        return {
            "blog_intro": content,
            "ebay_search_query": "",
            "products_angle": "",
        }
    return data
