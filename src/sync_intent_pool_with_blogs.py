import os
import pandas as pd
import re

BASE_DIR = "/home/ubuntu/blog-factory"
BLOGS_DIR = os.path.join(BASE_DIR, "docs", "blogs")
POOL_CSV = os.path.join(BASE_DIR, "docs", "indexes", "intent_pool.cleaned.csv")


def normalize_slug(s):
    s = str(s).strip().lower()
    s = s.replace(".md", "")
    s = re.sub(r"[^a-z0-9]+", "-", s)   # sadece harf/rakam, diğer her şeyi tire yap
    s = re.sub(r"-+", "-", s)           # birden fazla tire → tek tire
    return s.strip("-")

def compare_blogs_with_csv():
    # 1. Blog dosyaları
    blog_files = [f for f in os.listdir(BLOGS_DIR) if f.endswith(".md")]
    blog_slugs = set(normalize_slug(f) for f in blog_files)
    print(f"📂 Blog dosyaları bulundu: {len(blog_slugs)}")

    # 2. CSV yükle
    df = pd.read_csv(POOL_CSV)
    df["slug_norm"] = df["slug"].apply(normalize_slug)
    csv_slugs = set(df["slug_norm"])
    csv_with_count = set(df.loc[df["generated_count"].fillna(0).astype(int) > 0, "slug_norm"])

    # 3. Karşılaştırma
    only_in_blogs = blog_slugs - csv_slugs
    only_in_csv = csv_slugs - blog_slugs
    matched = blog_slugs & csv_slugs
    matched_with_count = matched & csv_with_count

    # 4. Özet tablo
    print("\n=== ÖZET ===")
    print(f"CSV toplam satır: {len(df)}")
    print(f"CSV 'generated_count > 0': {len(csv_with_count)}")
    print(f"Blog klasöründe .md dosyası: {len(blog_slugs)}")
    print(f"Eşleşen slug sayısı: {len(matched)}")
    print(f"Eşleşen ve 'generated_count > 0' olan: {len(matched_with_count)}")
    print(f"Sadece bloglarda var ama CSV’de yok: {len(only_in_blogs)}")
    print(f"Sadece CSV’de var ama bloglarda yok: {len(only_in_csv)}")

    # 5. Detay örnekler
    if only_in_blogs:
        print("\n⚠️ Bloglarda olup CSV’de olmayan ilk 5:", list(only_in_blogs)[:5])
    if only_in_csv:
        print("\n⚠️ CSV’de olup bloglarda olmayan ilk 5:", list(only_in_csv)[:5])
    if matched_with_count:
        print("\n✅ Blog+CSV eşleşen ve count>0 olan ilk 5:", list(matched_with_count)[:5])

if __name__ == "__main__":
    compare_blogs_with_csv()
