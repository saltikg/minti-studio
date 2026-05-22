#!/usr/bin/env python3
import os, duckdb
from utils import slugify
from pathlib import Path
import shutil

BASE_DIR = "/home/ubuntu/blog-factory"
DB_PATH  = os.path.join(BASE_DIR, "warehouse", "blog_factory.duckdb")
DOCS_DIR = os.path.join(BASE_DIR, "docs/blogs")

con = duckdb.connect(DB_PATH)
rows = con.execute("""
    SELECT i.idea_id, i.idea_title, i.category_slug
    FROM ideas i
    JOIN blog_posts b ON b.idea_id = i.idea_id
    WHERE lower(b.status)='published'
""").fetchall()
con.close()

moved = 0
for idea_id, title, cat in rows:
    old_dir = Path(DOCS_DIR) / cat / idea_id
    new_dir = Path(DOCS_DIR) / cat / slugify(title)
    if old_dir.exists():
        if not new_dir.exists():
            shutil.move(str(old_dir), str(new_dir))
            print(f"✅ moved {old_dir} → {new_dir}")
            moved += 1
        else:
            print(f"⚠️ target already exists, skipped: {new_dir}")
    else:
        # idea_id klasörü yoksa slug klasör zaten vardır
        pass

print(f"Done. Moved {moved} folders.")
