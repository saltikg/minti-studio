#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Dynamic LLM-based Blog Generator (Deal Mode 3)
----------------------------------------------
- Works with existing eBay Browse + DuckDB structure.
- Generates dynamic title and introduction using LLM (no static deal templates).
- Uses same blog publishing pipeline as 2-ebay_products_ingest.py.

Usage:
  python3 src/ebay/3-ebay_dynamic_ingest.py \
    --keyword "national dessert day" \
    --category-slug seasonal \
    --limit 80 --price-min 15 --price-max 300
"""

import os, re, json, duckdb, requests, argparse
from openai import OpenAI
from dotenv import load_dotenv
from datetime import datetime
import time
import unicodedata


load_dotenv("/home/ubuntu/blog-factory/.env")

DB_PATH = os.getenv("DB_PATH", "/home/ubuntu/blog-factory/warehouse/blog_factory.duckdb")
WEB_ROOT = "/var/www/html"
BASE_URL = "https://mintistudio.com"
TRENDING_CATEGORY_SLUG = os.getenv("TRENDING_CATEGORY_SLUG", "trending-now")

# --- Affiliate Settings ---
EBAY_AFFILIATE_CAMPAIGN_ID = os.getenv("EPN_DEFAULT_CAMPID")
EBAY_AFFILIATE_TOOL_ID = os.getenv("EPN_TOOL_ID", "10001")


client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
EBAY_BASE = "https://api.ebay.com"

CATEGORY_MAP = {
    "watches": "31387",
    "cell_phones": "9355",
    "jewelry": "281",
    "handbags": "169291",
    "fashion": "11450",
    "seasonal": "11450"
}


def slugify(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = s.encode("ascii", "ignore").decode("ascii")
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    s = re.sub(r"-{2,}", "-", s)
    return s or "post"




def ensure_unique_slug(con, base_slug: str, idea_id: str) -> str:
    """
    Aynı slug mevcutsa (ve farklı idea_id'ye aitse) -2, -3 ... ekle.
    Kendi idea_id’mize ait olan aynı slug kabul edilir (yeniden yayın).
    """
    slug = base_slug
    i = 2
    while True:
        row = con.execute(
            "SELECT 1 FROM blog_contents WHERE slug = ? AND idea_id <> ? LIMIT 1",
            [slug, str(idea_id)]
        ).fetchone()
        if not row:
            return slug
        slug = f"{base_slug}-{i}"
        i += 1


def http_get(path, params):
    from ebay_auth import get_token
    headers = {
        "Authorization": f"Bearer {get_token()}",
        "X-EBAY-C-MARKETPLACE-ID": os.getenv("EBAY_MARKETPLACE", "EBAY_US"),
        "Accept": "application/json",
    }
    r = requests.get(f"{EBAY_BASE}{path}", headers=headers, params=params, timeout=20)
    if r.status_code == 401:
        get_token(force_refresh=True)
        r = requests.get(f"{EBAY_BASE}{path}", headers=headers, params=params, timeout=20)
    r.raise_for_status()
    return r.json()


import re


def normalize_image_url(url):
    if not url:
        return url
    return re.sub(r's-l\d+\.(jpg|jpeg|png|webp)$', 's-l1600.webp', url)


def generate_dynamic_blog_intro(keyword, category_slug, top_titles):
    prompt = f"""
You are a content writer for an affiliate deals website.
Write a detailed blog about trending products or seasonal ideas.

Keyword: "{keyword}"
Category: "{category_slug}"
Example products: {", ".join(top_titles[:6])}

Generate a full JSON with these keys:
{{
  "title": "A catchy, SEO-optimized title that includes '2025'.",
  "intro": "A 3-5 sentence introduction. It should be conversational and set the context.",
  "buyers_guide": "A practical buying guide with 3-4 distinct sections. Each section MUST have a bolded subheading (e.g., '**1. Focus on Quality and Fabric**'). Do NOT include product card placeholders.",
  "faq": "3-5 common questions with short answers in a markdown list format (e.g., '**1. Question?** Answer.').",
  "conclusion": "A short closing summary encouraging readers to explore or shop."
}}

Rules:
- Use markdown for bolding and structure.
- Keep tone natural and informative.
- Focus on buyer usefulness, not hype.
- Mention examples from the product list naturally.
- Keep FAQ in Q&A format with markdown bullets.
Return only valid JSON.
"""
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.8,
            max_tokens=800,
        )
        text = resp.choices[0].message.content.strip()
        # More robust JSON extraction: find the JSON code block
        match = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
        if match:
            json_str = match.group(1)
        else:
            json_str = text[text.find("{") : text.rfind("}") + 1]
        data = json.loads(json_str)
        return (
            data.get("title"),
            data.get("intro"),
            data.get("buyers_guide"),
            data.get("faq"),
            data.get("conclusion"),
        )
    except Exception as e:
        print(f"⚠️ LLM blog generation failed: {e}")
        return None, None, None, None, None


def _generate_product_card_html(product_data: dict) -> str:
    """Generates a simple HTML card for a product."""
    title = product_data.get("title", "Product")
    image_url = product_data.get("image_url", "")
    price = product_data.get("price", "")
    item_web_url = product_data.get("item_web_url", "#")
    base_url = product_data.get("item_web_url", "#")
    custom_id = product_data.get("custom_id", "trend-post")

    # Append affiliate tracking parameters to the URL
    if EBAY_AFFILIATE_CAMPAIGN_ID:
        affiliate_params = f"campid={EBAY_AFFILIATE_CAMPAIGN_ID}&toolid={EBAY_AFFILIATE_TOOL_ID}&customid={custom_id}"
        if "?" in base_url:
            item_web_url = f"{base_url}&{affiliate_params}"
        else:
            item_web_url = f"{base_url}?{affiliate_params}"
    else:
        item_web_url = base_url

    # Basic inline styles for a simple card. Consider moving to CSS classes on frontend.
    # Added a "View on eBay" button for compliance.
    html = f"""
    <div style="border: 1px solid #eee; padding: 10px; margin: 15px 5px; text-align: center; max-width: 200px; display: inline-block; vertical-align: top; box-shadow: 0 2px 4px rgba(0,0,0,0.1); border-radius: 5px;">
        <img src="{image_url}" alt="{title}" style="max-width: 100%; height: 150px; object-fit: cover; display: block; margin: 0 auto 10px; border-radius: 3px;">
        <h4 style="font-size: 0.9em; margin: 0 0 10px; line-height: 1.2; font-weight: normal; height: 4.5em; overflow: hidden;">{title}</h4>
        <p style="font-weight: bold; color: #e53935; font-size: 1.1em; margin: 0 0 10px;">{price}</p>
        <a href="{item_web_url}" target="_blank" rel="noopener noreferrer" style="display: inline-block; padding: 8px 12px; background-color: #3665f3; color: white; text-decoration: none; border-radius: 20px; font-size: 0.9em; font-weight: bold;">View on eBay</a>
    </div>
    """
    return html


def save_product_and_link(con, item: dict, idea_id, category_slug: str = None) -> None:
    """
    1) Ürünü `products` tablosuna upsert eder
    2) Görseli `product_media` tablosuna ekler (varsa)
    3) Ürünü fikre `idea_products` ile bağlar (FK -> trend_ideas)
       NOT: Bu fonksiyon çağrılmadan önce blog_contents INSERT yapılmış olmalı.

    FK güvenliği:
      - products.category_slug: Şimdilik NULL yazıyoruz (referans tabloya bağlı FK çakışmasın diye)
      - idea_products.idea_id: trend_ideas(idea_id) INT beklediği için kesin INT’e çeviriyoruz
      - trend_ideas’ta ebeveyn kayıt yoksa bağ kurmayı atlıyoruz
    """
    # 0) Ürün kimliği
    pid = item.get("itemId")
    if not pid:
        return

    # 1) Ürün temel alanları
    price_value = None
    price_obj = item.get("price") or {}
    if isinstance(price_obj, dict):
        price_value = price_obj.get("value")

    title = item.get("title")
    brand = item.get("brand")

    # 2) products upsert (category_slug -> NULL; aksi halde FK referans hatası alabilirsin)
    con.execute(
        """
        INSERT OR REPLACE INTO products
            (parent_asin, product_title, brand, price, category_slug, source, external_id)
        VALUES
            (?, ?, ?, ?, ?, 'ebay', ?)
        """,
        [pid, title, brand, price_value, None, pid]
    )

    # 3) product_media (görsel)
    img_url = normalize_image_url(((item.get("image") or {}).get("imageUrl")))
    if img_url:
        con.execute(
            "INSERT OR IGNORE INTO product_media (parent_asin, image_url, source) VALUES (?, ?, 'ebay')",
            [pid, img_url]
        )

    # 4) idea_products (FK -> ideas)
    idea_id_str = str(idea_id)

    # Ebeveyn gerçekten var mı? (ideas veya trend_ideas)
    parent_exists = con.execute(
        "SELECT 1 FROM ideas WHERE idea_id = ? LIMIT 1", [idea_id_str]
    ).fetchone() or con.execute(
        "SELECT 1 FROM trend_ideas WHERE idea_id = ? LIMIT 1", [idea_id_str]
    ).fetchone()

    if not parent_exists:
        return



    # Bağı ekle (idea_products.idea_id de VARCHAR)
    con.execute(
        "INSERT OR IGNORE INTO idea_products (idea_id, parent_asin) VALUES (?, ?)",
        [idea_id_str, pid]
    )



def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keyword", required=True)
    ap.add_argument("--category-slug", default="seasonal")
    ap.add_argument("--limit", type=int, default=60)
    ap.add_argument("--price-min", type=int, default=15)
    ap.add_argument("--price-max", type=int, default=400)
    ap.add_argument("--idea-id", help="The original idea_id from trend_ideas table.")
    args = ap.parse_args()

    print(f"🟩 Running Dynamic Ingest for '{args.keyword}' (category={args.category_slug})")

    # 1️⃣ Query eBay
    cat_id = CATEGORY_MAP.get(args.category_slug, "11450")
    params = {
        "q": args.keyword,
        "limit": args.limit,
        "filter": f"priceCurrency:USD,price:[{args.price_min}..{args.price_max}],buyingOptions:{{FIXED_PRICE,BEST_OFFER}}"
    }
    data = http_get("/buy/browse/v1/item_summary/search", params)
    items = data.get("itemSummaries", [])
    if not items:
        print("❌ No items found.")
        return

    # Use the passed idea_id if available, otherwise generate a new one.
    if args.idea_id:
        idea_id = args.idea_id
    else:
        idea_id = f"trend-{args.keyword.replace(' ', '-')}-{datetime.utcnow().strftime('%Y%m%d-%H%M')}"

    
    con = duckdb.connect(DB_PATH, read_only=False)

    try:
        con.execute("UPDATE trend_ideas SET status='in_progress' WHERE idea_id = ?", [str(idea_id)])
    except Exception as e:
        print(f"⚠️ Unable to set in_progress: {e}")

     

   
    top_items = items[:8]
    top_titles = [i["title"] for i in top_items]

    # Collect detailed product info for top 5 items for embedding as cards
    product_cards_data = []
    image_urls = []
    for i, item in enumerate(top_items[:5]):
        img_url = normalize_image_url((item.get("image") or {}).get("imageUrl"))
        image_urls.append(img_url)
        product_cards_data.append({
            "title": item.get("title"),
            "image_url": img_url,
            "price": (item.get("price") or {}).get("value"),
            "item_web_url": item.get("itemWebUrl"),
            "custom_id": f"trend-{idea_id}"
        })


    # 2️⃣ Generate blog intro + title
    title, intro, guide, faq, conclusion = generate_dynamic_blog_intro(args.keyword, args.category_slug, top_titles)

    if not title:
        title = f"{args.keyword.title()} 2025: Trending Picks"
    if not intro:
        intro = f"Discover what's trending around '{args.keyword}' in 2025 — here are this week's most interesting finds and gifts."

    # Append a general affiliate disclosure to the conclusion for compliance.
    disclosure_text = "\n\n<hr><p><small><i>Our posts may contain affiliate links. As an eBay Partner, we may be compensated if you make a purchase through links on our site.</i></small></p>"
    if conclusion:
        conclusion += disclosure_text

    # 3️⃣ Save blog
    idea_id = args.idea_id or f"trend-{args.keyword.replace(' ', '-')}-{datetime.utcnow().strftime('%Y%m%d-%H%M')}"
    base_slug = slugify(title or args.keyword)
    con = duckdb.connect(DB_PATH, read_only=False)

    # Slug’ı benzersizleştir (farklı idea_id’lerle çakışmasın)
    slug = ensure_unique_slug(con, base_slug, str(idea_id))

    # UPSERT (DuckDB → INSERT OR REPLACE)
    con.execute("""
        INSERT OR REPLACE INTO blog_contents (
            idea_id, title, slug, category_slug,
            hero_image_url, hero_alt,
            introduction, buyers_guide, faq, conclusion, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, now())
    """, [
        str(idea_id), title,
        slug, TRENDING_CATEGORY_SLUG,   # ← burada sabit
        (image_urls[0] if image_urls else None),
        title,
        (intro or ""), (guide or ""), (faq or ""), (conclusion or "")
    ])

    # Ürünleri bağla
    for item in top_items[:5]:
        save_product_and_link(con, item, idea_id, args.category_slug)

    # Son slug’ı DB’den doğrula ve yazdır
    row = con.execute("""
        SELECT idea_id, slug, category_slug
        FROM blog_contents
        WHERE idea_id = ?
        LIMIT 1
    """, [str(idea_id)]).fetchone()

    if row:
        _iid, _slug, _cat = row
        print(f"📝 Blog Created → {title}")
        print(f"🌐 URL: https://mintistudio.com/{_cat}/{_slug}/")
        # --- Yayınlandı olarak işaretle + son çalışma zamanı / sayaç ---
    
    try:
        con.execute("UPDATE trend_ideas SET status='published' WHERE idea_id = ?", [str(idea_id)])
    except Exception as e:
        print(f"⚠️ Idea state update skipped: {e}")


    con.close()


    
if __name__ == "__main__":
    main()
