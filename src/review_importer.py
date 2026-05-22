#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Review Importer
---------------
LLM kullanarak müşteri yorumlarını özetler ve doğrudan DuckDB'ye yazar.

Kullanım:
  python -m src.review_importer --category electronics --limit 1
  python -m src.review_importer --category electronics
"""

import argparse
import pandas as pd
import re
from tqdm import tqdm

from .warehouse_full import connect
from .writer import summarize_reviews_with_llm, upsert_review_summaries_to_db

# =============================
# Parsing helper
# =============================
def parse_summary_text(parent_asin, text: str) -> pd.DataFrame:
    """
    LLM'den dönen raw string'i kolonlara ayırır.
    """
    review_paragraph = ""
    review_pros, review_cons = [], []
    review_loved, review_tips = [], []
    review_summary_short = ""

    lines = text.splitlines()
    current = None
    for line in lines:
        line = line.strip()
        if not line:
            continue

        if line.lower().startswith("**pros"):
            current = "pros"; continue
        if line.lower().startswith("**cons"):
            current = "cons"; continue
        if line.lower().startswith("**what customers loved"):
            current = "loved"; continue
        if line.lower().startswith("**tips"):
            current = "tips"; continue
        if line.lower().startswith("**shortsummary"):
            current = "summary"; continue

        if current == "pros" and line.startswith("-"):
            review_pros.append(line.lstrip("- ").strip())
        elif current == "cons" and line.startswith("-"):
            review_cons.append(line.lstrip("- ").strip())
        elif current == "loved" and line.startswith("-"):
            review_loved.append(line.lstrip("- ").strip())
        elif current == "tips" and line.startswith("-"):
            review_tips.append(line.lstrip("- ").strip())
        elif current == "summary":
            review_summary_short += line + " "
        elif current is None:
            # Paragraph section
            review_paragraph += line + " "

    return pd.DataFrame([{
        "parent_asin": parent_asin,
        "review_paragraph": review_paragraph.strip(),
        "review_pros": "\n".join(review_pros),
        "review_cons": "\n".join(review_cons),
        "review_summary_short": review_summary_short.strip(),
        "review_loved": "\n".join(review_loved),
        "review_tips": "\n".join(review_tips),
    }])


# =============================
# Main importer
# =============================
def import_reviews_from_db(category: str, limit: int = 0):
    con = connect()

    q = f"""
    SELECT
      p.parent_asin,
      p.product_title,
      COALESCE(p.description, '') AS description,
      COALESCE(p.features, '') AS features,
      COALESCE(p.pros_raw, '') AS pros_raw,
      COALESCE(p.cons_raw, '') AS cons_raw,
      COALESCE(p.avg_rating, 0) AS avg_rating,
      COALESCE(p.n_reviews, 0) AS n_reviews,
      '' AS reviews_5star,
      '' AS reviews_1star
    FROM v_products p
    WHERE p.category_slug=?
        AND p.parent_asin NOT IN (SELECT parent_asin FROM product_review_summaries)
    """
    if limit and limit > 0:
        q += f" LIMIT {limit}"

    df_in = con.execute(q, [category]).df()
    print(f"Processing {len(df_in)} products from category={category} (limit={limit or 'ALL'})")

    if df_in.empty:
        print("ℹ️ No products to process.")
        return

    all_out = []
    for _, row in tqdm(df_in.iterrows(), total=len(df_in), desc="Summarizing"):
        reviews_5star = []
        reviews_1star = []

        try:
            text = summarize_reviews_with_llm(reviews_5star, reviews_1star)
            df_out = parse_summary_text(row["parent_asin"], text)
            all_out.append(df_out)
        except Exception as e:
            print(f"⚠️ {row['parent_asin']} failed: {e}")

    if all_out:
        final_df = pd.concat(all_out, ignore_index=True)
        n = upsert_review_summaries_to_db(final_df)
        print(f"✅ Upserted rows: {n}")


# =============================
# CLI
# =============================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--category", required=True, help="Category slug (e.g., 'electronics')")
    ap.add_argument("--limit", type=int, default=0, help="Limit number of products (default=ALL)")
    args = ap.parse_args()

    import_reviews_from_db(args.category, args.limit)


if __name__ == "__main__":
    main()
