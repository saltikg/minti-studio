import os, re, json, math, time, argparse, sys
from typing import List, Tuple, Dict, Optional, Iterable, Set
from dotenv import load_dotenv
load_dotenv("/home/ubuntu/blog-factory/.env")  # senin projedeki ile aynı

import requests
import duckdb

import csv
EXPORT_DIR = "/home/ubuntu/blog-factory/warehouse/exports"


# ======================
# DuckDB
# ======================
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.getenv("DB_PATH", "/home/ubuntu/blog-factory/warehouse/blog_factory.duckdb")
DEFAULT_DB = DB_PATH  # argparse varsayılanı buradan gelsin

OPENAI_MODEL_DEFAULT = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# =========================
# eBay endpoints (autosuggest + opsiyonel browse)
# =========================
AUTOSUG_URL = "https://autosug.ebay.com/autosug"
BROWSE_URL  = "https://api.ebay.com/buy/browse/v1/item_summary/search"
EBAY_MARKET = os.getenv("EBAY_MARKETPLACE", "EBAY_US")
EBAY_TOKEN  = os.getenv("EBAY_OAUTH_TOKEN", "")

 
 
# =========================
# DuckDB DDL (alan adları birebir aynı)
# =========================
DDL = """
CREATE SEQUENCE IF NOT EXISTS seasons_id_seq START 1;
CREATE SEQUENCE IF NOT EXISTS season_phrases_id_seq START 1;

CREATE TABLE IF NOT EXISTS seasons (
  id           BIGINT PRIMARY KEY DEFAULT nextval('seasons_id_seq'),
  season_name  VARCHAR UNIQUE NOT NULL,
  seeds_json   JSON NOT NULL,
  theme_tokens JSON,
  type_tokens  JSON,
  created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS season_phrases (
  id           BIGINT PRIMARY KEY DEFAULT nextval('season_phrases_id_seq'),
  season_id    BIGINT NOT NULL,
  seed         VARCHAR NOT NULL,
  phrase       VARCHAR NOT NULL,
  kept         BOOLEAN NOT NULL,
  score        DOUBLE,
  theme_hits   INTEGER,
  type_hits    INTEGER,
  drop_reason  VARCHAR,
  created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (season_id, phrase)
);

CREATE INDEX IF NOT EXISTS idx_season_phrases_season ON season_phrases(season_id);
CREATE INDEX IF NOT EXISTS idx_season_phrases_kept   ON season_phrases(season_id, kept);
"""

def get_conn(db_path: str):
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    con = duckdb.connect(db_path)
    con.execute(DDL)
    return con

def upsert_season(con, season_name: str, seeds: List[str]) -> int:
    # 1) Kökten grup çıkarımı
    root = season_root_from_name(season_name).lower()
    watches_brands = {
        'seiko','citizen','casio','rolex','omega','tissot','hamilton','orient','timex',
        'longines','tag','tag-heuer','bulova','rado','garmin','suunto','hublot'
    }

    # watches-* ile başlayanlar ya da marka kökleri "watches" grubuna
    if season_name.startswith('watches-') or root in watches_brands:
        season_group = 'watches'
    else:
        # burada diğer gruplar için mantık ekleyebilirsin (toys, fashion, seasonal, …)
        season_group = 'seasonal'

    # 2) Upsert
    row = con.execute("SELECT id FROM seasons WHERE season_name=?", [season_name]).fetchone()
    if row:
        con.execute(
            "UPDATE seasons SET seeds_json=?, season_group=? WHERE season_name=?",
            [json.dumps(seeds), season_group, season_name]
        )
        sid = row[0]
    else:
        con.execute(
            "INSERT INTO seasons (season_name, seeds_json, season_group) VALUES (?, ?, ?)",
            [season_name, json.dumps(seeds), season_group]
        )
        sid = con.execute("SELECT id FROM seasons WHERE season_name=?", [season_name]).fetchone()[0]
    return sid



def merge_phrases(con, rows: List[Tuple]):
    if not rows:
        return
    con.execute("""CREATE TEMP TABLE tmp_phr (
        season_id BIGINT, seed VARCHAR, phrase VARCHAR, kept BOOLEAN,
        score DOUBLE, theme_hits INT, type_hits INT, drop_reason VARCHAR
    )""")
    con.executemany("INSERT INTO tmp_phr VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
    con.execute("""
        INSERT INTO season_phrases (
            season_id, seed, phrase, kept, score, theme_hits, type_hits, drop_reason
        )
        SELECT
            season_id, seed, phrase, kept, score, theme_hits, type_hits, drop_reason
        FROM tmp_phr
        ON CONFLICT (season_id, phrase) DO UPDATE SET
            seed = EXCLUDED.seed,
            kept = EXCLUDED.kept,
            score = EXCLUDED.score,
            theme_hits = EXCLUDED.theme_hits,
            type_hits = EXCLUDED.type_hits,
            drop_reason = EXCLUDED.drop_reason,
            created_at = now()
    """)
    con.execute("DROP TABLE tmp_phr")

 
# =========================
# Autosuggest + (opsiyonel) Browse total kontrol
# =========================
def _parse_jsonp(txt: str) -> dict:
    if "<html" in txt[:300].lower():
        return {"__html__": True}
    m = re.match(r'^[^(]+\((.*)\)\s*$', txt, re.S)
    if m:
        body = m.group(1)
        try: return json.loads(body)
        except Exception: return {}
    try: return json.loads(txt)
    except Exception: return {}

def autosuggest(prefix: str, maxresults: int = 60) -> List[str]:
    """
    eBay autosuggest için dayanıklı istek:
    - User-Agent ekler
    - JSONP formatını parse eder
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.ebay.com/",
    }
    params = {"kwd": prefix, "sId": 0, "maxResults": maxresults, "callback": "cb"}
    try:
        r = requests.get(AUTOSUG_URL, params=params, headers=headers, timeout=12)
        if r.status_code != 200: return []
        data = _parse_jsonp(r.text)
        if data.get("__html__"): return []
        res = data.get("res")
        out: List[str] = []
        if isinstance(res, dict) and isinstance(res.get("sug"), list):
            out = [s for s in res["sug"] if isinstance(s, str)]
        elif isinstance(res, list):
            out = [(it.get("kwd") or "").strip() for it in res if isinstance(it, dict) and it.get("kwd")]
        return [s for s in out if s]
    except Exception:
        return []
# =========================
# Alpha sweep: root + a..z
# =========================
# --- utils ---
def season_root_from_name(season_name: str) -> str:
    s = season_name.strip().lower()
    s = s.split()[0]
    s = s.split("-")[0]
    return s

def alpha_prefixes(root: str):
    letters = "abcdefghijklmnopqrstuvwxyz"
    return [f"{root} {c}" for c in letters]

def normalize(s: str):
    import re
    s = s.lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s, s.split()

def quick_prefilter(phrases, season_root: str, min_words: int = 3):
    """Sadece: season token içeriyor mu? ve kelime sayısı >= min_words?"""
    kept = []
    dropped = []
    for p in phrases:
        norm, toks = normalize(p)
        if season_root in toks and len(toks) >= min_words:
            kept.append(p)
        else:
            dropped.append(p)  # raporlama istersen kullanırız
    return kept, dropped

def _strip_json_fence(txt: str) -> str:
    t = txt.strip()
    if t.startswith("```"):
        # ```json ... ``` veya ``` ... ```
        t = t.strip("`")
        # 'json\n' prefix’i varsa kırp
        t = re.sub(r'^\s*json\s*\n', '', t, flags=re.I)
    return t.strip()

def _openai_chat(model: str, system_prompt: str, user_prompt: str, temperature: float = 0.2) -> str:
    """
    OpenAI chat çağrısı için sürüm uyumluluğu: önce 1.x (OpenAI client), olmazsa 0.x (openai.ChatCompletion).
    Dönen içerik: assistant message text (string).
    """
    # 1) Önce 1.x deneyelim
    try:
        from openai import OpenAI  # 1.x
        client = OpenAI()
        rsp = client.chat.completions.create(
            model=model,
            temperature=temperature,
            messages=[{"role": "system", "content": system_prompt},
                      {"role": "user", "content": user_prompt}],
        )
        return (rsp.choices[0].message.content or "").strip()
    except Exception:
        pass

    # 2) 0.x fallback
    try:
        import openai  # 0.x
        if not getattr(openai, "api_key", None):
            openai.api_key = os.getenv("OPENAI_API_KEY")  # .env'den geldi
        rsp = openai.ChatCompletion.create(
            model=model,
            temperature=temperature,
            messages=[{"role": "system", "content": system_prompt},
                      {"role": "user", "content": user_prompt}],
        )
        return (rsp["choices"][0]["message"]["content"] or "").strip()
    except Exception as e:
        # En kötü senaryo: boş dön
        return ""


def llm_rank_phrases(phrases, season_root: str, model: str, topk: int, minscore: float):
    import json
    if not phrases:
        return [], []

    sys_prompt = (
        "You are ranking real eBay search queries related to a seasonal shopping theme. "
        "These phrases already come from eBay autosuggest, meaning real users have searched for them. "
        "Estimate which queries are most likely to be searched frequently during this season, "
        "based on human buying intent, cultural relevance, and product diversity. "
        "Focus on market intuition, not strict categories. "
        "Use a scoring system combining intent(0.3), seasonal relevance(0.2), popularity potential(0.3), diversity(0.1), and novelty(0.1). "
        "Return ONLY a JSON array: {phrase, score(0–1), keep(bool), reason}."
    )


    batch_size = 60
    all_items = []
    uniq = sorted(set(phrases))
    for i in range(0, len(uniq), batch_size):
        chunk = uniq[i:i+batch_size]
        user_prompt = "Rank these phrases:\n" + "\n".join(f"- {p}" for p in chunk)
        raw = _openai_chat(model=model, system_prompt=sys_prompt, user_prompt=user_prompt, temperature=0.2)
        text = _strip_json_fence(raw)
        try:
            data = json.loads(text)
            items = data["items"] if isinstance(data, dict) and "items" in data else (data if isinstance(data, list) else [])
        except Exception:
            items = []
        all_items.extend(items)

    selected = [x for x in all_items if x.get("keep") and float(x.get("score",0)) >= float(minscore)]
    selected.sort(key=lambda x: x.get("score",0), reverse=True)
    return selected[:topk], all_items


def consolidate_decisions(season_id: int, chosen_items: list):
    seen = {}
    for it in chosen_items:
        phr = it["phrase"]
        sc  = float(it.get("score", 0) or 0)
        cur = seen.get(phr)
        if cur is None or sc > cur["score"]:
            seen[phr] = {"score": sc}

    rows = []
    for phr, val in seen.items():
        rows.append((season_id, "llm", phr, True, val["score"], 0, 0, None))  # seed='llm'
    return rows

 
 
def export_llm_inputs_csv(season_name: str, candidates: list, out_dir: str = EXPORT_DIR) -> str:
    """
    Prefilter + unique sonrasında LLM'e GİDECEK adayları CSV olarak yazar.
    Columns: season, phrase
    """
    os.makedirs(out_dir, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    safe = re.sub(r"[^a-z0-9_-]+", "-", season_name.lower())
    path = os.path.join(out_dir, f"{safe}-llm-inputs-{ts}.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["season", "phrase"])
        for p in candidates:
            w.writerow([season_name, p])
    print(f"[EXPORT] LLM input candidates -> {path}  (n={len(candidates)})")
    return path


def export_llm_outputs_csv(season_name: str, items: list, out_dir: str = EXPORT_DIR) -> str:
    """
    (Opsiyonel) LLM'den dönen TÜM item'ları (keep true/false) CSV olarak yazar.
    Columns: season, phrase, score, keep, reason
    Bu fonksiyonu kullanmak için llm_rank_phrases'ın all_items döndürmesi gerekir (aşağıda gösterdim).
    """
    os.makedirs(out_dir, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    safe = re.sub(r"[^a-z0-9_-]+", "-", season_name.lower())
    path = os.path.join(out_dir, f"{safe}-llm-outputs-{ts}.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["season", "phrase", "score", "keep", "reason"])
        for it in items:
            w.writerow([season_name, it.get("phrase",""), it.get("score",""),
                        bool(it.get("keep")), it.get("reason") or ""])
    print(f"[EXPORT] LLM all outputs -> {path}  (n={len(items)})")
    return path


# =========================
# Main
# =========================
def main():
    ap = argparse.ArgumentParser(description="Season → eBay autosuggest (prefix alpha-sweep) → DuckDB ingest")
    ap.add_argument("--season", required=True, help="örn: halloween-2025 | thanksgiving-2025")
    ap.add_argument("--db", default=DEFAULT_DB, help="DuckDB path")
    ap.add_argument("--min-words", type=int, default=3)
    ap.add_argument("--llm-rank", action="store_true",
                help="Pre-filter (season token + min 3 words) sonrası LLM ile rerank/top-k seç")
    ap.add_argument("--llm-topk", type=int, default=10)
    ap.add_argument("--llm-minscore", type=float, default=0.65)
    ap.add_argument("--openai-model", default=OPENAI_MODEL_DEFAULT)


    args = ap.parse_args()

    root = season_root_from_name(args.season)
    if root not in ("halloween", "thanksgiving"):
        # gene de istediğin davranış: verilen saison köküyle alpha-sweep
        pass

    prefixes = alpha_prefixes(root)
    # seasons.seeds_json'a yazılacak olan "seed"ler = bu prefix listesi
    con = get_conn(args.db)
    season_id = upsert_season(con, args.season, prefixes)

    print(f"🔎 Season: {args.season} (id={season_id})  Root='{root}'  Seeds={len(prefixes)}  Market={EBAY_MARKET}")

    # 1) autosuggest topla
    per_seed: Dict[str, List[str]] = {}
    for i, pref in enumerate(prefixes, 1):
        try:
            sugs = autosuggest(pref, maxresults=30)
        except Exception as e:
            sugs = []
        per_seed[pref] = sugs
        # hafif yavaşlat (autosuggest'e da nazik olalım)
        time.sleep(0.08)

    # 2) sadece 2 kural ile pre-filter (season token + min words)
    all_candidates = []
    for pref, sugs in per_seed.items():
        kept, _ = quick_prefilter(sugs, root, min_words=args.min_words)  # min_words=3 öneriyorum
        all_candidates.extend(kept)

    # 2.5) unique listeyi çıkar ve CSV'ye yaz (LLM inputları)
    unique_cands = sorted(set(all_candidates))
    export_llm_inputs_csv(args.season, unique_cands)

    # 3) LLM rerank (aktifse); değilse hepsini kept saymak istersen burayı değiştir
    selected = []
    if args.llm_rank and not os.getenv("OPENAI_API_KEY"):
        print("ERROR: OPENAI_API_KEY is not set; cannot use --llm-rank.", file=sys.stderr)
        sys.exit(2)

    if args.llm_rank:
        selected, all_scored = llm_rank_phrases(
            phrases=unique_cands,
            season_root=root,
            model=args.openai_model,
            topk=args.llm_topk,
            minscore=args.llm_minscore
        )
        export_llm_outputs_csv(args.season, all_scored)  # <-- tüm LLM sonuçlarını da CSV'le
    else:
        selected = [{"phrase": p, "score": 0.7, "keep": True, "reason": "prefilter"} for p in unique_cands]

    unique_cands = sorted(set(all_candidates))
    print(f"[DBG] raw_suggests_max=780  prefilter_pass={len(all_candidates)}  unique_after_prefilter={len(unique_cands)}")


    # 4) konsolide edip DB’ye yaz
    rows_to_merge = consolidate_decisions(season_id, selected)
    merge_phrases(con, rows_to_merge)

    # 5) küçük özet
    kept_rows = con.execute("SELECT COUNT(*) FROM season_phrases WHERE season_id=? AND kept=TRUE", [season_id]).fetchone()[0]
    total_rows = con.execute("SELECT COUNT(*) FROM season_phrases WHERE season_id=?", [season_id]).fetchone()[0]
    print(f"✅ Ingest (LLM={'on' if args.llm_rank else 'off'}): kept_total={kept_rows} / total={total_rows}")

    print(f"[DBG] llm_selected={len(selected)}  (topk={args.llm_topk}, minscore={args.llm_minscore})")

    preview = con.execute("""
        SELECT phrase, score FROM season_phrases
        WHERE season_id=? AND kept=TRUE
        ORDER BY score DESC NULLS LAST, phrase
        LIMIT 20
        """, [season_id]).fetchall()
    if preview:
        print("\nTop kept samples:")
        for p, sc in preview:
            try: print(f"  - {p}  (score={sc:.2f})")
            except: print(f"  - {p}")


    
if __name__ == "__main__":
    main()
