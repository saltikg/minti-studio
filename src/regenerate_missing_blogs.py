#!/usr/bin/env python3
import os, duckdb
from pathlib import Path
from utils import slugify

BASE_DIR = "/home/ubuntu/blog-factory"
DB_PATH = os.path.join(BASE_DIR, "warehouse", "blog_factory.duckdb")
DOCS_DIR = os.path.join(BASE_DIR, "docs/blogs")

con = duckdb.connect(DB_PATH)
rows = con.execute("""
    SELECT i.idea_id, i.idea_title, i.category_slug, b.blog_url
    FROM blog_posts b
    JOIN ideas i ON b.idea_id = i.idea_id
    WHERE lower(b.status)='published'
""").fetchall()
con.close()

created = 0
for idea_id, title, cat, url in rows:
    slug = url.strip("/").split("/")[-1]  # DB’den slug al
    out_dir = Path(DOCS_DIR) / cat / slug
    out_file = out_dir / "index.md"
    if not out_file.exists():
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file.write_text(f"# {title}\n\n*Placeholder for blog content.*\n", encoding="utf-8")
        print(f"➕ created {out_file}")
        created += 1

print(f"Done. Created {created} missing blogs.")
