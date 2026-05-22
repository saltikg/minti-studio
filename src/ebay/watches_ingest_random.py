#!/usr/bin/env python3
import os, random, subprocess, duckdb

DB_PATH = os.getenv("DB_PATH", "/home/ubuntu/blog-factory/warehouse/blog_factory.duckdb")
PY = "/home/ubuntu/blog-factory/.venv/bin/python"
INGEST = "/home/ubuntu/blog-factory/src/ebay/2-ebay_products_ingest.py"

con = duckdb.connect(DB_PATH, read_only=True)
rows = con.execute("""
  SELECT season_name
  FROM seasons
  WHERE season_group='watches'
  ORDER BY created_at DESC
""").fetchall()
con.close()

seasons = [r[0] for r in rows] or []
choice = random.choice(seasons) if seasons else None
if not choice:
    raise SystemExit("No watches seasons found.")

cmd = [PY, INGEST, "--season-name", choice]
subprocess.run(cmd, check=False)
