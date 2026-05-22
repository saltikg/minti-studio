#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import duckdb
import os

BASE_DIR = "/home/ubuntu/blog-factory"
DB_PATH = os.path.join(BASE_DIR, "warehouse", "blog_factory.duckdb")
DOCS_DIR = os.path.join(BASE_DIR, "docs/blogs")

def main():
    con = duckdb.connect(DB_PATH, read_only=False)
    rows = con.execute("""
        SELECT idea_id, blog_url
        FROM blog_posts
        WHERE lower(status) = 'published'
    """).fetchall()

    broken = []
    for idea_id, url in rows:
        url = (url or "").strip("/")
        path = os.path.join(DOCS_DIR, url, "index.md")
        if not os.path.exists(path):
            broken.append((idea_id, url))
            print(f"❌ MISSING: {path}")
        else:
            print(f"✅ EXISTS: {path}")

    if not broken:
        print("✅ No broken blogs found. All good.")
        return

    print(f"❌ Found {len(broken)} broken blogs:")
    for idea_id, url in broken:
        print(f"- {idea_id} → {url}")

    ans = input(f"\nDo you want to mark these {len(broken)} blogs as FAILED in DB? (y/n): ").strip().lower()
    if ans == "y":
        ids = [f"'{b[0]}'" for b in broken]
        sql = f"""
            UPDATE blog_posts
            SET status = 'failed'
            WHERE idea_id IN ({','.join(ids)});
        """
        con.execute(sql)
        print(f"🗑️  Updated {len(broken)} blog(s) to status=failed.")
    else:
        print("ℹ️ Nothing changed.")

    con.close()

if __name__ == "__main__":
    main()
