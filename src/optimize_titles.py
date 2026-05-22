import duckdb
import pandas as pd
from pytrends.request import TrendReq
from sklearn.feature_extraction.text import CountVectorizer
from sentence_transformers import SentenceTransformer
import numpy as np

# --- SBERT modeli (senin indirdiğin model path)
model = SentenceTransformer("/home/ubuntu/blog-factory/models/all-MiniLM-L6-v2")


# --- DuckDB bağlantısı
con = duckdb.connect("/home/ubuntu/blog-factory/warehouse/blog_factory.duckdb", read_only=True)

# --- Pytrends init
pytrends = TrendReq(hl="en-US", tz=360)

# --- N-gram extractor
def extract_ngrams(text, ngram_range=(2,3), top_k=5):
    vec = CountVectorizer(ngram_range=ngram_range, stop_words="english").fit([text])
    ngrams = vec.get_feature_names_out()
    return ngrams[:top_k]

# --- Dinamik category detect (SBERT + cosine similarity)
def detect_category_id(category_hint, con):
    df_cat = con.execute("SELECT category_id, category_name FROM trends_categories").df()
    
    # encode
    query_vec = model.encode([category_hint])[0]
    cat_vecs = model.encode(df_cat["category_name"].tolist())
    
    # cosine similarity
    sims = np.dot(cat_vecs, query_vec) / (np.linalg.norm(cat_vecs, axis=1) * np.linalg.norm(query_vec))
    best_idx = int(np.argmax(sims))
    
    best_id = int(df_cat.iloc[best_idx]["category_id"])
    best_name = df_cat.iloc[best_idx]["category_name"]
    
    print(f" → Auto-detected category: {best_name} ({best_id}) for hint='{category_hint}'")
    return best_id

# --- Trend skor hesapla
def get_trend_score(keyword, cat_id=0):
    try:
        pytrends.build_payload([keyword], cat=cat_id, timeframe="today 12-m", geo="US")
        data = pytrends.interest_over_time()
        if data.empty:
            return 0
        return int(data[keyword].mean())
    except Exception:
        return 0

# --- Priority label
def priority_label(score):
    if score >= 70:
        return "High Priority"
    elif score >= 20:
        return "Medium Priority"
    else:
        return "Low Priority"

# --- Başlık rewrite
def rewrite_title(idea_title, keyword):
    base = idea_title.split(":")[0]
    return f"Best {keyword} 2025: {base}"

# --- Optimize fonksiyonu
def optimize_title(row):
    idea_title = row["idea_title"]
    category_slug = row["category_slug"]

    print(f"\n🔎 Processing: {idea_title} | Category hint: {category_slug}")
    
    # 1. N-gram çıkar
    ngrams = extract_ngrams(idea_title)
    print(f" → Extracted n-grams: {ngrams}")
    
    # 2. Category detect (slug varsa onu, yoksa title kullan)
    hint = category_slug if category_slug else idea_title
    cat_id = detect_category_id(hint, con)
    
    # 3. En iyi keyword seç
    best_kw, best_score = None, 0
    for kw in ngrams:
        score = get_trend_score(kw, cat_id)
        print(f"    · {kw}: trend_score={score}")
        if score > best_score:
            best_kw, best_score = kw, score

    # 4. Fallback (related queries)
    if best_score < 5 and best_kw:
        print(f" → Low score for {best_kw}, trying related_queries fallback...")
        try:
            pytrends.build_payload([best_kw], cat=cat_id, timeframe="today 12-m", geo="US")
            related = pytrends.related_queries()
            if best_kw in related and related[best_kw]["top"] is not None:
                candidates = related[best_kw]["top"]["query"].head(3).tolist()
                print(f"    · Related candidates: {candidates}")
                for cand in candidates:
                    s = get_trend_score(cand, cat_id)
                    print(f"      - {cand}: trend_score={s}")
                    if s > best_score:
                        best_kw, best_score = cand, s
        except Exception as e:
            print(f"    · Related queries failed: {e}")

    if not best_kw:
        best_kw = ngrams[0]
        best_score = 0

    optimized = rewrite_title(idea_title, best_kw)
    label = priority_label(best_score)

    print(f" ✅ Final Choice: {best_kw} | score={best_score} | {label}")
    print(f" → Optimized Title: {optimized}")

    return pd.Series([optimized, label, best_score])

# --- DB’den örnek başlıkları çek
df_ideas = con.execute("""
    SELECT idea_id, idea_title, category_slug
    FROM ideas
    USING SAMPLE 3;
""").df()

# --- Optimize et
df_ideas[["optimized_title", "priority", "trend_score"]] = df_ideas.apply(optimize_title, axis=1)

print("\n==================== SUMMARY ====================")
print(df_ideas[["idea_title", "category_slug", "optimized_title", "priority", "trend_score"]])
