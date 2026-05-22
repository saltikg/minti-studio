import pandas as pd
from .warehouse_full import connect

# ========= CSV tabanlı (legacy) =========
def load_products(csv_path: str) -> pd.DataFrame:
    """
    Legacy: Ürünleri CSV'den yükler (intent_pool.csv akışı için).
    """
    df = pd.read_csv(csv_path)
    if "parent_asin" not in df.columns:
        if "asin" in df.columns:
            df = df.rename(columns={"asin": "parent_asin"})
        else:
            raise ValueError("products CSV must contain 'parent_asin' column")
    if "amazon_link" not in df.columns:
        df["amazon_link"] = df["parent_asin"].astype(str).apply(lambda a: f"https://www.amazon.com/dp/{a}")
    return df

def subset_by_asins(df: pd.DataFrame, asins):
    """
    Legacy: Verilen DataFrame içinde ASIN filtrelemesi yapar.
    """
    asins = [str(a).strip() for a in asins if str(a).strip()]
    if not asins:
        return df.iloc[0:0].copy()
    sub = df[df["parent_asin"].astype(str).isin(asins)].copy()
    if "amazon_link" not in sub.columns:
        sub["amazon_link"] = sub["parent_asin"].astype(str).apply(lambda a: f"https://www.amazon.com/dp/{a}")
    return sub

# ========= DB tabanlı (yeni) =========
def load_products_db() -> pd.DataFrame:
    """
    DB: v_products görünümünden tüm ürünleri (review alanları dahil) çeker.
    """
    con = connect()
    df = con.execute("SELECT * FROM v_products").df()
    if "amazon_link" not in df.columns:
        df["amazon_link"] = df["parent_asin"].astype(str).apply(lambda a: f"https://www.amazon.com/dp/{a}")
    return df

def subset_by_asins_db(asins):
    """
    DB: v_products içinden yalnızca verilen ASIN'leri döndürür.
    Review kolonları da gelir (review_paragraph, review_loved, review_tips, review_pros, review_cons).
    """
    asins = [str(a).strip() for a in asins if str(a).strip()]
    if not asins:
        return pd.DataFrame()

    con = connect()
    placeholders = ",".join(["?"] * len(asins))
    query = f"SELECT * FROM v_products WHERE parent_asin IN ({placeholders})"
    df = con.execute(query, asins).df()

    if "amazon_link" not in df.columns:
        df["amazon_link"] = df["parent_asin"].astype(str).apply(lambda a: f"https://www.amazon.com/dp/{a}")
    return df
