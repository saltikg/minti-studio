#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Google Trends Data Fetcher
--------------------------
1. Fetch today's trending search topics (optionally by category like 'shopping').
2. Display top keywords with interest scores.
3. Next step: store in google_trends_score table and compute LLM scores.

Requires:
  pip install pytrends duckdb openai
"""

import os
import datetime as dt
from pytrends.request import TrendReq

DB_PATH = os.getenv("DB_PATH", "/home/ubuntu/blog-factory/warehouse/blog_factory.duckdb")
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = "gpt-4o-mini"

def fetch_daily_trends(country_code="US", category=18):
    """
    Fetch today's trending searches from Google Trends.
    category=18 → Shopping (https://github.com/pat310/google-trends-api/wiki/Google-Trends-Categories)
    """
    pytrends = TrendReq(hl='en-US', tz=360)
    today = dt.date.today().isoformat()

    print(f"📅 Fetching {country_code} daily trends for {today}...")

    try:
        daily_trends = pytrends.trending_searches(pn=country_code)
        print("\n🔥 Top Daily Trending Searches:")
        for i, kw in enumerate(daily_trends[0:20], 1):
            print(f"{i:02d}. {kw}")
        return daily_trends
    except Exception as e:
        print("❌ Error fetching daily trends:", e)
        return []

def fetch_related_queries(keyword_list, country_code="US"):
    """
    For a given keyword list, get related queries (to later filter for shopping intent)
    """
    pytrends = TrendReq(hl='en-US', tz=360)
    pytrends.build_payload(keyword_list, cat=0, timeframe='now 7-d', geo=country_code)
    related = pytrends.related_queries()
    print("\n🛍 Related queries for top keywords:")
    for kw, val in related.items():
        if val and 'top' in val and not val['top'].empty:
            top_related = val['top']['query'].head(5).tolist()
            print(f" - {kw}: {top_related}")
    return related

if __name__ == "__main__":
    # Step 1: fetch today's trending searches
    trends = fetch_daily_trends()

    # Step 2: pick first 5 and show related queries
    if len(trends) > 0:
        fetch_related_queries(trends[:5].tolist())
