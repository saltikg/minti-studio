import os, subprocess, shlex, time, json, re, sys
from datetime import datetime
from typing import List, Optional
from dotenv import load_dotenv
from openai import OpenAI
from app.db import connect_ro, connect_rw

import time, contextlib



# --- ENV / Paths ---
load_dotenv("/home/ubuntu/blog-factory/.env")  # gerekirse yolunu değiştirin

BASE_URL    = os.getenv("BASE_URL", "https://mintiproduct.com")
PYTHON_BIN  = os.getenv("PYTHON_BIN", sys.executable) # Use the current python interpreter by default
INGEST_PATH = os.getenv("INGEST_PATH", "/home/ubuntu/blog-factory/src/ebay/2-ebay_products_ingest.py")
TRENDING_CATEGORY_SLUG = os.getenv("TRENDING_CATEGORY_SLUG", "trending-now")

# --- OpenAI (optional) ---
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL   = os.getenv("OPENAI_MODEL_GPT", "gpt-4o-mini")
client = OpenAI(api_key=OPENAI_API_KEY)  # veya OpenAI() -> ENV kullanır

# ---- DB helpers ----

def db_ro():
    return connect_ro()


def ensure_category_row(con, slug: str, parent_slug: str | None = None, sort_order: int = 0):
    """
    categories_tree tablosunda slug yoksa kök (veya verilen parent altında) bir satır oluşturur.
    """
    name = slug.replace("-", " ").strip().title() or slug

    # categories_tree genelde: slug, name, parent_slug, sort_order, nav_visible
    con.execute("""
        INSERT INTO categories_tree (slug, name, parent_slug, sort_order, nav_visible)
        SELECT ?,    ?,    ?,           ?,           TRUE
        WHERE NOT EXISTS (
            SELECT 1 FROM categories_tree WHERE slug = ?
        )
    """, [slug, name, parent_slug, sort_order, slug])



@contextlib.contextmanager
def db_writable(retries: int = 8, backoff_sec: float = 1.0, lock_timeout: int = 60):
    con = connect_rw()
    try:
        yield con
        con.commit()
    finally:
        con.close()
# ------------------------------------------

 

# ==== FAQ structured-output helpers =========================================
def _json_repair(s: str) -> str:
    """Code fence / akıllı tırnak / tek→çift tırnak gibi basit onarımlar."""
    if not s:
        return s
    s = s.strip()
    # ```json ... ``` ayıkla
    if s.startswith("```"):
        s = s.split("```", 1)[-1]
        if s.endswith("```"):
            s = s[:-3]
        s = s.strip()
    # akıllı tırnakları düzelt
    s = (s.replace("’", "'").replace("‘","'")
           .replace("“", '"').replace("”", '"').replace("`","'"))
    # JSON gibi başlıyorsa ve tek tırnak yoğun ise, kaba dönüşüm yap
    if s and s[0] in "[{" and ('"' not in s) and ("'" in s):
        s = s.replace("'", '"')
    return s

def _validate_faq_payload(obj) -> list | None:
    """
    Beklenen format:
      {"faq":[{"question":"...?", "answer":"..."}, ...]}
    Dönen: faq list (list of dict) ya da None.
    """
    try:
        if not isinstance(obj, dict): return None
        faq = obj.get("faq")
        if not isinstance(faq, list) or not faq: return None
        clean = []
        for it in faq:
            if not isinstance(it, dict): continue
            q = str(it.get("question") or "").strip()
            a = str(it.get("answer") or "").strip()
            if len(q) < 3 or len(a) < 1: continue
            if not q.endswith("?"): q = q.rstrip(".") + "?"
            clean.append({"question": q, "answer": a})
        return clean if clean else None
    except Exception:
        return None

def ensure_faq_column(con):
    """blog_contents’ta faq_json kolonu yoksa ekle (duckdb)."""
    exists = con.execute("""
        SELECT 1
        FROM information_schema.columns
        WHERE table_name = 'blog_contents' AND column_name = 'faq_json'
        LIMIT 1
    """).fetchone()
    if not exists:
        con.execute("ALTER TABLE blog_contents ADD COLUMN faq_json VARCHAR")

def generate_faq_json(topic: str, desc: str) -> list | None:
    """
    LLM'den sadece JSON (schema: {faq:[{question,answer}]}) üretir,
    JSON'u doğrular/onarır, valid değilse None döner.
    """
    if not client or not OPENAI_API_KEY:
        return None

    system = (
        "You produce FAQs for a product-trend blog. "
        "Return ONLY valid JSON object with key 'faq' as an array of {question,answer}. "
        "No markdown, no prose."
    )
    user = f"""
Trend topic: "{topic}"
Context: "{desc}"

Rules:
- 5–8 items.
- Each item must have "question" and "answer".
- Plain text, concise, shopper-friendly.
- End questions with '?'.
Return ONLY a JSON object like:
{{"faq":[{{"question":"...?", "answer":"..."}}, ...]}}
"""

    try:
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.4,
            max_tokens=600,
            response_format={"type": "json_object"},  # <-- JSON'a zorla
        )
        raw = (resp.choices[0].message.content or "").strip()
    except Exception as e:
        log(f"⚠️ FAQ LLM error: {e}")
        return None

    # 1) direkt parse + validate
    try:
        obj = json.loads(raw)
        faq = _validate_faq_payload(obj)
        if faq: return faq
    except Exception:
        pass

    # 2) hafif onarım ve tekrar dene
    repaired = _json_repair(raw)
    try:
        obj2 = json.loads(repaired)
        faq2 = _validate_faq_payload(obj2)
        if faq2: return faq2
    except Exception:
        pass

    # 3) başaramazsa None
    log("⚠️ FAQ JSON validation failed.")
    return None
# ============================================================================



def now_ms() -> int:
    return int(time.time() * 1000)

def log(msg: str):
    print(msg, flush=True)

def fetch_pending_affiliate(con, max_n: int):
        rows = con.execute("""
            SELECT idea_id, trend_id, title, idea_type, category_slug
            FROM trend_ideas
            WHERE idea_type = 'affiliate'
            AND (status IS NULL OR status = '' OR lower(status) IN ('pending', 'in_progress'))
            ORDER BY created_at ASC
            LIMIT ?
        """, [max_n]).fetchall()
        return rows   # ← 🔥 bu satırı ekle




def fetch_trend_context(con, trend_id: int):
    row = con.execute("""
        SELECT trend, COALESCE(description,'')
        FROM trend_topics
        WHERE trend_id = ?
        LIMIT 1
    """, [trend_id]).fetchone()
    if not row:
        return None, ""
    return row[0], row[1] or ""

def llm_keywords(topic: str, title_hint: str, desc: str) -> List[str]:
    """
    Generate 2–3 eBay search keywords based on the trend topic.
    If LLM fails or no result, return an empty list (skip idea).
    """
    if not client:
        log("⚠️ No OpenAI client found. Skipping keyword generation.")
        return []

    prompt = f"""
You are an affiliate marketing analyst for an e-commerce trend website.
Given a trending search term, generate 2–3 short eBay search queries 
that real shoppers might use to find related **products**.

Trend: "{topic or title_hint}"
Context: "{desc}"

Rules:
- Focus on product searches, not abstract ideas or news.
- Always generate buyer-intent keywords (physical items people could purchase).
- Use nouns like: gifts, merch, shirt, gadget, decor, collectible, accessory, gear, tool, costume, etc.
- Avoid vague or conceptual terms.
- Output ONLY a JSON array, no explanation.
Examples:
["pumpkin carving kit", "halloween spooky decor", "pumpkin costume"]
"""


    try:
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.6,
            max_tokens=120,
        )
        txt = (resp.choices[0].message.content or "").strip()
        start, end = txt.find("["), txt.rfind("]")
        if start == -1 or end == -1:
            raise ValueError("Invalid JSON format")
        arr = json.loads(txt[start:end+1])
        arr = [s.strip() for s in arr if isinstance(s, str) and s.strip()]
        if not arr:
            raise ValueError("Empty keyword list")
        return arr[:3]
    except Exception as e:
        log(f"⚠️ LLM keyword generation failed for '{topic}': {e}")
        return []  # no fallback!



def run_ingest(keyword: str, category_slug: str, trend_idea_id: int, price_min: Optional[int], price_max: Optional[int], limit: int, dry_run: bool=False) -> int:
    # ✅ Special mode: Dynamic LLM-based blog generation (deal_mode=3)
    if os.getenv("DEAL_MODE") == "3":
        dynamic_path = "/home/ubuntu/blog-factory/src/ebay/3-ebay_dynamic_ingest.py"
        args = [
            PYTHON_BIN, dynamic_path,
            "--keyword", keyword,
            "--category-slug", category_slug,
            "--limit", str(limit),
            "--price-min", str(price_min or 15),
            "--price-max", str(price_max or 400),
            "--idea-id", str(trend_idea_id) # Pass the original idea_id
        ]
        log(f"🧠 Launching Dynamic Blog (Deal Mode 3): {' '.join(shlex.quote(a) for a in args)}")
        if dry_run:
            log("(Dry run) Would execute dynamic ingest.")
            return 0
        proc = subprocess.run(args, capture_output=True, text=True)
        if proc.stdout:
            log(proc.stdout.strip())
        if proc.stderr:
            log(proc.stderr.strip())
        return proc.returncode


    if dry_run:
        log(f"💡 [DRY-RUN] Would run ingest with keyword='{keyword}' category='{category_slug}' post_tag=trend-{trend_idea_id}")
        return 0

    args = [
        PYTHON_BIN, INGEST_PATH,
        "--deal-mode", "1",
        "--keyword", keyword,
        "--category-slug", category_slug,
        "--limit", str(limit),
        "--unique", "1",
        "--post-tag", f"trend-{trend_idea_id}",
        "--enrich-limit", "30",
    ]
    if price_min is not None:
        args += ["--price-min", str(price_min)]
    if price_max is not None:
        args += ["--price-max", str(price_max)]

    log("▶️  " + " ".join(shlex.quote(a) for a in args))
    proc = subprocess.run(args, capture_output=True, text=True)
    # log outputs for debugging
    if proc.stdout:
        log(proc.stdout.strip())
    if proc.stderr:
        log(proc.stderr.strip())
    return proc.returncode

def find_published_blog(con, trend_idea_id: int):
    # blog_contents.idea_id VARCHAR olabilir; hem string hem int eşlemesini kapsa:
    row = con.execute("""
        SELECT idea_id, title, slug, category_slug, updated_at
        FROM blog_contents
        WHERE idea_id = CAST(? AS VARCHAR)
           OR try_cast(idea_id AS BIGINT) = ?
        ORDER BY updated_at DESC
        LIMIT 1
    """, [trend_idea_id, trend_idea_id]).fetchone()
    return row


def publish_record(con, idea_id: int, blog_slug: str, cat_slug: str):
    pub_id = now_ms()
    if cat_slug:
        url = f"{BASE_URL.rstrip('/')}/{cat_slug.strip('/')}/{blog_slug.strip('/')}/"
    else:
        url = f"{BASE_URL.rstrip('/')}/{blog_slug.strip('/')}/"

    # 1) publish kaydı
    con.execute("""
        INSERT INTO trend_publications (pub_id, idea_id, published_url, published_at)
        VALUES (?, ?, ?, now())
    """, [pub_id, idea_id, url])
    
    # blog_contents.idea_id'yi normalize et (trend-... veya NULL ise numeric yap)
    try:
        con.execute("""
            UPDATE blog_contents
            SET idea_id = ?
            WHERE slug = ?
            AND (idea_id IS NULL OR CAST(idea_id AS VARCHAR) LIKE 'trend-%')
        """, [idea_id, blog_slug])
    except Exception as _e:
        log(f"ℹ️ idea_id normalize skipped: {_e}")


    # 2) Eğer category_slug bir sezon ise seasons tablosundan id’yi bul (yoksa oluştur),
    #    sonra season_phrases’e bağla (phrase=idea_id, kept=TRUE)
    if cat_slug and re.match(r".*-\d{4}$", cat_slug):  # ör: halloween-2025
        row = con.execute("SELECT id FROM seasons WHERE season_name = ? LIMIT 1", [cat_slug]).fetchone()
        if not row:
            # yoksa sezona auto-create (isteğe bağlı)
            con.execute("""
                INSERT INTO seasons (season_name, season_group, created_at)
                VALUES (?, 'seasonal', now())
            """, [cat_slug])
            row = con.execute("SELECT id FROM seasons WHERE season_name = ? LIMIT 1", [cat_slug]).fetchone()

        season_id = row[0]
        # mevcut bağ var mı?
        ex = con.execute("""
            SELECT 1 FROM season_phrases WHERE season_id = ? AND phrase = ? LIMIT 1
        """, [season_id, str(idea_id)]).fetchone()
        if not ex:
            con.execute("""
                INSERT INTO season_phrases (season_id, phrase, seed, kept, created_at)
                VALUES (?, ?, ?, TRUE, now())
            """, [season_id, str(idea_id), f"trend-{idea_id}"])


        # Mark idea as published (avoid reprocessing)
        con.execute("""
            UPDATE trend_ideas
            SET status = 'published', updated_at = now()

            WHERE idea_id = ?
        """, [idea_id])

    return url



def mark_status(con, idea_id: int, status: str):
    con.execute("""
        UPDATE trend_ideas
        SET status = ?, updated_at = now()
        WHERE idea_id = ?
    """, [status, idea_id])

def process_one(row, price_min, price_max, limit, dry_run=False) -> bool:
    """Manages its own database connection lifecycle to avoid lock conflicts."""
    idea_id, trend_id, title, idea_type, category_slug = row
    log(f"\n🟩 Processing idea_id={idea_id} type={idea_type} cat={category_slug}")

    with db_ro() as con:
        topic, desc = fetch_trend_context(con, trend_id)
        if not topic:
            log(f"⚠️ Trend {trend_id} not found. Marking failed.")
            with db_writable() as wcon:
                mark_status(wcon, idea_id, "failed")
            return False

    kws = llm_keywords(topic, title, desc)
    if not kws:
        log(f"⚠️ Could not generate keywords for trend_id={trend_id}. Marking failed.")
        with db_writable() as wcon:
            mark_status(wcon, idea_id, "failed")
        return False


    # Mark the idea as 'in_progress' in a separate, committed transaction
    # before launching any subprocesses to ensure visibility.
    with db_writable() as con:
        mark_status(con, idea_id, "in_progress")
        con.commit()

    log(f"🔑 Keywords: {kws}")

    # ✅ Trend tabanlı fikirler için dynamic LLM blog modu
    if idea_type == "affiliate" and trend_id:
        os.environ["DEAL_MODE"] = "3"
        log("🧠 DEAL_MODE=3 → Using LLM Dynamic Blog Generator (3-ebay_dynamic_ingest.py)")
    else:
        os.environ.pop("DEAL_MODE", None)

    for idx, kw in enumerate(kws, 1):
        rc = run_ingest(kw, category_slug, idea_id, price_min, price_max, limit, dry_run=dry_run)

        # After subprocess, open a fresh connection to check results and update status
      
        with db_writable() as con:
            if rc != 0:
                log(f"⚠️ Ingest returned non-zero (rc={rc}) for kw[{idx}]='{kw}'. Reverting to pending.")
                mark_status(con, idea_id, "pending")
                continue

            if dry_run:
                return True

            row_bc = find_published_blog(con, idea_id)
            if row_bc:
                _bc_idea, _bc_title, bc_slug, bc_cat, _bc_updated = row_bc
                final_cat = TRENDING_CATEGORY_SLUG
                url = publish_record(con, idea_id, bc_slug, final_cat)
                log(f"✅ Published → {url}")

                # --- AUTHOR LINK (by slug) ---------------------------------------
                try:
                    # kategoriyi categories tablosunda garanti et (özellikle trending-now için)
                    ensure_category_row(con, final_cat)

                    # slug'a göre yazar seç (tercihen en son güncellenen)
                    row_author = con.execute("""
                        SELECT author_id
                        FROM authors
                        WHERE primary_category_slug = ?
                        ORDER BY created_at DESC NULLS LAST
                        LIMIT 1
                    """, [final_cat]).fetchone()


                    if row_author:
                        author_id = row_author[0]
                        # blog_posts satırına yazar bağla (idea_id ile)
                        # blog_posts'ta slug yok; slug blog_contents'ta.
                        # Slug ile bc'ye bağlanıp idea_id eşitliğinden bp'yi güncelliyoruz.
                        con.execute("""
                            UPDATE blog_posts AS bp
                            SET author_id = ?
                            FROM blog_contents AS bc
                            WHERE bc.slug = ?
                            AND CAST(bp.idea_id AS VARCHAR) = CAST(bc.idea_id AS VARCHAR)
                        """, [author_id, bc_slug])


                        log(f"👤 Linked author '{author_id}' to post (idea_id={idea_id}).")

                        # İsteğe bağlı: authors.primary_category boşsa, categories_tree.name ile doldur
                        con.execute("""
                            UPDATE authors a
                            SET primary_category = ct.name
                            FROM categories_tree ct
                            WHERE a.primary_category IS NULL
                            AND a.primary_category_slug = ct.slug
                            AND a.author_id = ?
                        """, [author_id])
                    else:
                        log("ℹ️ No author found for this category slug; skipping author link.")
                except Exception as e:
                    log(f"⚠️ Author link error: {e}")
                # ---------------------------------------------------------------


                # --- FAQ üret ve kaydet ---
                try:
                    ensure_faq_column(con)
                    # küçük bir okuma daha lazım; ister aynı con ile çağır (tablo içinde), ister ayrı ro aç
                    topic2, desc2 = fetch_trend_context(con, trend_id)
                    faq_list = generate_faq_json(topic2 or _bc_title or "", desc2 or "")
                    if faq_list:
                        con.execute(
                            "UPDATE blog_contents SET faq_json = ? WHERE slug = ?",
                            [json.dumps({"faq": faq_list}, ensure_ascii=False), bc_slug]
                        )
                        log(f"🧩 FAQ (json) saved for slug={bc_slug} ({len(faq_list)} items)")
                    else:
                        log("ℹ️ FAQ generation skipped or failed.")
                except Exception as e:
                    log(f"⚠️ FAQ save error: {e}")
                # --- /FAQ ---

                return True
            else:
                log(f"ℹ️ No matching blog found after kw[{idx}]='{kw}'. Trying next...")


    log("❌ Failed after all keywords.")
    
    return False




def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-per-run", type=int, default=1)
    ap.add_argument("--price-min", type=int, default=15)
    ap.add_argument("--price-max", type=int, default=400)
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--dry-run", type=int, default=0)
    ap.add_argument("--fallback-keyword", default=None)
    args = ap.parse_args()

    # We open and close connections inside the loop to avoid holding a lock
    rows = []
    with db_ro() as con:
        rows = fetch_pending_affiliate(con, args.max_per_run)
        if not rows:
            log("ℹ️ No pending affiliate ideas.")
            return

    # optional override: prepend fallback keyword if provided
    global llm_keywords
    if args.fallback_keyword:
        _orig_llm = llm_keywords
        def _with_fallback(topic, title_hint, desc):
            arr = _orig_llm(topic, title_hint, desc)
            return [args.fallback_keyword] + [k for k in arr if k != args.fallback_keyword]
        llm_keywords = _with_fallback  # type: ignore

    for r in rows:
        try:
            process_one(r, args.price_min, args.price_max, args.limit, dry_run=bool(args.dry_run))
        except Exception as e:
            log(f"💥 Exception for idea_id={r[0]}: {e}")
            try:
                with db_writable() as error_con:
                    mark_status(error_con, r[0], "failed")
            except Exception as e2:
                log(f"⚠️ Could not mark failed due to lock: {e2}")


if __name__ == "__main__":
    main()
