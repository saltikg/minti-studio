import os, random, datetime
import requests

def research_trends_lite(locale="US", limit=20):
    """
    Günün trendlerini araştırır:
    - Google Trends
    - eBay Popular Items
    - YouTube trending titles
    (mock + örnekleme)
    """

    today = datetime.date.today().isoformat()
    results = []

    # 1. Google Trends (mock)
    try:
        r = requests.get(f"https://trends.google.com/trending?geo={locale}")
    except Exception:
        r = None
    if r and r.ok:
        # Normalde buradan parsing yapılır, örnek için placeholder:
        google_items = ["Thanksgiving decor", "Taylor Swift outfit", "Red Panda toy"]
    else:
        google_items = ["Cozy winter outfits", "Smart home gifts", "Holiday deals"]

    for g in google_items:
        results.append({
            "topic": g,
            "source": "Google Trends",
            "category": "search",
            "locale": locale,
            "volume": random.randint(50, 100),
            "reason": f"Spiking interest on {today}"
        })

    # 2. eBay örnek ürün trendleri
    ebay_items = ["Refurbished iPhone", "Wireless earbuds", "Smartwatch sale"]
    for e in ebay_items:
        results.append({
            "topic": e,
            "source": "eBay",
            "category": "commerce",
            "locale": locale,
            "volume": random.randint(40, 90),
            "reason": "High click volume on eBay API"
        })

    # 3. YouTube örnek trendleri
    yt_items = ["Holiday vlog ideas", "Tech unboxing 2025", "Black Friday deals"]
    for y in yt_items:
        results.append({
            "topic": y,
            "source": "YouTube",
            "category": "video",
            "locale": locale,
            "volume": random.randint(60, 120),
            "reason": "High view spike"
        })

    # Sırala ve sınırla
    results = sorted(results, key=lambda x: x["volume"], reverse=True)[:limit]
    return results
