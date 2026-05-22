import duckdb
from pytrends.request import TrendReq
import pandas as pd

# 📌 DuckDB bağlantısı
con = duckdb.connect("/home/ubuntu/blog-factory/warehouse/blog_factory.duckdb")

# 📌 Pytrends init
pytrends = TrendReq(hl="en-US", tz=360)

# 📌 Google Trends kategorilerini al
categories = pytrends.categories()

# Pytrends categories JSON → DataFrame
def flatten_categories(cat_list, parent=None):
    rows = []
    for c in cat_list:
        rows.append((c["id"], c["name"], parent))
        if "children" in c:
            rows.extend(flatten_categories(c["children"], parent=c["id"]))
    return rows

rows = flatten_categories(categories["children"])
df = pd.DataFrame(rows, columns=["category_id", "category_name", "parent_id"])

# 📌 DuckDB tablo oluştur
con.execute("""
CREATE TABLE IF NOT EXISTS trends_categories (
    category_id INTEGER,
    category_name VARCHAR,
    parent_id INTEGER
)
""")

# 📌 Önce eski kayıtları temizle
con.execute("DELETE FROM trends_categories")

# 📌 Yeni kayıtları ekle
con.register("df_view", df)
con.execute("INSERT INTO trends_categories SELECT * FROM df_view")

print(f"✅ {len(df)} categories loaded into trends_categories table.")
