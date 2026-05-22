#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, duckdb
BASE_DIR = "/home/ubuntu/blog-factory"
DB_PATH  = os.path.join(BASE_DIR, "warehouse", "blog_factory.duckdb")
INDEX_MD = os.path.join(BASE_DIR, "docs/blogs/index.md")

def main():
    con = duckdb.connect(DB_PATH, read_only=True)
    rows = con.execute("""
        SELECT i.idea_title, b.blog_url
        FROM blog_posts b
        JOIN ideas i ON b.idea_id=i.idea_id
        WHERE lower(b.status)='published'
        ORDER BY i.category_slug, i.idea_title
    """).fetchall()
    con.close()

    with open(INDEX_MD, "w", encoding="utf-8") as f:
        f.write("---\n")
        f.write("title: Minti Product\n")
        f.write("---\n\n")
        f.write("# Blog Index\n\n")
        for title, url in rows:
            # blog_url zaten /{cat}/{slug}/ formatında
            f.write(f"- [{title}]({url})\n")

    print(f"✅ rebuilt index.md with {len(rows)} items")

if __name__ == "__main__":
    main()
