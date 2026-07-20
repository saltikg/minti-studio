# app/trends.py
import os, json, re
from flask import Blueprint, Response, render_template
from dotenv import load_dotenv


load_dotenv()
BASE_URL = os.getenv("BASE_URL", "https://mintistudio.com").rstrip("/")

trend_bp = Blueprint("trend_bp", __name__, url_prefix="")
from app.db import connect_ro

# routes.py’deki bu iki fonksiyonu KULLANACAĞIZ:
from .epn import build_epn_link, make_custom_id
from .routes import get_categories, _affiliatize_products, fetch_ebay_products, split_intro_lead, split_faq_and_tail, format_buyers_guide, format_conclusion
# ve utils:
from .utils_content import _inject_product_cards_into_text

def _connect_ro():
    return connect_ro()


def _coerce_pythonish_faq(raw: str) -> str:
    """
    Python-list benzeri (JSON olmayan) metni sağlam JSON listesine dönüştürür.
    Örn: ['1. Soru? Cevap...', '2. Soru? Cevap...', 5. Soru? Cevap...]
    Çıktı: [{"question": "...", "answer": "..."}, ...] (JSON string)
    Düzeltilemezse orijinali döner.
    """
    if not raw:
        return raw

    s = raw.strip()
    # Sadece köşeli parantezle başlayan şüpheli içerikleri dene
    if not s.startswith("["):
        return raw

    # normalize quotes
    s = s.replace("’", "'").replace("`", "'")

    # dış köşeli parantezleri temizle
    body = s[1:-1].strip()

    # Madde yakalayıcı:
    #   - başında opsiyonel tırnak
    #   - sayi + .  (örn. 1. , 12. )
    #   - soru işaretine kadar SORU
    #   - soru işaretinden sonra bir sonraki maddenin başına kadar CEVAP
    pat = re.compile(
        r"""(?:^|,\s*)         # madde başı veya önce virgül
            ['"]?             # opsiyonel tırnak
            \s*(\d+)\.\s*     # madde numarası (yakalayıp atacağız)
            (.+?)\?           # soru (soru işaretine kadar, tembel)
            \s*               # boşluklar
            (.*?)             # cevap (tembel)
            (?=(?:,\s*['"]?\s*\d+\.\s|$))  # bir sonraki madde ya da son
        """,
        re.S | re.X,
    )

    items = []
    for m in pat.finditer(body):
        q = (m.group(2) or "").strip()
        a = (m.group(3) or "").strip()
        # sonundaki stray tırnakları temizle
        q = re.sub(r"^['\"]|['\"]$", "", q)
        a = re.sub(r"^['\"]|['\"]$", "", a)
        if q:
            items.append({"question": q + "?", "answer": a})

    if not items:
        # son çare: basit virgül bölme (soru işareti bulunamayanlar)
        fallback = []
        for piece in re.split(r",\s*(?=(?:['\"]?\s*\d+\.\s))", body):
            piece = piece.strip().strip("'").strip('"')
            if not piece:
                continue
            # numara önekini at
            piece = re.sub(r"^\s*\d+\.\s*", "", piece)
            # soru/cevap ayırmayı yine dene
            qm = piece.find("?")
            if qm != -1:
                fallback.append({"question": piece[:qm+1].strip(), "answer": piece[qm+1:].strip()})
            else:
                fallback.append({"question": piece, "answer": ""})
        if fallback:
            return json.dumps(fallback, ensure_ascii=False)

        return raw  # vazgeç

    return json.dumps(items, ensure_ascii=False)



def fetch_row_by_slug(slug: str):
    con = _connect_ro()
    result = con.execute("""
        SELECT
            bc.*,
            bp.date_published,
            a.display_name AS author_name,
            a.avatar_url AS author_avatar_url,
            a.author_bio
        FROM blog_contents bc
        LEFT JOIN blog_posts bp
        ON CAST(bc.idea_id AS VARCHAR) = CAST(bp.idea_id AS VARCHAR)
        LEFT JOIN authors a ON bp.author_id = a.author_id
        WHERE bc.slug = ?
        LIMIT 1
    """, [slug])
    row = result.fetchone()
    cols = [d[0] for d in result.description] if result.description else []
    con.close()
    if not row:
        return None
    return dict(zip(cols, row))

@trend_bp.route("/trending-now/<slug>/", methods=["GET","HEAD"])
def trend_detail(slug):
    row = fetch_row_by_slug(slug)
    if not row or (row.get("category_slug") or "") != "trending-now":
        return Response("Not found", status=404)

    idea_id = row.get("idea_id")
    title   = (row.get("title") or "").strip()

    # ürünleri çek
    ebay_products = fetch_ebay_products(idea_id, limit=15)

    # affiliate etiketleri uygula
    if ebay_products:
        _affiliatize_products(ebay_products, post_slug=slug, season=None, placement="in-article")

    # metin blokları
    intro   = str(row.get("overview_updated") or row.get("introduction") or "").strip()
    gallery = str(row.get("product_gallery") or "").strip()
    urunler = str(row.get("urunler") or "").strip()
    buyers  = str(row.get("buyers_guide") or "").strip()


    val = row.get("faq_json")
    if isinstance(val, (dict, list)):
        faq_raw = json.dumps(val, ensure_ascii=False)
    else:
        faq_raw = (val or "").strip()

    # Eski fallback da kalsın:
    faq_raw = (row.get("faq_json") or "").strip() or str(row.get("faq") or "").strip()


    faq_fixed = _coerce_pythonish_faq(faq_raw) if faq_raw else ""
    faq_html, faq_tail = split_faq_and_tail(faq_fixed if faq_fixed else faq_raw)
    concl   = str(row.get("conclusion") or "").strip()

    # ürün kartlarını metne enjekte et (trend özel davranış)
    if ebay_products:
        # placeholder varsa doldur; yoksa sona ekler
        urunler = _inject_product_cards_into_text(urunler or "", ebay_products)

    # buyers_guide html’ine çevir
    buyers_html = format_buyers_guide(buyers) if buyers else ""
    final_conclusion = concl or faq_tail
    # lead/rest
    lead_intro, rest_intro = split_intro_lead(intro)

    post = {
        "title": title,
        "author_name": row.get("author_name"),
        "author_avatar_url": row.get("author_avatar_url"),
        "author_bio": row.get("author_bio"),
        "date_published": row.get("date_published"),
        "hero_image_url": row.get("hero_image_url") or (ebay_products[0].get("image") if ebay_products else None),
        "hero_alt": row.get("hero_alt") or title,
        "introduction": intro,
        "gallery": gallery,
        "urunler": urunler,                          # kartlar enjekte edilmiş
        "buyers_guide": buyers_html,
        "related_links": json.loads(row["related_links_json"]) if row.get("related_links_json") else None,
        "faq": faq_html if faq_raw else "",
        "conclusion": format_conclusion(final_conclusion) if final_conclusion else "",
        "insights_table": "",                        # istersen trends’te kapalı tut
        "intro_lead": lead_intro,
        "intro_rest": rest_intro,
        "top5_table": "",                            # trends: tablo kapalı
        "auth_guarantee": False,
    }

    cats = get_categories()

    # Trend: grid’i opsiyonel göstermek istersen post_trend.html içinde bakarsın
    return render_template("post_trend.html",
                           categories=cats,
                           post=post,
                           ebay_products=ebay_products)
