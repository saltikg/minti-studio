import requests
from typing import List, Dict
from .utils import get_app_token

def get_top_products(keyword: str, limit: int = 10) -> List[Dict]:
    """
    eBay Browse API’den verilen keyword için ürün listesi döner.
    """
    token = get_app_token()
    url = "https://api.ebay.com/buy/browse/v1/item_summary/search"
    headers = {"Authorization": f"Bearer {token}"}
    params = {"q": keyword, "limit": limit}
    r = requests.get(url, headers=headers, params=params)
    r.raise_for_status()
    items = r.json().get("itemSummaries", [])
    return [
        {
            "title": it["title"],
            "price": f"{it['price']['value']} {it['price']['currency']}",
            "brand": it.get("brand"),
            "image": it.get("image", {}).get("imageUrl"),
            "url": it["itemWebUrl"]
        }
        for it in items
    ]

if __name__ == "__main__":
    kws = ["halloween costumes", "pumpkin lights"]
    for kw in kws:
        print(f"\n🔎 Top products for: {kw}")
        for p in get_top_products(kw):
            print(f"- {p['title']} | {p['price']} | {p['url']}")
