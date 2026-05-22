#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, duckdb
from utils import slugify  # pipeline'da kullanılanla aynı

BASE_DIR = "/home/ubuntu/blog-factory"
DB_PATH  = os.path.join(BASE_DIR, "warehouse", "blog_factory.duckdb")

def main():
    con = duckdb.connect(DB_PATH, read_only=False)
    rows = con.execute("""
        SELECT i.idea_id, i.idea_title, i.category_slug
        FROM ideas i
        JOIN blog_posts b ON b.idea_id = i.idea_id
        WHERE lower(b.status) = 'published'
    """).fetchall()

    updated = 0
    for idea_id, title, cat in rows:
        slug = slugify(title)
        url  = f"/{cat}/{slug}/"
        con.execute("UPDATE blog_posts SET blog_url = ? WHERE idea_id = ?", [url, idea_id])
        updated += 1

    con.close()
    print(f"✅ blog_url updated for {updated} published posts → /{{category}}/{{slug}}/")

if __name__ == "__main__":
    main()
