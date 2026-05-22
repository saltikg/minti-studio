import duckdb
import pandas as pd
from pytrends.request import TrendReq
from tabulate import tabulate

# 📌 DuckDB bağlantısı
con = duckdb.connect("/home/ubuntu/blog-factory/warehouse/blog_factory.duckdb", read_only=True) 

df_ideas = con.execute("""
    SELECT idea_id, idea_title, category_slug
    FROM ideas
    LIMIT 5;
""").df()

# 📌 Test için target_keywords ekleyelim
manual_keywords = [
    "nose studs, alternative piercings",
    "eyelashes, faux eyelashes, real eyelashes",
    "tea tree oil, lemon oil benefits",
    "hair accessories for kids",
    "premium brushes, makeup brushes"
]
df_ideas["target_keywords"] = manual_keywords

# 📌 Pytrends init
pytrends = TrendReq(hl="en-US", tz=360)

# 📌 Kategori eşleme → Google Trends category ID
category_map = {
    "electronics": 5,
    "beauty": 44,
    "health": 45,
    "games": 8,
    "shopping": 18
}

# --- Trend skor hesapla
def get_trend_score(keyword, cat_id=0):
    try:
        #pytrends.build_payload([keyword], cat=cat_id, timeframe="today 12-m", geo="US")
        pytrends.build_payload([keyword], cat=cat_id, timeframe="now 7-d", geo="US")   # son 7 gün
        #pytrends.build_payload([keyword], cat=cat_id, timeframe="now 1-d", geo="US")   # son 1 gün
        
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
    target_keywords = row["target_keywords"].split(",")
    category_slug = row["category_slug"]

    main_kw = target_keywords[0].strip()
    cat_id = category_map.get(category_slug, 0)

    score = get_trend_score(main_kw, cat_id)

    # Eğer skor düşükse fallback çalıştır
    if score < 5:
        try:
            pytrends.build_payload([main_kw], cat=cat_id, timeframe="today 12-m", geo="US")
            related = pytrends.related_queries()
            if main_kw in related and related[main_kw]["top"] is not None:
                candidates = related[main_kw]["top"]["query"].head(3).tolist()
                best_kw, best_score = None, 0
                for cand in candidates:
                    s = get_trend_score(cand, cat_id)
                    if s > best_score:
                        best_kw, best_score = cand, s
                if best_score >= 5:
                    return pd.Series([rewrite_title(idea_title, best_kw), priority_label(best_score), best_score])
        except Exception:
            pass

    return pd.Series([rewrite_title(idea_title, main_kw), priority_label(score), score])

# 📌 Uygula
df_ideas[["optimized_title", "priority", "trend_score"]] = df_ideas.apply(optimize_title, axis=1)

#print(df_ideas[["idea_title", "target_keywords", "optimized_title", "priority", "trend_score"]])
#pd.set_option("display.max_columns", None)
#pd.set_option("display.width", 2000)
#print(df_ideas[["idea_title", "target_keywords", "optimized_title", "priority", "trend_score"]])
# Kısaltılmış tablo
for _, row in df_ideas.iterrows():
    print(f"Original: {row['idea_title']}")
    print(f"Optimized: {row['optimized_title']}")
    print(f"Priority: {row['priority']} | Trend Score: {row['trend_score']}")
    print("-" * 80)


