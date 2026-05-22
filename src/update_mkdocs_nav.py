#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, yaml, duckdb
from utils import slugify

BASE_DIR   = "/home/ubuntu/blog-factory"
DB_PATH    = os.path.join(BASE_DIR, "warehouse", "blog_factory.duckdb")
MKDOCS_YML = os.path.join(BASE_DIR, "mkdocs.yml")
DOCS_DIR   = os.path.join(BASE_DIR, "docs/blogs")

def get_categories():
    con = duckdb.connect(DB_PATH, read_only=True)
    df = con.execute("""
        SELECT DISTINCT i.category_slug
        FROM ideas i
        JOIN blog_posts b ON b.idea_id=i.idea_id
        WHERE lower(b.status)='published'
        ORDER BY 1
    """).df()
    con.close()
    return [r for r in df["category_slug"].tolist()]

def write_category_index(cat):
    con = duckdb.connect(DB_PATH, read_only=True)
    df = con.execute("""
        SELECT i.idea_title
        FROM ideas i
        JOIN blog_posts b ON b.idea_id=i.idea_id
        WHERE i.category_slug=? AND lower(b.status)='published'
        ORDER BY i.idea_title
    """, [cat]).df()
    con.close()

    os.makedirs(os.path.join(DOCS_DIR, cat), exist_ok=True)
    out = os.path.join(DOCS_DIR, cat, "index.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write(f"# {cat.replace('_',' ').title()}\n\n")
        for _, row in df.iterrows():
            s = slugify(row["idea_title"])
            # ABSOLUTE link: /{cat}/{slug}/
            f.write(f"- [{row['idea_title']}](/{cat}/{s}/)\n")

def update_nav():
    cats = get_categories()
    with open(MKDOCS_YML, "r") as fh:
        cfg = yaml.safe_load(fh)

    nav = [{"Home": "index.md"}]
    for cat in cats:
        write_category_index(cat)
        nav.append({cat.replace("_"," ").title(): f"{cat}/index.md"})  # docs_dir=docs/blogs'e göre relatif

    cfg["docs_dir"] = "docs/blogs"  # emin olmak için
    cfg["nav"] = nav

    with open(MKDOCS_YML, "w") as fh:
        yaml.dump(cfg, fh, sort_keys=False)
    print("✅ mkdocs.yml + category index’ler güncellendi.")

if __name__ == "__main__":
    update_nav()
