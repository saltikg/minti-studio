#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Helper script to view generated ideas and their competitors from the database.

Usage:
  # View ideas and competitors for a specific ASIN
  python -m src.view_ideas --asin B0BXP115DF

  # View the most recently created idea
  python -m src.view_ideas --last

  # View competitors for a specific idea_id
  python -m src.view_ideas --idea-id i-best-samsung-galaxy-s23-fe-cases-2024-our-top-picks-for-style-and-protection
"""

import argparse
import duckdb
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "warehouse", "blog_factory.duckdb")

def main():
    parser = argparse.ArgumentParser(description="View generated ideas and competitors.")
    parser.add_argument("--asin", help="The parent_asin to look up.")
    parser.add_argument("--idea-id", help="The idea_id to look up.")
    parser.add_argument("--last", action="store_true", help="View the most recently added idea.")
    args = parser.parse_args()

    if not args.asin and not args.idea_id and not args.last:
        parser.error("Either --asin, --idea-id, or --last must be provided.")

    con = duckdb.connect(DB_PATH, read_only=True)

    idea_ids = []

    if args.asin:
        print(f"🔎 Searching for ideas related to ASIN: {args.asin}\n")
        # Find all idea_ids associated with this ASIN
        idea_ids = con.execute(
            "SELECT DISTINCT idea_id FROM idea_products WHERE parent_asin = ?", [args.asin]
        ).df()['idea_id'].tolist()

        if not idea_ids:
            print("No ideas found for this ASIN.")
            con.close()
            return
    elif args.last:
        print("🔎 Searching for the last added idea...\n")
        last_idea_id_row = con.execute(
            "SELECT idea_id FROM ideas ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        if last_idea_id_row:
            idea_ids = [last_idea_id_row[0]]
        else:
            print("No ideas found in the database.")
            con.close()
            return
    else:
        idea_ids = [args.idea_id]

    for idea_id in idea_ids:
        idea_title = con.execute("SELECT idea_title FROM ideas WHERE idea_id = ?", [idea_id]).fetchone()[0]
        print("=" * 80)
        print(f"💡 Idea: {idea_title}")
        print(f"   (ID: {idea_id})")
        print("-" * 80)
        print("👥 Products in this idea (Center Product + Competitors):")

        products_df = con.execute("""
            SELECT ip.parent_asin, p.product_title
            FROM idea_products ip
            JOIN v_products p ON ip.parent_asin = p.parent_asin
            WHERE ip.idea_id = ?
        """, [idea_id]).df()

        for _, row in products_df.iterrows():
            print(f"  - {row['parent_asin']}: {row['product_title']}")
        print("\n")

    con.close()

if __name__ == "__main__":
    main()