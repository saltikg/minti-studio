#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os, json, subprocess, shlex, re, random, time
from datetime import datetime
from dotenv import load_dotenv
from openai import OpenAI
from datetime import date


load_dotenv("/home/ubuntu/blog-factory/.env")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=OPENAI_API_KEY)

# Lüks / Authentic odaklı seed markalar (genişletilebilir)
SEED_BRANDS = [
    "Gucci", "Prada", "Saint Laurent", "Burberry", "Balenciaga", "Lacoste",
    "Moncler", "Canada Goose", "Alexander McQueen", "Versace",
    "Ferragamo", "Bottega Veneta", "Fendi", "Dolce & Gabbana",
    "Celine", "Loewe", "Givenchy", "Hermès", "Chanel", "Dior"
]

CATEGORIES = [
    "fashion", "handbags", "jewelry", "watches"
]

def _prompt() -> str:
    seed_brands = ", ".join(SEED_BRANDS)
    seed_cats = ", ".join(CATEGORIES)
    today = datetime.utcnow().strftime("%Y-%m-%d")
    return f"""
You are an eBay luxury deals strategist. Today is {today}.
Pick ONE brand and a category among [{seed_cats}] suitable for **Authenticity Guarantee** when possible.
Prefer brands from this seed list if relevant: {seed_brands}.

Return ONLY valid JSON with:
- brand (string)
- category (one of: {seed_cats})
- filters: object with:
    - auth_guarantee (0 or 1)
    - discount_min (integer, typical 30–60)
    - color (optional, string like "Red" or "Black" — pick only if it helps)
    - condition ("new"|"used"|"any")
    - price_min (optional int), price_max (optional int)
    - buying_options (string, comma list e.g. "FIXED_PRICE,BEST_OFFER")
    - keyword (optional, extra term like "tshirt", "hoodie", "card holder")

Constraints:
- Favor categories likely to have AUTHENTICITY_GUARANTEE (watches, handbags, select fashion).
- Do NOT pick shoes today (avoid sneakers/boots/heels).
- If you pick fashion, aim for accessories/apparel like "tshirt" or "hoodie" over shoes.
- Make sure discount_min is not too high to zero out results.

Example good output:
{{
  "brand": "Lacoste",
  "category": "fashion",
  "filters": {{
    "auth_guarantee": 1,
    "discount_min": 40,
    "color": "Red",
    "condition": "new",
    "price_min": 50,
    "price_max": 400,
    "buying_options": "FIXED_PRICE,BEST_OFFER",
    "keyword": "tshirt"
  }}
}}
"""

def ask_llm():
    resp = client.chat.completions.create(
        model=os.getenv("OPENAI_MODEL_GPT", "gpt-4o-mini"),
        messages=[{"role": "user", "content": _prompt()}],
        temperature=0.5,
        max_tokens=600,
    )
    raw = resp.choices[0].message.content.strip()
    s, e = raw.find("{"), raw.rfind("}")
    data = json.loads(raw[s:e+1] if s != -1 and e != -1 else raw)
    return data

def _arg_or_none(key, val):
    if val is None or val == "" or (isinstance(val, int) and val < 0):
        return None
    return (key, str(val))

def run_ingest(decision: dict, relax=False):
    brand = decision.get("brand")
    category = decision.get("category")
    f = decision.get("filters", {}) or {}

    # shoes hariç tut → exclude kelimeleri
    exclude_words = "shoe,sneaker,boot,heel,loafer,trainer"

    # relax modda bazı kısıtları gevşet
    discount_min = int(f.get("discount_min") or 0)
    color = f.get("color")
    if relax:
        # 1) renk yoksa bırak; varsa kaldır
        color = None
        # 2) min indirimi düşür (örn. 40 → 30)
        discount_min = max(0, min(discount_min, 30))

    cmd_parts = [
        "/home/ubuntu/blog-factory/.venv/bin/python",
        "/home/ubuntu/blog-factory/src/ebay/2-ebay_products_ingest.py",
        "--deal-mode", "1",
        "--unique", "1",
        "--brand-aspect", "1",
        "--limit", "180",
        "--discount-band", "10",
        "--buying-options", f.get("buying_options") or "FIXED_PRICE,BEST_OFFER",
        "--exclude-words", exclude_words,
    ]

    # zorunlu/opsiyonel parametreleri ekle
    for pair in [
        _arg_or_none("--brand", brand),
        _arg_or_none("--category", category),
        _arg_or_none("--auth-guarantee", f.get("auth_guarantee")),
        _arg_or_none("--discount-min", discount_min),
        _arg_or_none("--color", color),
        _arg_or_none("--condition", f.get("condition") or "any"),
        _arg_or_none("--price-min", f.get("price_min")),
        _arg_or_none("--price-max", f.get("price_max")),
        _arg_or_none("--keyword", f.get("keyword")),
        # kategoriye özel slug istersen:
        _arg_or_none("--category-slug", "special-deals"),
    ]:
        if pair:
            cmd_parts.extend(pair)

    cmd = " ".join(shlex.quote(x) for x in cmd_parts)
    print("▶️ Running:", cmd)
    return subprocess.run(cmd, shell=True)

def main():
    try:
        decision = ask_llm()
        print("🤖 LLM Decision:", json.dumps(decision, ensure_ascii=False))
    except Exception as e:
        # LLM hata → seed fallback (örnek)
        print("LLM karar hatası, fallback kullanılıyor:", e)
        decision = {
            "brand": random.choice(SEED_BRANDS),
            "category": random.choice(CATEGORIES),
            "filters": {
                "auth_guarantee": 1,
                "discount_min": 35,
                "condition": "any",
                "buying_options": "FIXED_PRICE,BEST_OFFER",
                "keyword": "tshirt"
            }
        }

    # İlk deneme
    r = run_ingest(decision, relax=False)
    # Eğer ingest hiçbir item bulamazsa (script kendi çıktısında yazıyor), 
    # genelde exit code 0 döner ama logda "No items matched..." görürsün.
    # İkinci bir gevşetilmiş deneme de yapalım:
    time.sleep(3)
    r2 = run_ingest(decision, relax=True)

if __name__ == "__main__":
    main()
