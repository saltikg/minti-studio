#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Blog Factory — DB-integrated pipeline (FINAL)
- DB’den fikirleri seçer (ideas + idea_products, blog_posts ile filtre)
- Title rewrite → LLM ile optimize edilip DB’de güncellenir
- İçerik yazımından sonra blog_posts tablosunu günceller (status=published, blog_url)
- Markdown dosyası oluşturur (docs/blogs/{category}/{slug}/index.md)
- CSV tabanlı akış (write_from_intent_pool) geriye dönük uyumluluk için korunmuştur
"""

import argparse, os, json, time, requests, re
import pandas as pd
from tqdm import tqdm
import duckdb
from datetime import datetime, timezone


from .writer import write_blog_markdown, summarize_reviews_with_llm
from .config import (
    DATA_CSV, BLOGS_DIR, DOCS_BLOG_INDEX, IDX_DIR, BLOG_INDEX_CSV,
    DEBUG_LAST_RUN, BASE_URL
)
from .data_loader import (
    load_products, subset_by_asins,   # CSV akışı için
    load_products_db, subset_by_asins_db  # DB akışı için
)
from .utils import (
    slugify, save_markdown, append_link_to_index, build_random_recommendations,
    reading_time_minutes, write_json, build_comparison_table,
    build_product_cards, build_product_gallery, build_product_cards_responsive
)
from .hero_image import generate_hero_assets


# ======================
# DuckDB
# ======================
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "warehouse", "blog_factory.duckdb")


WEB_ROOT = os.getenv("WEB_ROOT", "/var/www/html")   # Nginx kökün neresi ise
BASE_URL = os.getenv("BASE_URL", "https://mintistudio.com")

def _ensure_dir(path: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)

def write_robots_txt(base_url: str = BASE_URL, web_root: str = WEB_ROOT):
    """
    /robots.txt dosyasını doğrudan web root'a yazar.
    """
    content = f"""User-agent: *
Allow: /

Sitemap: {base_url.rstrip('/')}/sitemap.xml
"""
    out_path = os.path.join(web_root, "robots.txt")
    _ensure_dir(out_path)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"✅ robots.txt → {out_path}")

def _fetch_published_urls():
    """
    DB'den published URL'leri çeker. blog_url varsa onu, yoksa /<slug>/ üretir.
    """
    con = connect_db()
    df = con.execute("""
        SELECT DISTINCT
            CASE
              WHEN bp.blog_url IS NOT NULL AND bp.blog_url <> ''
                   THEN bp.blog_url
              ELSE '/' || bc.slug || '/'
            END AS url,
            COALESCE(bc.updated_at, bp.updated_at, now()) AS lastmod
        FROM blog_posts bp
        LEFT JOIN blog_contents bc ON bc.idea_id = bp.idea_id
        WHERE bp.status = 'published'
    """).df()
    con.close()
    return df

def write_sitemap_xml(base_url: str = BASE_URL, web_root: str = WEB_ROOT):
    """
    Tek dosyalık basit sitemap.xml yazar (50k URL sınırının çok altındaysanız yeterli).
    Gerekirse kolayca “sitemap index + parça” yapısına genişletilebilir.
    """
    df = _fetch_published_urls()
    if df.empty:
        print("ℹ️ sitemap için published URL yok.")
        urls = []
    else:
        # mutlak hale getir + lastmod formatla
        urls = []
        for _, r in df.iterrows():
            loc = r["url"] or "/"
            if loc.startswith("/"):
                loc = base_url.rstrip("/") + loc
            lastmod = pd.to_datetime(r["lastmod"]).tz_localize(None) if pd.notna(r["lastmod"]) else datetime.utcnow()
            urls.append((loc, lastmod.strftime("%Y-%m-%d")))

    # Add category pages
    con = connect_db()
    try:
        category_slugs = con.execute("SELECT DISTINCT category_slug FROM blog_contents WHERE category_slug IS NOT NULL").df()['category_slug'].tolist()
        for slug in category_slugs:
            full_url = f"{base_url.rstrip('/')}/{slug}/"
            if not any(u[0] == full_url for u in urls):
                urls.append((full_url, datetime.utcnow().strftime("%Y-%m-%d")))
    finally:
        con.close()

    # Manually add static pages to the sitemap
    static_pages = ["/", "/about/", "/ethics-policy/", "/privacy-policy/", "/terms-of-service/", "/authors/"]
    today = datetime.utcnow().strftime("%Y-%m-%d")
    for page_path in static_pages:
        full_url = base_url.rstrip("/") + page_path
        # Avoid adding duplicates if a static page (like home '/') is already in the list
        if not any(u[0] == full_url for u in urls):
            urls.append((full_url, today))

    items = "\n".join(
        f"  <url>\n    <loc>{loc}</loc>\n    <lastmod>{lastmod}</lastmod>\n    <changefreq>weekly</changefreq>\n    <priority>0.6</priority>\n  </url>"
        for loc, lastmod in urls
    )
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
{items}
</urlset>
"""
    out_path = os.path.join(web_root, "sitemap.xml")
    _ensure_dir(out_path)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(xml)
    print(f"✅ sitemap.xml → {out_path}")

def _md_bold_to_html(text: str) -> str:
    # **...** → <strong>...</strong>
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text or "")


def connect_db():
    return duckdb.connect(DB_PATH, read_only=False)


def _ensure_blog_contents_table(con):
    con.execute("""
    CREATE TABLE IF NOT EXISTS blog_contents (
      idea_id        VARCHAR PRIMARY KEY,
      title          VARCHAR NOT NULL,
      category_slug  VARCHAR NOT NULL,
      slug           VARCHAR NOT NULL,
      front_matter   TEXT,
      hero_image_url VARCHAR,
      hero_alt       VARCHAR,
      introduction   TEXT,
      product_gallery TEXT,
      urunler        TEXT,
      buyers_guide   TEXT,
      faq            TEXT,
      conclusion     TEXT,
      recommendations TEXT,
      cta            TEXT,
      md_full        TEXT,
      updated_at     TIMESTAMP DEFAULT now()
    )
    """)
    # İsteğe bağlı: kritik alanlar boş kalmasın diye CHECK (DuckDB destekler)
    # con.execute("ALTER TABLE blog_contents ADD CHECK (length(trim(introduction)) >= 1)")
    # (Var olan tabloda alter sırasında hata almamak için isteğe bağlı bırakıyorum.)


def upsert_blog_contents(idea_id, title, slug, category_slug,
                         front_matter, introduction, product_gallery,
                         urunler, buyers_guide, faq, conclusion,
                         recommendations, cta, md_full,
                         hero_url=None, hero_alt=None):
    con = connect_db()
    _ensure_blog_contents_table(con)
    try:
        con.execute("BEGIN TRANSACTION;")
        con.execute("""
            INSERT INTO blog_contents AS bc
            (idea_id, title, slug, category_slug,
             front_matter, hero_image_url, hero_alt, introduction, product_gallery,
             urunler, buyers_guide, faq, conclusion,
             recommendations, cta, md_full, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, now())
            ON CONFLICT (idea_id) DO UPDATE SET
              title           = EXCLUDED.title,
              slug            = EXCLUDED.slug,
              category_slug   = EXCLUDED.category_slug,
              front_matter    = EXCLUDED.front_matter,
              hero_image_url  = EXCLUDED.hero_image_url,
              hero_alt        = EXCLUDED.hero_alt,
              introduction    = EXCLUDED.introduction,
              product_gallery = EXCLUDED.product_gallery,
              urunler         = EXCLUDED.urunler,
              buyers_guide    = EXCLUDED.buyers_guide,
              faq             = EXCLUDED.faq,
              conclusion      = EXCLUDED.conclusion,
              recommendations = EXCLUDED.recommendations,
              cta             = EXCLUDED.cta,
              md_full         = EXCLUDED.md_full,
              updated_at      = now();
        """, [idea_id, title, slug, category_slug, front_matter,
              hero_url, hero_alt, introduction, product_gallery, 
              urunler, buyers_guide, faq, conclusion,
              recommendations, cta, md_full])

        # 🔎 Yaz-sonrası doğrulama
        row = con.execute("""
            SELECT idea_id,
                   coalesce(length(trim(introduction)),0) AS intro_len,
                   coalesce(length(trim(buyers_guide)),0) AS guide_len,
                   coalesce(length(trim(conclusion)),0) AS concl_len
            FROM blog_contents WHERE idea_id = ?
        """, [idea_id]).fetchone()

        if not row or row[1] == 0 or row[2] == 0 or row[3] == 0:
            con.execute("ROLLBACK;")
            raise RuntimeError(f"blog_contents verification failed for idea_id={idea_id}")

        con.execute("COMMIT;")
    except Exception as e:
        try:
            con.execute("ROLLBACK;")
        except Exception:
            pass
        print(f"❌ upsert_blog_contents failed: {e}")
        raise
    finally:
        con.close()



# ======================
# 🔥 Title rewrite with LLM
# ======================
from openai import OpenAI
from dotenv import load_dotenv
load_dotenv()

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

def rewrite_title(old_title: str) -> str:
    try:
        resp = client.chat.completions.create(
            model=OPENAI_MODEL_GPT,
            messages=[
                {"role": "system", "content": "You are an SEO title generator."},
                {"role": "user", "content": PROMPT_TMPL.format(title=old_title)},
            ],
            max_tokens=60,
            temperature=0.4,
        )
        new_title = resp.choices[0].message.content.strip()
        return new_title.strip('"').strip("'")
    except Exception as e:
        print(f"⚠️ Title rewrite failed: {e}")
        return old_title



# ======================
# LLM Preflight: title–product uyum kontrolü
# ======================

import math, textwrap

def _build_product_payload(products_df: pd.DataFrame, max_items: int = 20):
    cols = [
        "parent_asin", "title", "brand", "category_slug", "price",
        "tags", "review_summary", "availability", "affiliate_url", "image_url"
    ]
    avail = [c for c in cols if c in products_df.columns]
    rows = []
    for _, r in products_df.head(max_items).iterrows():
        rows.append({
            "parent_asin": str(r.get("parent_asin","")),
            "title": str(r.get("title",""))[:220],
            "brand": str(r.get("brand","")),
            "category_slug": str(r.get("category_slug","")),
            "price": r.get("price", None),
            "tags": r.get("tags", None),
            "review_summary": str(r.get("review_summary",""))[:280],
            "availability": r.get("availability", None),
            "affiliate_url": r.get("affiliate_url", None),
            "image_url": r.get("image_url", None),
        })
    return rows

def _adjust_top_count_in_title(title: str, n: int) -> str:
    # "Top 10 ..." → "Top 4 ...", "Best 7" → "Best 3"
    import re
    if n < 2:
        # Çok az ürün varsa Top/Best sayısını kaldırıp genel başlığa çevir
        t = re.sub(r"\b(Top|Best)\s*\d+\b", r"Best", title, flags=re.I)
        return t
    # varsa sayıyı değiştir
    def repl(m):
        word = m.group(1)
        return f"{word} {n}"
    t = re.sub(r"\b(Top|Best)\s*\d+\b", repl, title, flags=re.I)
    return t

def preflight_filter_products_llm(
    title: str,
    products_df: pd.DataFrame,
    required_min: int = 2,
    max_list: int = 10,
    persona_hint: str = ""
) -> dict:
    """
    LLM ile başlık–ürün uyum kontrolü yapar ve JSON karar döndürür.
    Dönen sözlük:
    {
      "final_title": str,
      "included_asins": [..],
      "excluded": [{"asin": "...", "reason": "..."}],
      "topline_rationale": str,
      "image_prompt": str,
      "notes_for_writer": str
    }
    """
    items = _build_product_payload(products_df, max_items=40)

    system_msg = (
        "You are a strict preflight QA agent for an affiliate blog pipeline. "
        "Your job is to enforce title–product alignment and return clean JSON."
    )

    user_msg = textwrap.dedent(f"""
    TITLE:
    {title}

    PERSONA_HINT (optional):
    {persona_hint or "N/A"}

    CANDIDATE_PRODUCTS (array of objects):
    {json.dumps(items, ensure_ascii=False)}

    RULES:
    - Keep only products that truly match the title intent (category/use-case/modifiers like budget/quiet/ANC/etc).
    - Exclude items that are unavailable, missing affiliate_url, wrong category, or duplicates/variants of same model family.
    - Prefer diversity (limit 2 per brand if many similar).
    - Be extremely strict. It is better to return an empty list of `included_asins` than to include a single irrelevant product.
    - If TITLE says "Top N"/"Best N", ensure N equals the number of kept products.
      If fewer products remain, adjust the N in the title accordingly. If too few (≤1), remove the number (just 'Best').
    - Return a purchase-leaning final_title (but don't change the main product type).
    - Propose a detailed, photorealistic hero image prompt. Describe a real-world scene (e.g., 'a modern kitchen counter', 'on a wooden desk next to a laptop'). Specify camera settings like '35mm lens, f/1.8 aperture, golden hour lighting'. The final image must look like a real photograph, not an illustration. Aspect ratio 16:9. No brand names or logos.
    - Return ONLY JSON with this schema:
      {{
        "final_title": str,
        "included_asins": [str, ...],   // unique ASINs kept
        "excluded": [{{"asin": str, "reason": str}}, ...],
        "topline_rationale": str,
        "image_prompt": str,
        "notes_for_writer": str
      }}
    """)

    try:
        resp = client.chat.completions.create(
            model=OPENAI_MODEL_GPT,
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.2,
            max_tokens=900,
        )
        raw = resp.choices[0].message.content.strip()
        # JSON güvenli parse
        start = raw.find("{")
        end = raw.rfind("}")
        parsed = json.loads(raw[start:end+1]) if start != -1 and end != -1 else json.loads(raw)
    except Exception as e:
        print(f"⚠️ Preflight LLM parse failed: {e}")
        # Fallback: hiç filtreleme yapma
        kept = products_df["parent_asin"].astype(str).tolist()
        return {
            "final_title": title,
            "included_asins": kept[:max_list],
            "excluded": [],
            "topline_rationale": "LLM fallback – no filtering",
            "image_prompt": "",
            "notes_for_writer": ""
        }

    # Güvenlik/iş kuralları sonrası son düzeltmeler
    kept = list(dict.fromkeys([str(a).strip() for a in parsed.get("included_asins", []) if a]))

    # Başlık sayısını N ile konsolide et
    final_title = _adjust_top_count_in_title(parsed.get("final_title", title) or title, len(kept))

    decision = {
        "final_title": final_title,
        "included_asins": kept,
        "excluded": parsed.get("excluded", []),
        "topline_rationale": parsed.get("topline_rationale", ""),
        "image_prompt": parsed.get("image_prompt", ""),
        "notes_for_writer": parsed.get("notes_for_writer", "")
    }
    return decision

def save_preflight_decision(idea_id: str, decision: dict):
    con = connect_db()
    con.execute("""
    CREATE TABLE IF NOT EXISTS blog_preflight_decisions (
      idea_id VARCHAR PRIMARY KEY,
      decision_json TEXT,
      updated_at TIMESTAMP DEFAULT now()
    )
    """)
    con.execute("""
      INSERT INTO blog_preflight_decisions (idea_id, decision_json, updated_at)
      VALUES (?, ?, now())
      ON CONFLICT (idea_id) DO UPDATE SET
        decision_json = EXCLUDED.decision_json,
        updated_at = now()
    """, [idea_id, json.dumps(decision, ensure_ascii=False)])
    con.close()






def update_title_in_db(idea_id, new_title):
    con = connect_db()
    con.execute("UPDATE ideas SET idea_title = ? WHERE idea_id = ?", [new_title, idea_id])

# ======================
# Helpers
# ======================
def _parse_competitors(s):
    if pd.isna(s) or s is None:
        return []
    return [a.strip() for a in str(s).split(",") if a.strip()]

def _normalize_slug(slug, title):
    s = (slug or "").strip()
    if not s:
        s = slugify(title)
    return s

def _metadata_card(meta):
    tags = ", ".join(meta.get("target_keywords", []))
    asins = ", ".join([meta.get("primary_asin", "")] + meta.get("competitor_asins", []))
    hero = meta.get("hero_image", "")
    canonical = meta.get("canonical_url", "")
    html = f"""
<div class="meta-card" style="display:flex;align-items:center;gap:16px;margin:1.5em 0;padding:1em;border:1px solid #eee;border-radius:8px;background:#fafafa;">
  <div class="meta-image" style="flex:0 0 120px;">
    <img src="{hero}" alt="Hero image" style="max-width:120px;border-radius:6px;"/>
<div class="meta-card">
  <div class="meta-image">
    <img src="{hero}" alt="Hero image"/>
  </div>
  <div class="meta-info" style="flex:1;">
  <div class="meta-info">
    <p><strong>Tags:</strong> {tags}</p>
    <p><strong>ASINs:</strong> {asins}</p>
    <p><strong>URL:</strong> {canonical}</p>
  </div>
</div>
"""
    return html

# ======================
# Amazon availability check
# ======================
def filter_with_amazon(df_subset,
                       delay=0.8,
                       strict=True,
                       timeout=7,
                       debug=None,
                       max_to_check=0):
    if df_subset is None or df_subset.empty:
        return df_subset

    headers = {
        "User-Agent": ("Mozilla/5.0"),
        "Accept-Language": "en-US,en;q=0.9",
    }
    asins = df_subset["parent_asin"].astype(str).tolist()
    kept, bad, amb = [], [], []

    to_check = asins if max_to_check in (0, None) else asins[:max_to_check]
    for asin in to_check:
        url = f"https://www.amazon.com/dp/{asin}"
        try:
            r = requests.get(url, headers=headers, timeout=timeout)
            ok = (r.status_code == 200)
            txt = (r.text or "").lower()
            signals = ["currently unavailable","page not found","no longer available","out of stock"]
            bad_signal = any(s in txt for s in signals)
            if ok and not bad_signal:
                kept.append(asin)
            else:
                bad.append(asin)
        except Exception:
            amb.append(asin)
        time.sleep(delay)

    if not strict:
        kept = sorted(set(kept + amb))

    unchecked = set(asins) - set(to_check)
    kept = sorted(set(kept).union(unchecked))

    if debug is not None:
        debug["amazon_check"] = {
            "checked_asins": to_check,
            "kept_asins": kept,
            "bad_asins": bad,
            "ambiguous": amb,
        }

    return df_subset[df_subset["parent_asin"].astype(str).isin(kept)].copy()

# ======================
# DB: Idea loader & blog status
# ======================
def load_candidate_ideas_from_db(max_blogs=1, category=None):
    con = connect_db()
    if category:
        df = con.execute("""
            SELECT i.idea_id, i.idea_title, i.category_slug,
                   string_agg(ip.parent_asin, '|') AS asins
            FROM ideas i
            JOIN idea_products ip ON ip.idea_id = i.idea_id
            LEFT JOIN blog_posts b ON b.idea_id = i.idea_id
            WHERE (b.idea_id IS NULL OR b.status NOT IN ('published', 'failed'))
              AND i.category_slug = ?
            GROUP BY i.idea_id, i.idea_title, i.category_slug
            ORDER BY random()
            LIMIT ?
        """, [category, max_blogs]).df()
    else:
        df = con.execute("""
            SELECT i.idea_id, i.idea_title, i.category_slug,
                   string_agg(ip.parent_asin, '|') AS asins
            FROM ideas i
            JOIN idea_products ip ON ip.idea_id = i.idea_id
            LEFT JOIN blog_posts b ON b.idea_id = i.idea_id
            WHERE b.idea_id IS NULL OR b.status NOT IN ('published', 'failed')
            GROUP BY i.idea_id, i.idea_title, i.category_slug
            ORDER BY random()
            LIMIT ?
        """, [max_blogs]).df()
    return df


def mark_blog_status(idea_id: str, status: str, blog_url: str = None, author_id: str = None):
    """
    Updates or inserts blog_posts row without requiring a blog_url column in DB.
    Falls back gracefully if blog_url field doesn't exist.
    """
    con = connect_db()
    try:
        # Check if blog_url exists in the table schema
        cols = [r[0] for r in con.execute("DESCRIBE blog_posts").fetchall()]
        has_blog_url = "blog_url" in cols

        if has_blog_url:
            con.execute("""
                UPDATE blog_posts
                SET status = ?,
                    blog_url = COALESCE(?, blog_url),
                    updated_at = now(),
                    author_id = COALESCE(?, author_id),
                    date_published = CASE
                        WHEN ? = 'published' AND (date_published IS NULL) THEN current_date
                        ELSE date_published
                    END
                WHERE idea_id = ?
            """, [status, blog_url, author_id, status, idea_id])
            con.execute("""
                INSERT INTO blog_posts (idea_id, status, blog_url, updated_at, author_id, date_published)
                SELECT ?, ?, ?, now(), ?, CASE WHEN ?='published' THEN current_date ELSE NULL END
                WHERE NOT EXISTS (SELECT 1 FROM blog_posts WHERE idea_id = ?)
            """, [idea_id, status, blog_url, author_id, status, idea_id])
        else:
            # same logic but without blog_url
            con.execute("""
                UPDATE blog_posts
                SET status = ?,
                    updated_at = now(),
                    author_id = COALESCE(?, author_id),
                    date_published = CASE
                        WHEN ? = 'published' AND (date_published IS NULL) THEN current_date
                        ELSE date_published
                    END
                WHERE idea_id = ?
            """, [status, author_id, status, idea_id])
            con.execute("""
                INSERT INTO blog_posts (idea_id, status, updated_at, author_id, date_published)
                SELECT ?, ?, now(), ?, CASE WHEN ?='published' THEN current_date ELSE NULL END
                WHERE NOT EXISTS (SELECT 1 FROM blog_posts WHERE idea_id = ?)
            """, [idea_id, status, author_id, status, idea_id])
    finally:
        con.close()


# ======================
# MkDocs nav update
# ======================
import yaml
MKDOCS_PATH = "mkdocs.yml"
DOCS_DIR = "docs/blogs"

def update_mkdocs_nav():
    con = connect_db()
    categories = con.execute("SELECT DISTINCT category_slug FROM ideas ORDER BY category_slug").df()["category_slug"].tolist()
    print("📂 Categories in DB:", categories)

    with open(MKDOCS_PATH, "r") as f:
        config = yaml.safe_load(f)

    nav = [{"Home": "index.md"}]

    for cat in categories:
        cat_title = cat.replace("_", " ").title()
        cat_dir = os.path.join(DOCS_DIR, cat)
        os.makedirs(cat_dir, exist_ok=True)

        index_file = os.path.join(cat_dir, "index.md")
        df = con.execute("SELECT idea_title FROM ideas WHERE category_slug = ? ORDER BY idea_title", [cat]).df()

        with open(index_file, "w") as f:
            f.write(f"# {cat_title}\n\n")
            if df.empty:
                f.write("_No published blogs yet._\n")
            else:
                for _, row in df.iterrows():
                    slug = slugify(row["idea_title"])
                    f.write(f"- [{row['idea_title']}]({slug}/)\n")

        nav.append({cat_title: f"{cat}/index.md"})


    config["nav"] = nav

    with open(MKDOCS_PATH, "w") as f:
        yaml.dump(config, f, sort_keys=False)

    print("✅ mkdocs.yml nav updated.")




# ======================
# Main writing loop (DB SOURCE)
# ======================
def write_from_db(products_csv,
                  max_blogs=1,
                  min_products=2,
                  availability_check=True,
                  amazon_strict=True,
                  request_delay=0.8,
                  request_timeout=7,
                  max_check=0,
                  category=None):
    debug = {"runs": []}
    ideas = load_candidate_ideas_from_db(max_blogs=max_blogs, category=category)
    if ideas.empty:
        print("ℹ️ No candidate ideas found.")
        write_json(DEBUG_LAST_RUN, debug)
        return

    for _, r in tqdm(ideas.iterrows(), total=len(ideas), desc="Writing blogs (DB)"):
        idea_id = str(r["idea_id"])
        blog_title = str(r["idea_title"]).strip()
        cat = r.get("category_slug", category) or "uncategorized"

        # ✨ Title rewrite
        new_title = rewrite_title(blog_title)
        if new_title and new_title != blog_title:
            update_title_in_db(idea_id, new_title)
            blog_title = new_title

        asins = [a for a in str(r.get("asins", "")).split("|") if a]
        slug = _normalize_slug("", blog_title)

        subset = subset_by_asins_db(asins)
        if subset is None or subset.empty or len(subset) < min_products:
            print(f"⚠️ Skip '{idea_id}': Not enough products found in DB for ASINs: {asins}")
            mark_blog_status(idea_id, "failed", None)
            continue

        # 🔎 LLM preflight: başlık–ürün uyum kontrolü
        preflight = preflight_filter_products_llm(
            title=blog_title,
            products_df=subset,
            required_min=min_products,
            max_list=10,
            persona_hint=""  # istersen buyer persona ipucu geç
        )
        # Kararı DB'ye logla
        save_preflight_decision(idea_id, preflight)

        # Başlığı, LLM'in düzelttiği hâliyle güncelle
        if preflight.get("final_title") and preflight["final_title"] != blog_title:
            blog_title = preflight["final_title"]
            update_title_in_db(idea_id, blog_title)

        # Ürün listesini LLM kararına göre daralt
        kept_asins = set(preflight.get("included_asins", []))
        if kept_asins:
            subset = subset[subset["parent_asin"].astype(str).isin(kept_asins)].copy()

        # (Opsiyonel) excluded gerekçelerini debug’a yazdırabilirsin
        # print("Excluded:", preflight.get("excluded", []))

        if subset is None or subset.empty or len(subset) < min_products:
            print(f"⚠️ Skip '{idea_id}': Not enough products left after LLM preflight filter.")
            mark_blog_status(idea_id, "failed", None)
            continue

        # ⛳️ Amazon availability check yine çalışsın (kesin OOS’ları elemek için)


        if availability_check:
            subset = filter_with_amazon(subset,
                                        delay=request_delay,
                                        strict=amazon_strict,
                                        timeout=request_timeout,
                                        debug=None,
                                        max_to_check=max_check)

        if subset is None or subset.empty or len(subset) < min_products:
            print(f"⚠️ Skip '{idea_id}': Not enough products left after Amazon availability check.")
            mark_blog_status(idea_id, "failed", None)
            continue

        gallery = build_product_gallery(subset)
        urunler = build_product_cards(subset)
        sections = write_blog_markdown(blog_title=blog_title,
                                       post_type="comparison",
                                       intent_category="comparison",
                                       primary_keywords=[],
                                       products_df=subset)
        recommendations = build_random_recommendations(blogs_dir=str(BLOGS_DIR),
                                                       current_slug=slug,
                                                       max_posts=3,
                                                       base_url=BASE_URL)

        parts = {
            "title": f"# {blog_title}\n",
            "meta_card": _metadata_card({
                "title": blog_title,
                "slug": slug,
                "date": pd.Timestamp.utcnow().strftime("%Y-%m-%d"),
                "reading_time_minutes": reading_time_minutes(""),
                "post_type": "comparison",
                "target_keywords": [],
                "primary_asin": asins[0] if asins else "",
                "competitor_asins": asins[1:],
                "hero_image": subset.iloc[0].get("image_url", ""),
                "canonical_url": f"/{slug}/"

            }),
            "intro": sections.get("intro", ""),
            "product_gallery": gallery,
            "urunler": urunler,
            "buyers_guide": sections.get("buyers_guide", ""),
            "faq": sections.get("faq", ""),
            "conclusion": sections.get("conclusion", ""),
            "recommendations": recommendations,
            "cta": sections.get("cta", "")
        }

        # ✅ LLM çıktısı temizleme + doğrulama
        
        parts = sanitize_parts(parts, blog_title)
        errs = validate_required(parts)
        if errs:
            print("⚠️ Content validation warnings:", errs)


        # --- BEGIN: hero & author integration ---
        USE_IMAGE_GEN = os.getenv("USE_IMAGE_GEN", "1")  # .env: 1=AI ile üret, 0=kategori resmi kullan

        def get_category_image(category_slug: str) -> dict:
            """
            app/static/images klasöründen kategoriye özel görseli döndürür.
            """
            static_dir = os.path.join(BASE_DIR, "app", "static", "img")

            fname = f"{category_slug}.png"
            path = os.path.join(static_dir, fname)
            url = f"{BASE_URL.rstrip('/')}/static/img/{fname}"
            if os.path.exists(path):
                return {
                    "banner_url": url,
                    "hero_url": url,
                    "thumb_url": url,
                    "alt": f"Category image for {category_slug}"
                }
            # fallback
            return {
                "banner_url": f"{BASE_URL.rstrip('/')}/static/img/default.png",
                "hero_url": f"{BASE_URL.rstrip('/')}/static/img/default.png",
                "thumb_url": f"{BASE_URL.rstrip('/')}/static/img/default.png",
                "alt": "Default category image"
            }

        if USE_IMAGE_GEN == "1":
            # LLM’den gelen prompt varsa ekle, yoksa fallback hazırla
            image_prompt = ""
            try:
                image_prompt = preflight.get("image_prompt", "") if isinstance(preflight, dict) else ""
            except Exception:
                pass

            if image_prompt:
                image_prompt = (
                    image_prompt
                    + ", casual smartphone photo, neutral daylight, slightly flat colors, slight grain, realistic imperfections, everyday setting, not cinematic, --no 3d,render,cgi,drawing,illustration"
                )
            else:
                image_prompt = f"""
                A casual smartphone snapshot of {blog_title}, 
                placed in a simple everyday setting (like a desk with a coffee mug or papers), 
                taken under neutral daylight, slightly flat colors, some natural shadows, 
                slight grain, realistic imperfections, not cinematic, 
                --no 3d,render,cgi,drawing,illustration
                """.strip()

            hero_assets = generate_hero_assets(slug=slug, title=blog_title, prompt=image_prompt)
        else:
            # Kategoriye özel sabit resim
            hero_assets = get_category_image(cat)

        hero_url = hero_assets.get("hero_url", "") or subset.iloc[0].get("image_url", "")
        hero_alt = hero_assets.get("alt", "") or blog_title
        # --- END: hero & author integration ---


        # meta_card'a bu URL’yi yaz
        parts["meta_card"] = _metadata_card({
            "title": blog_title,
            "slug": slug,
            "date": pd.Timestamp.utcnow().strftime("%Y-%m-%d"),
            "reading_time_minutes": reading_time_minutes(""),
            "post_type": "comparison",
            "target_keywords": [],
            "primary_asin": asins[0] if asins else "",
            "competitor_asins": asins[1:],
            "hero_image": hero_url,
            "canonical_url": f"/{slug}/"
        })

        # 2) BOLD: HTML vurgusu uygulayalım
        for key in ["intro","buyers_guide","conclusion","faq"]:
            parts[key] = _md_bold_to_html(parts.get(key,""))

        # 3) AUTHOR: DB'den rastgele bir yazar seç
        def get_random_author_id():
            con = connect_db()
            author_id = con.execute("SELECT author_id FROM authors ORDER BY random() LIMIT 1").fetchone()[0]
            con.close()
            return author_id

        def get_author_for_category(cat):
            con = connect_db()
            row = con.execute("""
                SELECT author_id
                FROM authors
                WHERE primary_category_slug = ?
                ORDER BY random()
                LIMIT 1
            """, [cat]).fetchone()
            con.close()
            return row[0] if row else None
        
        # Eskisi:
        # author_id = get_random_author_id()

        # Yeni:
        author_id = get_author_for_category(cat)
        if not author_id:  # o kategoride hiç author yoksa fallback
            author_id = get_random_author_id()
        # --- END: hero & author integration ---



        order = ["title", "meta_card", "intro", "product_gallery",
                 "urunler", "recommendations", "buyers_guide",
                 "faq", "conclusion"]
        md_full = "\n\n".join(parts[k] for k in order if parts.get(k))

        # parts ve md_full hazırlandıktan sonra:
        upsert_blog_contents(
            idea_id=idea_id,
            title=blog_title,
            slug=slug,
            category_slug=cat,
            front_matter=parts.get("meta_card",""),
            introduction=parts.get("intro",""),
            product_gallery=parts.get("product_gallery",""),
            urunler=parts.get("urunler",""),
            buyers_guide=parts.get("buyers_guide",""),
            faq=parts.get("faq",""),
            conclusion=parts.get("conclusion",""),
            recommendations=parts.get("recommendations",""),
            cta=parts.get("cta",""),
            md_full=md_full,
            hero_url=hero_url,
            hero_alt=hero_alt,
        )

        # DB'ye parçalı içerik yaz

        out_path = os.path.join(str(BLOGS_DIR), f"{slug}.md")

        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        save_markdown(out_path, md_full)

        blog_url = f"/{slug}/"

        mark_blog_status(idea_id, "published", blog_url, author_id=author_id)

        append_link_to_index(DOCS_BLOG_INDEX, blog_title, blog_url)



    write_json(DEBUG_LAST_RUN, debug)
    
    # MkDocs yok → doğrudan web köküne yaz
    write_robots_txt(BASE_URL, WEB_ROOT)
    write_sitemap_xml(BASE_URL, WEB_ROOT)

    #update_mkdocs_nav()
    #import subprocess, sys
    #subprocess.run([sys.executable, "-m", "mkdocs", "build", "--clean"], check=False)


# ======================
# Content validation & patch
# ======================
MIN_CHARS = {
    "introduction": 120,     # ~ 2-3 cümle
    "buyers_guide": 180,     # madde listesi dahil
    "conclusion": 120,
    "faq": 60                # isteğe bağlı ama boş kalmasın
}

REQUIRED_SECTIONS = ["introduction", "buyers_guide", "conclusion"]  # kritik

def _normalize_text(s):
    s = (s or "").strip()
    # markdown'da görünür boşluklar bazen \n\n kalır, kırp
    return re.sub(r"\n{3,}", "\n\n", s)

def _fallback_snippet(kind, title):
    if kind == "introduction":
        return f"""**Quick Take:** Looking for the best picks for <strong>{title}</strong>? Below we highlight why these products stand out and what to check before you buy."""
    if kind == "buyers_guide":
        # ❌ <h2>Buyer’s Guide</h2> başlığını KALDIR
        return """<ul>
  <li><strong>Key Features:</strong> Focus on build quality, warranty, and ease of use.</li>
  <li><strong>Price vs. Value:</strong> Don’t overpay for minor upgrades; compare core specs.</li>
  <li><strong>Real-World Fit:</strong> Check size/weight, comfort, and noise in everyday use.</li>
</ul>"""
    if kind == "conclusion":
        # ❌ <h2>Conclusion</h2> başlığını KALDIR
        return """<p>If you need a quick recommendation: pick the “Best Overall” for balance, “Best Budget” for price, and “Best Premium” for top performance.</p>"""
    if kind == "faq":
        # ❌ <h2>FAQ</h2> başlığını KALDIR
        return """<p><strong>Q:</strong> How do I choose among similar models?<br><strong>A:</strong> Match features to your use-case and check return/warranty terms.</p>"""
    return ""

def sanitize_parts(parts: dict, blog_title: str) -> dict:
    """Boş/çok kısa kritik alanları doldurur, hepsini normalize eder."""
    cleaned = dict(parts)
    for key in ["intro","buyers_guide","conclusion","faq","product_gallery","urunler","meta_card","title"]:
        cleaned[key] = _normalize_text(cleaned.get(key, ""))

    # Kritik alanlar: min karakter eşiği uygula, boşsa fallback doldur
    mapping = {"intro":"introduction","buyers_guide":"buyers_guide","conclusion":"conclusion","faq":"faq"}
    for short_key, canonical in mapping.items():
        txt = cleaned.get(short_key, "")
        need_min = MIN_CHARS.get(canonical, 0)
        if len(re.sub(r"\s+", " ", txt)) < need_min:
            cleaned[short_key] = _fallback_snippet(canonical, blog_title)

    return cleaned

def validate_required(parts: dict) -> list:
    """Eksik/kısa kritik alanları rapor eder."""
    errs = []
    mapping = {"intro":"introduction","buyers_guide":"buyers_guide","conclusion":"conclusion"}
    for short_key, canonical in mapping.items():
        txt = parts.get(short_key, "")
        need_min = MIN_CHARS.get(canonical, 0)
        if len(re.sub(r"\s+", " ", txt)) < need_min:
            errs.append(f"{canonical} too short/empty")
    return errs



# ======================
# CSV SOURCE (legacy)
# ======================
def write_from_intent_pool(products_csv, intent_pool_csv,
                           max_blogs=1, min_products=2):
    df_prod = load_products(products_csv)
    pool = pd.read_csv(intent_pool_csv)
    rows_index = []
    debug = {"runs": []}

    if "generated_count" in pool.columns:
        pool = pool[pool["generated_count"].fillna(0).astype(int) < 1].reset_index(drop=True)
    else:
        pool["generated_count"] = 0

    if pool.empty:
        write_json(DEBUG_LAST_RUN, debug)
        return

    pool = pool.sample(frac=1).reset_index(drop=True)
    total = min(len(pool), max_blogs)

    for i, r in tqdm(pool.head(max_blogs).iterrows(), total=total, desc="Writing blogs (CSV)"):
        blog_title = str(r.get("blog_title", "")).strip()
        parent_asin = str(r.get("parent_asin", "")).strip()
        comp_asins = _parse_competitors(r.get("competitor_asins", ""))
        asins = [a for a in [parent_asin] + comp_asins if a]

        post_type = str(r.get("post_type", "comparison")).strip()
        intent_category = str(r.get("intent_category", "comparison")).strip()
        target_keywords = [k.strip() for k in str(r.get("target_keywords", "")).split(",") if k.strip()]
        slug = _normalize_slug(r.get("slug", ""), blog_title)

        subset = subset_by_asins(df_prod, asins)
        if subset is None or subset.empty or len(subset) < min_products:
            continue

        gallery = build_product_gallery(subset)
        urunler = build_product_cards(subset)
        sections = write_blog_markdown(blog_title=blog_title,
                                       post_type=post_type,
                                       intent_category=intent_category,
                                       primary_keywords=target_keywords,
                                       products_df=subset)
        recommendations = build_random_recommendations(blogs_dir=str(BLOGS_DIR),
                                                       current_slug=slug,
                                                       max_posts=3,
                                                       base_url=BASE_URL)

        parts = {
            "title": f"# {blog_title}\n",
            "meta_card": _metadata_card({
                "title": blog_title,
                "slug": slug,
                "date": pd.Timestamp.utcnow().strftime("%Y-%m-%d"),
                "reading_time_minutes": reading_time_minutes(""),
                "post_type": post_type,
                "target_keywords": target_keywords,
                "primary_asin": parent_asin,
                "competitor_asins": comp_asins,
                "hero_image": subset.iloc[0].get("image_url", ""),
                # ✅ düzeltildi
                "canonical_url": f"/legacy/{slug}/"


            }),
            "intro": sections.get("intro", ""),
            "product_gallery": gallery,
            "urunler": urunler,
            "buyers_guide": sections.get("buyers_guide", ""),
            "faq": sections.get("faq", ""),
            "conclusion": sections.get("conclusion", ""),
            "recommendations": recommendations,
            "cta": sections.get("cta", "")
        }

        order = ["title","meta_card","intro","product_gallery",
                 "urunler","recommendations","buyers_guide","faq","conclusion"]
        md_full = "\n\n".join(parts[k] for k in order if parts.get(k))

        category_dir = os.path.join(str(BLOGS_DIR), "legacy")
        out_path = os.path.join(category_dir, slug, "index.md")
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        save_markdown(out_path, md_full)

        blog_url = f"/legacy/{slug}/"
        append_link_to_index(DOCS_BLOG_INDEX, blog_title, blog_url)

    write_json(DEBUG_LAST_RUN, debug)


 
 

# ======================
# CLI
# ======================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default=DATA_CSV)
    ap.add_argument("--intent-pool", default=os.path.join(str(IDX_DIR), "intent_pool.cleaned.csv"))
    ap.add_argument("--max-blogs", type=int, default=1)
    ap.add_argument("--min-products", type=int, default=2)
    ap.add_argument("--availability-check", type=int, default=1)
    ap.add_argument("--amazon-strict", type=int, default=1)
    ap.add_argument("--request-delay", type=float, default=0.8)
    ap.add_argument("--request-timeout", type=int, default=7)
    ap.add_argument("--max-check", type=int, default=0)
    ap.add_argument("--source", choices=["db", "csv"], default="db")
    ap.add_argument("--category", default=None)

    args = ap.parse_args()

    if args.source == "db":
        write_from_db(products_csv=args.input,
                      max_blogs=args.max_blogs,
                      min_products=args.min_products,
                      availability_check=bool(args.availability_check),
                      amazon_strict=bool(args.amazon_strict),
                      request_delay=args.request_delay,
                      request_timeout=args.request_timeout,
                      max_check=args.max_check,
                      category=args.category)
    else:
        write_from_intent_pool(products_csv=args.input,
                               intent_pool_csv=args.intent_pool,
                               max_blogs=args.max_blogs,
                               min_products=args.min_products)

if __name__ == "__main__":
    main()
