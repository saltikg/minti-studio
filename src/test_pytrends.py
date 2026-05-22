#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys, json, requests, pandas as pd
from pytrends.request import TrendReq
import time, random


def fetch_categories():
    """Kategorileri göster (tarayıcıdan JSON çekerek)"""
    url = "https://trends.google.com/trends/api/explore/pickers/category"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/119.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://trends.google.com/trends/",
    }

    resp = requests.get(url, headers=headers)
    if resp.status_code != 200:
        print(f"❌ Request failed with status {resp.status_code}")
        print(resp.text[:500])
        return

    text = resp.text.lstrip(")]}',")
    data = json.loads(text)

    def print_categories(children, indent=0):
        for child in children:
            print(" " * indent + f"{child['name']} (ID: {child['id']})")
            if "children" in child:
                print_categories(child["children"], indent + 2)

    print("\n📂 Google Trends Category IDs:\n")
    print_categories(data["children"])
    print("\n✅ Yukarıdaki ID'lerden birini terminalden verebilirsin: python3 src/test_pytrends.py <category_id>\n")


def fetch_category_trends(cat_id):
    """Belirtilen kategori ID için trend sorguları getir"""
    pytrends = TrendReq(hl='en-US', tz=360)
    seed_keywords = ["best", "top", "vs", "under", "trend", "buy"]

    results = pd.DataFrame()


    for kw in seed_keywords:
        try:
            pytrends.build_payload(
                kw_list=[kw],
                cat=int(cat_id),
                timeframe='today 3-m',   # 7 gün yerine 3 ay daha güvenli
                geo='US'
            )
            related = pytrends.related_queries()
            ...
        except Exception as e:
            print(f"⚠️ Hata: {kw} için sorgu çekilemedi. {e}")
        # 👇 her istek arasında rastgele bekleme
        time.sleep(random.uniform(10, 20))


    if results.empty:
        print(f"⚠️ No trends found for category {cat_id}.")
    else:
        fname = f"trends_cat_{cat_id}.csv"
        results.to_csv(fname, index=False)
        print(f"\n📊 Google Trends for Category {cat_id} (last 7 days):\n")
        print(results.head(20))
        print(f"\n✅ Data saved to {fname}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Kullanım:\n")
        print("  python3 src/test_pytrends.py categories      # kategori ağacını göster")
        print("  python3 src/test_pytrends.py <category_id>   # örn. python3 src/test_pytrends.py 20\n")
    else:
        arg = sys.argv[1]
        if arg == "categories":
            fetch_categories()
        else:
            fetch_category_trends(arg)
