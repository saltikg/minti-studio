# src/review_summarizer.py
import os
import re
import pandas as pd
from tqdm import tqdm
from dotenv import load_dotenv
import argparse

from .writer import summarize_reviews_with_llm, upsert_review_summaries_to_db  # LLM + DB upsert
from .warehouse_full import connect, ensure_schema  # DB

# .env dosyasından OPENAI_API_KEY yükle (CSV modu için eski davranış)
load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# ---- Varsayılan CSV yolları (eski davranış) ----
INPUT_CSV = "data/beauty_products_summaries.csv"
OUTPUT_CSV = "data/beauty_products_summaries_with_reviews.csv"


# =========================================================
# Helpers (CSV ve DB modunda ortak)
# =========================================================
def _split_reviews(cell):
    """
    pros_raw/cons_raw alanlarını listelere ayırır.
    Desteklenen ayraçlar: `|`, `||`, yeni satır(lar).
    """
    if pd.isna(cell) or not str(cell).strip():
        return []
    parts = re.split(r"\s*\|\s*|\n{2,}|\r?\n", str(cell))
    return [p.strip(' "\'') for p in parts if p and p.strip()]


def _first_sentence(text: str, max_chars: int = 200) -> str:
    """Basit, güvenli tek cümle çıkarımı (LLM düşerse yedek)."""
    t = (text or "").strip()
    if not t:
        return ""
    m = re.split(r"(?<=[\.\!\?])\s+", t)
    one = m[0] if m else t
    one = re.sub(r"\s+", " ", one).strip()
    if len(one) > max_chars:
        one = one[: max_chars - 1].rstrip() + "…"
    return one


def parse_summary(summary: str):
    """
    LLM çıktısını parçalar: paragraf, pros, cons, short, loved, tips.
    Beklenen format:
      ### Customer Insights
      <para>

      **Pros**
      - ...
      **Cons**
      - ...
      **ShortSummary**
      <one-liner>

      **What Customers Loved**
      - ...
      **Tips from Users**
      - ...
    """
    text = (summary or "").strip()

    # Modelin eksik girdi uyarılarını filtrele
    guard_phrases = [
        "seems like your message may have been cut off",
        "provide the customer reviews",
        "could you please provide",
    ]
    if any(p in text.lower() for p in guard_phrases):
        return "", "", "", "", "", ""

    para, pros, cons, short, loved, tips = "", "", "", "", "", ""

    # Paragraph (Pros öncesi kısım)
    m_para = re.split(r"\*\*Pros\*\*", text, flags=re.IGNORECASE)
    if m_para:
        para = m_para[0]
        para = para.replace("### Customer Insights", "")
        para = para.strip()

    # Pros
    m_pros = re.search(r"\*\*Pros\*\*(.*?)(\*\*Cons\*\*|$)", text, flags=re.I | re.S)
    if m_pros:
        pros = m_pros.group(1).strip()

    # Cons
    m_cons = re.search(r"\*\*Cons\*\*(.*?)(\*\*ShortSummary\*\*|$)", text, flags=re.I | re.S)
    if m_cons:
        cons = m_cons.group(1).strip()

    # ShortSummary
    m_short = re.search(r"\*\*ShortSummary\*\*(.*?)(\*\*What Customers Loved\*\*|$)", text, flags=re.I | re.S)
    if m_short:
        short = m_short.group(1).strip()
    if not short:
        short = _first_sentence(para, max_chars=200)

    # Loved
    m_loved = re.search(r"\*\*What Customers Loved\*\*(.*?)(\*\*Tips from Users\*\*|$)", text, flags=re.I | re.S)
    if m_loved:
        loved = m_loved.group(1).strip()

    # Tips
    m_tips = re.search(r"\*\*Tips from Users\*\*(.*)$", text, flags=re.I | re.S)
    if m_tips:
        tips = m_tips.group(1).strip()

    # Kozmetik temizlik
    def _clean_block(s: str) -> str:
        s = (s or "").strip()
        s = re.sub(r"\n{3,}", "\n\n", s)
        return s

    return (
        _clean_block(para),
        _clean_block(pros),
        _clean_block(cons),
        _clean_block(short),
        _clean_block(loved),
        _clean_block(tips),
    )


# =========================================================
# MODE 1 — CSV: Eski davranış (CSV → with_reviews.csv)
# =========================================================
def run_csv_mode(n_limit=None, resume=False, input_csv=INPUT_CSV, output_csv=OUTPUT_CSV):
    if not os.getenv("OPENAI_API_KEY"):
        raise ValueError("❌ OPENAI_API_KEY .env dosyasında bulunamadı.")

    df = pd.read_csv(input_csv)

    if os.path.exists(output_csv):
        df_out = pd.read_csv(output_csv, dtype=str)
        print(f"ℹ️ Resuming from existing file: {output_csv}")
        for col in ("review_paragraph", "review_pros", "review_cons", "review_summary_short",
                    "review_loved", "review_tips"):
            if col not in df_out.columns:
                df_out[col] = ""
    else:
        df_out = df.copy()
        for col in ("review_paragraph", "review_pros", "review_cons", "review_summary_short",
                    "review_loved", "review_tips"):
            df_out[col] = ""
        df_out = df_out.astype({c: "string" for c in (
            "review_paragraph", "review_pros", "review_cons",
            "review_summary_short", "review_loved", "review_tips"
        )})

    total, processed, skipped = len(df_out), 0, 0

    for idx, row in tqdm(df_out.iterrows(), total=total):
        if n_limit and processed >= n_limit:
            break

        asin = row["parent_asin"]
        already_done = (
            pd.notna(row.get("review_paragraph")) and str(row["review_paragraph"]).strip() != ""
        ) or (
            pd.notna(row.get("review_summary_short")) and str(row["review_summary_short"]).strip() != ""
        )
        if resume and already_done:
            skipped += 1
            continue

        pros_raw = _split_reviews(row.get("pros_raw", ""))
        cons_raw = _split_reviews(row.get("cons_raw", ""))

        if not pros_raw and not cons_raw:
            skipped += 1
            continue

        print(f"▶️ CSV mode: {idx+1}/{total} ASIN={asin} (pros={len(pros_raw)}, cons={len(cons_raw)})")
        summary = summarize_reviews_with_llm(pros_raw, cons_raw, max_reviews=10)
        para, pros, cons, short, loved, tips = parse_summary(summary)

        df_out.at[idx, "review_paragraph"] = str(para)
        df_out.at[idx, "review_pros"] = str(pros)
        df_out.at[idx, "review_cons"] = str(cons)
        df_out.at[idx, "review_summary_short"] = str(short)
        df_out.at[idx, "review_loved"] = str(loved)
        df_out.at[idx, "review_tips"] = str(tips)

        df_out.to_csv(output_csv, index=False)
        processed += 1

    print(f"✅ Done! Saved to {output_csv}")
    print(f"ℹ️ New processed: {processed}, Skipped: {skipped}, Total rows: {total}")


# =========================================================
# MODE 2 — DB: pros_raw/cons_raw → LLM → product_review_summaries (direct)
# =========================================================
def run_db_mode(category: str, limit: int = None, wipe_first: bool = False):
    """
    v_products'tan (pros_raw/cons_raw) alır, LLM ile özet çıkarır, doğrudan
    product_review_summaries tablosuna yazar. CSV üretmez.
    """
    con = connect()
    ensure_schema(con)

    if wipe_first:
        con.execute("""
            DELETE FROM product_review_summaries
            WHERE parent_asin IN (
                SELECT parent_asin FROM products WHERE category_slug=?
            )
        """, [category])
        print(f"🧹 Wiped existing summaries for category={category}")

    # Özetlenmemiş (ve pros/cons dolu) ürünleri çek
    q = """
        SELECT parent_asin, product_title, pros_raw, cons_raw
        FROM v_products
        WHERE category_slug = ?
          AND (COALESCE(pros_raw, '') <> '' OR COALESCE(cons_raw, '') <> '')
          AND parent_asin NOT IN (SELECT parent_asin FROM product_review_summaries)
        ORDER BY random()
    """
    params = [category]
    if limit and limit > 0:
        q += " LIMIT ?"
        params.append(limit)

    df_in = con.execute(q, params).df()
    print(f"⚡ DB mode: {len(df_in)} products to summarize (category={category}, limit={limit or 'ALL'})")

    if df_in.empty:
        print("ℹ️ No products to process.")
        return

    out_rows = []
    for _, row in tqdm(df_in.iterrows(), total=len(df_in), desc="Summarizing"):
        asin = row["parent_asin"]
        pros = _split_reviews(row.get("pros_raw", ""))
        cons = _split_reviews(row.get("cons_raw", ""))

        if not pros and not cons:
            continue

        summary = summarize_reviews_with_llm(pros, cons, max_reviews=10)
        para, pros_s, cons_s, short, loved, tips = parse_summary(summary)

        out_rows.append({
            "parent_asin": asin,
            "review_paragraph": str(para),
            "review_pros": str(pros_s),
            "review_cons": str(cons_s),
            "review_summary_short": str(short),
            "review_loved": str(loved),
            "review_tips": str(tips),
        })

        # İstersen burada küçük batch'lerle yazabilirsin
        if len(out_rows) >= 50:
            df_batch = pd.DataFrame(out_rows)
            upsert_review_summaries_to_db(df_batch)
            out_rows = []

    # Kalanları yaz
    if out_rows:
        df_batch = pd.DataFrame(out_rows)
        upsert_review_summaries_to_db(df_batch)

    # Kontrol
    cnt = con.execute("""
        SELECT COUNT(*) FROM product_review_summaries
        WHERE parent_asin IN (SELECT parent_asin FROM products WHERE category_slug=?)
    """, [category]).fetchone()[0]
    print(f"✅ DB mode finished. product_review_summaries rows for {category}: {cnt}")


# =========================================================
# CLI
# =========================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["csv", "db"], default="csv",
                    help="csv: eski davranış (CSV → _with_reviews.csv); db: LLM özetlerini doğrudan DB'ye yaz")
    ap.add_argument("--limit", type=int, default=None, help="Kaç ürün özetlenecek (None=hepsi)")
    ap.add_argument("--resume", action="store_true",
                    help="[csv modunda] Zaten işlenmiş ürünleri atla (kaldığı yerden devam)")
    ap.add_argument("--input-csv", default=INPUT_CSV, help="[csv modunda] giriş CSV yolu")
    ap.add_argument("--output-csv", default=OUTPUT_CSV, help="[csv modunda] çıkış CSV yolu")
    ap.add_argument("--category", default=None, help="[db modunda] category_slug (ör. 'electronics')")
    ap.add_argument("--wipe-first", action="store_true", help="[db modunda] kategorideki mevcut özetleri sil")

    args = ap.parse_args()

    if args.mode == "csv":
        # Eski akış (CSV → with_reviews.csv)
        run_csv_mode(n_limit=args.limit, resume=args.resume,
                     input_csv=args.input_csv, output_csv=args.output_csv)
    else:
        # DB akışı (pros/cons → LLM → product_review_summaries)
        if not args.category:
            raise SystemExit("❌ --category zorunludur (db modunda).")
        run_db_mode(category=args.category, limit=args.limit, wipe_first=args.wipe_first)


if __name__ == "__main__":
    main()
