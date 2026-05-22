# src/clean_competitors_db.py
from __future__ import annotations
import os, re, argparse
from typing import List, Dict, Tuple, Optional
import numpy as np
import pandas as pd
from tqdm import tqdm
from sentence_transformers import SentenceTransformer

from .warehouse_full import connect, ensure_schema

# =========================
# Config / Defaults
# =========================
EMBED_MODEL = os.getenv("INTENTS_EMBED_MODEL", "sentence-transformers/all-MiniLM-L6-v2")

DEFAULT_MIN_SIM = 0.35
DEFAULT_HIGH_SIM = 0.65
DEFAULT_MIN_JACCARD = 0.04
DEFAULT_KEEP_TOP_K = None          # 🚨 artık default = None
DEFAULT_EXCL_SAME_BRAND = True
DEFAULT_REQUIRE_ANCHOR = True
DEFAULT_REQUIRE_SAME_CAT = True
DEFAULT_BRAND_DIVERSITY = True

STOPWORDS = {
    "the","and","a","an","of","for","to","in","on","with","by","from","is","are","be",
    "this","that","it","its","as","at","or","your","you","i","we","they","their","our",
    "set","kit","new","best","top","pro","professional","premium","plus","pack","pcs",
    "piece","pieces","size","sizes","gift","women","men","girls","boys","color","colors"
}

def _norm_text(s: str) -> str:
    return "" if str(s or "").lower() == "nan" else str(s or "")

def _build_text(row: pd.Series) -> str:
    return " ".join(_norm_text(row.get(c,"")) for c in ["product_title","description","features"])

def _tokenize(text: str) -> List[str]:
    text = (text or "").lower()
    toks = re.split(r"[^a-z0-9]+", text)
    out = []
    for t in toks:
        if not t or t in STOPWORDS or t.isdigit():
            continue
        t = re.sub(r"(ing|ed|es|s)$", "", t)
        if len(t) >= 3 and t not in STOPWORDS:
            out.append(t)
    return out

def _jaccard(a: List[str], b: List[str]) -> float:
    return 0.0 if not a or not b else len(set(a) & set(b)) / len(set(a) | set(b))

def _anchor_tokens(tokens: List[str], top_n: int = 8) -> List[str]:
    freq: Dict[str, int] = {}
    for t in tokens: freq[t] = freq.get(t,0)+1
    return [w for w,_ in sorted(freq.items(), key=lambda kv:(kv[1], len(kv[0])), reverse=True)[:top_n]]

def _fetch_ideas_bundle(con, category: Optional[str], idea_id: Optional[str], limit: Optional[int]) -> pd.DataFrame:
    where, params = [], []
    if category:
        where.append("i.category_slug = ?"); params.append(category)
    if idea_id:
        where.append("i.idea_id = ?"); params.append(idea_id)
    wsql = "WHERE " + " AND ".join(where) if where else ""
    lim = f"LIMIT {int(limit)}" if limit and not idea_id else ""

    q = f"""
    SELECT
      i.idea_id, i.idea_title, i.category_slug,
      ip.parent_asin, vp.product_title, vp.description, vp.features,
      COALESCE(vp.brand,'') AS brand,
      COALESCE(vp.category_slug,'') AS prod_category
    FROM ideas i
    JOIN idea_products ip USING(idea_id)
    JOIN v_products vp ON vp.parent_asin = ip.parent_asin
    {wsql}
    ORDER BY i.created_at DESC, i.idea_id
    {lim}
    """
    return con.execute(q, params).df()

def _decide_center_for_idea(df_one_idea: pd.DataFrame, model: SentenceTransformer) -> str:
    idea_title = str(df_one_idea.iloc[0]["idea_title"])
    prod_texts = df_one_idea.apply(_build_text, axis=1).tolist()
    emb = np.asarray(model.encode([idea_title]+prod_texts, normalize_embeddings=True, show_progress_bar=False), dtype=np.float32)
    sims = emb[1:] @ emb[0]
    return str(df_one_idea.iloc[int(np.argmax(sims))]["parent_asin"])

def _clean_for_idea(df_prod: pd.DataFrame, model: SentenceTransformer,
                    center_asin: str,
                    min_sim=DEFAULT_MIN_SIM,
                    high_sim=DEFAULT_HIGH_SIM,
                    min_jaccard=DEFAULT_MIN_JACCARD,
                    keep_top_k: Optional[int]=DEFAULT_KEEP_TOP_K,
                    exclude_same_brand=DEFAULT_EXCL_SAME_BRAND,
                    require_anchor=DEFAULT_REQUIRE_ANCHOR,
                    require_same_category=DEFAULT_REQUIRE_SAME_CAT,
                    brand_diversity=DEFAULT_BRAND_DIVERSITY) -> Tuple[List[str], List[Tuple]]:
    df = df_prod.copy().reset_index(drop=True)
    df["__text"] = df.apply(_build_text, axis=1)
    df["__tokens"] = df["__text"].apply(_tokenize)

    asins = df["parent_asin"].astype(str).tolist()
    if center_asin not in asins: return [], [("WARN", f"center_not_found:{center_asin}")]
    emb = np.asarray(model.encode(df["__text"].tolist(), normalize_embeddings=True, show_progress_bar=False), dtype=np.float32)

    c_idx = asins.index(center_asin)
    c_vec, c_tokens = emb[c_idx], df.iloc[c_idx]["__tokens"]
    c_brand, c_cat = str(df.iloc[c_idx].get("brand","")).lower(), str(df.iloc[c_idx].get("prod_category","")).lower()
    anchors = set(_anchor_tokens(c_tokens))

    pool, debug_rows = [], []
    for i,row in df.iterrows():
        a = str(row["parent_asin"])
        if a == center_asin: continue
        sim = float(np.dot(c_vec, emb[i]))
        jac = _jaccard(c_tokens, row["__tokens"])
        has_anchor = bool(anchors & set(row["__tokens"]))
        a_brand, a_cat = str(row.get("brand","")).lower(), str(row.get("prod_category","")).lower()

        if require_same_category and a_cat and c_cat and (a_cat != c_cat): continue
        if sim < min_sim: continue
        if require_anchor and not has_anchor and (sim < high_sim) and (jac < min_jaccard): continue

        score = sim*0.85 + jac*0.15
        pool.append((a, score, a_brand))

    pool.sort(key=lambda x: x[1], reverse=True)

    selected, used_brands = [], {}
    for a,sc,b in pool:
        if exclude_same_brand and b == c_brand and b != "": continue
        if brand_diversity and b and used_brands.get(b,0)>0: continue
        selected.append(a); used_brands[b]=used_brands.get(b,0)+1

    # Eğer keep_top_k verilmişse kısıtla
    if keep_top_k and keep_top_k>0:
        selected = selected[:keep_top_k]

    # Populate debug rows with ALL candidates and their final decision
    all_candidates = {p[0]: p for p in pool} # ASIN -> (asin, score, brand)
    for i, row in df.iterrows():
        a = str(row["parent_asin"])
        if a == center_asin: continue
        # Find the original scores for this candidate
        sim = float(np.dot(c_vec, emb[i]))
        jac = _jaccard(c_tokens, row["__tokens"])
        score = all_candidates.get(a, (None, 0, None))[1]
        decision = "KEEP" if a in selected else "REMOVE"
        debug_rows.append((center_asin, a, sim, jac, score, decision))

    return selected, debug_rows

def clean_competitors_db(category: Optional[str], idea_id: Optional[str], limit: Optional[int],
                         min_sim=DEFAULT_MIN_SIM, high_sim=DEFAULT_HIGH_SIM, min_jaccard=DEFAULT_MIN_JACCARD,
                         keep_top_k: Optional[int]=DEFAULT_KEEP_TOP_K,
                         exclude_same_brand=DEFAULT_EXCL_SAME_BRAND, require_anchor=DEFAULT_REQUIRE_ANCHOR,
                         require_same_category=DEFAULT_REQUIRE_SAME_CAT, brand_diversity=DEFAULT_BRAND_DIVERSITY,
                         debug_csv: Optional[str]=None, dry_run=False,
                         min_competitors_to_keep: int = 1,
):

    con = connect(); ensure_schema(con)
    df = _fetch_ideas_bundle(con, category, idea_id, limit)
    if df.empty: return print("ℹ️ No ideas found.")

    # Load the model ONCE here
    print("🚀 Loading embedding model...")
    local_path = "/home/ubuntu/blog-factory/models/all-MiniLM-L6-v2"
    model = SentenceTransformer(local_path)
    print("✅ Model loaded.")

    groups = df.groupby("idea_id", sort=False)
    dbg_rows_all=[]; total_before=0; total_after=0; ideas_deleted=0
    for idea,g in tqdm(groups,desc="Cleaning ideas"):
        center_asin = _decide_center_for_idea(g, model)
        before = len(g); total_before+=max(0,before-1)
        selected, dbg = _clean_for_idea(g, model, center_asin, keep_top_k=keep_top_k)
        dbg_rows_all.extend([(idea,) + row for row in dbg])
        if dry_run:
            total_after += len(selected)
            continue
        con.execute("BEGIN")
        try:
            # This is the logic you added previously. It should now work correctly.
            if len(selected) < min_competitors_to_keep:
                print(f"  -> Marking idea '{idea}' as FAILED (found {len(selected)} competitors, need {min_competitors_to_keep})")
                # Update status if exists
                con.execute("""
                    UPDATE blog_posts SET status = 'failed', updated_at = now() WHERE idea_id = ?
                """, [idea])
                # Insert status if not exists (UPSERT)
                con.execute("""
                    INSERT INTO blog_posts (idea_id, status, updated_at)
                    SELECT ?, 'failed', now()
                    WHERE NOT EXISTS (SELECT 1 FROM blog_posts WHERE idea_id = ?)
                """, [idea, idea])

                ideas_deleted += 1
            else:
                total_after += len(selected)
                con.execute("DELETE FROM idea_products WHERE idea_id=? AND parent_asin<>?", [idea, center_asin])
                con.execute("DELETE FROM idea_products WHERE idea_id=? AND parent_asin<>?", [idea, center_asin]) # Only delete competitors
                if selected:
                    insert_data = [(idea, asin) for asin in selected]
                    con.executemany("INSERT INTO idea_products (idea_id, parent_asin) VALUES (?, ?) ON CONFLICT DO NOTHING", insert_data)
            con.execute("COMMIT")
        except Exception as e:
            con.execute("ROLLBACK"); raise e
    print(f"✅ Competitors cleaned | Before: {total_before}, After: {total_after}, Removed: {total_before-total_after}")
    if ideas_deleted > 0:
        print(f"🗑️ Ideas deleted due to insufficient competitors: {ideas_deleted}")

    if debug_csv and dbg_rows_all:
        pd.DataFrame(dbg_rows_all,columns=["idea_id","center_asin","candidate_asin","sim","jaccard","score","decision"]).to_csv(debug_csv,index=False)
        print(f"📝 debug written: {debug_csv}")

def main():
    ap=argparse.ArgumentParser(description="Filter competitors in DB (idea_products)")
    ap.add_argument("--category"); ap.add_argument("--idea-id"); ap.add_argument("--limit",type=int)
    ap.add_argument("--keep-top-k",type=int,default=None,help="Opsiyonel, verilmezse sınırsız (sadece uygunlar)")
    ap.add_argument("--min-competitors-to-keep", type=int, default=1, help="Minimum number of competitors for an idea to be kept.")
    ap.add_argument("--debug-csv"); ap.add_argument("--dry-run",action="store_true")
    args=ap.parse_args()
    clean_competitors_db(category=args.category,idea_id=args.idea_id,limit=args.limit,
                         keep_top_k=args.keep_top_k,debug_csv=args.debug_csv,dry_run=args.dry_run,
                         min_competitors_to_keep=args.min_competitors_to_keep)

if __name__=="__main__": main()
