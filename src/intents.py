# src/intents.py
"""
Phase-2 – Intent Pool Generator (LLM + SBERT)

MODES
-----
- DB mode (default): DB'den ürünleri okur, LLM ile fikir üretir, sonucu DB'ye yazar:
    ideas(idea_id, idea_title, category_slug)
    idea_products(idea_id, parent_asin)

- CSV mode (legacy): Phase-1 CSV -> docs/indexes/intent_pool.csv

Examples
--------
# DB mode
python -m src.intents \
  --source db \
  --category electronics \
  --max-products 5 \
  --k 5 --mmr \
  --temp 0.3 \
  --ideas-per-product 3 \
  --resume

# CSV mode (legacy)
python -m src.intents \
  --source csv \
  --csv data/beauty_products_summaries.csv \
  --out docs/indexes/intent_pool.csv \
  --max-products 5 \
  --k 5 --mmr \
  --temp 0.3 \
  --ideas-per-product 3
"""
from __future__ import annotations

import os
import re
import json
import uuid
from dataclasses import dataclass
from typing import List, Dict, Any, Iterable, Optional

import numpy as np
import pandas as pd
from tenacity import retry, wait_exponential, stop_after_attempt

from sentence_transformers import SentenceTransformer
from openai import OpenAI
from dotenv import load_dotenv

import datetime
current_year = datetime.datetime.now().year

# DB
from .warehouse_full import connect, ensure_schema

# -------------------------------------------------
# Config & Env
# -------------------------------------------------
load_dotenv()
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL_GPT = os.getenv("OPENAI_MODEL_GPT", "gpt-4o-mini")
EMBED_MODEL = os.getenv("INTENTS_EMBED_MODEL", "sentence-transformers/all-MiniLM-L6-v2")

# ---- Filtering & title rules (optional, backward-compatible) ----
INTENTS_MIN_SIMILARITY = os.getenv("INTENTS_MIN_SIMILARITY")  # e.g. "0.65"
MIN_SIMILARITY = float(INTENTS_MIN_SIMILARITY) if INTENTS_MIN_SIMILARITY not in (None, "") else None
INTENTS_MIN_COMPETITORS = os.getenv("INTENTS_MIN_COMPETITORS", "0")  # e.g. "1" or "3"
MIN_COMPETITORS = int(INTENTS_MIN_COMPETITORS) if INTENTS_MIN_COMPETITORS.isdigit() else 0
BRANDLESS_TITLES = os.getenv("INTENTS_BRANDLESS_TITLES", "0") == "1"


LLM_SYSTEM = (
    "You are an SEO strategist and affiliate-content copywriter for an Amazon-focused blog. "
    "Your goal is to generate blog title ideas with clear commercial investigation intent "
    "(comparison, roundup, buyer guide, brand, use case, alternatives, care/maintenance), "
    "optimized for searcher intent and CTR, while staying editorially neutral (no pushy sales). "
    "Do not use price or budget language. Return pure JSON only."
)


import datetime
current_year = datetime.datetime.now().year

LLM_USER_TMPL = f"""CENTER PRODUCT
Title: {{title}}
Pros: {{pros}}
Cons: {{cons}}

TASK
Generate EXACTLY {{ideas_n}} SEO-optimized blog title ideas that signal commercial investigation intent.
Cover distinct intents:
- comparison
- roundup/best-of
- brand-focused
- buyer_guide
- use_case
- alternatives
- care_maintenance

STRICT RULES for EACH idea:
- Include the main product keyword near the beginning.
- Title length MUST be 55–65 characters.
- For roundup or comparison ideas, the title MUST include the current year: {current_year}.
- Strengthen buying intent using **non-price** modifiers only, such as audience/skill level (e.g., runners, beginners), use-case (trail, indoor training), features (waterproof, lightweight), environment/season (summer, winter, marathon season), or event/holiday (Black Friday, holidays).
- Keep an editorial, neutral tone (no hard-sell), but maximize clarity and CTR with precise wording (e.g., Best, Top 10, Guide, Review).
- Avoid vague phrases and meaningless tokens.

Return a PURE JSON array of objects with these fields ONLY:
- title (string)
- intent_category (comparison | roundup | brand | buyer_guide | use_case | alternatives | care_maintenance)
- post_type (single | comparison | roundup)
- target_keywords (array of 3–6 concise, lower-case keywords; no price terms)
"""



# Utils
def _slugify(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s[:180]

def _get_first_nonempty(product: pd.Series, keys: Iterable[str], default: str = "") -> str:
    for k in keys:
        if k in product and pd.notna(product[k]) and str(product[k]).strip():
            return str(product[k])
    return default

def _build_text(row: pd.Series) -> str:
    parts = [
        str(row.get("product_title", "")),
        str(row.get("description", "")),
        str(row.get("features", "")),
    ]
    return " ".join(p for p in parts if p and p != "nan")

# -------------------------------------------------
# Embedding & Retrieval
# -------------------------------------------------
@dataclass
class EmbedIndex:
    model_name: str
    embeddings: np.ndarray  # shape (N, d), L2-normalized

    def topk(self, center_idx: int, k: int, exclude_indices: Optional[Iterable[int]] = None) -> List[int]:
        vec = self.embeddings[center_idx]
        sims = self.embeddings @ vec  # cosine if normalized
        if exclude_indices is None:
            exclude_indices = []
        mask = np.ones(len(sims), dtype=bool)
        mask[list(exclude_indices) + [center_idx]] = False
        idxs = np.where(mask)[0]
        top = idxs[np.argsort(sims[idxs])[::-1][:k]]
        return top.tolist()

def build_embed_index(texts: List[str], model_name: str = EMBED_MODEL, normalize: bool = True) -> EmbedIndex:
    model = SentenceTransformer(model_name)
    emb = model.encode(texts, normalize_embeddings=normalize, show_progress_bar=False)
    emb = np.asarray(emb, dtype=np.float32)
    return EmbedIndex(model_name=model_name, embeddings=emb)

def mmr_select(
    emb: np.ndarray,
    center_idx: int,
    candidate_idx: List[int],
    k: int = 5,
    lambda_diversity: float = 0.7,
) -> List[int]:
    if not candidate_idx:
        return []
    selected: List[int] = []
    center_vec = emb[center_idx]
    cand = np.array(candidate_idx, dtype=int)

    sim_to_center = emb[cand] @ center_vec
    pairwise = emb[cand] @ emb[cand].T

    while len(selected) < min(k, len(cand)):
        if not selected:
            i = int(np.argmax(sim_to_center))
            selected.append(i)
            continue
        sel_mask = np.zeros(len(cand), dtype=bool)
        sel_mask[selected] = True
        max_sim_to_sel = pairwise[:, sel_mask].max(axis=1)
        scores = lambda_diversity * sim_to_center - (1 - lambda_diversity) * max_sim_to_sel
        scores[selected] = -1e9
        i = int(np.argmax(scores))
        selected.append(i)

    return cand[selected].tolist()

def top_competitors_sbert(
    df: pd.DataFrame,
    index: EmbedIndex,
    center_idx: int,
    k: int = 5,
    use_mmr: bool = True,
    lambda_diversity: float = 0.7,
    min_similarity: Optional[float] = None,   # <-- YENİ
) -> List[str]:
    N = len(df)
    if N <= 1:
        return []

    center_vec = index.embeddings[center_idx]
    sims = index.embeddings @ center_vec
    sims[center_idx] = -1e9

    # Eğer eşik verildiyse, havuzu önce eşik ile daralt
    if min_similarity is not None:
        eligible = np.where(sims >= min_similarity)[0]
        eligible = eligible[eligible != center_idx]
        if eligible.size == 0:
            return []
        pool_idxs = eligible.tolist()
    else:
        # eski davranış: en benzer geniş havuz
        if use_mmr:
            pool_k = min(N - 1, max(k * 5, 25))
            pool_idxs = np.argsort(sims)[::-1][:pool_k].tolist()
        else:
            # doğrudan top-k
            mask = np.ones(N, dtype=bool)
            mask[center_idx] = False
            idxs = np.where(mask)[0]
            pool_idxs = idxs[np.argsort(sims[idxs])[::-1][:k]].tolist()

    if use_mmr:
        chosen_idx = mmr_select(index.embeddings, center_idx, pool_idxs, k=k, lambda_diversity=lambda_diversity)
    else:
        # MMR yoksa, havuz içinden top-k
        chosen_idx = sorted(pool_idxs, key=lambda i: sims[i], reverse=True)[:k]

    asins = df.iloc[chosen_idx]["parent_asin"].astype(str).tolist()
    return asins

# -------------------------------------------------
# LLM – idea generation
# -------------------------------------------------
_client: Optional[OpenAI] = None
def _get_client() -> OpenAI:
    global _client
    if _client is None:
        if not OPENAI_API_KEY:
            raise RuntimeError("OPENAI_API_KEY is not set.")
        _client = OpenAI(api_key=OPENAI_API_KEY)
    return _client

@retry(wait=wait_exponential(multiplier=1, max=20), stop=stop_after_attempt(5))
def _chat(messages: List[Dict[str, str]], max_tokens: int = 1200, temperature: float = 0.3):
    client = _get_client()
    return client.chat.completions.create(
        model=OPENAI_MODEL_GPT,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
    )

def ideas_for_product(
    product: pd.Series,
    *,
    temperature: float = 0.35,
    ideas_n: int = 10,
    brandless_titles: bool = False,   # <-- YENİ
) -> List[Dict[str, Any]]:
    pros = _get_first_nonempty(product, ["review_pros", "pros_summary", "pros_raw"], default="")
    cons = _get_first_nonempty(product, ["review_cons", "cons_summary", "cons_raw"], default="")

    user_tmpl = LLM_USER_TMPL
    if brandless_titles:
        user_tmpl += (
            "\nExtra rule: Do NOT include brand names in the blog titles. "
            "Always use the generic product type instead."
        )

    prompt = user_tmpl.format(
        title=str(product.get("product_title", ""))[:280],
        pros=str(pros)[:800],
        cons=str(cons)[:800],
        ideas_n=ideas_n,
    )


    resp = _chat(
        [
            {"role": "system", "content": LLM_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        temperature=temperature,
        max_tokens=1200,
    )

    text = resp.choices[0].message.content.strip().strip("`")
    if text.lower().startswith("json"):
        text = text[4:].strip()

    try:
        ideas = json.loads(text)
        norm: List[Dict[str, Any]] = []
        for it in ideas[:ideas_n]:  # güvenlik: fazla gelirse kırp
            norm.append({
                "blog_title": str(it.get("title", "")).strip(),
                "intent_category": str(it.get("intent_category", "")).strip(),
                "target_keywords": ", ".join([str(x) for x in it.get("target_keywords", [])][:6]),
                "post_type": str(it.get("post_type", "")).strip() or "single",
            })
        return [x for x in norm if x["blog_title"]]
    except Exception:
        return []

# -------------------------------------------------
# CSV MODE (legacy)
# -------------------------------------------------
def _embedding_cache_path_from_csv(csv_path: str) -> str:
    base = os.path.splitext(os.path.basename(csv_path))[0]
    return os.path.join("docs", "indexes", "embeddings", f"{base}.npy")

def _read_done_asins_csv(out_csv: str) -> set:
    try:
        prev = pd.read_csv(out_csv)
        return set(prev["parent_asin"].astype(str).tolist())
    except FileNotFoundError:
        return set()

def _append_rows_csv(out_csv: str, rows: List[Dict[str, Any]]):
    if not rows:
        return
    df = pd.DataFrame(rows)
    df["slug"] = (
        df["blog_title"].astype(str)
        .str.lower()
        .str.replace(r"[^a-z0-9]+", "-", regex=True)
        .str.strip("-")
    )
    try:
        prev = pd.read_csv(out_csv)
        merged = pd.concat([prev, df], ignore_index=True)
        merged = merged.drop_duplicates(subset=["slug"])
        os.makedirs(os.path.dirname(out_csv), exist_ok=True)
        merged.to_csv(out_csv, index=False)
    except FileNotFoundError:
        os.makedirs(os.path.dirname(out_csv), exist_ok=True)
        df.to_csv(out_csv, index=False)

def generate_intents_for_csv(
    csv_path: str,
    out_csv: str = "docs/indexes/intent_pool.csv",
    max_products: int = 5,
    k_competitors: int = 5,
    temperature: float = 0.3,
    use_mmr: bool = True,
    lambda_diversity: float = 0.7,
    start_index: int = 0,
    flush_every: int = 10,
    ideas_per_product: int = 10,
) -> pd.DataFrame:
    df = pd.read_csv(csv_path)

    required_cols = ["parent_asin", "product_title", "description", "features"]
    for c in required_cols:
        if c not in df.columns:
            raise ValueError(f"Missing required column: {c}")

    emb_path = _embedding_cache_path_from_csv(csv_path)
    os.makedirs(os.path.dirname(emb_path), exist_ok=True)
    if os.path.exists(emb_path):
        emb = np.load(emb_path)
        if emb.shape[0] != len(df):
            texts = df.apply(_build_text, axis=1).tolist()
            index = build_embed_index(texts, model_name=EMBED_MODEL, normalize=True)
            np.save(emb_path, index.embeddings)
        else:
            index = EmbedIndex(model_name=EMBED_MODEL, embeddings=emb)
    else:
        texts = df.apply(_build_text, axis=1).tolist()
        index = build_embed_index(texts, model_name=EMBED_MODEL, normalize=True)
        np.save(emb_path, index.embeddings)

    done_asins = _read_done_asins_csv(out_csv)
    batch_rows: List[Dict[str, Any]] = []
    processed = 0

    N_total = len(df)
    end_N = N_total if max_products is None or max_products <= 0 else min(max_products, N_total)
    i_start = max(0, start_index)

    for i in range(i_start, end_N):
        product = df.iloc[i]
        center_asin = str(product["parent_asin"]).strip()

        if center_asin in done_asins:
            print(f"[skip] {i+1}/{end_N} {center_asin} already in {out_csv}")
            continue

        print(f"[{i+1}/{end_N}] Generating ideas for ASIN={center_asin} ...", flush=True)

        ideas = ideas_for_product(product, temperature=temperature, ideas_n=ideas_per_product)
        if not ideas:
            print("  -> no ideas returned, continue")
            continue

        comp_asins = top_competitors_sbert(
            df=df, index=index, center_idx=i, k=k_competitors,
            use_mmr=use_mmr, lambda_diversity=lambda_diversity,
        )
        comp_asins_str = ",".join(comp_asins)

        for it in ideas:
            row = {
                "parent_asin": center_asin,
                "blog_title": it["blog_title"],
                "intent_category": it["intent_category"],
                "target_keywords": it["target_keywords"],
                "post_type": it["post_type"],
                "competitor_asins": comp_asins_str,
            }
            row["slug"] = _slugify(row["blog_title"]) or _slugify(center_asin + "-" + str(uuid.uuid4())[:8])
            batch_rows.append(row)

        processed += 1
        if processed % max(1, flush_every) == 0:
            _append_rows_csv(out_csv, batch_rows)
            done_asins.add(center_asin)
            print(f"  -> flushed {len(batch_rows)} rows to {out_csv}")
            batch_rows = []

    if batch_rows:
        _append_rows_csv(out_csv, batch_rows)
        print(f"Final flush: wrote {len(batch_rows)} rows")

    return pd.DataFrame(batch_rows)

# -------------------------------------------------
# DB MODE (read from DB, write to DB)
# -------------------------------------------------
def _embedding_cache_path_from_db(category: str) -> str:
    return os.path.join("docs", "indexes", "embeddings", f"{category}.npy")

def _read_done_asins_db(con, category: str) -> set:
    rows = con.execute("""
        -- An ASIN is "done" if it's the first product in an idea group,
        -- which we treat as the "center" product for that idea.
        WITH ranked_products AS (
            SELECT parent_asin,
                   ROW_NUMBER() OVER(PARTITION BY idea_id ORDER BY ctid) as rn
            FROM idea_products
        )
        SELECT DISTINCT parent_asin FROM ranked_products WHERE rn = 1
    """).fetchall()
    return set(r[0] for r in rows if r)

def _ensure_category(con, slug: str):
    name = slug.replace("_", " ").title()
    con.execute("""
        INSERT INTO categories (slug, name)
        SELECT ?, ? WHERE NOT EXISTS (SELECT 1 FROM categories WHERE slug=?)
    """, [slug, name, slug])

def _insert_idea_db(con, *, idea_id: str, idea_title: str, category_slug: str):
    con.execute("""
        INSERT INTO ideas (idea_id, idea_title, category_slug)
        SELECT ?, ?, ?
        WHERE NOT EXISTS (SELECT 1 FROM ideas WHERE idea_id = ?)
    """, [idea_id, idea_title, category_slug, idea_id])

def _insert_idea_products_db(con, *, idea_id: str, asins: List[str]):
    for a in asins:
        con.execute("""
            INSERT INTO idea_products (idea_id, parent_asin)
            SELECT ?, ?
            WHERE NOT EXISTS (
              SELECT 1 FROM idea_products WHERE idea_id=? AND parent_asin=?
            )
        """, [idea_id, a, idea_id, a])

def _make_unique_idea_id(con, slug: str) -> str:
    base = f"i-{slug}"
    cand = base
    while True:
        row = con.execute("SELECT 1 FROM ideas WHERE idea_id=?", [cand]).fetchone()
        if not row:
            return cand
        cand = f"{base}-{str(uuid.uuid4())[:6]}"

def generate_intents_for_db(
    category: str,
    max_products: int = 5,
    k_competitors: int = 5,
    temperature: float = 0.3,
    use_mmr: bool = True,
    lambda_diversity: float = 0.7,
    start_index: int = 0,
    resume: bool = True,
    ideas_per_product: int = 10,
):
    con = connect()
    ensure_schema(con)
    _ensure_category(con, category)

    # Ürünleri getir: summaries varsa review_pros/cons oradan, yoksa pros_raw/cons_raw
    df_all = con.execute("""
        SELECT
          p.parent_asin,
          p.product_title,
          p.description,
          p.features,
          COALESCE(s.review_pros, '') AS review_pros,
          COALESCE(s.review_cons, '') AS review_cons,
          COALESCE(p.pros_raw, '')   AS pros_raw,
          COALESCE(p.cons_raw, '')   AS cons_raw
        FROM v_products p
        LEFT JOIN product_review_summaries s USING(parent_asin)
        WHERE p.category_slug = ?
    """, [category]).df()

    if df_all.empty:
        print(f"ℹ️ No products found for category={category}")
        return pd.DataFrame()

    # Embedding cache (kategori bazlı)
    emb_path = _embedding_cache_path_from_db(category)
    os.makedirs(os.path.dirname(emb_path), exist_ok=True)
    texts = df_all.apply(_build_text, axis=1).tolist()
    rebuild = True
    if os.path.exists(emb_path):
        try:
            emb = np.load(emb_path)
            if emb.shape[0] == len(texts):
                index = EmbedIndex(model_name=EMBED_MODEL, embeddings=emb)
                rebuild = False
        except Exception:
            rebuild = True
    if rebuild:
        print(f"Rebuilding embedding index for category '{category}'...")
        index = build_embed_index(texts, model_name=EMBED_MODEL, normalize=True)
        np.save(emb_path, index.embeddings)

    done_asins = _read_done_asins_db(con, category) if resume else set()

    df_to_process = df_all.iloc[start_index:].reset_index(drop=True)
    if max_products and max_products > 0:
        df_to_process = df_to_process.head(max_products).copy()

    written = 0
    for i, product in df_to_process.iterrows():
        center_asin = str(product["parent_asin"]).strip()
        if center_asin in done_asins:
            print(f"[skip] {i+1}/{len(df_to_process)} {center_asin} already has ideas in DB")
            continue

        print(f"[{i+1}/{len(df_to_process)}] Generating ideas for ASIN={center_asin} ...", flush=True)

        ideas = ideas_for_product(product, temperature=temperature, ideas_n=ideas_per_product)
        if not ideas:
            print("  -> no ideas returned, continue")
            continue

                # Competitors (SBERT + optional MMR + optional threshold)
        # Find the original index of the product in the full dataframe
        original_idx_series = df_all.index[df_all['parent_asin'] == center_asin]
        center_idx = original_idx_series[0] if not original_idx_series.empty else -1
        comp_asins = top_competitors_sbert(
            df=df_all,
            index=index,
            center_idx=center_idx,
            k=k_competitors,
            use_mmr=use_mmr,
            lambda_diversity=lambda_diversity,
            min_similarity=MIN_SIMILARITY,   # <-- YENİ: ENV ile eşik uygula (yoksa None)
        )

        # Eğer minimum rakip şartı sağlanmıyorsa: bu ürünü atla (LLM'e gitme, DB'ye yazma)
        if MIN_COMPETITORS > 0 and len(comp_asins) < MIN_COMPETITORS:
            print(f"  -> skipped: not enough similar competitors "
                  f"(found={len(comp_asins)}, min_required={MIN_COMPETITORS}, "
                  f"min_sim={MIN_SIMILARITY})")
            continue

        asins_full = [center_asin] + [a for a in comp_asins if a != center_asin]

        # LLM fikirleri (markasız başlık opsiyonu ENV ile)
        for it in ideas_for_product(product, temperature=temperature, ideas_n=ideas_per_product,
                                    brandless_titles=BRANDLESS_TITLES):   # <-- YENİ
            title = it["blog_title"]
            slug = _slugify(title) or _slugify(center_asin + "-" + str(uuid.uuid4())[:8])
            idea_id = _make_unique_idea_id(con, slug)

            _insert_idea_db(con, idea_id=idea_id, idea_title=title, category_slug=category)
            _insert_idea_products_db(con, idea_id=idea_id, asins=asins_full)
            written += 1

        print(f"  -> wrote {len(ideas)} ideas to DB")

    print(f"✅ DB mode finished. Total ideas written: {written}")
    return pd.DataFrame()

# -------------------------------------------------
# CLI
# -------------------------------------------------
def main():
    import argparse
    parser = argparse.ArgumentParser(description="Generate intent pool (DB or CSV)")
    parser.add_argument("--source", choices=["db", "csv"], default="db")

    # Common knobs
    parser.add_argument("--max-products", type=int, default=5)
    parser.add_argument("--k", type=int, default=5, help="Competitors per idea")
    parser.add_argument("--temp", type=float, default=0.3)
    parser.add_argument("--mmr", action="store_true", help="Enable MMR diversification")
    parser.add_argument("--no-mmr", dest="mmr", action="store_false")
    parser.set_defaults(mmr=True)
    parser.add_argument("--lambda-div", type=float, default=0.7)
    parser.add_argument("--start-index", type=int, default=0)
    parser.add_argument("--ideas-per-product", type=int, default=10,
                        help="Her ürün için üretilecek fikir sayısı")

    # DB mode
    parser.add_argument("--category", help="[db] category_slug (e.g., electronics)")
    parser.add_argument("--resume", action="store_true", help="[db] skip ASINs that already have ideas")

    # CSV mode
    parser.add_argument("--csv", help="[csv] Phase-1 CSV path")
    parser.add_argument("--out", default="docs/indexes/intent_pool.csv", help="[csv] output CSV")

    args = parser.parse_args()

    if args.source == "db":
        if not args.category:
            raise SystemExit("❌ --category is required in DB mode.")
        generate_intents_for_db(
            category=args.category,
            max_products=args.max_products,
            k_competitors=args.k,
            temperature=args.temp,
            use_mmr=args.mmr,
            lambda_diversity=args.lambda_div,
            start_index=args.start_index,
            resume=args.resume,
            ideas_per_product=args.ideas_per_product,
        )
    else:
        if not args.csv:
            raise SystemExit("❌ --csv is required in CSV mode.")
        generate_intents_for_csv(
            csv_path=args.csv,
            out_csv=args.out,
            max_products=args.max_products,
            k_competitors=args.k,
            temperature=args.temp,
            use_mmr=args.mmr,
            lambda_diversity=args.lambda_div,
            start_index=args.start_index,
            flush_every=10,
            ideas_per_product=args.ideas_per_product,
        )

if __name__ == "__main__":
    main()
