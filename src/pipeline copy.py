#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Blog Factory — DB-integrated pipeline
- Fikir seçimini DuckDB’den yapar (ideas + idea_products, blog_posts ile filtre)
- İçerik yazımından sonra blog_posts tablosunu günceller (status=published, blog_url)
- Eski CSV tabanlı akış (write_from_intent_pool) geriye dönük uyumluluk için korunmuştur
"""

import argparse, os, json, time, requests, re
import pandas as pd
from tqdm import tqdm
import duckdb

from .writer import write_blog_markdown, summarize_reviews_with_llm
from .config import (
    DATA_CSV, BLOGS_DIR, DOCS_BLOG_INDEX, IDX_DIR, BLOG_INDEX_CSV,
    DEBUG_LAST_RUN, BASE_URL
)
#from .data_loader import load_products, subset_by_asins

# YENİ
from .data_loader import (
    load_products, subset_by_asins,   # CSV akışı için
    load_products_db, subset_by_asins_db  # DB akışı için
)

from .utils import (
    slugify, save_markdown, append_link_to_index, build_random_recommendations,
    reading_time_minutes, write_json, build_comparison_table, build_product_cards, build_product_gallery
)

# ======================
# DuckDB
# ======================
DB_PATH = "warehouse/blog_factory.duckdb"

def connect_db():
    # read_only=False çünkü blog_posts güncelleyeceğiz
    return duckdb.connect(DB_PATH, read_only=False)

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
  </div>
  <div class="meta-info" style="flex:1;">
    <p><strong>Tags:</strong> {tags}</p>
    <p><strong>ASINs:</strong> {asins}</p>
    <p><strong>URL:</strong> {canonical}</p>
  </div>
</div>
"""
    return html

# ======================
# Front matter helper  -> MkDocs Meta (markdown 'meta' extension)
# ======================
def _ensure_front_matter(meta: dict) -> str:
    """
    MkDocs 'meta' extension format:
    Title: ...
    slug: ...
    target_keywords: a, b, c

    (boş satır)
    <markdown body>
    """
    lines = []
    for k, v in meta.items():
        if isinstance(v, list):
            lines.append(f"{k}: {', '.join(str(x) for x in v)}")
        else:
            lines.append(f"{k}: {v}")
    return "\n".join(lines) + "\n\n"

# ======================
# Amazon availability check
# ======================
def filter_with_amazon(df_subset,
                       delay=0.8,
                       strict=True,
                       timeout=7,
                       debug=None,
                       max_to_check=0):
    """
    Amazon erişilebilirlik filtresi.
    """
    if df_subset is None or df_subset.empty:
        return df_subset

    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0.0.0 Safari/537.36"),
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
            signals = [
                "currently unavailable",
                "page not found",
                "404 - document not found",
                "no longer available",
                "out of stock",
            ]
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
            "strict": strict,
            "delay": delay,
            "timeout": timeout,
        }

    return df_subset[df_subset["parent_asin"].astype(str).isin(kept)].copy()

# ======================
# DB: Idea loader & blog status
# ======================
def load_candidate_ideas_from_db(max_blogs=1):
    """
    blog_posts.status='published' olmayan fikirleri getirir.
    ASIN'leri '|' ile birleştirir.
    """
    con = connect_db()
    df = con.execute("""
        SELECT
          i.idea_id,
          i.idea_title,
          i.category_slug,
          string_agg(ip.parent_asin, '|') AS asins
        FROM ideas i
        JOIN idea_products ip ON ip.idea_id = i.idea_id
        LEFT JOIN blog_posts b ON b.idea_id = i.idea_id
        WHERE b.idea_id IS NULL OR b.status != 'published'
        GROUP BY i.idea_id, i.idea_title, i.category_slug
        ORDER BY random()
        LIMIT ?
    """, [max_blogs]).df()
    return df

def mark_blog_status(idea_id: str, status: str, blog_url: str = None):
    """
    blog_posts upsert (UPDATE + INSERT) — DuckDB uyumlu
    """
    con = connect_db()
    con.execute("""
        UPDATE blog_posts
        SET status = ?, blog_url = COALESCE(?, blog_url), updated_at = now()
        WHERE idea_id = ?
    """, [status, blog_url, idea_id])
    con.execute("""
        INSERT INTO blog_posts (idea_id, status, blog_url, updated_at)
        SELECT ?, ?, ?, now()
        WHERE NOT EXISTS (SELECT 1 FROM blog_posts WHERE idea_id = ?)
    """, [idea_id, status, blog_url, idea_id])

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
                  max_check=0):
    """
    Fikirleri DB'den alır, blogları üretir, Markdown yazar, blog_posts'u günceller.
    """
    debug = {"runs": []}

    # Ürünleri (şimdilik) CSV'den yüklüyoruz
    #df_prod = load_products(products_csv)

    ideas = load_candidate_ideas_from_db(max_blogs=max_blogs)
    if ideas.empty:
        print("ℹ️ No candidate ideas found (all are already published).")
        write_json(DEBUG_LAST_RUN, debug)
        return

    for _, r in tqdm(ideas.iterrows(), total=len(ideas), desc="Writing blogs (DB)"):
        idea_id = str(r.get("idea_id"))
        blog_title = str(r.get("idea_title", "")).strip()
        asins = [a for a in str(r.get("asins", "")).split("|") if a]
        slug = _normalize_slug("", blog_title)

        debug_run = {"idea_id": idea_id, "title": blog_title, "slug": slug, "asins": asins}

        subset = subset_by_asins_db(asins)

        if subset is None or subset.empty or len(subset) < min_products:
            print(f"⚠️ Skip '{blog_title}' — not enough products for ASINs: {asins}")
            debug_run["skip_reason"] = "min_products"
            debug["runs"].append(debug_run)
            # draft olarak işaretlemek istersen:
            mark_blog_status(idea_id, "failed", None)
            continue

        if availability_check:
            subset = filter_with_amazon(
                subset,
                delay=request_delay,
                strict=amazon_strict,
                timeout=request_timeout,
                debug=debug_run,
                max_to_check=max_check
            )

        if subset is None or subset.empty or len(subset) < min_products:
            print(f"⚠️ Skip '{blog_title}' — not enough available products after Amazon check")
            debug_run["skip_reason"] = "min_products_after_availability"
            debug["runs"].append(debug_run)
            mark_blog_status(idea_id, "failed", None)
            continue

        # --- Content pieces ---
        gallery = build_product_gallery(subset)
        urunler = build_product_cards(subset)

        # Burada istersen summarize_reviews_with_llm kullanarak intro/faq zenginleştirebilirsin.
        sections = write_blog_markdown(
            blog_title=blog_title,
            post_type="comparison",
            intent_category="comparison",
            primary_keywords=[],
            products_df=subset
        )

        recommendations = build_random_recommendations(
            blogs_dir=str(BLOGS_DIR),
            current_slug=slug,
            max_posts=3,
            base_url=BASE_URL
        )

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
                "canonical_url": f"{slug}.md"
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

        order = [
            "title", "meta_card", "intro", "product_gallery",
            "urunler", "recommendations", "buyers_guide", "faq", "conclusion",
        ]
        md_full = "\n\n".join(parts[k] for k in order if parts.get(k))

        # --- Write markdown ---
        out_path = os.path.join(str(BLOGS_DIR), f"{slug}.md")
        save_markdown(out_path, md_full)
        print(f"📝 wrote: {out_path}")

        # --- Update MkDocs index ---
        append_link_to_index(DOCS_BLOG_INDEX, blog_title, slug)

        # --- Mark in DB: published ---
        blog_url = f"/{slug}/"
        mark_blog_status(idea_id, "published", blog_url)

        debug_run["written"] = out_path
        debug["runs"].append(debug_run)

    write_json(DEBUG_LAST_RUN, debug)

# ======================
# CSV SOURCE (legacy; kept for backward compatibility)
# ======================
def write_from_intent_pool(products_csv, intent_pool_csv, max_blogs=1, min_products=2):
    df_prod = load_products(products_csv)
    pool = pd.read_csv(intent_pool_csv)

    rows_index = []
    debug = {"runs": []}

    if "generated_count" in pool.columns:
        pool = pool[pool["generated_count"].fillna(0).astype(int) < 1].reset_index(drop=True)
    else:
        pool["generated_count"] = 0

    if pool.empty:
        print("ℹ️ No rows with generated_count < 1. Exiting.")
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

        debug_run = {"title": blog_title, "asins": asins, "slug": slug}

        subset = subset_by_asins(df_prod, asins)
        if subset is None or subset.empty or len(subset) < min_products:
            print(f"⚠️ Skip '{blog_title}' — not enough products found for ASINs: {asins}")
            debug_run["skip_reason"] = "min_products"
            debug["runs"].append(debug_run)
            continue

        if AVAILABILITY_CHECK:
            subset = filter_with_amazon(
                subset,
                delay=REQUEST_DELAY,
                strict=AMAZON_STRICT,
                timeout=REQUEST_TIMEOUT,
                debug=debug_run,
                max_to_check=MAX_CHECK
            )

        if subset is None or subset.empty or len(subset) < min_products:
            print(f"⚠️ Skip '{blog_title}' — not enough available products after Amazon check")
            debug_run["skip_reason"] = "min_products_after_availability"
            debug["runs"].append(debug_run)
            continue

        # 1. Product Gallery
        gallery = build_product_gallery(subset)

        # 2. Product Cards
        urunler = build_product_cards(subset)

        # 3. Blog Body (intro, buyers guide, faq, conclusion, cta)
        sections = write_blog_markdown(
            blog_title=blog_title,
            post_type=post_type,
            intent_category=intent_category,
            primary_keywords=target_keywords,
            products_df=subset
        )

        # 4. Related Posts
        recommendations = build_random_recommendations(
            blogs_dir=str(BLOGS_DIR),
            current_slug=slug,
            max_posts=3,
            base_url=BASE_URL
        )

        # 5. Parçaları dict olarak topla
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
                "canonical_url": f"{slug}.md"
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

        order = [
            "title", "meta_card", "intro", "product_gallery",
            "urunler", "recommendations", "buyers_guide", "faq", "conclusion",
        ]

        md_full = "\n\n".join(parts[k] for k in order if parts.get(k))

        # 6. Dosyaya yaz
        out_path = os.path.join(str(BLOGS_DIR), f"{slug}.md")
        save_markdown(out_path, md_full)
        print(f"📝 wrote: {out_path}")

        # 7. Index güncellemeleri
        append_link_to_index(DOCS_BLOG_INDEX, blog_title, slug)
        rows_index.append({
            "blog_title": blog_title,
            "slug": f"{slug}.md",
            "products_used": ";".join(asins),
            "intent_category": intent_category,
            "target_keywords": ",".join(target_keywords)
        })

        debug_run["written"] = out_path
        debug["runs"].append(debug_run)

        try:
            gc = int(r.get("generated_count", 0))
        except Exception:
            gc = 0
        pool.loc[i, "generated_count"] = gc + 1

    # pool & index kaydet
    if rows_index:
        pd.DataFrame(rows_index).to_csv(BLOG_INDEX_CSV, index=False)

    pool.to_csv(intent_pool_csv, index=False)
    write_json(DEBUG_LAST_RUN, debug)

# ======================
# Ek: küçük yardımcılar (opsiyonel)
# ======================
def build_faq_conclusion(df_subset):
    parts = []
    for _, row in df_subset.iterrows():
        if pd.notna(row.get("review_summary_short", "")):
            parts.append(row["review_summary_short"])
    summary = " ".join(parts[:3])  # max 3 ürün özeti
    return f"## FAQ & Conclusion\n\n{summary}\n"

def rebuild_blog_index(intent_pool_csv, blog_index_csv):
    """
    intent_pool.cleaned.csv'den blog_index.csv'yi yeniden üretir.
    Sadece generated_count > 0 olan satırları alır.
    (DB akışında genelde gerekmez; geriye dönük uyumluluk için duruyor.)
    """
    pool = pd.read_csv(intent_pool_csv)
    pool = pool[pool["generated_count"].fillna(0).astype(int) > 0]
    if pool.empty:
        print("ℹ️ No generated blogs found.")
        return

    df = pool[[
        "blog_title",
        "slug",
        "parent_asin",
        "intent_category",
        "target_keywords",
        "post_type",
        "competitor_asins",
        "generated_count"
    ]].copy()

    df["slug"] = df["slug"].astype(str).str.strip() + ".md"
    df.to_csv(blog_index_csv, index=False)
    print(f"✅ Blog index rebuilt: {blog_index_csv} ({len(df)} rows)")

# ======================
# CLI
# ======================
def main():
    ap = argparse.ArgumentParser()
    # Ürün CSV yolu (şimdilik bununla devam ediyoruz)
    ap.add_argument("--input", default=DATA_CSV)

    # Geriye dönük uyumluluk için:
    ap.add_argument("--intent-pool", default=os.path.join(str(IDX_DIR), "intent_pool.cleaned.csv"))

    ap.add_argument("--max-blogs", type=int, default=1)
    ap.add_argument("--min-products", type=int, default=2)

    ap.add_argument("--availability-check", type=int, default=1)
    ap.add_argument("--amazon-strict", type=int, default=1)
    ap.add_argument("--request-delay", type=float, default=0.8)
    ap.add_argument("--request-timeout", type=int, default=7)
    ap.add_argument("--max-check", type=int, default=0)

    # Kaynak seçimi: 'db' (varsayılan) ya da 'csv'
    ap.add_argument("--source", choices=["db", "csv"], default="db")

    args = ap.parse_args()

    # Globaller (CSV akışı için)
    global AVAILABILITY_CHECK, AMAZON_STRICT, REQUEST_DELAY, REQUEST_TIMEOUT, MAX_CHECK
    AVAILABILITY_CHECK = bool(args.availability_check)
    AMAZON_STRICT = bool(args.amazon_strict)
    REQUEST_DELAY = float(args.request_delay)
    REQUEST_TIMEOUT = int(args.request_timeout)
    MAX_CHECK = int(args.max_check)

    if args.source == "db":
        write_from_db(
            products_csv=args.input,
            max_blogs=args.max_blogs,
            min_products=args.min_products,
            availability_check=bool(args.availability_check),
            amazon_strict=bool(args.amazon_strict),
            request_delay=args.request_delay,
            request_timeout=args.request_timeout,
            max_check=args.max_check
        )
    else:
        # Eski CSV tabanlı akış
        write_from_intent_pool(
            products_csv=args.input,
            intent_pool_csv=args.intent_pool,
            max_blogs=args.max_blogs,
            min_products=args.min_products
        )

if __name__ == "__main__":
    main()
