#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Test: intents.py akışını çalıştır ama DB'ye yazma.
- Rastgele ürün seç
- Rakiplerini SBERT ile bul (cosine threshold ile filtrele)
- LLM ile blog fikirleri üret (marka ismi kullanmadan)
- Sonucu ekrana yaz
"""

import random
import numpy as np
import pandas as pd
import argparse

from src.warehouse_full import connect
from src.intents import (
    _build_text,
    build_embed_index,
    EMBED_MODEL,
    ideas_for_product,
    _chat,
    LLM_SYSTEM,
    LLM_USER_TMPL,
)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--category", type=str, default="tools_and_home_improvement")
    parser.add_argument("--k", type=int, default=10, help="Max rakip sayısı")
    parser.add_argument("--ideas", type=int, default=3, help="LLM fikir sayısı")
    parser.add_argument("--th", type=float, default=0.65, help="Cosine similarity eşiği")
    args = parser.parse_args()

    con = connect()
    df_all = con.execute("""
        SELECT
          p.parent_asin,
          p.product_title,
          p.description,
          p.features,
          COALESCE(s.review_pros, '') AS review_pros,
          COALESCE(s.review_cons, '') AS review_cons,
          COALESCE(p.pros_raw, '')   AS pros_raw,
          COALESCE(p.cons_raw, '')   AS cons_raw
        FROM v_products p
        LEFT JOIN product_review_summaries s USING(parent_asin)
        WHERE p.category_slug = ?
    """, [args.category]).df()

    if df_all.empty:
        print(f"❌ No products found for category={args.category}")
        return

    # Rastgele ürün seç
    idx = random.randint(0, len(df_all) - 1)
    product = df_all.iloc[idx]

    print("🎯 Seçilen ürün:")
    print(f"ASIN   : {product['parent_asin']}")
    print(f"Title  : {product['product_title']}")
    print("=" * 80)

    # Embeddings
    texts = df_all.apply(_build_text, axis=1).tolist()
    index = build_embed_index(texts, model_name=EMBED_MODEL, normalize=True)

    # Cosine similarity
    sims = index.embeddings @ index.embeddings[idx]
    sims[idx] = -1e9

    valid_idx = [i for i, s in enumerate(sims) if s >= args.th]
    top_idx = sorted(valid_idx, key=lambda i: sims[i], reverse=True)[:args.k]

    print(f"🔎 {len(top_idx)} rakip bulundu (eşik={args.th}):")
    for j in top_idx:
        row = df_all.iloc[j]
        sim = float(sims[j])
        print(f"- [{sim:.3f}] {row['product_title']} (ASIN={row['parent_asin']})")
    print("=" * 80)

    # Blog fikirleri üret (marka ismini yasaklayan prompt ile)
    pros = product.get("review_pros") or product.get("pros_raw") or ""
    cons = product.get("review_cons") or product.get("cons_raw") or ""

    custom_prompt = (
        LLM_USER_TMPL
        + "\nExtra rule: Do NOT include brand names in the blog titles. "
        + "Always use the generic product type instead."
    )

    prompt = custom_prompt.format(
        title=str(product.get("product_title", ""))[:280],
        pros=str(pros)[:800],
        cons=str(cons)[:800],
        ideas_n=args.ideas,
    )

    resp = _chat(
        [
            {"role": "system", "content": LLM_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        temperature=0.3,
        max_tokens=1200,
    )

    text = resp.choices[0].message.content.strip().strip("`")
    if text.lower().startswith("json"):
        text = text[4:].strip()

    import json
    try:
        ideas = json.loads(text)
    except Exception:
        ideas = []

    if not ideas:
        print("⚠️ LLM hiçbir fikir döndürmedi.")
    else:
        print(f"💡 Blog fikirleri ({args.ideas} adet, marka adı olmadan):")
        for it in ideas:
            print(f"- {it.get('title','')} ({it.get('intent_category','')}, post_type={it.get('post_type','')})")

if __name__ == "__main__":
    main()
