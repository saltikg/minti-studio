#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Blog Factory — Full DuckDB Warehouse (MERGE'siz)
Normalize şema + upsert (DELETE+INSERT) + durum tabloları

Komutlar:
  python src/warehouse_full.py init
  python src/warehouse_full.py sync-products --data-dir data
  python src/warehouse_full.py sync-summaries --data-dir data
  python src/warehouse_full.py import-ideas --ideas-csv data/ideas.csv
  python src/warehouse_full.py mark-status --idea-id i-001 --status published --blog-url https://site/blog/x
"""

import os, re, glob, argparse, duckdb

DB_PATH = "warehouse/blog_factory.duckdb"

DDL = r"""
-- ==== Dimension: categories (slug PK) ====
CREATE TABLE IF NOT EXISTS categories (
  slug VARCHAR PRIMARY KEY,
  name VARCHAR
);

-- ==== Product core ====
CREATE TABLE IF NOT EXISTS products (
  parent_asin   VARCHAR PRIMARY KEY,
  product_title VARCHAR,
  brand         VARCHAR,
  price         VARCHAR,
  category_slug VARCHAR REFERENCES categories(slug)
);

-- ==== Metrics ====
CREATE TABLE IF NOT EXISTS product_metrics (
  parent_asin VARCHAR PRIMARY KEY REFERENCES products(parent_asin),
  avg_rating  DOUBLE,
  n_reviews   INTEGER
);

-- ==== Long texts ====
CREATE TABLE IF NOT EXISTS product_text (
  parent_asin  VARCHAR PRIMARY KEY REFERENCES products(parent_asin),
  description  VARCHAR,
  features     VARCHAR,
  pros_raw     VARCHAR,
  cons_raw     VARCHAR
);

-- ==== Media ====
CREATE TABLE IF NOT EXISTS product_media (
  parent_asin VARCHAR PRIMARY KEY REFERENCES products(parent_asin),
  image_url   VARCHAR
);

-- ==== LLM review summaries (optional) ====
CREATE TABLE IF NOT EXISTS product_review_summaries (
  parent_asin           VARCHAR PRIMARY KEY REFERENCES products(parent_asin),
  review_paragraph      VARCHAR,
  review_pros           VARCHAR,
  review_cons           VARCHAR,
  review_summary_short  VARCHAR,
  review_loved          VARCHAR,
  review_tips           VARCHAR
);

-- ==== Ideas & blog status ====
CREATE TABLE IF NOT EXISTS ideas (
  idea_id       VARCHAR PRIMARY KEY,
  idea_title    VARCHAR,
  created_at    TIMESTAMP DEFAULT now(),
  category_slug VARCHAR REFERENCES categories(slug)
);

CREATE TABLE IF NOT EXISTS idea_products (
  idea_id     VARCHAR REFERENCES ideas(idea_id),
  parent_asin VARCHAR REFERENCES products(parent_asin),
  PRIMARY KEY (idea_id, parent_asin)
);

CREATE TABLE IF NOT EXISTS blog_posts (
  idea_id    VARCHAR PRIMARY KEY REFERENCES ideas(idea_id),
  status     VARCHAR,   -- draft | queued | published | failed
  blog_url   VARCHAR,
  updated_at TIMESTAMP DEFAULT now()
);

-- ==== Convenience view ====
CREATE OR REPLACE VIEW v_products AS
SELECT p.parent_asin, p.product_title, p.brand, p.price,
       m.avg_rating, m.n_reviews,
       t.description, t.features, t.pros_raw, t.cons_raw,
       media.image_url,
       r.review_paragraph, r.review_pros, r.review_cons,
       r.review_loved, r.review_tips,
       c.slug AS category_slug, c.name AS category_name
FROM products p
LEFT JOIN product_metrics m   ON m.parent_asin = p.parent_asin
LEFT JOIN product_text t      ON t.parent_asin = p.parent_asin
LEFT JOIN product_media media ON media.parent_asin = p.parent_asin
LEFT JOIN product_review_summaries r ON r.parent_asin = p.parent_asin
LEFT JOIN categories c        ON c.slug = p.category_slug;
"""

# ---- your connect() (memory limit via env) ----
def connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = duckdb.connect(DB_PATH)
    con.execute("PRAGMA threads = " + str(os.cpu_count()))
    mem_limit = os.environ.get("DUCKDB_MEMORY_LIMIT", "1GB")  # örn: "1500MB", "2GB"
    con.execute(f"PRAGMA memory_limit = '{mem_limit}';")
    return con

# -------------- helpers --------------
def slug_from_filename(fn: str) -> str:
    m = re.search(r"([^/\\]+)_products_summaries(?:_with_reviews)?\.csv$", fn, re.I)
    return m.group(1).lower() if m else "unknown"

def title_from_slug(slug: str) -> str:
    return (slug or "").replace("_", " ").title() if slug else "Unknown"

def ensure_schema(con):
    con.execute(DDL)

# -------------- commands --------------
def cmd_init(args):
    con = connect()
    ensure_schema(con)
    # seed (idempotent)
    for slug in ("electronics", "beauty"):
        con.execute("""
        INSERT INTO categories (slug, name)
        SELECT ?, ? WHERE NOT EXISTS (SELECT 1 FROM categories WHERE slug=?)
        """, [slug, title_from_slug(slug), slug])
    print(f"✅ init done → {DB_PATH}")

def sync_one_products_csv(con, csv_path: str):
    slug = slug_from_filename(csv_path)
    name = title_from_slug(slug)

    # ensure category
    con.execute("""
    INSERT INTO categories (slug, name)
    SELECT ?, ? WHERE NOT EXISTS (SELECT 1 FROM categories WHERE slug=?)
    """, [slug, name, slug])

    # stage CSV
    con.execute("CREATE TEMP TABLE stg AS SELECT * FROM read_csv_auto(?, sample_size=-1)", [csv_path])
    # add category_slug
    con.execute("CREATE TEMP TABLE stg2 AS SELECT s.*, ? AS category_slug FROM stg s", [slug])

    # products upsert (DELETE + INSERT)
    con.execute("""
        CREATE TEMP TABLE src_products AS
        SELECT parent_asin, product_title, brand, price, category_slug FROM stg2
    """)
    con.execute("DELETE FROM products WHERE parent_asin IN (SELECT parent_asin FROM src_products)")
    con.execute("""
        INSERT INTO products
        SELECT parent_asin, product_title, brand, price, category_slug
        FROM src_products
    """)
    con.execute("DROP TABLE src_products")

    # product_metrics
    con.execute("""
        CREATE TEMP TABLE src_metrics AS
        SELECT parent_asin, avg_rating, n_reviews FROM stg2
    """)
    con.execute("DELETE FROM product_metrics WHERE parent_asin IN (SELECT parent_asin FROM src_metrics)")
    con.execute("""
        INSERT INTO product_metrics
        SELECT parent_asin, avg_rating, n_reviews FROM src_metrics
    """)
    con.execute("DROP TABLE src_metrics")

    # product_text
    con.execute("""
        CREATE TEMP TABLE src_text AS
        SELECT parent_asin, description, features, pros_raw, cons_raw FROM stg2
    """)
    con.execute("DELETE FROM product_text WHERE parent_asin IN (SELECT parent_asin FROM src_text)")
    con.execute("""
        INSERT INTO product_text
        SELECT parent_asin, description, features, pros_raw, cons_raw FROM src_text
    """)
    con.execute("DROP TABLE src_text")

    # product_media
    con.execute("""
        CREATE TEMP TABLE src_media AS
        SELECT parent_asin, image_url FROM stg2
    """)
    con.execute("DELETE FROM product_media WHERE parent_asin IN (SELECT parent_asin FROM src_media)")
    con.execute("""
        INSERT INTO product_media
        SELECT parent_asin, image_url FROM src_media
    """)
    con.execute("DROP TABLE src_media")

    con.execute("DROP TABLE stg; DROP TABLE stg2;")
    print(f"✅ synced {os.path.basename(csv_path)} (category={slug})")

def cmd_sync_products(args):
    con = connect()
    ensure_schema(con)
    files = sorted(glob.glob(os.path.join(args.data_dir, "*_products_summaries.csv")))
    if not files:
        print("⚠️ No CSV files found in", args.data_dir)
        return
    for f in files:
        sync_one_products_csv(con, f)

def cmd_sync_summaries(args):
    con = connect()
    ensure_schema(con)
    files = sorted(glob.glob(os.path.join(args.data_dir, "*_products_summaries_with_reviews.csv")))
    if not files:
        print("⚠️ No *_with_reviews.csv found in", args.data_dir)
        return
    for f in files:
        con.execute("CREATE TEMP TABLE stg AS SELECT * FROM read_csv_auto(?, sample_size=-1)", [f])
        con.execute("""
            CREATE TEMP TABLE src_summaries AS
            SELECT
              parent_asin,
              review_paragraph,
              review_pros,
              review_cons,
              review_summary_short,
              review_loved,
              review_tips
            FROM stg
        """)
        con.execute("DELETE FROM product_review_summaries WHERE parent_asin IN (SELECT parent_asin FROM src_summaries)")
        con.execute("""
        INSERT INTO product_review_summaries
        SELECT parent_asin, review_paragraph, review_pros, review_cons,
               review_summary_short, review_loved, review_tips
        FROM src_summaries
        """)
        con.execute("DROP TABLE src_summaries; DROP TABLE stg;")
        print(f"✅ synced summaries {os.path.basename(f)}")

def cmd_import_ideas(args):
    # ideas.csv: idea_id, idea_title, asins, category_slug
    con = connect()
    ensure_schema(con)

    # read_csv_auto yerine read_csv (explicit schema) kullanalım
    con.execute(f"""
        CREATE TEMP TABLE stg_ideas AS
        SELECT *
        FROM read_csv('{args.ideas_csv}',
            columns={{
                'idea_id': 'VARCHAR',
                'idea_title': 'VARCHAR',
                'asins': 'VARCHAR',
                'category_slug': 'VARCHAR'
            }},
            header=True
        )
    """)

    # boş satırları at
    con.execute("DELETE FROM stg_ideas WHERE idea_id IS NULL OR idea_id = ''")

    # ensure categories
    slugs = [r[0] for r in con.execute("SELECT DISTINCT category_slug FROM stg_ideas").fetchall()]
    for s in slugs:
        if s:  # boş slug olmasın
            con.execute("""
            INSERT INTO categories (slug, name)
            SELECT ?, ? WHERE NOT EXISTS (SELECT 1 FROM categories WHERE slug=?)
            """, [s, title_from_slug(s), s])

    # ideas upsert (DELETE + INSERT)
    con.execute("DELETE FROM ideas WHERE idea_id IN (SELECT idea_id FROM stg_ideas WHERE idea_id IS NOT NULL)")
    con.execute("""
        INSERT INTO ideas (idea_id, idea_title, category_slug)
        SELECT idea_id, idea_title, category_slug
        FROM stg_ideas
        WHERE idea_id IS NOT NULL
    """)

    # explode ASINs and link (dedupe)
    con.execute("""
        CREATE TEMP TABLE stg_asins AS
        SELECT
        idea_id,
        TRIM(x) AS parent_asin
        FROM stg_ideas,
        UNNEST(
            str_split(
                regexp_replace(asins, ',', '|', 'g'),
                '|'
            )
        ) AS t(x);
    """)
    con.execute("DELETE FROM idea_products WHERE idea_id IN (SELECT idea_id FROM stg_asins)")
    con.execute("""
        INSERT INTO idea_products (idea_id, parent_asin)
        SELECT sa.idea_id, sa.parent_asin
        FROM stg_asins sa
        JOIN products p ON p.parent_asin = sa.parent_asin
    """)
    con.execute("DROP TABLE stg_asins; DROP TABLE stg_ideas;")

    print("✅ ideas + idea_products imported")

def cmd_mark_status(args):
    con = connect()
    ensure_schema(con)
    # UPDATE varsa güncelle, yoksa INSERT
    con.execute("""
    UPDATE blog_posts
    SET status = ?, blog_url = COALESCE(?, blog_url), updated_at = now()
    WHERE idea_id = ?
    """, [args.status, args.blog_url, args.idea_id])
    con.execute("""
    INSERT INTO blog_posts (idea_id, status, blog_url, updated_at)
    SELECT ?, ?, ?, now()
    WHERE NOT EXISTS (SELECT 1 FROM blog_posts WHERE idea_id = ?)
    """, [args.idea_id, args.status, args.blog_url, args.idea_id])
    print(f"✅ {args.idea_id} → {args.status}" + (f" ({args.blog_url})" if args.blog_url else ""))

# -------------- CLI --------------
def main():
    ap = argparse.ArgumentParser(description="Blog Factory — Full DuckDB Warehouse (no MERGE)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="create schema & seed categories")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("sync-products", help="normalize & upsert *_products_summaries.csv")
    p.add_argument("--data-dir", default="data")
    p.set_defaults(func=cmd_sync_products)

    p = sub.add_parser("sync-summaries", help="upsert *_products_summaries_with_reviews.csv")
    p.add_argument("--data-dir", default="data")
    p.set_defaults(func=cmd_sync_summaries)

    p = sub.add_parser("import-ideas", help="import ideas.csv and link ASINs")
    p.add_argument("--ideas-csv", default="data/ideas.csv")
    p.set_defaults(func=cmd_import_ideas)

    p = sub.add_parser("mark-status", help="mark blog status for an idea")
    p.add_argument("--idea-id", required=True)
    p.add_argument("--status", required=True, choices=["draft","queued","published","failed"])
    p.add_argument("--blog-url", default=None)
    p.set_defaults(func=cmd_mark_status)

    args = ap.parse_args()
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    args.func(args)

if __name__ == "__main__":
    main()
