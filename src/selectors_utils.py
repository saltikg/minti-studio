# src/selectors.py
import re
import numpy as np
import pandas as pd

# Semantik için sklearn (TF-IDF + cosine). Yüklü değilse hafif bir fallback yapar.
try:
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import linear_kernel
    _SKLEARN_OK = True
except Exception:
    _SKLEARN_OK = False

def _norm(s):
    if not isinstance(s, str): return ""
    s = s.lower()
    s = re.sub(r"[^a-z0-9\s\-]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def _row_text(row: pd.Series):
    # Ürün hakkında arama yapılacak birleşik metin
    fields = [
        row.get("product_title",""),
        row.get("description",""),
        row.get("features",""),
        row.get("categories",""),
        row.get("pros_summary",""),
        row.get("cons_summary",""),
        # 🔥 önemlisi: ham review metinleri de dahil
        row.get("pros_raw",""),
        row.get("cons_raw",""),
    ]
    # string olmayanları eliyoruz
    parts = []
    for x in fields:
        if isinstance(x, str):
            parts.append(x)
        elif isinstance(x, (int, float)):
            parts.append(str(x))
    return _norm(" ".join(parts))

def _ensure_columns(df: pd.DataFrame):
    need = ["product_title","avg_rating","n_reviews","price","parent_asin"]
    for c in need:
        if c not in df.columns:
            df[c] = None
    return df

def _brand_in_title(row: pd.Series, brand: str):
    return _norm(brand) in _norm(row.get("product_title",""))

# ----- Geleneksel seçiciler (kalsın) -----
def select_use_keywords(df: pd.DataFrame, kw: list[str], top_k=7, min_hits=2):
    df = _ensure_columns(df).copy()
    # eskisi: saf keyword eşleşmesi
    def _score_keywords(row: pd.Series, kws: list[str]):
        txt = _row_text(row)
        hits = 0
        for kw in kws:
            if not kw: 
                continue
            k = _norm(kw)
            if f" {k} " in f" {txt} " or k in txt:
                hits += 1
        return hits

    df["kw_hits"] = df.apply(lambda r: _score_keywords(r, kw), axis=1)
    cand = df[df["kw_hits"] >= min_hits].copy()
    if cand.empty:
        cand = df[df["kw_hits"] >= 1].copy()
    cand["score"] = cand["kw_hits"]*3.0 + cand["avg_rating"].fillna(0)*1.5 + (cand["n_reviews"].fillna(0).clip(upper=5000)/1000.0)
    return cand.sort_values("score", ascending=False).head(top_k)

def select_brand(df: pd.DataFrame, brand: str, top_k=7):
    df = _ensure_columns(df).copy()
    df["brand_hit"] = df.apply(lambda r: _brand_in_title(r, brand), axis=1)
    cand = df[df["brand_hit"]].copy()
    if cand.empty:
        return df.sort_values(["avg_rating","n_reviews"], ascending=[False,False]).head(top_k)
    cand["score"] = cand["avg_rating"].fillna(0)*2.0 + (cand["n_reviews"].fillna(0).clip(upper=5000)/2000.0)
    return cand.sort_values("score", ascending=False).head(top_k)

def select_affordable(df: pd.DataFrame, top_k=7):
    df = _ensure_columns(df).copy()
    def _to_float(x):
        try:
            if isinstance(x, str):
                x = x.replace("$","").replace(",","").strip()
            return float(x)
        except: return None
    df["price_num"] = df["price"].apply(_to_float)
    cand = df.dropna(subset=["price_num"]).copy()
    cand["score"] = -cand["price_num"] + cand["avg_rating"].fillna(0)*2.0
    return cand.sort_values("score", ascending=False).head(top_k)

def select_top_rated(df: pd.DataFrame, min_rating=4.3, top_k=10):
    df = _ensure_columns(df).copy()
    cand = df[df["avg_rating"].fillna(0) >= min_rating].copy()
    cand["score"] = cand["avg_rating"].fillna(0)*2.0 + (cand["n_reviews"].fillna(0).clip(upper=10000)/2000.0)
    return cand.sort_values("score", ascending=False).head(top_k)

# ----- Yeni: Semantik seçici -----
def _build_query_text(title: str, target_keywords: list[str] | None) -> str:
    title = title or ""
    kws = []
    if target_keywords:
        kws.extend([k.strip() for k in target_keywords if isinstance(k, str) and k.strip()])
    # "best ... for ..." pattern'ından use-case çıkarmaya çalış
    m = re.search(r"best\s+(.*?)\s+for\s+(.*)", title, flags=re.I)
    if m:
        kws.extend([m.group(1), m.group(2)])
    # query = title + keywords birleşimi
    q = " ".join([title] + kws)
    return _norm(q)

def select_semantic(df: pd.DataFrame, title: str, target_keywords: list[str] | None, top_k=7, min_sim=0.05):
    """
    TF-IDF + cosine similarity ile semantik benzerlik seçimi.
    pros_raw / cons_raw dahil geniş korpustan çalışır.
    """
    df = _ensure_columns(df).copy()

    # corpus hazırla
    if "corpus_text" not in df.columns:
        df["corpus_text"] = df.apply(_row_text, axis=1)

    query_text = _build_query_text(title, target_keywords)
    if not _SKLEARN_OK:
        # sklearn yoksa hafif fallback: keyword seçici
        return select_use_keywords(df, kw=query_text.split(), top_k=top_k, min_hits=2)

    # TF-IDF (unigram + bigram), stop_words='english' Amazon verisinde genelde işe yarar
    vec = TfidfVectorizer(ngram_range=(1,2), stop_words="english", max_df=0.9, min_df=2)
    X = vec.fit_transform(df["corpus_text"])
    qv = vec.transform([query_text])
    sims = linear_kernel(qv, X).ravel()

    cand = df.copy()
    cand["semantic_sim"] = sims

    # küçük bir eşik uygula (çok alakasızları ayıklamak için)
    cand = cand[cand["semantic_sim"] >= min_sim].copy()
    if cand.empty:
        # yine de tamamen boş kalmasın
        cand = df.copy()

    # Başlık/kategori tam eşleşme bonusu (outlier'ları azaltır)
    # query kelimelerinden biri title veya categories'te geçiyorsa küçük bir boost
    tokens = set(query_text.split())
    def _title_cat_boost(row):
        txt = _norm(str(row.get("product_title","")) + " " + str(row.get("categories","")))
        return 0.5 if any(t in txt for t in tokens) else 0.0
    cand["tc_boost"] = cand.apply(_title_cat_boost, axis=1)

    # Nihai skor: semantik + kalite sinyali
    cand["score"] = (
        cand["semantic_sim"].fillna(0.0) * 3.0 +
        cand["tc_boost"] +
        cand["avg_rating"].fillna(0.0) * 1.2 +
        (cand["n_reviews"].fillna(0.0).clip(upper=8000) / 4000.0)
    )

    cand = cand.sort_values("score", ascending=False)
    return cand.head(top_k)
