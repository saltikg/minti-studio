 
import re, json, os, sys, random, math, datetime as dt
from typing import Any, Dict, List, Optional
import requests, duckdb, openai
from ebay_auth import get_token   # EBAY_CLIENT_ID & EBAY_CLIENT_SECRET ile çalışıyor
import json
from dotenv import load_dotenv
load_dotenv("/home/ubuntu/blog-factory/.env")
import time
import pandas as pd
from datetime import datetime
import argparse
 
import numpy as np, decimal
from datetime import date, datetime

DEBUG_LLM = bool(int(os.getenv("DEBUG_LLM", "1")))  # 1=logla, 0=loglama
LOG_DIR = "/home/ubuntu/blog-factory/logs"
os.makedirs(LOG_DIR, exist_ok=True)



# üst tarafa bir brand→uyumlu kategoriler haritası ekleyelim
BRAND_CATEGORY_MAP = {
    "rolex": ["watches"],
    "citizen": ["watches"],
    "seiko": ["watches"],
    "alexander mcqueen": ["fashion","handbags"],
    "versace": ["fashion","handbags"],
    "john hardy": ["jewelry"]
}

CATEGORY_POOL = ["watches","handbags","jewelry","fashion"]

# === sd_* tabanlı dinamik taksonomi ===
def load_sd_taxonomy(con):
    # sd_categories -> CATEGORY_POOL
    cats = [r[0] for r in con.execute("SELECT slug FROM sd_categories ORDER BY slug").fetchall()]
    # sd_brand_categories -> {brand: [cats]}
    rows = con.execute("SELECT brand_slug, category_slug FROM sd_brand_categories").fetchall()
    brand_to_cats = {}
    for b, c in rows:
        brand_to_cats.setdefault(b.lower(), []).append(c.lower())
    # sd_brands -> primary_category_slug
    prim_rows = con.execute("SELECT slug, primary_category_slug FROM sd_brands").fetchall()
    primary_map = {b.lower(): c.lower() for b, c in prim_rows}
    return cats, brand_to_cats, primary_map


import json, re

# --- Parsers: Markdown **veya** JSON list kabul eder ---
import json, re

_num_bold = re.compile(r"^\s*(?:\d+[\)\.\-]?\s*)?(?:\*\*)?(?P<t>.+?)(?:\*\*)?\s*$")

def _strip_num_bold(s: str) -> str:
    m = _num_bold.match(s.strip())
    return (m.group("t") if m else s).strip()

def _split_title_desc(line: str):
    m = re.match(r"^\s*(?P<t>[^:–\-]+)\s*[:–\-]\s*(?P<d>.+)\s*$", line.strip())
    if m:
        return m.group("t").strip(), m.group("d").strip()
    return None, None

def buyers_guide_to_json(val) -> list[dict]:
    if not val:
        return []
    if isinstance(val, list):
        items = val
    elif isinstance(val, str) and val.strip().startswith("["):
        try:
            items = json.loads(val)
        except Exception:
            items = [s for s in val.splitlines() if s.strip()]
    else:
        items = [s for s in val.splitlines() if s.strip()]

    out = []
    for it in items:
        if isinstance(it, dict) and ("title" in it or "desc" in it):
            t = _strip_num_bold(str(it.get("title", "")))
            d = str(it.get("desc", "")).strip()
            if t or d:
                out.append({"title": t or "Overview", "desc": d})
            continue
        line = str(it).strip()
        if not line:
            continue
        t, d = _split_title_desc(line)
        if not t:
            t = _strip_num_bold(line)
            d = ""
        out.append({"title": t, "desc": d})

    if not out:
        return [{"title": "Overview", "desc": re.sub(r"\s+", " ", str(val)).strip()}]
    return out

def faq_to_json(val) -> list[dict]:
    if not val:
        return []
    if isinstance(val, list):
        seq = val
    elif isinstance(val, str) and val.strip().startswith("["):
        try:
            seq = json.loads(val)
        except Exception:
            seq = [val]
    else:
        blocks = [b.strip() for b in re.split(r"\n\s*\n", str(val)) if b.strip()]
        seq = blocks

    if seq and isinstance(seq[0], dict) and ("q" in seq[0] or "a" in seq[0]):
        out = []
        for d in seq:
            q = _strip_num_bold(str(d.get("q", "")))
            a = str(d.get("a", "")).strip()
            if q or a:
                out.append({"q": q, "a": a})
        return out

    out = []
    buf_q = None
    qpat = re.compile(r"^\s*\*\*(?:\d+\.\s*)?(?P<q>.+?)\*\*\s*$")
    for piece in seq:
        s = str(piece).strip()
        m = qpat.match(s)
        if m:
            if buf_q:
                out.append({"q": buf_q, "a": ""})
            buf_q = _strip_num_bold(m.group("q"))
        else:
            if buf_q is None:
                buf_q = "FAQ"
            out.append({"q": buf_q, "a": s})
            buf_q = None
    if buf_q:
        out.append({"q": buf_q, "a": ""})

    if not out:
        return [{"q": "FAQ", "a": str(val).strip()}]
    return out


def compute_constraints(decision_ctx, threshold=2, con=None):
    """
    - Son 2 günde aşırı temsil edilen kategorileri yasakla (>= threshold)
    - En az içerik üretilen kategorileri "preferred" sıraya koy
    - Eğer con verilmişse sd_categories’ten dinamik CATEGORY_POOL kullan
    """
    recent_cats = decision_ctx.get("recent_categories") or {}
    recent_brands = [b.lower() for b in (decision_ctx.get("recent_brands") or [])]

    # Dinamik kategori havuzu
    if con is not None:
        pool = [r[0] for r in con.execute("SELECT slug FROM sd_categories ORDER BY slug").fetchall()]
        if not pool:
            pool = ["watches","handbags","jewelry","fashion"]
    else:
        pool = ["watches","handbags","jewelry","fashion"]

    # Overrepresented
    disallowed_categories = {c for c, n in recent_cats.items() if n >= threshold}

    # En az temsil edileni öne alan sıralama
    ranked = sorted(pool, key=lambda c: recent_cats.get(c, 0))
    preferred_categories = [c for c in ranked if c not in disallowed_categories]
    preferred_category = preferred_categories[0] if preferred_categories else None

    return {
        "disallowed_categories": sorted(list(disallowed_categories)),
        "disallowed_brands": sorted(list(set(recent_brands))),
        "preferred_category": preferred_category,
        "preferred_categories": preferred_categories
    }


try:
    import pandas as pd
    _HAS_PD = True
except:
    _HAS_PD = False

import numpy as np, decimal
from datetime import date, datetime

DEBUG_LLM = bool(int(os.getenv("DEBUG_LLM", "1")))  # 1=logla, 0=loglama
LOG_DIR = "/home/ubuntu/blog-factory/logs"
os.makedirs(LOG_DIR, exist_ok=True)

def _json_default(o):
    try:
        import pandas as pd
        if isinstance(o, (pd.Timestamp, pd.Timedelta, type(pd.NaT))):
            try:
                return None if pd.isna(o) else o.isoformat()
            except Exception:
                return str(o)
    except Exception:
        pass
    if isinstance(o, (datetime, date)): return o.isoformat()
    if isinstance(o, np.integer): return int(o)
    if isinstance(o, np.floating): return float(o)
    if isinstance(o, np.bool_): return bool(o)
    if isinstance(o, decimal.Decimal): return float(o)
    return str(o)

def _dump_json(obj, path):
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False, indent=2, default=_json_default))
    except Exception as e:
        print(f"⚠️ dump_json failed for {path}: {e}")

def _now_id():
    return dt.datetime.utcnow().strftime("%Y%m%d-%H%M%S")


# ---------------- ENV ----------------
DB_PATH = os.getenv("DB_PATH", "/home/ubuntu/blog-factory/warehouse/blog_factory.duckdb")
EBAY_BASE = "https://api.ebay.com"
MARKETPLACE = os.getenv("EBAY_MARKETPLACE", "EBAY_US")
OAUTH = os.getenv("EBAY_OAUTH_TOKEN")

WEB_ROOT = "/var/www/html"
BASE_URL = "https://mintistudio.com"

OPENAI_KEY = os.getenv("OPENAI_API_KEY")

OPENAI_MODEL = "gpt-4o-mini"
CTX_COUNTRY = os.getenv("EBAY_CONTEXT_COUNTRY", "US")
CTX_ZIP = os.getenv("EBAY_CONTEXT_ZIP", "94301")
from openai import OpenAI
VERBOSE_ENRICH_LOG = True

openai.api_key = OPENAI_KEY
client = OpenAI(api_key=OPENAI_KEY)
RATE_LIMIT_SLEEP = 0.15  # gerekirse düşür/çıkar

# ---------------- Helpers ----------------
 

def write_robots_txt(base_url: str = BASE_URL, web_root: str = WEB_ROOT):
    """robots.txt dosyasını yaz."""
    content = f"""User-agent: *
Allow: /

Sitemap: {base_url.rstrip('/')}/sitemap.xml
"""
    out_path = os.path.join(web_root, "robots.txt")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"✅ robots.txt → {out_path}")



def headers() -> Dict[str, str]:
    h = {
        "Authorization": f"Bearer {get_token()}",
        "X-EBAY-C-MARKETPLACE-ID": MARKETPLACE,
        "Accept": "application/json",
    }
    parts = []
    if CTX_COUNTRY or CTX_ZIP:
        parts.append(f"contextualLocation=country={CTX_COUNTRY},zip={CTX_ZIP}")
    if parts:
        h["X-EBAY-C-ENDUSERCTX"] = ";".join(parts)
    return h


# ---------------- Helpers ----------------


# ---- DEAL MODE: category map ----
CATEGORY_MAP = {
    "watches": "31387",
    "cell_phones": "9355",
    "jewelry": "281",
    "handbags": "169291",
    "fashion": "11450",
}

def _build_browse_params_for_deal(args) -> dict:
    params = {
        "limit": min(int(getattr(args, "limit", 200) or 200), 200),
        "offset": 0,
    }
    q_parts = []
    if getattr(args, "brand", None):
        q_parts.append(args.brand)
    if getattr(args, "keyword", None):
        q_parts.append(args.keyword)
    if q_parts:
        params["q"] = " ".join(q_parts)

    cat_key = getattr(args, "category", None)
    if cat_key and CATEGORY_MAP.get(cat_key):
        params["category_ids"] = CATEGORY_MAP[cat_key]

    filt = ["priceCurrency:USD"]
    cond = getattr(args, "condition", "any")
    if cond == "new":
        filt.append("conditionIds:{1000}")
    elif cond == "used":
        filt.append("conditionIds:{3000|4000|5000|6000}")

    minv = getattr(args, "price_min", None)
    maxv = getattr(args, "price_max", None)
    if minv is not None and maxv is not None:
        filt.append(f"price:[{int(minv)}..{int(maxv)}]")
    elif minv is not None:
        filt.append(f"price:[{int(minv)}..]")
    elif maxv is not None:
        filt.append(f"price:[..{int(maxv)}]")

    if getattr(args, "buying_options", None):
        opts = ",".join([o.strip() for o in args.buying_options.split(",") if o.strip()])
        if opts:
            filt.append(f"buyingOptions:{{{opts}}}")

    if int(getattr(args, "auth_guarantee", 0) or 0) == 1:
        filt.append("qualifiedPrograms:{AUTHENTICITY_GUARANTEE}")

    if getattr(args, "refurbished", "none") == "any":
        filt.append("conditionIds:{2000|2010|2020|2030}")

    # ✅ aspect_filter birleştirme
    aspects = []
    if getattr(args, "brand", None) and int(getattr(args, "brand_aspect", 0) or 0) == 1:
        aspects.append(f"Brand:{{{args.brand}}}")
    if getattr(args, "color", None):
        # Color adını Title Case yapalım (eBay çoğunlukla böyle tutuyor)
        color_val = str(args.color).strip().title()
        aspects.append(f"Color:{{{color_val}}}")

    if aspects:
        params["aspect_filter"] = ",".join(aspects)

    if filt:
        params["filter"] = ",".join(filt)
    return params




def _calc_discount_pct(item: dict) -> float:
    mp = item.get("marketingPrice") or {}
    raw = mp.get("discountPercentage")
    if not raw:
        return 0.0
    try:
        return float(str(raw).replace("%", "").strip())
    except Exception:
        return 0.0


def _browse_with_auth_fallback(params, require_auth_flag=False):
    path = "/buy/browse/v1/item_summary/search"
    # 1) normal dene
    try:
        data = http_get(path, params)
        # 🔹 server 200 ama boş set ise: qualifiedPrograms'ı sök, in-memory filtrele
        if (not data.get("itemSummaries")) and "qualifiedPrograms:" in (params.get("filter","")):
            filt = params.get("filter","")
            p3 = dict(params)
            p3["filter"] = re.sub(r'(,)?\s*qualifiedPrograms:\{[^}]+\}\s*(,)?',
                                  lambda m: ',' if (m.group(1) and m.group(2)) else '',
                                  filt).strip(', ')
            data2 = http_get(path, p3)
            return data2, True   # ⬅︎ in-memory AUTH kontrolü
        return data, False
    except requests.HTTPError as e:
        if e.response is None or e.response.status_code != 400:
            raise
    # 2) GUARANTEE → VERIFICATION
    filt = params.get("filter", "")
    if "qualifiedPrograms:{AUTHENTICITY_GUARANTEE}" in filt:
        p2 = dict(params)
        p2["filter"] = filt.replace("qualifiedPrograms:{AUTHENTICITY_GUARANTEE}",
                                    "qualifiedPrograms:{AUTHENTICITY_VERIFICATION}")
        try:
            data = http_get(path, p2)
            if (not data.get("itemSummaries")):
                # yine boşsa komple sök
                raise requests.HTTPError("empty after VERIFICATION")
            return data, False
        except:
            pass
    # 3) komple çıkar
    p3 = dict(params)
    p3["filter"] = re.sub(r'(,)?\s*qualifiedPrograms:\{[^}]+\}\s*(,)?',
                          lambda m: ',' if (m.group(1) and m.group(2)) else '',
                          filt).strip(', ')
    data = http_get(path, p3)
    return data, True



def _item_has_auth(item: dict) -> bool:
    q = item.get("qualifiedPrograms") or []
    if isinstance(q, str):
        q = [s.strip() for s in q.split(",")]
    q = [str(x).upper() for x in q]
    if any(s.startswith("AUTHENTICITY_") for s in q):
        return True
    return False

def _item_has_auth_deep(item: dict) -> bool:
    # önce summary
    if _item_has_auth(item):
        return True
    # sonra detail
    iid = item.get("itemId")
    if not iid:
        return False
    try:
        it = http_get(f"/buy/browse/v1/item/{iid}", {})
    except:
        return False
    # detail’de de aynı kontrol + geniş arama
    if _item_has_auth(it):
        return True
    blob = json.dumps(it).upper()
    return ("AUTHENTICITY_GUARANTEE" in blob) or ("AUTHENTICITY_VERIFICATION" in blob)



def update_idea_product_deal_fields(con, idea_id: str, product_id: str, item: dict):
    """idea_products üzerinde indirim/fiyat/aktivite alanlarını günceller."""
    mp = item.get("marketingPrice") or {}
    pct = _calc_discount_pct(item)
    orig = (mp.get("originalPrice") or {}).get("value")
    sale = (item.get("price") or {}).get("value")

    con.execute("""
        UPDATE idea_products
        SET discount_pct   = ?,
            original_price = ?,
            sale_price     = ?,
            is_active      = TRUE,
            last_checked_at = now()
        WHERE idea_id = ? AND parent_asin = ?
    """, [pct, float(orig) if orig else None, float(sale) if sale else None, idea_id, product_id])


def enrich_deal_status(con, idea_id: str, item_ids: list):
    if not item_ids:
        return
    now_utc = dt.datetime.now(dt.timezone.utc)

    for iid in item_ids:
        try:
            it = http_get(f"/buy/browse/v1/item/{iid}", {})
        except Exception:
            continue

        # item end date
        end_raw = it.get("itemEndDate")
        try:
            end_dt = dt.datetime.fromisoformat(end_raw.replace("Z", "+00:00")) if end_raw else None
        except Exception:
            end_dt = None

        # availability
        avs = (it.get("estimatedAvailabilities") or [])
        avail = None
        if avs:
            avail = avs[0].get("estimatedAvailabilityStatus")

        is_active = True
        if end_dt and end_dt < now_utc:
            is_active = False
        if (avail or "").upper() == "OUT_OF_STOCK":
            is_active = False

        con.execute("""
            UPDATE idea_products
            SET item_end_date = ?,
                availability_status = ?,
                is_active = ?,
                last_checked_at = now()
            WHERE idea_id = ? AND parent_asin = ?
        """, [end_dt, avail, is_active, idea_id, iid])

        time.sleep(RATE_LIMIT_SLEEP)


def run_deal_flow(args):
    """
    1) İlk tarama: LLM/manuel paramlarla eBay araması
    2) Hiç ürün yoksa kategori-agnostik "relax & retry" merdiveni uygula:
       - stage 1: AUTH kaldır
       - stage 2: condition filtresini kaldır (any)
       - stage 3: price band'ı kaldır
       - stage 4: aspect_filter'ı (Brand/Color) kaldır
       * her aşamada ürün bulunursa durur ve o paramlarla devam eder
    3) Bulunan ürünleri DB'ye yaz, enrich et, blog üret, publish et
    """

    def _relax_params_for_retry(params: dict, stage: int) -> dict:
        """Filtreleri kademeli gevşetir (kategoriye özel değil, genel kural)."""
        p = dict(params)
        filt = p.get("filter", "") or ""

        # 1) AUTH -> kaldır
        if stage >= 1 and "qualifiedPrograms:" in filt:
            filt = re.sub(r'(,)?\s*qualifiedPrograms:\{[^}]+\}', '', filt)

        # 2) condition -> any (yani conditionIds bloklarını sök)
        if stage >= 2 and "conditionIds:" in filt:
            filt = re.sub(r'(,)?\s*conditionIds:\{[^}]+\}', '', filt)

        # 3) price band -> kaldır (price:[..])
        if stage >= 3 and "price:[" in filt:
            filt = re.sub(r'(,)?\s*price:\[[^\]]+\]', '', filt)

        p["filter"] = filt.strip(", ").strip()

        # 4) aspect_filter (Brand/Color) -> kaldır
        if stage >= 4 and "aspect_filter" in p:
            p.pop("aspect_filter", None)

        return p

    con = db_connect()
    try:
        # 1) Kategori (menü): varsayılan special-deals
        deal_category_slug = getattr(args, "category_slug", None) or "special-deals"
        ensure_category_exists(con, deal_category_slug, "Special Deals")

        # 2) LLM anahtar cümle (başlığa istatistik yazarken kullandığımız küçük string)
        parts = []
        if getattr(args, "target_discount", None):
            parts.append(f"Up to {int(args.target_discount)}% Off")
        if getattr(args, "brand", None):
            parts.append(args.brand)
        if getattr(args, "category", None):
            parts.append(args.category.replace("_", " ").title())
        llm_keyword = " ".join(parts).strip() or "Special Deals"

        # 3) idea ve rule kaydı (benzersiz idea_id)
        base_id = slugify(f"deal-{llm_keyword}-{deal_category_slug}")
        suffixes = []
        if int(getattr(args, "unique", 0) or 0) == 1:
            suffixes.append(dt.datetime.utcnow().strftime("%Y%m%d-%H%M"))
        if getattr(args, "post_tag", None):
            suffixes.append(slugify(args.post_tag))
        idea_id = f"{base_id}-{'-'.join(suffixes)}" if suffixes else base_id

        create_idea(con, idea_id, llm_keyword, deal_category_slug)

        rules = {
            "brand": getattr(args, "brand", None),
            "category_key": getattr(args, "category", None),
            "target_discount": getattr(args, "target_discount", None),
            "discount_band": getattr(args, "discount_band", 10),
            "price_min": getattr(args, "price_min", None),
            "price_max": getattr(args, "price_max", None),
            "buyingOptions": getattr(args, "buying_options", None),
            "auth_guarantee": int(getattr(args, "auth_guarantee", 0) or 0),
            "refurbished": getattr(args, "refurbished", "none"),
            "limit": getattr(args, "limit", 200),
            "exclude_words": getattr(args, "exclude_words", ""),
            "include_words": getattr(args, "include_words", "")
        }
        con.execute("""
            INSERT INTO idea_rules_deal (idea_id, rules_json, created_at, updated_at)
            VALUES (?, ?, now(), now())
            ON CONFLICT (idea_id) DO UPDATE SET rules_json = EXCLUDED.rules_json, updated_at = now()
        """, [idea_id, json.dumps(rules)])

        # ---------------- 4) eBay Browse çağrısı + ilk tarama ----------------
        params = _build_browse_params_for_deal(args)
        print("DEAL PARAMS:", json.dumps(params, ensure_ascii=False))

        kept_items, titles_for_llm, item_ids_enrich = [], [], []
        page_limit = int(getattr(args, "limit", 200) or 200)
        max_pages = max(1, math.ceil(page_limit / 200))

        # hedef indirim/delta
        tgt  = float(getattr(args, "target_discount", 0) or 0.0)
        band = float(getattr(args, "discount_band", 10) or 10.0)
        low, high = (max(0.0, tgt - band), (tgt + band) if tgt else 100.0)
        min_disc = float(getattr(args, "discount_min", 0) or 0.0)

        dbg_total = dbg_disc_fail = dbg_auth_fail = 0

        def _scan_with_params(p: dict, relax_stage: int = 0):
            """Verilen parametrelerle sayfa sayfa tarayıp eşleşenleri toplar."""
            nonlocal dbg_total, dbg_disc_fail, dbg_auth_fail
            local_kept, local_titles, local_ids = [], [], []
            offset = 0

            # relax aşaması için include/exclude'ı da yumuşat (stage>=2'de include kapat)
            excl = [w.strip().lower() for w in (getattr(args, "exclude_words", "") or "").split(",") if w.strip()]
            incl = [w.strip().lower() for w in (getattr(args, "include_words", "") or "").split(",") if w.strip()]
            if relax_stage >= 2:
                incl = []  # include_words aşırı daraltıyorsa relax aşamalarında kaldır

            for _ in range(max_pages):
                p["offset"] = offset
                data, must_check_auth = _browse_with_auth_fallback(p)
                items = data.get("itemSummaries") or []
                if not items:
                    break

                for it in items:
                    title_lc = (it.get("title") or "").lower()

                    # ❌ exclude words
                    if excl and any(x in title_lc for x in excl):
                        continue
                    # ✅ include words
                    if incl and not all(x in title_lc for x in incl):
                        continue

                    pct = _calc_discount_pct(it)
                    dbg_total += 1

                    # İndirim mantığı: min_disc varsa ona bak; yoksa target band
                    if min_disc > 0:
                        if pct <= 0 or pct < min_disc:
                            dbg_disc_fail += 1
                            continue
                    elif tgt and (pct <= 0 or pct < low or pct > high):
                        dbg_disc_fail += 1
                        continue

                    # AUTH kontrolü (ilk çağrıdaki fallback "must_check_auth" ise deep kontrol)
                    if int(getattr(args, "auth_guarantee", 0) or 0) == 1 and relax_stage < 1:
                        ok = _item_has_auth(it) if not must_check_auth else _item_has_auth_deep(it)
                        if not ok:
                            dbg_auth_fail += 1
                            continue

                    pid, title = save_product(con, it)
                    save_media(con, pid, it)
                    trust = seller_trust(it)
                    save_metrics(con, pid, trust)
                    save_idea_product_link(con, idea_id, pid)
                    update_idea_product_deal_fields(con, idea_id, pid, it)

                    local_kept.append(it)
                    local_titles.append(title)
                    local_ids.append(it.get("itemId"))

                    if len(local_kept) >= page_limit:
                        break

                if len(local_kept) >= page_limit:
                    break
                offset += 200

            return local_kept, local_titles, local_ids

        # İlk deneme
        k, t, ids = _scan_with_params(params, relax_stage=0)
        kept_items += k; titles_for_llm += t; item_ids_enrich += ids

        # ---------------- 4.b) Relax & Retry (genel kural) ----------------
        if not kept_items:
            print("❌ No items matched initial filters — starting relax & retry...")
            for stage in range(1, 5):
                rp = _relax_params_for_retry(params, stage)
                print(f"♻️ Retry stage {stage} with params:", json.dumps(rp, ensure_ascii=False))
                k, t, ids = _scan_with_params(rp, relax_stage=stage)
                if k:
                    kept_items += k; titles_for_llm += t; item_ids_enrich += ids
                    params = rp  # bulunduysa bu param seti ile devam
                    print(f"✅ Found items after relax stage {stage}")
                    break

        if not kept_items:
            print("❌ Still no items after all relax stages")
            con.close()
            return

        print(f"DEAL DEBUG: total={dbg_total} disc_fail={dbg_disc_fail} auth_fail={dbg_auth_fail}")

        # ---------------- 5) Enrich (expire/stock + shipping/returns) ----------------
        enrich_n = int(getattr(args, "enrich_limit", 0) or 0)
        ids_for_enrich = [iid for iid in item_ids_enrich if iid]
        if enrich_n > 0:
            ids_for_enrich = ids_for_enrich[:enrich_n]

        enrich_deal_status(con, idea_id, ids_for_enrich)
        enrich_shipping_and_returns(con, ids_for_enrich)

        # ---------------- 6) Vitrin listesi (dup engelle + indirim DESC) --------------
        def _norm_title(s):
            return re.sub(r'[^a-z0-9]+', '', (s or '').lower())

        seen_keys = set()
        display_items = []
        for it in sorted(kept_items, key=lambda x: _calc_discount_pct(x), reverse=True):
            try:
                price_val = int(float((it.get("price") or {}).get("value") or 0))
            except Exception:
                price_val = 0
            key = (it.get("itemId"), _norm_title(it.get("title")), price_val)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            display_items.append(it)
            if len(display_items) >= 15:
                break

        # İstatistikler (sadece display_items üstünden)
        disc_vals = []
        sale_vals = []
        for it in display_items:
            d = _calc_discount_pct(it)
            if d and d > 0:
                disc_vals.append(d)
            try:
                sale_vals.append(float((it.get("price") or {}).get("value")))
            except Exception:
                pass

        max_disc = int(max(disc_vals)) if disc_vals else 0
        avg_disc = round(sum(disc_vals)/len(disc_vals), 1) if disc_vals else 0
        pr_min  = (min(sale_vals) if sale_vals else None)
        pr_max  = (max(sale_vals) if sale_vals else None)

        # Hero görseli
        raw_hero = (display_items[0].get("image") or {}).get("imageUrl") if display_items else None
        hero_image = normalize_image_url(raw_hero) if raw_hero else None

        # LLM için istatistikli anahtar
        llm_keyword_stats = f"""{llm_keyword}
        [DEAL_STATS]
        brand={getattr(args,'brand',None)}
        category={getattr(args,'category',None)}
        total={len(display_items)}
        max_discount={max_disc}
        avg_discount={avg_disc}
        price_min={pr_min}
        price_max={pr_max}
        [/DEAL_STATS]"""

        # 7) LLM içerik
        titles_for_llm = [(it.get("title") or "") for it in display_items]
        blog = generate_blog_content(llm_keyword_stats, titles_for_llm)
        blog["hero_alt"] = blog["title"]

        # 8) Kaydet + publish + related
        idea_saved, slug_out, cat_slug = save_blog(
            con, idea_id, blog, category=deal_category_slug, hero_image=hero_image
        )
        assign_author_and_publish(idea_saved, deal_category_slug)
        con2 = db_connect()
        ensure_related_links(con2, idea_saved, deal_category_slug)
        con2.close()

    finally:
        con.close()

    # 9) iç link + sitemap
    try:
        if not getattr(args, "skip_internal", False):
            print(f"🔄 Enriching internal links for {idea_saved} ...")
            os.system(f'/home/ubuntu/blog-factory/.venv/bin/python /home/ubuntu/blog-factory/src/ebay/blog_internal_links.py "{idea_saved}" >> /home/ubuntu/blog-factory/logs/internal_links.log 2>&1')
            print(f"✅ Internal links enriched for {idea_saved}")
        else:
            print("⏭️  Internal link enrichment skipped (flag)")
    except Exception as e:
        print(f"⚠️ Internal link enrichment skipped: {e}")

    print(f"📝 DEAL Blog created: {blog['title']} (category={deal_category_slug}, idea={idea_saved})")


############ ----- DEALS ------- #########

def build_candidate_brands_seed_only(con, top_n=12):
    rows = con.execute("SELECT brand FROM sd_seed_brands ORDER BY brand LIMIT ?", [top_n]).fetchall()
    seeds = [r[0] for r in rows]
    return {
        "recently_published_14d": [],   # cold-start: boş
        "quality_window_30d": [],       # cold-start: boş
        "candidates": seeds             # sadece seed markalar
    }

def build_season_context_neutral():
    return {
        "season_name": None,
        "season_group": "seasonal",
        "palette": ["neutral","black","silver"],
        "themes": ["everyday value","authentic pieces"],
        "shipping_urgency": "Order soon for on-time delivery."
    }

def build_decision_context_neutral():
    return {
        "recent_categories": {},
        "recent_brands": [],
        "allow_new_category": True,
        "allow_new_brand": True
    }



def build_season_context(con):
    # seasons tablosu varsa en yeni season’ı çek; yoksa ay bazlı fallback
    row = con.execute("""
        SELECT season_name, COALESCE(season_group, 'seasonal') AS season_group
        FROM seasons
        ORDER BY id DESC
        LIMIT 1
    """).fetchone()
    if row:
        season_name, season_group = row[0], row[1]
    else:
        season_name, season_group = None, 'seasonal'

    # çok basit palet/tema; istersen genişletiriz
    palette = ["burnt orange","deep red","warm neutrals"] if season_name and "thanksgiving" in season_name else ["classic black","navy","silver"]
    themes  = ["gift ideas","cozy style","last-minute shipping"] if season_group=='seasonal' else ["everyday wear","classic style","authentic pieces"]
    urgency = "Order soon for on-time delivery."  # sabit bir uyarı; kargodan çekebiliriz

    return {
        "season_name": season_name,
        "season_group": season_group,
        "palette": palette,
        "themes": themes,
        "shipping_urgency": urgency
    }

def build_decision_context(con, days_back=2):
    # Kategori dağılımı (sadece deals yayınları)
    cat_rows = con.execute(f"""
        SELECT sd_category_key, COUNT(*) AS n
        FROM sd_publications
        WHERE date_published >= current_date - INTERVAL {int(days_back)} DAY
        GROUP BY 1
        ORDER BY n DESC
    """).fetchall()
    cats = {r[0]: r[1] for r in cat_rows} if cat_rows else {}

    # Marka listesi (son 2 gün)
    brand_rows = con.execute(f"""
        SELECT brand, MAX(date_published) AS last_pub
        FROM sd_publications
        WHERE date_published >= current_date - INTERVAL {int(days_back)} DAY
        GROUP BY 1
        ORDER BY last_pub DESC
    """).fetchall()
    brands = [r[0] for r in brand_rows if r and r[0]]

    if cats:
        max_cat = max(cats.values())
        min_cat = min(cats.values())
        allow_new_category = (max_cat - min_cat) <= 1
    else:
        allow_new_category = True  # hiç yayın yoksa yeni kategori önerilebilir

    allow_new_brand = (len(set(brands)) == len(brands))  # hepsi farklıysa yeni marka denenebilir

    return {
        "recent_categories": cats,
        "recent_brands": brands,
        "allow_new_category": allow_new_category,
        "allow_new_brand": allow_new_brand
    }

def llm_decide_deal(candidate_info: dict,
                    season_ctx: dict,
                    decision_ctx: dict,
                    constraints: dict | None = None) -> dict:
    """
    constraints (optional) = {
      "disallowed_categories": [...],
      "disallowed_brands": [...],
      "preferred_category": "handbags" | None,
      "preferred_categories": [...]
    }
    """
    constraints = constraints or {
        "disallowed_categories": [],
        "disallowed_brands": [],
        "preferred_category": None,
        "preferred_categories": []
    }

    # ---- llm_decide_deal (özet değişiklikler) ----
    schema_txt = {
        "sd_category_key":"watches|handbags|fashion|jewelry|new",
        "brand":"string",   # zorunlu
        "filters":{
            "auth_guarantee":"0|1",
            "target_discount":"int?",
            "discount_min":"int?",
            "price_min":"int?",
            "price_max":"int?",
            "condition":"new|used|any",
            "color":"string|null",
            "exclude_words":"string",
            "include_words":"string"
        },
        "buying_options":"string",
        "post_tag":"string",
        "title":"string?"
        }

        # prompt kurallarına açıkça ekleyin:
        # - First choose sd_category_key; THEN (optionally) select brand compatible with that category.
        # - If no strong brand candidate, set "brand" to null and keep category-only search.


    def _json_default(o):
        try:
            import pandas as pd
            if isinstance(o, (pd.Timestamp, )):
                return o.isoformat()
        except Exception:
            pass
        import datetime as _dt
        if isinstance(o, (_dt.date, _dt.datetime)):
            return o.isoformat()
        return str(o)
    cats_pool, brand_cat_map, primary_map = load_sd_taxonomy(duckdb.connect(DB_PATH))

    prompt = (
        "You are an editorial planner for eBay deals.\n\n"
        "Input blocks:\n"
        "[candidate_brands]\n"
        f"{json.dumps(candidate_info, ensure_ascii=False, default=_json_default)}\n\n"
        "[season_context]\n"
        f"{json.dumps(season_ctx, ensure_ascii=False, default=_json_default)}\n\n"
        "[recent_publication_stats]\n"
        f"{json.dumps(decision_ctx, ensure_ascii=False, default=_json_default)}\n\n"
        "[catalog]\n"
        f"{json.dumps({'categories': cats_pool, 'brand_category_map': brand_cat_map}, ensure_ascii=False, default=_json_default)}\n\n"
        "Decision rules:\n"
        "- Balance categories: if one category dominates in the last 2 days, prefer a different one.\n"
        "- Do NOT propose new categories. Pick only from [catalog.categories].\n"
        "- After choosing sd_category_key, pick a brand that is COMPATIBLE with that category based on [catalog.brand_category_map].\n"
        "- Brand must NOT be empty.\n"
        "- If multiple categories are equally balanced, choose RANDOMLY among the existing categories from [catalog.categories] (excluding disallowed).\n"
        "- If multiple compatible brands exist for the chosen category, you MAY pick any (prefer ones not recently published), but it must exist in the candidates list or in [catalog.brand_category_map].\n"
        "- Prefer AUTHENTICITY for watches/handbags; for fashion/jewelry, AUTHENTICITY may be 0.\n"
        "- Avoid shoes/sneakers/boots in filters.\n"
        "- Target discounts: 30–60 when feasible; otherwise discount_min 20–30.\n"
        "- Color: If the category is 'fashion' (or 'handbags') and season palette exists, set filters.color to one of the season palette (e.g. 'red','black','navy','silver'). Otherwise leave color null.\n\n"
        "HARD CONSTRAINTS:\n"
        f"- You MUST NOT pick any brand in this disallowed list: {json.dumps(constraints.get('disallowed_brands', []), ensure_ascii=False)}\n"
        f"- You MUST NOT set sd_category_key to any of: {json.dumps(constraints.get('disallowed_categories', []), ensure_ascii=False)}\n"
        f"- Prefer (in order) these categories: {json.dumps(constraints.get('preferred_categories', []), ensure_ascii=False)}\n\n"
        "Return STRICT JSON only for this schema:\n"
        f"{json.dumps(schema_txt, ensure_ascii=False)}\n"
    )

    if DEBUG_LLM:
        print("\n========== LLM PROMPT ==========\n", prompt, "\n========== END PROMPT ==========\n")
        _dump_json({"prompt": prompt}, os.path.join(LOG_DIR, f"llm_prompt_{_now_id()}.json"))

    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
        max_tokens=500,
    )
    raw = (resp.choices[0].message.content or "").strip()

    try:
        start, end = raw.find("{"), raw.rfind("}")
        data = json.loads(raw[start:end+1]) if start != -1 and end != -1 else json.loads(raw)
    except Exception:
        data = {
            "brand": "",
            "sd_category_key": "fashion",
            "filters": {"auth_guarantee":"0","condition":"any","exclude_words":"shoe,sneaker,boot,heel"},
            "buying_options": "FIXED_PRICE,BEST_OFFER",
            "post_tag": "",
            "title": ""
        }

    if DEBUG_LLM:
        _dump_json({"decision": data}, os.path.join(LOG_DIR, f"llm_decision_{_now_id()}.json"))
        print("========== LLM DECISION ==========\n", json.dumps(data, ensure_ascii=False, indent=2), "\n==================================\n")

    return data



# ---- http_get: 401 için bir kere token yenile, tekrar dene ----
def http_get(path: str, params: Dict[str, Any]) -> Dict[str, Any]:
    url = f"{EBAY_BASE}{path}"
    def _do():
        return requests.get(url, headers=headers(), params=params, timeout=20)
    r = _do()
    if r.status_code == 401:
        # token süresi dolmuş olabilir → yenile ve bir kez daha dene
        from ebay_auth import get_token
        get_token(force_refresh=True)
        r = _do()
    r.raise_for_status()
    return r.json()


def days_between(now: dt.datetime, future: Optional[str]) -> Optional[int]:
    if not future: return None
    try: d = dt.datetime.fromisoformat(future.replace("Z", "+00:00"))
    except: return None
    return max(0, int(round((d - now).total_seconds()/86400)))

def seller_trust(item: Dict[str, Any]) -> Dict[str, Any]:
    seller = item.get("seller") or {}
    fb_pct = float(seller.get("feedbackPercentage") or 0) / 100.0
    fb_score = int(seller.get("feedbackScore") or 0)
    score_norm = min(1.0, math.log10(max(1, fb_score)) / 3.0)

    returns_accepted = 1.0 if (item.get("returnTerms") or {}).get("returnsAccepted") else 0.0
    eta_days = None
    ship_opts = item.get("shippingOptions") or []
    if ship_opts:
        eta_days = days_between(dt.datetime.now(dt.timezone.utc), ship_opts[0].get("minEstimatedDeliveryDate"))

    ship_bonus = 0
    if eta_days is not None:
        if eta_days <= 5: ship_bonus = 1.0
        elif eta_days <= 8: ship_bonus = 0.6
        elif eta_days <= 12: ship_bonus = 0.3

    score = 0.55*fb_pct + 0.25*score_norm + 0.10*returns_accepted + 0.10*ship_bonus
    label = "low"
    if score >= 0.75: label = "high"
    elif score >= 0.55: label = "medium"

    return {
        "score": round(score, 3), "label": label,
        "fb_pct": round(fb_pct*100,1), "fb_score": fb_score,
        "returns": "Yes" if returns_accepted else "No", "eta_days": eta_days
    }


def _iso_to_dt(s: Optional[str]) -> Optional[dt.datetime]:
    if not s: 
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None

def _period_to_days(period: Optional[dict]) -> Optional[int]:
    """eBay returnTerms.returnPeriod → {'value': 30, 'unit': 'DAY'|'BUSINESS_DAY'|'MONTH'}"""
    if not period: 
        return None
    v = period.get("value")
    u = (period.get("unit") or "").upper()
    if v is None:
        return None
    if u in ("DAY", "BUSINESS_DAY"):
        return int(v)
    if u == "MONTH":
        return int(v) * 30
    return None

def _pick_best_shipping(shipping_options: list) -> Optional[dict]:
    """En düşük kargo ücretine sahip (veya ilk) seçeneği döndür."""
    if not shipping_options:
        return None
    # Bazı item’larda cost olmayabilir → None’ları büyük kabul et
    def _cost(opt):
        c = ((opt.get("shippingCost") or {}).get("value"))
        try: return float(c)
        except: return 9e9
    return sorted(shipping_options, key=_cost)[0]


# ---------------- DB ----------------
def db_connect():
    return duckdb.connect(DB_PATH)


def ensure_category_exists(con, slug: str, name: Optional[str] = None):
    """Ensures a category exists in the categories table."""
    if not name:
        name = slug.replace("_", " ").replace("-", " ").title()
    con.execute("""
        INSERT INTO categories (slug, name)
        VALUES (?, ?)
        ON CONFLICT (slug) DO NOTHING
    """, [slug, name])

def create_idea(con, idea_id: str, title: str, category_slug: str):
    """Ensures the idea exists in the ideas table."""
    con.execute("""
        INSERT INTO ideas (idea_id, idea_title, category_slug)
        VALUES (?, ?, ?)
        ON CONFLICT (idea_id) DO NOTHING
    """, [idea_id, title, category_slug])

def save_product(con, item: Dict[str, Any]):
    pid = item['itemId']
    price_value = (item.get("price") or {}).get("value")
    con.execute("""
        INSERT OR REPLACE INTO products (parent_asin, product_title, brand, price, category_slug, source, external_id)
        VALUES (?, ?, ?, ?, ?, 'ebay', ?)
    """, [pid, item.get("title"), item.get("brand"), price_value, "ebay", item["itemId"]])
    return pid, item.get("title")



def enrich_shipping_and_returns(con, item_ids: List[str]) -> None:
    if not item_ids:
        return
    now_utc = dt.datetime.now(dt.timezone.utc)

    rows = []
    for iid in item_ids:
        try:
            it = http_get(f"/buy/browse/v1/item/{iid}", {})  # 🔁 tekil çağrı (403 derdi yok)
        except requests.HTTPError as e:
            print(f"⚠️ item fetch failed: {iid} status={getattr(e.response, 'status_code', None)}")
            continue
        except Exception as e:
            print(f"⚠️ item fetch error: {iid} err={e}")
            continue

        pid = it.get("itemId")
        if not pid:
            continue

        ship = _pick_best_shipping(it.get("shippingOptions") or [])
        ship_type = (ship or {}).get("type")
        ship_cost = (ship or {}).get("shippingCost") or {}
        try:
            ship_cost_val = float(ship_cost.get("value")) if ship_cost.get("value") is not None else None
        except Exception:
            ship_cost_val = None
        ship_cost_ccy = ship_cost.get("currency")

        ship_min_eta = _iso_to_dt((ship or {}).get("minEstimatedDeliveryDate"))
        ship_max_eta = _iso_to_dt((ship or {}).get("maxEstimatedDeliveryDate"))
        ship_cutoff  = _iso_to_dt((ship or {}).get("cutOffDate"))
        ship_eta_min_days = days_between(now_utc, (ship or {}).get("minEstimatedDeliveryDate"))
        ship_eta_max_days = days_between(now_utc, (ship or {}).get("maxEstimatedDeliveryDate"))
        ship_free = (ship_cost_val == 0.0)

        rt = it.get("returnTerms") or {}
        returns_accepted      = bool(rt.get("returnsAccepted", False))
        return_window_days    = _period_to_days(rt.get("returnPeriod"))
        return_shipping_payer = (rt.get("returnShippingCostPayer") or None)
        refund_method         = (rt.get("refundMethod") or None)
        return_method         = (rt.get("returnMethod") or None)

        try:
            restock_pct = float((rt.get("restockingFeePercentage") or "").replace("%",""))/100.0 \
                          if rt.get("restockingFeePercentage") else None
        except Exception:
            restock_pct = None

        condition_id   = (it.get("conditionId") or it.get("condition"))
        condition_name = (it.get("condition") or None)

        # 🔊 terminal log
        if VERBOSE_ENRICH_LOG:
            ttl = (it.get("title") or "").strip()
            ship_cost_txt = "FREE" if ship_free else (
                f"{ship_cost_val:g} {ship_cost_ccy}" if ship_cost_val is not None else "n/a"
            )
            if ship_eta_min_days is not None and ship_eta_max_days is not None:
                eta_txt = f"{ship_eta_min_days}-{ship_eta_max_days} days"
            elif ship_eta_min_days is not None:
                eta_txt = f"{ship_eta_min_days}+ days"
            else:
                eta_txt = "n/a"
            ret_txt = "Yes" if returns_accepted else "No"
            if returns_accepted and return_window_days:
                ret_txt += f" • {return_window_days} days"
            if return_shipping_payer:
                ret_txt += f" • payer={return_shipping_payer}"
            cond_txt = condition_name or (str(condition_id) if condition_id is not None else "n/a")
            print(f"🟦 Ürün: {ttl[:100]}")
            print(f"   🚚 Kargo: {ship_type or 'n/a'} • {ship_cost_txt} • ETA {eta_txt}")
            print(f"   🔁 İade: {ret_txt}")
            print(f"   📦 Koşul: {cond_txt}")

        rows.append([
            pid, ship_type, ship_cost_val, ship_cost_ccy, ship_free,
            ship_min_eta, ship_max_eta, ship_cutoff, ship_eta_min_days, ship_eta_max_days,
            returns_accepted, return_window_days, return_shipping_payer, refund_method, return_method, restock_pct,
            str(condition_id) if condition_id is not None else None, condition_name,
            json.dumps(it)
        ])

        time.sleep(RATE_LIMIT_SLEEP)

    if rows:
        con.executemany("""
            INSERT OR REPLACE INTO product_enrichment (
                product_id,
                ship_type, ship_cost_value, ship_cost_currency, ship_free,
                ship_min_eta, ship_max_eta, ship_cutoff, ship_eta_min_days, ship_eta_max_days,
                returns_accepted, return_window_days, return_shipping_payer, refund_method, return_method, restocking_fee_pct,
                condition_id, condition_name,
                source_json, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, now())
        """, rows)


def normalize_image_url(url: str) -> str:
    """
    eBay image URLs are usually like:
    - https://i.ebayimg.com/images/g/xxxx/s-l225.jpg
    - https://i.ebayimg.com/images/g/xxxx/s-l500.png
    - https://i.ebayimg.com/images/g/xxxx/s-l64.webp
    We want to standardize to the largest version: s-l1600.webp
    """
    if not url:
        return url
    return re.sub(r's-l\d+\.(?:jpg|jpeg|png|webp)$', 's-l1600.webp', url)

def save_media(con, pid: str, item: dict):
    img = (item.get("image") or {}).get("imageUrl")
    if img:
        img = normalize_image_url(img)   # 🔥 yüksek çözünürlüklü URL’ye çevir
        con.execute("""
            INSERT OR IGNORE INTO product_media (parent_asin, image_url, source)
            VALUES (?, ?, 'ebay')
        """, [pid, img])

def save_metrics(con, pid: str, metrics: Dict[str, Any]):
    con.execute("""
        INSERT INTO product_metrics_ebay
        (product_id, seller_score, feedback_pct, feedback_score, returns, eta_days, trust_level)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, [
        pid,
        metrics["score"],
        metrics["fb_pct"],
        metrics["fb_score"],
        metrics["returns"],
        metrics["eta_days"],
        metrics["label"]
    ])

def save_idea_product_link(con, idea_id: str, product_id: str):
    """Links a product to a blog idea in the idea_products table."""
    con.execute("""
        INSERT OR IGNORE INTO idea_products (idea_id, parent_asin)
        VALUES (?, ?)
    """, [idea_id, product_id])

def slugify(s: str) -> str:
    return re.sub(r'[^a-z0-9]+', '-', s.lower()).strip('-')

def _extract_section(md_text: str, section_name: str) -> str:
    if not md_text:
        return ""

    # Normalize apostrophes/quotes
    norm_text = md_text.replace("’", "'").replace("`", "'").replace("‘", "'")

    aliases = {
        "introduction": [r"introduction"],
        "buyers_guide": [r"buyer'?s?\s+guide", r"buying\s+guide"],
        "faq": [r"faqs?", r"q\s*&\s*a"],
        "conclusion": [r"conclusion", r"final\s+thoughts", r"wrap\s+up"],
    }

    pats = aliases.get(section_name.lower(), [section_name])
    for pat in pats:
        regex = re.compile(
            rf"^\s*##\s*{pat}\s*\n(.*?)(?=\n\s*##\s|\Z)",
            re.S | re.I | re.M
        )
        m = regex.search(norm_text)
        if m:
            return m.group(1).strip()

    return ""

# ---------------- LLM ----------------
def generate_blog_content(keyword: str, products: list) -> dict:
    product_titles = "\n".join([f"- {p}" for p in products])

    prompt = f"""
    You are an expert SEO content writer and e-commerce copywriter writing for a blog that curates trending online products.
    
    Your task is to generate a **complete, structured blog post** for the keyword: "{keyword}".

    You have access to these product titles from eBay:
    {product_titles}

    Return ONLY a valid **JSON object** with the following keys:
    - "title": a **clickworthy, SEO-optimized title** that includes the year “2025” (or current year).
    It should feel fresh and relevant — examples:
    *“Best {keyword} of 2025: Top Picks for Smart Shoppers”*,  
    *“Ultimate 2025 Guide to {keyword}: What’s Worth Buying Now”*,  
    *“Top 10 {keyword} in 2025 (Expert Buying Tips Inside)”*.  
    The title must be under 120 characters, use natural capitalization, and clearly promise value.

    - "introduction": a **short, high-impact opening section** (3–5 sentences max) written in a conversational tone.
    The goal is to instantly **hook** the reader — make it sound like a friendly expert helping them choose.
    You may start with a **question**, a **surprising fact**, or a **relatable statement**.
    If [DEAL_STATS] is provided, explicitly mention the max_discount (e.g., "up to {{max_discount}}% off"),
    the total number of items (if ≥ 3), and a quick price range if available (e.g., "${{price_min}}–${{price_max}}").

    Examples:
    - “Are you searching for the perfect {keyword} that actually lives up to the hype?”
    - “It’s 2025 — and {keyword} are back in style for all the right reasons.”
    - “Whether you’re upgrading your setup or buying your first {keyword}, here’s what to look for.”
    The introduction should end with a smooth bridge sentence leading into the Quick Take section,
    such as “Here’s what you need to know before buying.”

    **Formatting allowed:**  
    - Use *Markdown* for structure.  
    - You may use **bold** or _italic_ emphasis for key phrases.  
    - Do not include headings (no ## or ###) inside the introduction.  
    - Keep it short, skimmable, and emotionally engaging.

    - "buyers_guide": a detailed buyer’s guide with numbered subsections (Markdown allowed).
    Focus on what factors matter when buying these products (features, durability, price, value, etc.).

    - "faq": 3–5 questions and answers in Markdown.
    Each question must be **bold** (like “**1. What should I look for?**”) followed by a clear, concise answer.

    - "conclusion": 2–3 short paragraphs that summarize the key insights and encourage the reader to explore the featured products.

    **Rules:**
    - Be factual but conversational — write like a trusted online expert.
    - Use Markdown formatting for readability.
    - Include bold or _italic_ emphasis naturally, but never all-caps.
    - Avoid repeating the exact keyword too often; use natural variations.
    - Do NOT include anything outside the JSON object (no explanations or commentary).
    - If the input includes a [DEAL_STATS] block with "max_discount" ≥ 10,
    the "title" MUST contain a phrase like "Up to {{max_discount}}% Off".
    Example: "Hamilton Watches 2025 — Up to 58% Off (Editor’s Picks)".


    Return only JSON.
    """

    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.7,
        max_tokens=1200,
    )

    raw = resp.choices[0].message.content.strip()
    start = raw.find("{")
    end = raw.rfind("}")
    try:
        data = json.loads(raw[start:end+1]) if start != -1 and end != -1 else json.loads(raw)
    except Exception as e:
        print("⚠️ LLM JSON parse failed:", e)
        data = {
            "title": f"Best {keyword} Buying Guide",
            "introduction": "",
            "buyers_guide": "",
            "faq": "",
            "conclusion": ""
        }

    # normalize + fallback
    def _normalize_text(s):
        if isinstance(s, dict):
            # bazen model nested JSON döndürebiliyor
            s = json.dumps(s, ensure_ascii=False)
        elif isinstance(s, list):
            s = "\n".join(str(x) for x in s)
        return re.sub(r"\n{3,}", "\n\n", (s or "").strip())

    def _fallback_snippet(kind):
        if kind == "introduction":
            return f"""**Quick Take:** Looking for the best {keyword}? Here are top picks and what to consider before buying."""
        if kind == "buyers_guide":
            return """1. **Key Features** – Focus on quality, durability, and ease of use.
2. **Price vs Value** – Compare specs, don’t overpay for minor upgrades.
3. **User Fit** – Check comfort, compatibility, and warranty terms."""
        if kind == "faq":
            return """**1. How do I choose the right option?**  
Match features to your use-case and check return/warranty terms."""
        if kind == "conclusion":
            return """In summary, pick the “Best Overall” for balance, “Best Budget” for price, and “Best Premium” for top performance."""
        return ""

    cleaned = {}
    for key in ["introduction", "buyers_guide", "faq", "conclusion"]:
        raw_val = data.get(key, "")
        if isinstance(raw_val, (dict, list)):
            raw_val = json.dumps(raw_val, ensure_ascii=False)
        val = _normalize_text(raw_val)

        if len(val) < 50:
            val = _fallback_snippet(key)
        cleaned[key] = val

    title = data.get("title", f"Best {keyword}").strip().strip('"').strip("'")

    return {
        "title": title,
        "introduction": cleaned["introduction"],
        "buyers_guide": cleaned["buyers_guide"],
        "faq": cleaned["faq"],
        "conclusion": cleaned["conclusion"],
    }

def assign_author_and_publish(idea_id: str, category_slug: str):
    """DB şemasına dokunmadan blog_posts satırını ekle/güncelle.
    - id PK otomatik artmıyor → next_id = MAX(id)+1
    - author: category_slug'e göre seç, yoksa rastgele
    - blog_title/hero bilgilerini blog_contents'ten çek
    """
    con = duckdb.connect(DB_PATH, read_only=False)

    # 1) Kategoriden author seç (yoksa rastgele)
    row = con.execute("""
        SELECT author_id
        FROM authors
        WHERE primary_category_slug = ?
        ORDER BY random()
        LIMIT 1
    """, [category_slug]).fetchone()
    if row:
        author_id = row[0]
    else:
        fallback = con.execute("SELECT author_id FROM authors ORDER BY random() LIMIT 1").fetchone()
        author_id = fallback[0] if fallback else None  # teorik olarak her zaman olmalı

    # 2) Başlık ve hero'ları blog_contents'ten çek
    bc = con.execute("""
        SELECT title, hero_image_url, hero_alt
        FROM blog_contents
        WHERE idea_id = ?
        LIMIT 1
    """, [idea_id]).fetchone()

    if bc:
        blog_title, hero_image_url, hero_alt = bc[0], bc[1], bc[2]
    else:
        blog_title = f"Best {category_slug.title()} Picks"
        hero_image_url, hero_alt = None, None

    # 3) Var mı? varsa UPDATE, yoksa INSERT (id zorunlu)
    exists = con.execute("SELECT id FROM blog_posts WHERE idea_id = ? LIMIT 1", [idea_id]).fetchone()

    if exists:
        con.execute("""
            UPDATE blog_posts
            SET blog_title     = ?,
                status         = 'published',
                author_id      = ?,
                hero_image_url = ?,
                hero_alt       = ?,
                date_published = COALESCE(date_published, current_date),
                created_at     = COALESCE(created_at, now())
            WHERE id = ?
        """, [blog_title, author_id, hero_image_url, hero_alt, exists[0]])
    else:
        # id otomatik değil → next_id hesapla
        next_id = con.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM blog_posts").fetchone()[0]
        con.execute("""
            INSERT INTO blog_posts
                (id, season_phrase_id, blog_title, status, created_at, idea_id,
                 author_id, date_published, hero_image_url, hero_alt, summary)
            VALUES
                (?, NULL, ?, 'published', now(), ?, ?, current_date, ?, ?, NULL)
        """, [next_id, blog_title, idea_id, author_id, hero_image_url, hero_alt])

    con.close()


def ensure_related_links(con, idea_id: str, category_slug: str):
    """If blog_contents.related_links_json is empty, fill with 4 latest posts from same category."""
    row = con.execute(
        "SELECT related_links_json FROM blog_contents WHERE idea_id=? LIMIT 1",
        [idea_id]
    ).fetchone()
    if row and row[0]:
        return  # already has links

    rows = con.execute("""
        SELECT title, slug
        FROM blog_contents
        WHERE category_slug = ? AND idea_id <> ?
        ORDER BY updated_at DESC
        LIMIT 4
    """, [category_slug, idea_id]).fetchall()

    if not rows:
        return

    payload = {"related_links": [{"title": t, "slug": s} for (t, s) in rows]}
    con.execute("""
        UPDATE blog_contents
        SET related_links_json = ?
        WHERE idea_id = ?
    """, [json.dumps(payload), idea_id])


# ---------------- DB: Save Blog ----------------
def save_blog(con, keyword, blog, category="seasonal", hero_image=None):
    # --- Parser çıktıları (hem MD hem JSON list destekler) ---
    bg_json  = buyers_guide_to_json(blog.get("buyers_guide", ""))
    faq_json = faq_to_json(blog.get("faq", ""))

    # idea_id akışı
    idea_id = blog.get("idea_id") or f"k-{slugify(keyword)}"
    idea_id = keyword

    slug = slugify(blog["title"])
    category_slug = category

    con.execute("""
        INSERT INTO blog_contents (
            idea_id, title, slug, category_slug,
            hero_image_url, hero_alt, introduction,
            product_gallery, urunler,
            buyers_guide, buyers_guide_json,
            faq,          faq_json,
            conclusion, recommendations, cta, md_full,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, now())
        ON CONFLICT (idea_id) DO UPDATE SET
            title            = EXCLUDED.title,
            slug             = EXCLUDED.slug,
            category_slug    = EXCLUDED.category_slug,
            hero_image_url   = EXCLUDED.hero_image_url,
            hero_alt         = EXCLUDED.hero_alt,
            introduction     = EXCLUDED.introduction,
            product_gallery  = EXCLUDED.product_gallery,
            urunler          = EXCLUDED.urunler,
            buyers_guide     = EXCLUDED.buyers_guide,
            buyers_guide_json= EXCLUDED.buyers_guide_json,
            faq              = EXCLUDED.faq,
            faq_json         = EXCLUDED.faq_json,
            conclusion       = EXCLUDED.conclusion,
            recommendations  = EXCLUDED.recommendations,
            cta              = EXCLUDED.cta,
            md_full          = EXCLUDED.md_full,
            updated_at       = now()
    """, [
        idea_id,
        blog["title"],
        slug,
        category_slug,
        hero_image,
        blog.get("hero_alt", blog["title"]),
        blog.get("introduction",""),
        blog.get("product_gallery",""),
        blog.get("urunler",""),

        blog.get("buyers_guide",""),
        json.dumps(bg_json, ensure_ascii=False),

        blog.get("faq",""),
        json.dumps(faq_json, ensure_ascii=False),

        blog.get("conclusion",""),
        blog.get("recommendations",""),
        blog.get("cta",""),
        blog.get("md_full",""),
    ])

    return idea_id, slug, category_slug



def random_keyword(con, season_name=None, seasons_back=5):
    if season_name:
        row = con.execute("""
            SELECT sp.phrase
            FROM season_phrases sp
            JOIN seasons s ON s.id = sp.season_id
            LEFT JOIN blog_posts bp ON bp.idea_id = sp.phrase
            WHERE s.season_name = ?
              AND sp.kept = TRUE
              AND bp.idea_id IS NULL
            ORDER BY random()
            LIMIT 1
        """, [season_name]).fetchone()
    else:
        row = con.execute("""
            SELECT sp.phrase
            FROM season_phrases sp
            JOIN seasons s ON s.id = sp.season_id
            LEFT JOIN blog_posts bp ON bp.idea_id = sp.phrase
            WHERE sp.season_id IN (
              SELECT id FROM seasons ORDER BY id DESC LIMIT ?
            )
              AND sp.kept = TRUE
              AND bp.idea_id IS NULL
            ORDER BY random()
            LIMIT 1
        """, [seasons_back]).fetchone()
    return row[0] if row else None

def _category_from_season_name(con, season_name: Optional[str]) -> str:
    if not season_name:
        return "seasonal"
    row = con.execute("""
        SELECT COALESCE(season_group, 'seasonal')
        FROM seasons
        WHERE season_name = ?
        LIMIT 1
    """, [season_name]).fetchone()
    grp = row[0] if row else "seasonal"
    return "watches" if grp == "watches" else "seasonal"


def _season_name_for_keyword(con, keyword: str) -> Optional[str]:
    row = con.execute("""
        SELECT s.season_name
        FROM season_phrases sp
        JOIN seasons s ON s.id = sp.season_id
        WHERE sp.phrase = ?
        LIMIT 1
    """, [keyword]).fetchone()
    return row[0] if row else None

def normalize_llm_filters(decision: dict) -> dict:
    f = dict(decision.get("filters") or {})
    cat = (decision.get("sd_category_key") or decision.get("category_key") or "").lower()

    if cat in ("jewelry", "fashion"):
        f["auth_guarantee"] = "0"
        f["condition"] = "any"

    # ✅ Jewelry'yi saatlerden ayır: pozitif & negatif
    if cat == "jewelry":
        inc = set([s.strip().lower() for s in (f.get("include_words") or "").split(",") if s.strip()])
        exc = set([s.strip().lower() for s in (f.get("exclude_words") or "").split(",") if s.strip()])

        # Pozitif: gerçek takı sınıfları
        inc |= {"ring","necklace","bracelet","earring","pendant"}
        # Negatif: saat & parça terminolojisi
        exc |= {"watch","bezel","dial","movement","case","strap","crown","hands","link","bracelet links"}

        f["include_words"] = ",".join(sorted(inc))
        f["exclude_words"] = ",".join(sorted(exc))

        # Jewelry'de çok dar fiyat bandı varsa gevşet
        try:
            pmn, pmx = f.get("price_min"), f.get("price_max")
            if pmn is not None and pmx is not None and (int(pmx) - int(pmn)) < 400:
                f.pop("price_min", None); f.pop("price_max", None)
        except Exception:
            f.pop("price_min", None); f.pop("price_max", None)

    # Genel temizlik
    if f.get("condition") not in ("new","used","any"):
        f["condition"] = "any"
    decision["buying_options"] = "FIXED_PRICE,BEST_OFFER"
    f["exclude_words"] = f.get("exclude_words") or "shoe,sneaker,boot,heel"
    decision["filters"] = f
    return decision


def _relax_params_for_retry(params, stage):
    p = dict(params)
    filt = p.get("filter", "")

    # 1) AUTH -> kaldır
    if stage >= 1 and "qualifiedPrograms:" in filt:
        filt = re.sub(r'(,)?\s*qualifiedPrograms:\{[^}]+\}', '', filt)

    # 2) condition -> any (yani conditionIds bloklarını sök)
    if stage >= 2 and "conditionIds:" in filt:
        filt = re.sub(r'(,)?\s*conditionIds:\{[^}]+\}', '', filt)

    # 3) price band -> kaldır
    if stage >= 3 and "price:[" in filt:
        filt = re.sub(r'(,)?\s*price:\[[^\]]+\]', '', filt)

    p["filter"] = filt.strip(", ")

    # 4) aspect_filter -> kaldır
    if stage >= 4:
        p.pop("aspect_filter", None)

    return p


def enforce_constraints_on_decision(decision: dict,
                                    constraints: dict,
                                    candidate_info: dict,
                                    brand_category_map: dict | None,
                                    con=None) -> dict:
    """
    - Kategori yasaklı/boşsa preferred'e zorla
    - Marka-kategori uyumsuzsa düzelt
    - Marka boş ise: seçilen kategoriye uyumlu markalardan birini ata
    - brand_category_map boş ise sd_brand_categories’den üret
    """
    import random

    # Dinamik harita gerekirse DB’den üret
    if (not brand_category_map) and con is not None:
        _, brand_category_map, _ = load_sd_taxonomy(con)

    # Kategori havuzu
    cats_pool = []
    if con is not None:
        cats_pool = [r[0] for r in con.execute("SELECT slug FROM sd_categories ORDER BY slug").fetchall()]
    if not cats_pool:
        cats_pool = ["watches","handbags","jewelry","fashion"]

    cat   = (decision.get("sd_category_key") or decision.get("category_key") or "").lower().strip()
    brand = (decision.get("brand") or "").lower().strip()

    disallowed_cats = set((constraints or {}).get("disallowed_categories") or [])
    preferred_cats  = (constraints or {}).get("preferred_categories") or []
    disallowed_brands = set((constraints or {}).get("disallowed_brands") or [])

    # 1) Kategori boşsa veya yasaklıysa:
    valid_cats = [c for c in cats_pool if c not in disallowed_cats]
    if (not cat) or (cat in disallowed_cats):
        if preferred_cats:
            # tercihli listede olanlardan, aynı zamanda valid olanları al; yoksa valid_cats
            pref_valid = [c for c in preferred_cats if c in valid_cats]
            if pref_valid:
                cat = random.choice(pref_valid)
            else:
                cat = random.choice(valid_cats) if valid_cats else (preferred_cats[0] if preferred_cats else cats_pool[0])
        else:
            cat = random.choice(valid_cats) if valid_cats else cats_pool[0]
        decision["sd_category_key"] = cat

    # 2) Marka boşsa: kategoriye uyumlu markalardan rastgele
    if not brand:
        compat = []
        if con is not None:
            compat = [r[0] for r in con.execute(
                "SELECT DISTINCT brand_slug FROM sd_brand_categories WHERE category_slug = ? ORDER BY brand_slug",
                [cat]
            ).fetchall()]
        if not compat:
            compat = list((brand_category_map or {}).keys())

        compat = [b for b in compat if b not in disallowed_brands]
        if compat:
            decision["brand"] = random.choice(compat)
            brand = decision["brand"]

    # 3) Marka-kategori uyumu yoksa: kategoriyi destekleyen markalardan rastgele seç
    cats_for_brand = (brand_category_map or {}).get(brand, [])
    if cat and brand and (cat not in cats_for_brand):
        compat = []
        if con is not None:
            compat = [r[0] for r in con.execute(
                "SELECT brand_slug FROM sd_brand_categories WHERE category_slug = ? ORDER BY brand_slug",
                [cat]
            ).fetchall()]
        compat = [b for b in compat if b not in disallowed_brands]
        if compat:
            decision["brand"] = random.choice(compat)
        else:
            # son çare: kategoriyi markanın ilk desteklediğine döndür
            if cats_for_brand:
                decision["sd_category_key"] = cats_for_brand[0]

    return decision



# ---------------- Main ----------------

def main():
    # ---------- Argparse ----------
    ap = argparse.ArgumentParser()
    ap.add_argument("--season-name", help="Sadece bu sezondan keyword seç (örn: thanksgiving-2025)")
    ap.add_argument("--seasons-back", type=int, default=5, help="Son N sezondan rastgele seç (varsayılan 5)")

    # --- DEAL MODE ARGÜMANLARI (MEVCUT) ---
    ap.add_argument("--deal-mode", type=int, default=0)
    ap.add_argument("--brand")
    ap.add_argument("--category", help="watches|cell_phones|jewelry|handbags|fashion")
    ap.add_argument("--target-discount", type=int)
    ap.add_argument("--discount-band", type=int, default=10)
    ap.add_argument("--price-max", type=int)
    ap.add_argument("--price-min", type=int)
    ap.add_argument("--buying-options", default="FIXED_PRICE,BEST_OFFER")
    ap.add_argument("--auth-guarantee", type=int, default=0)
    ap.add_argument("--refurbished", choices=["none","any"], default="none")
    ap.add_argument("--limit", type=int, default=400)
    ap.add_argument("--keyword")
    ap.add_argument("--category-slug", help="varsayılan: special-deals")
    ap.add_argument("--discount-min", type=int, help="Minimum discount %, e.g. 40")
    ap.add_argument("--condition", choices=["new","used","any"], default="any")
    ap.add_argument("--brand-aspect", type=int, default=0, help="1: aspect_filter Brand:{brand} kullan")
    ap.add_argument("--unique", type=int, default=0, help="1: idea_id'ye tarih etiketi ekle")
    ap.add_argument("--post-tag", help="idea_id'ye eklenecek ekstra etiket (ör: v2, oct14, test)")
    ap.add_argument("--enrich-limit", type=int, default=30, help="Shipping/returns enrich edilecek item sayısı (0=hepsi)")
    ap.add_argument("--skip-internal", action="store_true", help="İç link zenginleştirmeyi atla")
    ap.add_argument("--skip-sitemap", action="store_true", help="robots.txt / sitemap / Google ping atla")
    ap.add_argument("--color", help="Color aspect (e.g., red, blue, black)")
    ap.add_argument("--exclude-words", default="", help="Comma-separated words to exclude if appear in title (e.g., shoe,sneaker,boot,heel)")
    ap.add_argument("--include-words", default="", help="Comma-separated words required in title (optional)")
    args = ap.parse_args()

    # ---------- LLM OTO KARAR DALI (deal_mode=2) ----------
    if args.deal_mode == 2:
        con = db_connect()
        run_id = f"sd-{dt.datetime.utcnow().strftime('%Y%m%d-%H%M%S')}"
        try:
            # 1) LLM context: candidates + season + denge
            candidate_info = build_candidate_brands_seed_only(con, top_n=12)
            season_ctx     = build_season_context_neutral()
            decision_ctx   = build_decision_context(con, days_back=2)   # DB’den
            constraints    = compute_constraints(decision_ctx, threshold=2, con=con)  # ← con verildi


            # 2) Run zarfını aç
            con.execute("""
                INSERT INTO sd_runs (run_id, candidate_json, season_json, decision_ctx, status)
                VALUES (?, ?, ?, ?, 'started')
            """, [
                run_id,
                json.dumps(candidate_info, ensure_ascii=False, default=_json_default),
                json.dumps(season_ctx,     ensure_ascii=False, default=_json_default),
                json.dumps(decision_ctx,   ensure_ascii=False, default=_json_default),
            ])

            # 3) LLM karar ver
            decision = llm_decide_deal(candidate_info, season_ctx, decision_ctx, constraints)
            # sd_* haritasını dinamik verelim
            cats_pool, brand_cat_map, primary_map = load_sd_taxonomy(con)
            decision = enforce_constraints_on_decision(decision, constraints, candidate_info, brand_cat_map, con=con)
            decision = normalize_llm_filters(decision)



            # 4) LLM kararını ve (istersen) prompt’unu kaydet
            # llm_decide_deal içinde prompt string’i tutulmuyorsa None yazıyoruz; istersen fonksiyonu prompt döndürecek şekilde genişletebilirsin.
            con.execute("""
                UPDATE sd_runs SET llm_prompt = COALESCE(llm_prompt, ?), llm_decision = ?
                WHERE run_id = ?
            """, [
                None,
                json.dumps(decision, ensure_ascii=False, default=_json_default),
                run_id
            ])

            # 5) Normalize decision (sd_decisions)
            con.execute("""
                INSERT INTO sd_decisions (run_id, brand, sd_category_key, filters_json, buying_options, post_tag, title_suggest)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, [
                run_id,
                (decision.get("brand") or "").lower(),
                (decision.get("sd_category_key") or decision.get("category_key") or "special-deals"),
                json.dumps(decision.get("filters") or {}, ensure_ascii=False, default=_json_default),
                decision.get("buying_options") or "FIXED_PRICE,BEST_OFFER",
                decision.get("post_tag"),
                decision.get("title")
            ])

            # 6) LLM kararını mevcut deal akışına Namespace olarak ver
                        # 6) LLM kararını mevcut deal akışına Namespace olarak ver
            def _sanitize_buying_options(s: Optional[str]) -> str:
                if not s:
                    return "FIXED_PRICE,BEST_OFFER"
                s = s.upper().replace(" ", "")
                valid = {"FIXED_PRICE","BEST_OFFER","AUCTION","CLASSIFIED_AD"}
                toks = [t for t in s.split(",") if t in valid]
                return ",".join(toks) if toks else "FIXED_PRICE,BEST_OFFER"

            # ---- Marka boş olabilir: flag ve include_words buna göre ayarlansın
            _brand_from_llm = (decision.get("brand") or None)
            _filters_from_llm = (decision.get("filters") or {})

            # marka yoksa aspect_filter kullanmayacağız
            _brand_aspect_flag = 1 if _brand_from_llm else 0

            # marka yoksa include_words'u tamamen kapat (daraltmasın)
            _include_words_val = ""
            if _brand_from_llm:
                _include_words_val = _filters_from_llm.get("include_words", "") or ""
            
            _decided_cat = (decision.get("sd_category_key") or decision.get("category_key") or "fashion")
            # menüde kategori yoksa yarat
            ensure_category_exists(con, _decided_cat, _decided_cat.title())
            category_chosen = (decision.get("sd_category_key") or decision.get("category_key") or "").lower() or "special-deals"

            ns = argparse.Namespace(
                deal_mode=1,
                brand=decision.get("brand"),
                category=category_chosen,
                target_discount=_filters_from_llm.get("target_discount"),
                discount_band=10,
                discount_min=_filters_from_llm.get("discount_min"),
                price_min=_filters_from_llm.get("price_min"),
                price_max=_filters_from_llm.get("price_max"),
                buying_options=_sanitize_buying_options(decision.get("buying_options")),
                auth_guarantee=int(_filters_from_llm.get("auth_guarantee", 1)),
                refurbished="none",
                limit=400,
                # watches ise ayrı kategori_slug kullanıyorsun; mevcut davranışı koruyorum
                #category_slug=category_chosen,   # ← ESKİ: watches ise watches, değilse special-deals. YENİ: her zaman seçilen kategori.
                category_slug=category_chosen,   # watches | handbags | jewelry | fashion

                condition=_filters_from_llm.get("condition", "any"),
                brand_aspect=_brand_aspect_flag,  # ← marka yoksa 0
                unique=1,
                post_tag=decision.get("post_tag"),
                enrich_limit=30,
                skip_internal=False,
                skip_sitemap=False,
                color=_filters_from_llm.get("color"),
                exclude_words=_filters_from_llm.get("exclude_words", "shoe,sneaker,boot,heel"),
                include_words=_include_words_val,  # ← marka yoksa boş string
                keyword=None
            )


        except Exception as e:
            try:
                con.execute("INSERT INTO sd_errors (run_id, step, error_msg) VALUES (?, ?, ?)", [run_id, 'llm', str(e)])
                con.execute("UPDATE sd_runs SET status='failed', notes=? WHERE run_id=?", [f"LLM stage failed: {e}", run_id])
            finally:
                con.close()
            raise

        # 7) Ingest + Publish (mevcut akış)
        try:
            run_deal_flow(ns)
            # Not: run_deal_flow idea_id return etmiyor; en güncel satırı DB'den çekiyoruz
            # publish bittikten sonra:
            row = con.execute("""
            SELECT r.idea_id, r.rules_json, bc.category_slug
            FROM idea_rules_deal r
            JOIN blog_contents bc ON bc.idea_id = r.idea_id
            ORDER BY r.updated_at DESC
            LIMIT 1
            """).fetchone()

            if row:
                idea_id = row[0]
                brand   = (json.loads(row[1]).get("brand") or "").lower()
                cat     = row[2] or "special-deals"
                con.execute("""
                INSERT INTO sd_publications (run_id, idea_id, brand, sd_category_key)
                VALUES (?, ?, ?, ?)
                """, [run_id, idea_id, brand, cat])
                con.execute("UPDATE sd_runs SET status='published' WHERE run_id=?", [run_id])
            else:
                con.execute("UPDATE sd_runs SET status='ingested', notes='No idea_id located' WHERE run_id=?", [run_id])

        except Exception as e:
            con.execute("INSERT INTO sd_errors (run_id, step, error_msg) VALUES (?, ?, ?)", [run_id, 'publish', str(e)])
            con.execute("UPDATE sd_runs SET status='failed', notes=? WHERE run_id=?", [f"Publish stage failed: {e}", run_id])
            con.close()
            raise

        con.close()
        return

    # ---------- MANUEL DEAL DALI (deal_mode=1) ----------
    if args.deal_mode == 1:
        run_deal_flow(args)
        return

    # ---------- SEZONSAL KEYWORD DALI (mevcut akış) ----------
    con = db_connect()
    idea_id = None
    try:
        keyword = random_keyword(con, season_name=args.season_name, seasons_back=args.seasons_back)
        print(f"🎯 Keyword: {keyword}")
        if not keyword:
            print("❌ No available keyword (season_phrases empty or all published)")
            return

        # keyword hangi season'dan geldi?
        kw_season_name = _season_name_for_keyword(con, keyword)
        category_slug = _category_from_season_name(con, kw_season_name)

        # kategori güvence
        ensure_category_exists(con, category_slug)

        # idea kaydı
        create_idea(con, keyword, (keyword or "").title(), category_slug)

        # eBay API
        data = http_get("/buy/browse/v1/item_summary/search", {"q": keyword, "limit": 20})
        items = data.get("itemSummaries") or []
        if not items:
            print("❌ No items found")
            return

        products: List[str] = []
        item_ids_for_enrich: List[str] = []

        for item in items:
            pid, title = save_product(con, item)
            save_media(con, pid, item)
            trust = seller_trust(item)
            save_metrics(con, pid, trust)
            save_idea_product_link(con, keyword, pid)

            products.append(title)
            if item.get("itemId"):
                item_ids_for_enrich.append(item["itemId"])
            print(f"   ✅ Saved: {str(title)[:70]}...")

        # enrichment
        enrich_shipping_and_returns(con, item_ids_for_enrich)

        # Hero image
        raw_hero = (items[0].get("image") or {}).get("imageUrl")
        hero_image = normalize_image_url(raw_hero)

        # Blog content
        blog = generate_blog_content(keyword, products)

        # DB’ye kaydet
        idea_id, slug, category_slug = save_blog(
            con, keyword, blog, category=category_slug, hero_image=hero_image
        )

        assign_author_and_publish(idea_id, category_slug)
        print(f"📝 Blog created: {blog['title']} (category={category_slug})")
    finally:
        # DuckDB lock sorunlarını önlemek için
        con.close()

    # İç link zenginleştirme (opsiyonel bayraklarla kontrol)
    try:
        if not args.skip_internal and idea_id:
            print(f"🔄 Enriching internal links for {idea_id} ...")
            os.system(f'/home/ubuntu/blog-factory/.venv/bin/python /home/ubuntu/blog-factory/src/ebay/blog_internal_links.py "{idea_id}" >> /home/ubuntu/blog-factory/logs/internal_links.log 2>&1')
            print(f"✅ Internal links enriched for {idea_id}")
        else:
            print("⏭️  Internal link enrichment skipped (flag or no idea_id)")
    except Exception as e:
        print(f"⚠️ Skipped internal link enrichment due to error: {e}")

    print("📡 Sitemap & robots.txt updated after blog creation and Google notified.")



if __name__ == "__main__":
    main()
