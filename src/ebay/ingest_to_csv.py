#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Seasonal eBay autosuggest → DuckDB ingest

Kurallar (dünkü mantık):
- Sadece sezon root'u + boşluk + a–z:  ör. "halloween a", "halloween b", ...
- thanksgiving için: "thanksgiving a"... (prefix hep season root)
- Tema/tip tabanlı skor + filtreler
- Apparel için 'halloween' (veya root) zorunluluğu açık (kapamak için --apparel-needs-root false)
- Global brand cap
- Alpha sweep: 1 → a–z, 2 → aa–zz

DB tabloları (önceden oluşturulmuş kabul):
  seasons(id BIGINT, season_name TEXT UNIQUE, root TEXT, created_at TIMESTAMP)
  season_phrases(id BIGINT, season_id BIGINT, seed TEXT, phrase TEXT,
                 kept BOOLEAN, score DOUBLE, reason TEXT, source TEXT, created_at TIMESTAMP)

Kullanım:
  cd ~/blog-factory/src/ebay
  python3 1-season_ingest.py --season halloween-2025
  # opsiyonel:
  # python3 1-season_ingest.py --season halloween-2025 --alpha-sweep 2 --min-score 1.0 --require-type false
"""

import argparse, os, re, time, json, datetime
from typing import List, Tuple, Dict, Optional

import duckdb
import requests

# ======================
# DuckDB yolunu dosya konumuna göre bul
# ======================
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "warehouse", "blog_factory.duckdb")
DEFAULT_DB = DB_PATH

# ======================
# Filtre sözlükleri (dünküyle aynı)
# ======================
THEME_TOKENS = {
    "halloween","pumpkin","witch","ghost","zombie","vampire","skeleton",
    "haunted","spooky","monster","clown","horror","bloody","devil","bat",
    "spider","skull","creepy","grave","mummy","lantern","tombstone","coffin",
    "web","cobweb","ghoul","werewolf","grim","reaper","cauldron","boo",
    "trick","treat","labubu"
}

TYPE_TOKENS = {
    # Decor / party
    "decor","decoration","decorations","yard","outdoor","indoor","door","window",
    "wreath","banner","garland","sign","tablecloth","centerpiece","cup","plate","napkin",
    "party","bucket","bag","bowl","lights","string","led","prop","props","animatronic",
    "inflatables","inflatable","projector","blow","mold","blowmold","lantern","village",
    # Costume / apparel
    "costume","costumes","mask","masks","makeup","face","paint","cape","wig","hat",
    "cloak","robe","onesie","pj","pajama","t-shirt","shirt","dress","hoodie",
    "sweatshirt","kids","women","men","adult","child","girl","boy"
}

APPAREL_TOKENS = {
    "hoodie","t-shirt","shirt","tee","sweatshirt","cap","caps","hat","beanie",
    "pj","pajama","onesie","dress","skirt","leggings"
}

EXACT_DENY_PHRASES = [
    "ghost in the shell","devil may cry","grave digger","witcher",
    "tsushima","hello kitty","atelier","rider 1973"
]
SUBSTR_DENY = [
    "collector edition","collectors edition","board game",
    "ps4","ps5","xbox","switch","snes","steam","dvd","blu ray","vhs",
    "golf bag","watch men","automatic watch","oil","capsule","seeds",
]
DENY_PATTERNS = re.compile(r"|".join([re.escape(x) for x in SUBSTR_DENY]), re.IGNORECASE)
YEAR_PATTERN = re.compile(r"\b(19\d{2}|20\d{2})\b")

BRAND_REGEXES = {
    "trick_or_treat_studios": re.compile(r"\btrick\s*or\s*treat\s*studios?\b", re.I),
    "spirit_halloween": re.compile(r"\bspirit\s*halloween\b", re.I),
}
BRAND_CAP_DEFAULT = 3

# ======================
# Yardımcılar
# ======================
def normalize(s: str):
    s = s.lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s, s.split()

def seasonal_score(tokens: List[str], root: str):
    theme_hits = sum(1 for t in tokens if t in THEME_TOKENS or t == root)
    type_hits  = sum(1 for t in tokens if t in TYPE_TOKENS)
    score = 0.0
    if theme_hits: score += 1.0
    if type_hits:  score += 1.0
    if root in tokens: score += 0.5
    joined = " ".join(tokens)
    for bonus in ["blow mold","string lights","animatronic","trick or treat","yard decor"]:
        if bonus in joined:
            score += 0.2
    # ekstra minik katkı
    score += max(0, theme_hits - 1) * 0.2
    return score, theme_hits, type_hits

def passes_filters(phrase: str, root: str, *, min_score: float, min_words: int, max_words: int,
                   require_type: bool, apparel_needs_root: bool):
    raw = phrase.lower()
    for bad in EXACT_DENY_PHRASES:
        if bad in raw:
            return False, ("exact_deny", 0, 0)
    if DENY_PATTERNS.search(raw):
        return False, ("denylist", 0, 0)
    if YEAR_PATTERN.search(raw) and root not in raw:
        return False, ("year_without_root", 0, 0)

    norm, tokens = normalize(raw)
    if len(tokens) < min_words or len(tokens) > max_words:
        return False, ("wordlen", 0, 0)

    score, th, tp = seasonal_score(tokens, root)

    if apparel_needs_root and any(t in APPAREL_TOKENS for t in tokens) and root not in tokens:
        return False, ("need_root_for_apparel", th, tp)

    if require_type and tp == 0:
        return False, ("need_type", th, tp)

    return (score >= min_score), (score, th, tp)

def build_alpha_seeds(root: str, sweep: int) -> List[str]:
    letters = "abcdefghijklmnopqrstuvwxyz"
    if sweep == 1:
        return [f"{root} {c}" for c in letters]
    if sweep == 2:
        return [f"{root} {a}{b}" for a in letters for b in letters]
    return [root]  # fallback (kullanmayacağız ama boş kalmasın)

# ======================
# eBay autosuggest (JSONP uyumlu)
# ======================
AUTOSUG_URL = "https://autosug.ebay.com/autosug"

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")

def parse_jsonp(txt: str) -> dict:
    if "<html" in txt[:300].lower():
        return {"__html__": True}
    m = re.match(r'^[^(]+\((.*)\)\s*$', txt, re.S)
    if m:
        body = m.group(1)
        try:
            return json.loads(body)
        except Exception:
            return {}
    try:
        return json.loads(txt)
    except Exception:
        return {}

def autosuggest(prefix: str, maxresults: int = 60) -> List[str]:
    headers = {
        "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"),
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.ebay.com/",
    }
    params = {"kwd": prefix, "sId": 0, "maxResults": maxresults, "callback": "cb"}
    r = requests.get("https://autosug.ebay.com/autosug", params=params, headers=headers, timeout=12)
    if r.status_code != 200:
        return []
    data = parse_jsonp(r.text)
    if data.get("__html__"):
        return []
    res = data.get("res")
    out: List[str] = []
    # Yeni biçim: {"res":{"sug":[...]}}
    if isinstance(res, dict) and isinstance(res.get("sug"), list):
        out = [s for s in res["sug"] if isinstance(s, str)]
    # Eski biçim: {"res":[{"kwd":"..."}, ...]}
    elif isinstance(res, list):
        out = [(it.get("kwd") or "").strip()
               for it in res if isinstance(it, dict) and it.get("kwd")]
    return [s for s in out if s]

# ======================
# DB helpers
# ======================
def ensure_season_and_get_id(con, season_name: str, root: str) -> int:
    con.execute("""
        INSERT INTO seasons (season_name, root, created_at)
        SELECT ?, ?, CURRENT_TIMESTAMP
        ON CONFLICT (season_name) DO NOTHING
    """, [season_name, root])
    sid = con.execute("SELECT id FROM seasons WHERE season_name = ?", [season_name]).fetchone()
    return int(sid[0])

def insert_phrase(con, season_id: int, seed: str, phrase: str, kept: bool,
                  score: Optional[float], reason: str, source: str = "autosuggest"):
    con.execute("""
        INSERT INTO season_phrases (season_id, seed, phrase, kept, score, reason, source, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
    """, [season_id, seed, phrase, kept, score if score is not None else None, reason, source])

# ======================
# CLI
# ======================
def detect_root_from_season(season_name: str) -> str:
    s = season_name.lower()
    if "halloween" in s:
        return "halloween"
    if "thanksgiving" in s:
        return "thanksgiving"
    # fallback: ilk kelime (ama bizim kullanımda halloween/thanksgiving olacak)
    return s.split("-")[0].split()[0]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", required=True, help="örn: halloween-2025")
    ap.add_argument("--db", default=DEFAULT_DB, help="DuckDB dosya yolu")
    # filtre & davranış
    ap.add_argument("--alpha-sweep", type=int, choices=[1,2], default=1, help="1=a–z, 2=aa–zz")
    ap.add_argument("--min-score", type=float, default=1.5)
    ap.add_argument("--min-words", type=int, default=2)
    ap.add_argument("--max-words", type=int, default=6)
    ap.add_argument("--max-per-seed", type=int, default=999, help="seed başına üst limit (vars: sınırsız)")
    ap.add_argument("--brand-cap", type=int, default=BRAND_CAP_DEFAULT)
    ap.add_argument("--require-type", type=lambda x: str(x).lower()!="false", default=True)
    ap.add_argument("--apparel-needs-root", type=lambda x: str(x).lower()!="false", default=True)
    ap.add_argument("--sleep-ms", type=int, default=90, help="istekler arası bekleme (ms)")

    args = ap.parse_args()
    root = detect_root_from_season(args.season)

    con = duckdb.connect(args.db)
    season_id = ensure_season_and_get_id(con, args.season, root)

    seeds = build_alpha_seeds(root, args.alpha_sweep)
    print(f"🔎 Season: {args.season} (id={season_id})  Root='{root}'  Seeds={len(seeds)}  Market=EBAY_US")

    kept_count = 0
    total_count = 0

    # global brand cap sayacı
    brand_counts = {name: 0 for name in BRAND_REGEXES}

    for seed in seeds:
        suggestions = autosuggest(seed, maxresults=60) or []
        # aynı seed içinde tekrarı at
        seen = set()
        for phr in suggestions:
            if phr in seen:
                continue
            seen.add(phr)
            total_count += 1

            # brand cap kontrolü (global)
            blocked_brand = None
            for name, rgx in BRAND_REGEXES.items():
                if rgx.search(phr):
                    if brand_counts[name] >= args.brand_cap:
                        blocked_brand = name
                        break

            if blocked_brand:
                insert_phrase(con, season_id, seed, phr, False, None, f"brand_cap:{blocked_brand}")
                continue

            ok, info = passes_filters(
                phr, root,
                min_score=args.min_score,
                min_words=args.min_words,
                max_words=args.max_words,
                require_type=args.require_type,
                apparel_needs_root=args.apparel_needs_root
            )

            if ok:
                score, th, tp = info
                insert_phrase(con, season_id, seed, phr, True, float(score), "ok")
                kept_count += 1
                # brand sayacı güncelle
                for name, rgx in BRAND_REGEXES.items():
                    if rgx.search(phr):
                        brand_counts[name] += 1
            else:
                # reason stringini derle
                reason, th, tp = info
                insert_phrase(con, season_id, seed, phr, False, None, reason)

        # nazik ol
        time.sleep(max(0, args.sleep_ms) / 1000.0)

    # Özet
    kept_total = con.execute(
        "SELECT COUNT(*) FROM season_phrases WHERE season_id=?", [season_id]
    ).fetchone()[0]
    kept_true = con.execute(
        "SELECT COUNT(*) FROM season_phrases WHERE season_id=? AND kept=TRUE", [season_id]
    ).fetchone()[0]

    print(f"✅ Ingest tamam: kept (bu çalıştırmada): ~{kept_count}/{total_count}")
    print(f"📦 DB toplam (season_id={season_id}): kept={kept_true} / total={kept_total}")

if __name__ == "__main__":
    main()
