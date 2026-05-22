#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Quick ingest: Random keyword → eBay products → DB insert
"""

import os, sys, random, json, duckdb, datetime as dt
import requests, math

DB_PATH = os.getenv("DB_PATH", "/home/ubuntu/blog-factory/warehouse/blog_factory.duckdb")
from dotenv import load_dotenv
load_dotenv()  # .env dosyasını oku

OAUTH = os.getenv("EBAY_OAUTH_TOKEN")
MARKET  = os.getenv("EBAY_MARKETPLACE", "EBAY_US")

if not OAUTH:
    print("ERROR: Set EBAY_OAUTH_TOKEN")
    sys.exit(1)

# --- Helpers ---
def headers():
    return {
        "Authorization": f"Bearer {OAUTH}",
        "X-EBAY-C-MARKETPLACE-ID": MARKET,
        "Accept": "application/json",
    }

def http_get(path, params):
    url = f"https://api.ebay.com{path}"
    r = requests.get(url, headers=headers(), params=params, timeout=20)
    r.raise_for_status()
    return r.json()

def http_get_abs(url):
    r = requests.get(url, headers=headers(), timeout=20)
    r.raise_for_status()
    return r.json()

def seller_trust(item):
    fb_pct = float(item.get("seller",{}).get("feedbackPercentage") or 0) / 100
    fb_score = int(item.get("seller",{}).get("feedbackScore") or 0)
    score_norm = min(1.0, math.log10(max(1, fb_score)) / 3.0)
    returns = 1.0 if (item.get("returnTerms") or {}).get("returnsAccepted") else 0.0
    eta_days = None
    ship = item.get("shippingOptions") or []
    if ship:
        try:
            eta_days = (dt.datetime.fromisoformat(ship[0]["minEstimatedDeliveryDate"].replace("Z","+00:00"))
                        - dt.datetime.now(dt.timezone.utc)).days
        except Exception: pass
    ship_bonus = 1.0 if eta_days and eta_days <= 5 else 0.0
    score = 0.55*fb_pct + 0.25*score_norm + 0.10*returns + 0.10*ship_bonus
    label = "low"
    if score >= 0.75: label = "high"
    elif score >= 0.55: label = "medium"
    return score, label, eta_days, returns

# --- MAIN FLOW ---
con = duckdb.connect(DB_PATH)

kw = con.execute("SELECT phrase FROM season_phrases ORDER BY random() LIMIT 1").fetchone()[0]
print(f"🎯 Keyword: {kw}")

data = http_get("/buy/browse/v1/item_summary/search", {"q": kw, "limit": 20})
items = data.get("itemSummaries") or []

for it in items:
    pid = it["itemId"]
    title = it.get("title")
    price = (it.get("price") or {}).get("value")
    img = (it.get("image") or {}).get("imageUrl")
    src = "ebay"

    # insert product
    con.execute("""
        INSERT OR REPLACE INTO products
        (parent_asin, product_title, brand, price, category_slug, source, external_id)
        VALUES (?, ?, NULL, ?, ?, ?, ?)
    """, [pid, title, price, "ebay", src, pid])

    if img:
        con.execute("""
            INSERT OR REPLACE INTO product_media
            (parent_asin, image_url, source) VALUES (?, ?, ?)
        """, [pid, img, src])

    # detail + metrics
    if it.get("itemHref"):
        detail = http_get_abs(it["itemHref"]+"?fieldgroups=PRODUCT")
        score, label, eta, ret = seller_trust(detail)
        rr = detail.get("reviewRating") or {}
        con.execute("""
            INSERT INTO product_metrics_ebay
            (id, product_id, seller_score, feedback_pct, feedback_score, returns,
             eta_days, trust_level, review_rating, review_count, summary)
            VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, [
            pid,
            score,
            float(detail.get("seller",{}).get("feedbackPercentage") or 0),
            int(detail.get("seller",{}).get("feedbackScore") or 0),
            "Yes" if ret else "No",
            eta,
            label,
            float(rr.get("averageRating") or 0),
            int(rr.get("reviewCount") or 0),
            None
        ])

print("✅ Done.")
