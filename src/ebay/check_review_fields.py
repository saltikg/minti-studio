#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse, datetime, html, os, re, shutil, time, json
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote_plus
from urllib.parse import urlencode

import requests

# --------- mevcut fonksiyonun (temel liste) ---------
# Not: senin projende bu import var; aynen kalsın. get_top_products temel aramayı yapıyor.
# from .browse import get_top_products

# Eğer import sorun olursa “minimum workable” bir arama ekleyelim (fallback):
def _fallback_search(query: str, limit: int = 30, marketplace: str = "EBAY_US") -> List[Dict[str, Any]]:
    """
    Minimal fallback: eBay Browse Search
    Gereken env: EBAY_OAUTH_TOKEN
    """
    token = os.getenv("EBAY_OAUTH_TOKEN")
    if not token:
        print("⚠️  EBAY_OAUTH_TOKEN yok; sadece placeholder veri döneceğim.")
        return []

    url = "https://api.ebay.com/buy/browse/v1/item_summary/search"
    params = {"q": query, "limit": str(limit)}
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-EBAY-C-MARKETPLACE-ID": marketplace,
    }
    r = requests.get(url, headers=headers, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()
    out = []
    for it in data.get("itemSummaries", []):
        # normalize minimum alanlar
        out.append({
            "itemId": it.get("itemId"),
            "epid": it.get("epid"),
            "title": it.get("title"),
            "url": it.get("itemWebUrl"),
            "image": (it.get("image") or {}).get("imageUrl"),
            "price": (it.get("price") or {}),
            "seller": it.get("seller") or {},
        })
    return out

def get_top_products(query: str, limit: int = 30) -> List[Dict[str, Any]]:
    """
    Projendeki browse.get_top_products yerine kullanabileceğin
    minimal fallback. Eğer senin modülün sorunsuzsa bunu silebilirsin.
    """
    try:
        # from .browse import get_top_products as _orig
        # return _orig(query, limit=limit)
        return _fallback_search(query, limit=limit)
    except Exception as e:
        print(f"⚠️  browse.get_top_products çalışmadı: {e}. Fallback arama kullanılacak.")
        return _fallback_search(query, limit=limit)

# ---------- yardımcılar ----------

def pick_first(d: Dict[str, Any], *keys):
    for k in keys:
        if d is None: break
        if k in d and d[k] not in (None, "", []):
            return d[k]
    return None

def parse_price(x) -> Optional[float]:
    if x is None: return None
    if isinstance(x, (int,float)): return float(x)
    if isinstance(x, str):
        m = re.search(r"([0-9]+(?:\.[0-9]+)?)", x.replace(",", ""))
        return float(m.group(1)) if m else None
    if isinstance(x, dict):
        v = x.get("value")
        try: return float(v)
        except Exception: return None
    return None

def normalize_histogram(h) -> Dict[str,int]:
    out = {"1":0,"2":0,"3":0,"4":0,"5":0}
    if not h: return out
    if isinstance(h, dict):
        for k,v in h.items():
            ks = str(k)
            if ks in out:
                try: out[ks] = int(v)
                except Exception: pass
        return out
    if isinstance(h, list):
        for row in h:
            try:
                r = str(row.get("rating")); c = int(row.get("count",0))
                if r in out: out[r] = c
            except Exception:
                continue
        return out
    return out

def has_hist_data(hist: Dict[str,int]) -> bool:
    return sum(hist.values()) > 0

def ebay_search_url(q: str, base: str) -> str:
    return f"{base.rstrip('/')}/sch/i.html?_nkw={quote_plus(q)}"

# ---------- eBay detay & ürün (product) sorguları ----------

def _get_env_token() -> Optional[str]:
    return os.getenv("EBAY_OAUTH_TOKEN")

def _req_json(url: str, marketplace: str = "EBAY_US", params: Optional[Dict[str,str]] = None) -> Optional[Dict[str, Any]]:
    token = _get_env_token()
    if not token:
        print("⚠️  EBAY_OAUTH_TOKEN bulunamadı; detay sorguları atlanacak.")
        return None
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-EBAY-C-MARKETPLACE-ID": marketplace,
    }
    try:
        r = requests.get(url, headers=headers, params=params or {}, timeout=20)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"⚠️  eBay isteği hata: {e} [{url}]")
        return None

def _best_item_id(p: Dict[str, Any]) -> Optional[str]:
    return p.get("itemId") or p.get("item_id") or p.get("itemIdFromMarketplace")

def _best_epid(p: Dict[str, Any]) -> Optional[str]:
    return p.get("epid") or p.get("productId") or p.get("epidFromMarketplace")


def _ebay_get_items_batch(item_ids: List[str],
                          marketplace: str = "EBAY_US",
                          fieldgroups: str = "PRODUCT") -> Optional[Dict[str, Any]]:
    """
    Browse API: GET /buy/browse/v1/item/?item_ids=...&fieldgroups=PRODUCT
    Tek seferde (max 20) item detayı çeker. primaryProductReviewRating dönebilir.
    """
    token = os.getenv("EBAY_OAUTH_TOKEN")
    if not token or not item_ids:
        return None

    base = "https://api.ebay.com/buy/browse/v1/item/"
    params = {"item_ids": ",".join(item_ids), "fieldgroups": fieldgroups}
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-EBAY-C-MARKETPLACE-ID": marketplace,
    }
    try:
        r = requests.get(base, headers=headers, params=params, timeout=20)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"⚠️  getItems hata: {e}")
        return None


def enrich_with_ebay_details(products: List[Dict[str, Any]],
                             max_details: int = 10,
                             marketplace: str = "EBAY_US",
                             delay_sec: float = 0.0) -> List[Dict[str, Any]]:
    """
    İlk 'max_details' ürün için tek batch getItems çağrısı yapar (fieldgroups=PRODUCT).
    Dönen 'primaryProductReviewRating' → averageRating / reviewCount / ratingHistograms
    alanlarını produkta map’ler. Seller feedback % ve price için de daha sağlam normalize dener.
    """
    if not products:
        return products

    # İlk N ürünün REST itemId’lerini topla (v1|... formatı)
    ids = []
    index_map = {}  # itemId -> original index
    for idx, p in enumerate(products[:max_details]):
        item_id = p.get("itemId") or p.get("item_id")
        if item_id:
            ids.append(item_id)
            index_map[item_id] = idx

    if not ids:
        return products

    data = _ebay_get_items_batch(ids, marketplace=marketplace, fieldgroups="PRODUCT")
    if not data:
        return products

    items = data.get("items") or []
    for it in items:
        item_id = it.get("itemId")
        if not item_id or item_id not in index_map:
            continue
        i = index_map[item_id]
        p = products[i]

        # price/seller (daha doluysa üzerine yaz)
        if it.get("price"):
            p["price"] = it["price"]
        if it.get("seller"):
            p["seller"] = it["seller"]

        # --- rating alanları (primaryProductReviewRating) ---
        pr = it.get("primaryProductReviewRating") or {}
        avg = pr.get("averageRating") or pr.get("rating") or pr.get("ratingValue")
        rc  = pr.get("reviewCount")  or pr.get("ratingCount")

        # ratingHistograms formatını normalize et (list/dict gelebilir)
        # Browse dokümana göre array of RatingHistogram. Bizim normalize_histogram zaten iki türü de karşılıyor.
        hist = pr.get("ratingHistograms")

        if avg is not None:
            p["averageRating"] = avg
        if rc is not None:
            p["reviewCount"] = rc
        if hist:
            p["ratingHistogram"] = hist

        products[i] = p

    return products

# ---------- HTML ----------

STYLE = """
body{font-family:ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,Inter,Arial,sans-serif;background:#0b1220;color:#e5e7eb;margin:0;padding:24px}
.wrap{max-width:1200px;margin:0 auto}
h1{font-size:1.6rem;margin:0 0 8px}
.muted{color:#94a3b8}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:14px;margin-top:16px}
.card{background:#0f172a;border:1px solid #233047;border-radius:14px;padding:14px}
.badge{background:#1e293b;border:1px solid #334155;border-radius:8px;padding:2px 8px;margin-left:8px;font-size:.8rem;color:#cbd5e1}
.k{display:inline-block;background:#172133;border:1px solid #233047;border-radius:999px;padding:4px 10px;margin:4px 6px 0 0;font-size:.875rem;color:#e2e8f0;text-decoration:none}
.k:hover{border-color:#3b82f6}
.pimg{width:100%;height:150px;object-fit:cover;border-radius:10px;background:#0a101a}
.ptitle{margin-top:8px;font-size:.95rem;line-height:1.25}
.ptitle a{color:#cde4ff;text-decoration:none}
.meta{margin-top:6px;font-size:.85rem;color:#a7b2c3}
.kpi{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}
.kpi span{background:#111a2b;border:1px solid #283754;border-radius:6px;padding:2px 6px;font-size:.8rem}
.empty{opacity:.6}
.hbars{display:flex;gap:2px;align-items:center;margin-top:6px}
.hbar{height:6px;background:#2dd4bf}
.head{display:flex;align-items:center;justify-content:space-between}
"""

def render_histogram(hist: Dict[str,int]) -> str:
    if not has_hist_data(hist):
        return '<div class="meta empty">histogram: EMPTY</div>'
    total = sum(hist.values())
    parts = []
    for k in ["5","4","3","2","1"]:
        w = (hist[k]/total)*100 if total>0 else 0
        parts.append(f'<div class="hbar" title="{k}★: {hist[k]}" style="width:{w:.1f}%"></div>')
    return f'<div class="hbars">{"".join(parts)}</div>'

def render_html(kw: str, products: List[Dict], out_path: str, ebay_base: str):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    kept = len(products)

    # sayaçlar
    c_avg = c_rc = c_hist = c_seller = 0
    cards = []

    for p in products:
        avg = pick_first(p, "averageRating","rating","ratingValue")
        try: avg = float(avg) if avg is not None else None
        except Exception: avg = None

        rc  = pick_first(p, "reviewCount","ratingCount")
        try: rc = int(rc) if rc is not None else None
        except Exception: rc = None

        hist = normalize_histogram(p.get("ratingHistogram"))
        seller_pct = None
        seller = p.get("seller") or {}
        sp = seller.get("feedbackPercentage")
        try: seller_pct = float(sp) if sp is not None else None
        except Exception: seller_pct = None

        if avg is not None: c_avg += 1
        if rc  is not None: c_rc  += 1
        if has_hist_data(hist): c_hist += 1
        if seller_pct is not None: c_seller += 1

        title = (p.get("title") or "").strip()
        url   = p.get("url") or p.get("itemWebUrl") or ebay_search_url(kw, ebay_base)
        img   = p.get("image") or (p.get("image") or {}).get("imageUrl") or ""
        price_val = parse_price(p.get("price"))
        price_txt = f"${price_val:,.2f}" if isinstance(price_val,(int,float)) else html.escape(str(p.get("price","")))

        parts = ['<div class="card">']
        if img:
            parts.append(f'<a href="{html.escape(url)}" target="_blank" rel="noopener"><img class="pimg" src="{html.escape(img)}" alt=""></a>')
        parts.append(f'<div class="ptitle"><a href="{html.escape(url)}" target="_blank" rel="noopener">{html.escape(title) or "View item"}</a></div>')
        parts.append(f'<div class="meta">{price_txt or ""}</div>')

        kpis = []
        kpis.append(f'★ {avg:.1f}' if avg is not None else '<span class="empty">★ EMPTY</span>')
        kpis.append(f'{rc} reviews' if rc is not None else '<span class="empty">reviews EMPTY</span>')
        kpis.append(f'seller {seller_pct:.1f}%' if seller_pct is not None else '<span class="empty">seller% EMPTY</span>')
        parts.append('<div class="kpi">' + "".join([f"<span>{k}</span>" if "EMPTY" not in k else k for k in kpis]) + '</div>')

        parts.append(render_histogram(hist))
        parts.append('</div>')
        cards.append("".join(parts))

    head = f"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><title>Review Field Check — {html.escape(kw)}</title>
<meta name="viewport" content="width=device-width, initial-scale=1" />
<style>{STYLE}</style></head><body>
<div class="wrap">
  <div class="head">
    <h1>Review Field Check — {html.escape(kw)}</h1>
    <span class="badge">{kept} products</span>
  </div>
  <div class="muted">Generated {html.escape(ts)} • averageRating: {c_avg}/{kept} • reviewCount: {c_rc}/{kept} • histogram: {c_hist}/{kept} • seller%: {c_seller}/{kept}</div>
  <div style="margin-top:10px">
    <a class="k" href="{html.escape(ebay_search_url(kw, ebay_base))}" target="_blank" rel="noopener">Open on eBay</a>
  </div>
  <div class="grid">
"""

    tail = """
  </div>
</div>
</body></html>
"""

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(head + "\n".join(cards) + tail)

# ---------- CLI ----------

def main():
    ap = argparse.ArgumentParser(description="Belirli bir keyword için ilk N üründe review alanlarının HTML raporu.")
    ap.add_argument("--kw", default="halloween blow mold")
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--html", default="review_check.html", help="Çıktı HTML dosyası")
    ap.add_argument("--publish-to", help="HTML’i bu yola kopyala (örn. /var/www/html/review_check.html)")
    ap.add_argument("--ebay-base", default="https://www.ebay.com")
    ap.add_argument("--marketplace", default="EBAY_US")
    ap.add_argument("--detail-cap", type=int, default=10, help="Detay çağrısı yapılacak ürün sayısı (rate-limit için)")
    args = ap.parse_args()

    # 1) Temel liste
    products = get_top_products(args.kw, limit=args.limit) or []

    # 2) İlk N ürünü detay & product summary ile zenginleştir
    products = enrich_with_ebay_details(products,
                                        max_details=args.detail_cap,
                                        marketplace=args.marketplace,
                                        delay_sec=0.25)

    # 3) HTML
    render_html(args.kw, products, args.html, args.ebay_base)
    print(f"🖼️  HTML yazıldı: {args.html}")

    # 4) Publish
    if args.publish_to:
        os.makedirs(os.path.dirname(args.publish_to), exist_ok=True)
        shutil.copyfile(args.html, args.publish_to)
        print(f"📤 Published to: {args.publish_to}")

if __name__ == "__main__":
    main()
