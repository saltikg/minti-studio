#!/usr/bin/env python3
import argparse, os, duckdb, pandas as pd
from dotenv import load_dotenv
import re
from tqdm import tqdm
import uuid

# ---------------- Load .env ----------------
load_dotenv()

# ---------------- CONFIG ----------------
CATEGORIES = [
    "All_Beauty",
    "Appliances",
    "Arts_Crafts_and_Sewing",
    "Automotive",
    "Baby_Products",
    "Books",
    "Cell_Phones_and_Accessories",
    "Clothing_Shoes_and_Jewelry",
    "Electronics",
    "Grocery_and_Gourmet_Food",
    "Health_and_Household",
    "Home_and_Kitchen",
    "Industrial_and_Scientific",
    "Kindle_Store",
    "Musical_Instruments",
    "Office_Products",
    "Patio_Lawn_and_Garden",
    "Pet_Supplies",
    "Sports_and_Outdoors",
    "Tools_and_Home_Improvement",
    "Toys_and_Games",
    "Video_Games"
]

LOCAL_DIR = "/home/ubuntu/amazon_reviews"
BASE_REPO = "https://huggingface.co/datasets/McAuley-Lab/Amazon-Reviews-2023/resolve/main/raw"

# ---------------- Helpers ----------------
def clean_text(s: str) -> str:
    if not isinstance(s, str):
        return ""
    s = re.sub(r"<.*?>", " ", s)
    s = re.sub(r"&[a-z]+;", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def pick_image(images):
    if images is None:
        return ""
    if hasattr(images, "tolist"):
        try:
            images = images.tolist()
        except Exception:
            return ""
    if not isinstance(images, list):
        return ""
    for img in images:
        if isinstance(img, dict):
            for key in ["hi_res", "large", "thumb"]:
                val = img.get(key)
                if val and isinstance(val, str) and val.strip():
                    return val.strip()
    return ""

def join_field(val):
    if val is None:
        return ""
    if isinstance(val, (list, tuple, set, pd.Series)):
        return " | ".join([str(x) for x in val if pd.notna(x)])
    if hasattr(val, "tolist"):
        try:
            return " | ".join([str(x) for x in val.tolist() if pd.notna(x)])
        except Exception:
            return str(val)
    return str(val)

# ---------------- Pipeline ----------------
def process_category(category, min_reviews=50, min_rating=3.8, limit=1000, n_each=10):
    print(f"\n📦 Processing category: {category}")

    # local dosya varsa onu kullan, yoksa URL
    url_reviews = f"{LOCAL_DIR}/{category}.jsonl"
    if not os.path.exists(url_reviews):
        url_reviews = f"{BASE_REPO}/review_categories/{category}.jsonl"

    url_meta = f"{LOCAL_DIR}/meta_{category}.jsonl"
    if not os.path.exists(url_meta):
        url_meta = f"{BASE_REPO}/meta_categories/meta_{category}.jsonl"

    # --- Ürünleri seç ---
    query = f"""
    WITH agg AS (
      SELECT
        parent_asin,
        COUNT(*) AS n_reviews,
        AVG(rating) AS avg_rating,
        SUM(COALESCE(helpful_vote,0)) AS helpful_sum
      FROM read_json_auto('{url_reviews}')
      GROUP BY parent_asin
    ),
    filtered AS (
      SELECT * FROM agg
      WHERE n_reviews >= {min_reviews} AND avg_rating >= {min_rating}
    )
    SELECT
      f.parent_asin,
      f.n_reviews,
      ROUND(f.avg_rating,2) AS avg_rating,
      f.helpful_sum,
      m.title AS product_title,
      m.price,
      m.main_category,
      m.details,
      m.images,
      m.features,
      m.description
    FROM filtered f
    LEFT JOIN (
        SELECT parent_asin, title, price, main_category, details, images, features, description
        FROM read_json_auto('{url_meta}', ignore_errors=true)
    ) m
    ON f.parent_asin = m.parent_asin
    ORDER BY f.n_reviews DESC
    LIMIT {limit}
    """
    df_products = duckdb.sql(query).df()

    asin_list = df_products["parent_asin"].tolist()
    if not asin_list:
        print("⚠️ No products found.")
        return

    # --- Reviewları getir (disk'e stream ederek) ---
    asin_csv = "', '".join(map(str, asin_list))
    tmp_dir = os.path.join(os.path.dirname(__file__), "..", "tmp")
    os.makedirs(tmp_dir, exist_ok=True)
    tmp_parquet = os.path.join(tmp_dir, f"reviews_{category.lower()}_{uuid.uuid4().hex}.parquet")

    pos_neg_buffer = n_each * 5  # her ürün için yeterli buffer

    duckdb.sql(f"""
    COPY (
      WITH ranked AS (
        SELECT
          parent_asin,
          text,
          rating,
          helpful_vote,
          CASE
            WHEN rating >= 4.0 THEN 'pos'
            WHEN rating <= 2.0 THEN 'neg'
            ELSE 'mid'
          END AS grp,
          ROW_NUMBER() OVER (
            PARTITION BY parent_asin,
                         CASE
                           WHEN rating >= 4.0 THEN 'pos'
                           WHEN rating <= 2.0 THEN 'neg'
                           ELSE 'mid'
                         END
            ORDER BY COALESCE(helpful_vote, 0) DESC
          ) AS rn
        FROM read_json_auto('{url_reviews}')
        WHERE parent_asin IN ('{asin_csv}')
          AND length(text) >= 80
      )
      SELECT parent_asin, text, rating, helpful_vote
      FROM ranked
      WHERE (grp = 'pos' AND rn <= {pos_neg_buffer})
         OR (grp = 'neg' AND rn <= {pos_neg_buffer})
    ) TO '{tmp_parquet}' (FORMAT PARQUET);
    """)

    df_reviews = pd.read_parquet(tmp_parquet)
    try:
        os.remove(tmp_parquet)
    except Exception:
        pass

    # --- Ürün bazlı pros/cons derle ---
    rows = []
    for _, meta in tqdm(df_products.iterrows(), total=len(df_products), desc="Ürünler işleniyor"):
        asin = meta["parent_asin"]

        df_pos = df_reviews[(df_reviews["parent_asin"]==asin) & (df_reviews["rating"]>=4.0)].head(n_each)
        df_neg = df_reviews[(df_reviews["parent_asin"]==asin) & (df_reviews["rating"]<=2.0)].head(n_each)

        pros_raw = " | ".join([clean_text(x) for x in df_pos["text"].tolist()])
        cons_raw = " | ".join([clean_text(x) for x in df_neg["text"].tolist()])

        brand = ""
        if isinstance(meta.get("details"), dict):
            brand = meta["details"].get("brand") or meta["details"].get("Brand") or ""

        row = {
            "parent_asin": asin,
            "product_title": meta.get("product_title",""),
            "brand": brand,
            "price": meta.get("price",""),
            "avg_rating": meta.get("avg_rating",""),
            "n_reviews": meta.get("n_reviews",""),
            "description": join_field(meta.get("description")),
            "features": join_field(meta.get("features")),
            "pros_raw": pros_raw,
            "cons_raw": cons_raw,
            "image_url": pick_image(meta.get("images"))
        }
        rows.append(row)

    # --- CSV'ye kaydet ---
    out_dir = os.path.join(os.path.dirname(__file__), "..", "data")
    os.makedirs(out_dir, exist_ok=True)

    name = category.replace("All_", "").lower() + "_products_summaries.csv"
    out_csv = os.path.join(out_dir, name)

    df_new = pd.DataFrame(rows)
    if os.path.exists(out_csv):
        df_new.to_csv(out_csv, mode="a", index=False, header=False)
    else:
        df_new.to_csv(out_csv, index=False)

    print(f"✅ Saved {len(rows)} products to {out_csv}")

# ---------------- Main ----------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--category", type=str, required=True)
    ap.add_argument("--limit", type=int, default=1000)
    ap.add_argument("--n_each", type=int, default=10)
    args = ap.parse_args()

    if args.category == "ALL":
        for cat in CATEGORIES:
            process_category(cat, limit=args.limit, n_each=args.n_each)
    else:
        if args.category not in CATEGORIES:
            raise SystemExit(f"Category {args.category} not in known list.")
        process_category(args.category, limit=args.limit, n_each=args.n_each)

if __name__ == "__main__":
    main()
