#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
eBay Seasonal (Halloween-first) quick tester
- Keyword search (Browse item_summary/search)
- Show result cards with SELLER TRUST label (for seasonal/catalogless listings)
- Fetch detail for the first item and show deeper insights
- If reviewRating exists (catalog product), compute rating insights (Wilson LB)
- If not, rely on seller metrics (feedback%, score, returns) + shipping ETA

ENV needed:
  EBAY_OAUTH_TOKEN            # required
  EBAY_MARKETPLACE=EBAY_US    # optional, default EBAY_US
  EBAY_AFFILIATE_CAMPAIGN_ID  # optional (EPN)
  EBAY_AFFILIATE_REFERENCE_ID # optional (EPN)
  EBAY_CONTEXT_COUNTRY=US     # optional, default US
  EBAY_CONTEXT_ZIP=94301      # optional, improves shipping estimates
"""

import os, sys, json, math, time, urllib.parse, datetime as dt
from typing import Any, Dict, List, Optional
import requests

EBAY_BASE = "https://api.ebay.com"
MARKETPLACE = os.getenv("EBAY_MARKETPLACE", "EBAY_US")

OAUTH = os.getenv("EBAY_OAUTH_TOKEN") or ""
AFF_CAMPAIGN = os.getenv("EBAY_AFFILIATE_CAMPAIGN_ID") or ""
AFF_REF = os.getenv("EBAY_AFFILIATE_REFERENCE_ID") or ""

CTX_COUNTRY = os.getenv("EBAY_CONTEXT_COUNTRY", "US")
CTX_ZIP = os.getenv("EBAY_CONTEXT_ZIP", "94301")

if not OAUTH:
    print("ERROR: Set EBAY_OAUTH_TOKEN first.")
    sys.exit(1)

def build_enduserctx() -> str:
    parts = []
    # affiliate optional
    if AFF_CAMPAIGN:
        parts.append(f"affiliateCampaignId={AFF_CAMPAIGN}")
        parts.append(f"affiliateReferenceId={AFF_REF}")
    # contextual location (improves shipping ETA/prices)
    if CTX_COUNTRY or CTX_ZIP:
        parts.append(f"contextualLocation=country={CTX_COUNTRY},zip={CTX_ZIP}")
    return ";".join(parts)

def headers() -> Dict[str, str]:
    h = {
        "Authorization": f"Bearer {OAUTH}",
        "X-EBAY-C-MARKETPLACE-ID": MARKETPLACE,
        "Accept": "application/json",
    }
    enduser = build_enduserctx()
    if enduser:
        h["X-EBAY-C-ENDUSERCTX"] = enduser
    return h

def http_get(path: str, params: Dict[str, Any]) -> Dict[str, Any]:
    url = f"{EBAY_BASE}{path}"
    r = requests.get(url, headers=headers(), params=params, timeout=25)
    if r.status_code >= 400:
        raise RuntimeError(f"GET {path} failed: {r.status_code} {r.text[:500]}")
    return r.json()

def http_get_abs(url: str) -> Dict[str, Any]:
    r = requests.get(url, headers=headers(), timeout=25)
    if r.status_code >= 400:
        raise RuntimeError(f"GET {url} failed: {r.status_code} {r.text[:500]}")
    return r.json()

# ---------- Review insights (for catalog products) ----------
def wilson_lower_bound(pos: int, n: int, z: float = 1.96) -> float:
    if n <= 0:
        return 0.0
    phat = pos / n
    denom = 1 + z*z/n
    center = phat + z*z/(2*n)
    margin = z*math.sqrt((phat*(1-phat) + z*z/(4*n))/n)
    return (center - margin)/denom

def review_insights(review_rating: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not review_rating:
        return None
    avg = float(review_rating.get("averageRating") or 0)
    n = int(review_rating.get("reviewCount") or 0)
    hist = review_rating.get("ratingHistograms") or []
    pos = sum(h.get("count", 0) for h in hist if int(h.get("rating", 0)) in (4,5))
    neg = sum(h.get("count", 0) for h in hist if int(h.get("rating", 0)) in (1,2))
    wlb = wilson_lower_bound(pos, n) if n else 0.0

    label = "low"
    if n >= 50 and wlb >= 0.70: label = "high"
    elif n >= 20 and wlb >= 0.50: label = "medium"

    return {
        "average": round(avg, 2),
        "count": n,
        "pos_share": round(pos/n, 3) if n else None,
        "neg_share": round(neg/n, 3) if n else None,
        "wilson_lb": round(wlb, 3),
        "label": label,
        "hist": hist
    }

# ---------- Seller trust (for seasonal/catalogless listings) ----------
def parse_iso8601(s: str) -> Optional[dt.datetime]:
    try:
        return dt.datetime.fromisoformat(s.replace("Z","+00:00"))
    except Exception:
        return None

def days_between(now: dt.datetime, future: Optional[str]) -> Optional[int]:
    if not future: return None
    d = parse_iso8601(future)
    if not d: return None
    delta = d - now
    return max(0, int(round(delta.total_seconds()/86400.0)))

def seller_trust_label(item: Dict[str, Any]) -> Dict[str, Any]:
    """
    Heuristic score (0..1) + label:
     - Feedback% (0.55)
     - FeedbackScore log scale (0.25)
     - Returns accepted (0.10)
     - Shipping speed bonus (0.10)
    """
    seller = item.get("seller") or {}
    # feedbackPercentage bazen string, bazen None olabilir
    try:
        fb_pct = float(seller.get("feedbackPercentage") or 0.0) / 100.0
    except Exception:
        fb_pct = 0.0

    try:
        fb_score = int(seller.get("feedbackScore") or 0)
    except Exception:
        fb_score = 0

    # 10^3 (1000) civarı ve üzeri güçlü kabul — log10 ölçek
    score_norm = min(1.0, math.log10(max(1, fb_score)) / 3.0)

    # DİKKAT: returnTerms alanı mevcut ama None olabilir → "or {}" kullan
    returns = item.get("returnTerms") or {}
    returns_accepted = 1.0 if returns.get("returnsAccepted") else 0.0

    # shippingOptions da None olabilir
    now = dt.datetime.now(dt.timezone.utc)
    ship_opts = item.get("shippingOptions") or []
    eta_days = None
    if ship_opts:
        eta_days = days_between(now, ship_opts[0].get("minEstimatedDeliveryDate"))
    ship_bonus = 0.0
    if eta_days is not None:
        if eta_days <= 5: ship_bonus = 1.0
        elif eta_days <= 8: ship_bonus = 0.6
        elif eta_days <= 12: ship_bonus = 0.3

    score = 0.55*fb_pct + 0.25*score_norm + 0.10*returns_accepted + 0.10*ship_bonus
    label = "low"
    if score >= 0.75: label = "high"
    elif score >= 0.55: label = "medium"

    reasons = []
    if seller.get("username"):
        reasons.append(f"seller {seller.get('username')}")
    reasons.append(f"fb%={round(fb_pct*100,1)} (score={fb_score})")
    reasons.append("returns=yes" if returns_accepted else "returns=no")
    if eta_days is not None:
        reasons.append(f"eta~{eta_days}d")

    return {"score": round(score, 3), "label": label, "reasons": ", ".join(reasons)}

# ---------- Picking / printing ----------
def pick_fields_from_summary(s: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "itemId": s.get("itemId"),
        "title": s.get("title"),
        "price": s.get("price"),
        "condition": s.get("condition"),
        "image": (s.get("image") or {}).get("imageUrl"),
        "seller": s.get("seller"),
        "shippingOptions": s.get("shippingOptions"),
        "itemWebUrl": s.get("itemWebUrl"),
        "itemAffiliateWebUrl": s.get("itemAffiliateWebUrl"),
        "itemHref": s.get("itemHref"),
        "returnTerms": s.get("returnTerms"),  # sometimes present in summary
    }

def pick_fields_from_detail(d: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "itemId": d.get("itemId"),
        "title": d.get("title"),
        "brand": d.get("brand"),
        "gtin": d.get("gtin"),
        "epid": d.get("epid"),
        "price": d.get("price"),
        "seller": d.get("seller"),
        "shippingOptions": d.get("shippingOptions"),
        "returnTerms": d.get("returnTerms"),
        "reviewRating": d.get("reviewRating"),
        "itemWebUrl": d.get("itemWebUrl"),
        "itemAffiliateWebUrl": d.get("itemAffiliateWebUrl"),
    }

def safe_url(d: Dict[str, Any]) -> str:
    return d.get("itemAffiliateWebUrl") or d.get("itemWebUrl") or ""

def print_card(idx: int, s: Dict[str, Any]) -> None:
    trust = seller_trust_label(s)
    p = s.get("price") or {}
    print(f"[{idx}] {s.get('title')}")
    print(f"    itemId: {s.get('itemId')}")
    print(f"    price:  {p.get('value')} {p.get('currency')}")
    print(f"    cond.:  {s.get('condition')}")
    seller = s.get("seller") or {}
    print(f"    seller: {seller.get('username', '-') } (fb%={seller.get('feedbackPercentage', '-')}, score={seller.get('feedbackScore', '-')})")
    print(f"    TRUST:  {trust['label']} (score={trust['score']}) — {trust['reasons']}")
    print(f"    link:   {safe_url(s)}")
    print()

# ---------- Core flow ----------
def search_items(keyword: str, limit: int = 20, category_id: Optional[str] = None) -> List[Dict[str, Any]]:
    params = {
        "q": keyword,
        "limit": limit,
        # Halloween primary categories often include Yard Décor (261649), Holiday & Seasonal (907)
        # "category_ids": category_id,
        # "filter": "conditions:{NEW|USED}",
        # "fieldgroups": "EXTENDED",
    }
    data = http_get("/buy/browse/v1/item_summary/search", params)
    items = data.get("itemSummaries") or []
    return [pick_fields_from_summary(x) for x in items]

def get_detail(item_href: str) -> Dict[str, Any]:
    # add fieldgroups=PRODUCT to help product fields when available
    url = item_href if "fieldgroups=" in item_href else (item_href + "?fieldgroups=PRODUCT")
    data = http_get_abs(url)
    return pick_fields_from_detail(data)

def main():
    # Defaults for Halloween
    default_kw = "halloween blow mold"
    keyword = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else default_kw
    limit = int(os.getenv("EBAY_LIMIT", "12"))

    print(f"🎃 Searching: {keyword} (limit={limit})  market={MARKETPLACE}\n")
    results = search_items(keyword, limit=limit)

    if not results:
        print("No results.")
        return

    for i, r in enumerate(results, 1):
        print_card(i, r)

    # pick the first result for deep dive
    first = results[0]
    if not first.get("itemHref"):
        print("\nNo itemHref for first item; cannot fetch detail.")
        return

    print("➡️  Fetching detail for the first item...\n")
    detail = get_detail(first["itemHref"])
    insight = review_insights(detail.get("reviewRating"))
    trust = seller_trust_label(detail)

    print(json.dumps({
        "title": detail.get("title"),
        "itemId": detail.get("itemId"),
        "price": detail.get("price"),
        "brand": detail.get("brand"),
        "gtin": detail.get("gtin"),
        "epid": detail.get("epid"),
        "reviewInsight": insight,     # may be None for seasonal items
        "sellerTrust": trust,
        "link": safe_url(detail)
    }, indent=2))

    # short human-readable add-on
    print("\n📌 Insight summary:")
    if insight:
        print(f"- Rating: avg={insight['average']} (n={insight['count']}), WilsonLB={insight['wilson_lb']} → {insight['label']}")
    else:
        print("- No product-level rating (likely non-catalog seasonal listing)")

    print(f"- Seller trust: {trust['label']} (score={trust['score']}) — {trust['reasons']}")
    returns = detail.get("returnTerms") or {}
    print(f"- Returns accepted: {'yes' if returns.get('returnsAccepted') else 'no'}")

if __name__ == "__main__":
    main()
