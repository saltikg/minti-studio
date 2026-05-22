import json, re
from openai import OpenAI
from .config import OPENAI_API_KEY, OPENAI_MODEL_GPT, MAX_OUT_TOKENS
from .utils import summarize_raw
import tiktoken
import time, uuid
import pandas as pd
from .warehouse_full import connect, ensure_schema

# OpenAI client
client = OpenAI(api_key=OPENAI_API_KEY)

SYSTEM = (
    "You are an expert SEO content writer for e-commerce comparison posts. "
    "Use ONLY the provided product JSON; do not invent price or specs. "
    "Write helpful, human, skimmable Markdown + HTML."
)

# --- Bold helpers for intro ---

TITLE_STOPWORDS = {
    "top", "best", "review", "guide", "you", "need", "to", "try",
    "for", "of", "the", "and", "with", "vs", "comparison",
    "alternative", "alternatives"
}

BENEFIT_KEYWORDS = [
    "affordable", "budget", "value", "portable", "compact", "lightweight",
    "durable", "quiet", "low noise", "waterproof", "wireless",
    "battery life", "long battery", "noise canceling", "anc", "easy to use",
    "comfortable", "fast", "reliable"
]

def _focus_phrase_from_title(title: str) -> str:
    t = re.sub(r"(?i)\b(Top|Best)\s*\d+\b", " ", title)
    t = re.sub(r"[^\w\s\-]", " ", t)
    tokens = [w for w in re.split(r"\s+", t.lower()) if w and w not in TITLE_STOPWORDS]
    phrase = " ".join(tokens).strip()
    return phrase if len(phrase) >= 4 else title.strip()

def _ensure_intro_has_bold(title: str, md_text: str, min_bolds: int = 1) -> str:
    """
    Intro içinde en az `min_bolds` tane **bold** olmasını garanti eder.
    Mevcut bold varsa bırakır; yoksa odak ifadeyi ve bir fayda kelimesini kalın yapar.
    """
    text = md_text or ""
    if text.count("**") >= 2 or ("**" in text and min_bolds <= 1):
        return text

    out = text
    added = 0

    focus = _focus_phrase_from_title(title)
    if focus and not re.search(r"\*\*.+?\*\*", out):
        out, n = re.subn(re.escape(focus), f"**{focus}**", out, count=1, flags=re.I)
        added += n

    if added < min_bolds:
        for kw in BENEFIT_KEYWORDS:
            if re.search(rf"\b{re.escape(kw)}\b", out, flags=re.I):
                out = re.sub(rf"\b{re.escape(kw)}\b", f"**{kw}**", out, count=1, flags=re.I)
                added += 1
                break

    return out


# ==================================================
# Review Summarizer (Pros / Cons)
# ==================================================
def summarize_reviews_with_llm(reviews_5star, reviews_1star, max_reviews=10, debug_canary: bool=False):
    """
    LLM ile müşteri yorumlarını özetler:
    - Kısa paragraf
    - Pros (3 madde)
    - Cons (3 madde)
    - Loved (müşterilerin en sevdiği yönler)
    - Tips (kullanıcı önerileri)
    """
    pros_text = "\n".join(reviews_5star[:max_reviews])
    cons_text = "\n".join(reviews_1star[:max_reviews])

    nonce = str(uuid.uuid4())[:8]  # Canary için kısa ID
    canary_note = (
        f"\n\nDEBUG\n-----\nHidden marker: {nonce}\n"
        "Please include exactly this hidden marker at the very end of your reply as an HTML comment: "
        f"<!--NONCE:{nonce}-->\n"
        "Do not explain the marker."
        if debug_canary else ""
    )

    payload = f"""
You are analyzing customer reviews of a product.

INPUT REVIEWS
-------------
Positive (5-star examples):
{pros_text}

Negative (1-star examples):
{cons_text}

TASK
----
1. Write a short neutral summary paragraph (max 3 sentences).
2. Then write exactly 3 concise bullet points under Pros and 3 under Cons.
3. After that, write 3 short bullets under **What Customers Loved** (direct quotes if possible).
4. Then write 3 short bullets under **Tips from Users** (practical usage tips from reviews).
5. You MUST always include ALL sections in the exact order, even if empty.
6. Use only information found in the reviews. Do not invent details.
7. Each bullet ≤ 20 words, unique, and easy to skim.

OUTPUT FORMAT
-------------
### Customer Insights

<paragraph here>

**Pros**
- bullet
- bullet
- bullet

**Cons**
- bullet
- bullet
- bullet

**ShortSummary**
<one-liner>

**What Customers Loved**
- bullet
- bullet
- bullet

**Tips from Users**
- bullet
- bullet
- bullet
{canary_note}
""".strip()

    print(f"📤 LLM'e gönderiliyor: {len(reviews_5star[:max_reviews])} positive + {len(reviews_1star[:max_reviews])} negative reviews")
    t0 = time.time()

    resp = client.chat.completions.create(
        model=OPENAI_MODEL_GPT,
        messages=[
            {"role": "system", "content": "You are an expert e-commerce analyst summarizing customer reviews."},
            {"role": "user", "content": payload}
        ],
        temperature=0.5,
        max_tokens=500,
    )

    dt = time.time() - t0
    print(f"📥 LLM yanıt süresi: {dt:.2f}s")
    print("🔎 RAW RESP:", resp)

    # Fallback handling
    summary = ""
    try:
        choice = resp.choices[0]
        if hasattr(choice, "message") and choice.message and getattr(choice.message, "content", None):
            summary = choice.message.content.strip()
        elif hasattr(choice, "text") and choice.text:
            summary = choice.text.strip()
    except Exception as e:
        print("⚠️ Yanıt parse edilemedi:", e)

    if not summary:
        print("⚠️ Model boş döndü, fallback olarak boş string kullanılacak.")

    # Canary kontrol
    if debug_canary:
        marker = f"<!--NONCE:{nonce}-->"
        if marker not in summary:
            print("⚠️ Canary marker bulunamadı: Yanıt cache/yerel mi üretildi?")
        else:
            print(f"✅ Canary doğrulandı ({marker})")
        summary = summary.replace(marker, "").strip()

    return summary



def upsert_review_summaries_to_db(df_reviews):
    """
    summarize_reviews_with_llm(...) çıktısını (DataFrame) doğrudan
    DuckDB product_review_summaries tablosuna yazar (DELETE+INSERT).
    Gerekli kolonlar:
      parent_asin, review_paragraph, review_pros, review_cons,
      review_summary_short, review_loved, review_tips
    """
    required = [
        "parent_asin",
        "review_paragraph",
        "review_pros",
        "review_cons",
        "review_summary_short",
        "review_loved",
        "review_tips",
    ]
    missing = [c for c in required if c not in df_reviews.columns]
    if missing:
        raise ValueError(f"Missing columns in LLM output: {missing}")

    df = df_reviews[required].copy().fillna("")

    con = connect()
    ensure_schema(con)

    # pandas DF'yi DuckDB içinde geçici tablo olarak kaydet
    con.register("tmp_reviews", df)

    # upsert = DELETE + INSERT (parent_asin bazlı)
    con.execute("""
        DELETE FROM product_review_summaries
        WHERE parent_asin IN (SELECT parent_asin FROM tmp_reviews)
    """)
    con.execute("""
        INSERT INTO product_review_summaries
        SELECT parent_asin, review_paragraph, review_pros, review_cons,
               review_summary_short, review_loved, review_tips
        FROM tmp_reviews
    """)

    con.unregister("tmp_reviews")
    return len(df)


# ==================================================
# Token counter
# ==================================================
def count_tokens(model: str, text: str) -> int:
    """Verilen metnin token sayısını döner."""
    try:
        enc = tiktoken.encoding_for_model(model)
    except Exception:
        enc = tiktoken.get_encoding("cl100k_base")
    return len(enc.encode(text))

# ==================================================
# Product JSON (LLM input için)
# ==================================================
def _products_json(df):
    df = df.copy()

    keep = ["product_title", "review_paragraph", "review_pros", "review_cons"]
    keep = [c for c in keep if c in df.columns]

    # Pros/cons string olursa listeye çevir
    for col in ("review_pros", "review_cons"):
        if col in df.columns:
            df[col] = df[col].apply(
                lambda x: [s.strip(" -•\t") for s in str(x).split("\n") if s.strip()]
                if pd.notna(x) else []
            )

    return json.dumps(df[keep].to_dict(orient="records"), ensure_ascii=False)[:180000]

# ==================================================
# Blog Writer (LLM ile gövde)
# ==================================================
def write_blog_markdown(*, blog_title: str, post_type: str, intent_category: str,
                        primary_keywords=None, products_df=None, words=1800) -> dict:
    primary_keywords = primary_keywords or []
    products_json = _products_json(products_df)
    # Fallback: if no keywords, extract from title for a cleaner prompt
    main_keyword_for_prompt = ", ".join(primary_keywords) or _focus_phrase_from_title(blog_title)

    payload = f"""
BLOG TITLE: {blog_title}
POST TYPE: {post_type}
INTENT CATEGORY: {intent_category}
PRIMARY KEYWORDS: {', '.join(primary_keywords)}

PRODUCTS (JSON):
{products_json}

TASKS:
1. Write a strong, engaging introduction. It MUST start with a "Quick Take" summary box formatted in Markdown, like this example:
   > **Quick Take:** In this guide to the best {main_keyword_for_prompt}, we found that the **Product A** is the best overall for its balance of features and value, while the **Product B** is a great budget-friendly alternative. We'll break down what makes each one stand out.
   After the summary box, continue with 1-2 more paragraphs of engaging introductory text.
2. Write a “Buyer’s Guide” section with 3–4 helpful subsections.
3. Add an FAQ section with 3–5 common questions + clear answers.
4. Write a concluding evaluation (3–4 paragraphs).
5. End with a short Call-to-Action paragraph (do not add a heading).
Tone: helpful, trustworthy, SEO-optimized, skimmable.

NOTE:
- Do NOT repeat the blog title in your output.
- Bold 2–3 key phrases in the INTRO using **double asterisks** to aid skimming (no overuse).
- Do NOT generate product-by-product summaries (they are handled separately).
- Do NOT create a comparison table (it is generated separately).
- Do NOT output YAML front matter or metadata.
- Return only pure Markdown body text.
"""

    in_tokens = count_tokens(OPENAI_MODEL_GPT, payload)
    print(f"📤 Prompt token count: {in_tokens}")

    resp = client.chat.completions.create(
        model=OPENAI_MODEL_GPT,
        messages=[
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": payload}
        ],
        temperature=0.6,
        max_tokens=MAX_OUT_TOKENS
    )

    text = resp.choices[0].message.content.strip()
    # YAML front-matter kazara geldiyse temizle
    text = re.sub(r"(?s)^\s*---.*?---\s*", "", text).lstrip()

    # ==========================
    # Bölümlere ayır (## veya ### başlıklarla)
    # ==========================
    parts = re.split(r"(?m)^#{2,3}\s+", text)
    intro = parts[0].strip() if parts else ""
    intro = _ensure_intro_has_bold(blog_title, intro, min_bolds=1)

    buyers_guide, faq, conclusion, cta = "", "", "", ""

    for part in parts[1:]:
        lower = part.lower()
        if lower.startswith("buyer"):
            buyers_guide = "## " + part.strip()
        elif lower.startswith("frequently") or lower.startswith("faq"):
            faq = "## " + part.strip()
        elif lower.startswith("conclusion"):
            conclusion = "## Conclusion\n\n" + part.strip().replace("Conclusion", "").strip()
        elif "call to action" in lower or "cta" in lower:
            # CTA genelde heading olmadan gelir, heading ekleme
            cta = part.strip()

    return {
        "intro": intro,
        "buyers_guide": buyers_guide,
        "faq": faq,
        "conclusion": conclusion,
        "cta": cta
    }
