import os, json, time, random
from io import BytesIO
from typing import Dict, Any, List, Tuple
from dotenv import load_dotenv
import duckdb
import requests
from PIL import Image, ImageDraw, ImageFont
from openai import OpenAI
from src.trends.instagram_tokens import get_instagram_credentials
 
def now_ms() -> int:
    return int(time.time() * 1000)

# =====================
# ENV / GLOBAL CONFIG
# =====================

load_dotenv("/home/ubuntu/blog-factory/.env")

DB_PATH        = os.getenv("DB_PATH", "/home/ubuntu/blog-factory/warehouse/blog_factory.duckdb")
BASE_URL       = os.getenv("BASE_URL", "https://mintistudio.com")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL   = os.getenv("OPENAI_MODEL_GPT", "gpt-4o-mini")

IG_API_BASE            = os.getenv("IG_API_BASE", "https://graph.facebook.com/v21.0")
IG_BUSINESS_ACCOUNT_ID = os.getenv("IG_BUSINESS_ACCOUNT_ID")
IG_ACCESS_TOKEN        = os.getenv("IG_ACCESS_TOKEN")

_INSTAGRAM_CREDS = get_instagram_credentials()
if _INSTAGRAM_CREDS:
    IG_ACCESS_TOKEN = IG_ACCESS_TOKEN or _INSTAGRAM_CREDS.get("page_access_token")
    IG_BUSINESS_ACCOUNT_ID = IG_BUSINESS_ACCOUNT_ID or _INSTAGRAM_CREDS.get("instagram_business_account_id")

STORY_W, STORY_H = 1080, 1440
OUT_DIR = "/home/ubuntu/blog-factory/instagram_out"

client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

STORY_W, STORY_H = 1080, 1440

OUT_DIR = "/home/ubuntu/blog-factory/instagram_out"

BG_IMAGE_PATH   = os.path.join(OUT_DIR, "int_post_bg1.png")
STAR_ICON_PATH  = os.path.join(OUT_DIR, "star.png")
ARROW_ICON_PATH = os.path.join(OUT_DIR, "arrow.png")

WHITE       = (255, 255, 255)
BLACK       = (0, 0, 0)
 
def log(msg: str):
    print(msg, flush=True)

def now_ms() -> int:
    return int(time.time() * 1000)

def db_ro():
    return duckdb.connect(DB_PATH, read_only=True)

def load_font(path_candidates: List[str], size: int, fallback_default: bool = True):
    for p in path_candidates:
        try:
            if p and os.path.exists(p):
                return ImageFont.truetype(p, size)
        except Exception:
            continue
    if fallback_default:
        return ImageFont.load_default()
    raise RuntimeError("No usable font found")

def measure_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> Tuple[int,int]:
    if hasattr(draw, "textbbox"):
        x0,y0,x1,y1 = draw.textbbox((0,0), text, font=font)
        return (x1-x0, y1-y0)
    if hasattr(draw, "textlength"):
        w = int(draw.textlength(text, font=font))
        ascent, descent = font.getmetrics()
        return (w, ascent+descent)
    if hasattr(draw, "textsize"):
        return draw.textsize(text, font=font)
    mask = font.getmask(text)
    return mask.size


def already_uploaded(con, idea_id: int, content_type: str) -> bool:
    row = con.execute(
        """
        SELECT COUNT(*)
        FROM trend_instagram_posts
        WHERE idea_id = ?
          AND json_valid(caption_json)
          AND json_extract_string(caption_json, '$.status') = 'uploaded'
          AND COALESCE(json_extract_string(caption_json, '$.content_type'), 'post') = ?
        """,
        [idea_id, content_type],
    ).fetchone()
    return bool(row and row[0] and row[0] > 0)



def gen_hashtags(products: list[dict]) -> list[str]:
    base = set(["#trending", "#deals", "#finds", "#shopping"])
    for p in products[:3]:
        title = (p.get("title") or "").lower()
        brand = (p.get("brand") or "").strip().replace(" ", "")
        cat   = (p.get("category_hint") or "").lower()

        if any(k in title for k in ["psa", "rookie", "auto", "patch", "rc", "card"]):
            base |= {"#sportscards", "#tradingcards", "#rookie"}
        if "watch" in cat or "watch" in title:
            base |= {"#watchfam", "#watches"}
        if any(k in title for k in ["tee", "t-shirt", "shirt", "hoodie"]):
            base |= {"#streetwear", "#tshirt"}
        if "costume" in title or "halloween" in title:
            base |= {"#halloween", "#costume"}

        if brand:
            base.add("#" + brand.lower().replace("&", "and"))

    out = [t for t in base if len(t) > 1]
    return out[:10]


def build_carousel_caption(headline: str, products: list[dict]) -> str:
    # kısa ve aksiyon odaklı – linkten hiç bahsetmiyoruz
    lines = [
        headline,
        random.choice([
            "RATE IT 1–10 IN THE COMMENTS 👇 WHICH SLIDE WINS?",
            "PICK A WINNER: DROP 1–10 👇",
            "YOUR SCORE? 1–10 👇 BEST SLIDE?"
        ])
    ]
    tags = gen_hashtags(products)
    if tags:
        lines.append(" ".join(tags))
    return "\n".join(lines)


def rule_based_headline(product: Dict[str, Any]) -> str:
    title = (product.get("title") or "").lower()
    cat   = (product.get("category_hint") or "").lower()

    if any(k in title for k in ["psa", "rookie", "auto", "patch", "rc", "card"]):
        return "SCORE THIS ROOKIE CARD!"
    if "watch" in cat or "watch" in title:
        return "RATE THIS WATCH!"
    if any(k in title for k in ["tee", "t-shirt", "shirt", "hoodie"]):
        return "ROCK THIS GRAPHIC TEE!"
    if any(k in title for k in ["costume", "halloween"]):
        return "RATE THIS COSTUME!"
    return "RATE THIS FIND!"


def llm_make_headline(product: Dict[str, Any], trend_url: str) -> str:
    # önce kural – çoğu durumda yeterli
    rb = rule_based_headline(product)
    if rb != "RATE THIS FIND!":
        return rb

    # kural genel kaldıysa LLM’den kısa bir başlık iste
    default_head = "SHOP THE TREND"
    if client is None or not OPENAI_API_KEY:
        return default_head

    prompt = f"""
You write ONE short hype headline for an Instagram shopping carousel.
Rules:
- ALL CAPS vibe.
- <= 28 characters.
- No emoji, no price, no brand.
Example product: "{product['title']}"
URL: "{trend_url}"
Return ONLY the headline text.
"""
    try:
        resp = client.chat_completions.create(  # if your SDK is older keep .chat.completions.create
            model=OPENAI_MODEL,
            messages=[{"role":"user","content":prompt}],
            temperature=0.4,
            max_tokens=40,
        )
        raw = (resp.choices[0].message.content or "").strip()
        raw = raw.strip('"').strip("'")
        return raw if raw else default_head
    except Exception as e:
        log(f"LLM fallback: {e}")
        return default_head

def draw_slide_badge(img: Image.Image, draw: ImageDraw.ImageDraw, index: int, total: int, is_dark: bool):
    label = f"{index}/{total}"
    font = load_font([
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"
    ], 34)

    w, h = measure_text(draw, label, font)
    pad_x, pad_y = 18, 10
    box_w, box_h = w + pad_x*2, h + pad_y*2

    # sağ-alt
    x = img.width - box_w - 32
    y = img.height - box_h - 32

    # yarı saydam arkaplan
    bg_col = (0,0,0,140) if not is_dark else (255,255,255,140)
    badge = Image.new("RGBA", (box_w, box_h), (0,0,0,0))
    ImageDraw.Draw(badge).rounded_rectangle([0,0,box_w,box_h], radius=16, fill=bg_col)
    img.paste(badge, (x,y), badge)

    txt_col = (255,255,255) if not is_dark else (0,0,0)
    draw.text((x + pad_x, y + pad_y), label, font=font, fill=txt_col)


def upload_story_slide(image_url: str, link: str, caption: str) -> bool:
    """
    Tek bir story frame upload eder.
    image_url -> bizim nginx üstünden publicly erişilen .jpg
    link      -> tıklanabilir link (published_url yani trend landing page)
    caption   -> kısa headline / CTA

    Dönüş: True = başarı, False = hata
    """
    if not IG_BUSINESS_ACCOUNT_ID or not IG_ACCESS_TOKEN:
        log("⚠ No IG creds in env, skipping story upload.")
        return False

    # Story publish endpoint (IG Graph API)
    story_url = f"{IG_API_BASE}/{IG_BUSINESS_ACCOUNT_ID}/stories"

    payload = {
        "image_url": image_url,
        "access_token": IG_ACCESS_TOKEN,
        "caption": caption[:100],     # story caption çok uzun olmasın
        "link": link,                 # story link sticker (bazı hesaplarda 'link' çalışır)
    }

    try:
        r = requests.post(story_url, data=payload, timeout=10)
        if r.status_code != 200:
            log(f"❌ Story upload failed {r.status_code}: {r.text}")
            return False
        log(f"✅ Story uploaded ok for {image_url}")
        return True
    except Exception as e:
        log(f"❌ Exception during story upload: {e}")
        return False

def insert_instagram_row(
    con,
    idea_id: int,
    image_path: str,
    headline: str,
    bg_name: str,
    slide_index: int,
    status: str = "generated",
    insta_id: int | None = None,
    content_type: str = "post",   # <— YENİ
    ig_media_id: str | None = None,
):
    if insta_id is None:
        insta_id = now_ms()

    payload = {
        "headline": headline,
        "bg_name": bg_name,
        "slide_index": slide_index,
        "status": status,
        "content_type": content_type,        # <— YENİ
    }
    if ig_media_id:
        payload["ig_media_id"] = ig_media_id

    con.execute(
        """
        INSERT INTO trend_instagram_posts
        (insta_id, idea_id, image_path, caption_json, created_at)
        VALUES (?, ?, ?, ?, now())
        """,
        [insta_id, idea_id, image_path, json.dumps(payload, ensure_ascii=False)],
    )
    return insta_id

def update_instagram_status_uploaded(con, insta_id: int, ig_media_id: str | None):
    row = con.execute(
        "SELECT caption_json FROM trend_instagram_posts WHERE insta_id = ? LIMIT 1",
        [insta_id],
    ).fetchone()
    if not row:
        return

    try:
        payload = json.loads(row[0])
    except Exception:
        payload = {}

    payload["status"] = "uploaded"
    if ig_media_id:
        payload["ig_media_id"] = ig_media_id

    con.execute(
        "UPDATE trend_instagram_posts SET caption_json = ? WHERE insta_id = ?",
        [json.dumps(payload, ensure_ascii=False), insta_id],
    )


def upload_to_instagram(image_path: str, caption_text: str) -> tuple[bool, str | None]:
    """
    IG feed post publish denemesi.
    1. /media
    2. /media_publish
    """

    if not IG_BUSINESS_ACCOUNT_ID or not IG_ACCESS_TOKEN:
        log("⚠ No IG creds in env, skipping upload.")
        return (False, None)

    public_url = local_path_to_public_url(image_path)

    # Step 1: create media container
    create_url = f"{IG_API_BASE}/{IG_BUSINESS_ACCOUNT_ID}/media"
    create_params = {
        "image_url": public_url,
        "caption": caption_text or "",
        "access_token": IG_ACCESS_TOKEN,
    }

    try:
        r1 = requests.post(create_url, data=create_params, timeout=10)
        if r1.status_code != 200:
            log(f"❌ IG create media failed {r1.status_code}: {r1.text}")
            return (False, None)

        creation_id = r1.json().get("id")
        if not creation_id:
            log("❌ IG create media response missing id")
            return (False, None)
    except Exception as e:
        log(f"❌ Exception during IG create media: {e}")
        return (False, None)

    # Step 2: publish
    publish_url = f"{IG_API_BASE}/{IG_BUSINESS_ACCOUNT_ID}/media_publish"
    publish_params = {
        "creation_id": creation_id,
        "access_token": IG_ACCESS_TOKEN,
    }

    try:
        r2 = requests.post(publish_url, data=publish_params, timeout=10)
        if r2.status_code != 200:
            log(f"❌ IG publish failed {r2.status_code}: {r2.text}")
            return (False, None)

        media_id = r2.json().get("id")
        if not media_id:
            log("❌ IG publish response missing media id")
            return (False, None)

        log(f"✅ IG published media_id={media_id}")
        return (True, media_id)

    except Exception as e:
        log(f"❌ Exception during IG publish: {e}")
        return (False, None)


def upload_bundle_as_carousel(image_paths: List[str], caption_text: str) -> tuple[bool, str | None]:
    """
    image_paths -> local file paths in order
    caption_text -> caption for the carousel
    returns (ok, final_media_id or None)
    """

    if not IG_BUSINESS_ACCOUNT_ID or not IG_ACCESS_TOKEN:
        log("⚠ No IG creds in env, skipping carousel upload.")
        return (False, None)

    child_ids: List[str] = []

    #
    # STEP 1: create each child as carousel item
    #
    for p in image_paths:
        public_url = local_path_to_public_url(p)
        create_child_url = f"{IG_API_BASE}/{IG_BUSINESS_ACCOUNT_ID}/media"
        create_child_params = {
            "image_url": public_url,
            "is_carousel_item": "true",
            "access_token": IG_ACCESS_TOKEN,
        }

        try:
            r_child = requests.post(create_child_url, data=create_child_params, timeout=10)
            if r_child.status_code != 200:
                log(f"❌ IG carousel child create failed {r_child.status_code}: {r_child.text}")
                return (False, None)

            child_id = r_child.json().get("id")
            if not child_id:
                log("❌ IG carousel child create missing id")
                return (False, None)

            child_ids.append(child_id)

        except Exception as e:
            log(f"❌ Exception during IG carousel child create: {e}")
            return (False, None)

    if len(child_ids) < 2:
        log(f"❌ Need at least 2 carousel children, got {len(child_ids)}")
        return (False, None)

    #
    # helper: create parent container via JSON body, then publish
    #
    def _create_and_publish_parent_json(include_media_type: bool):
        """
        include_media_type=True:
           send {"media_type":"CAROUSEL", "children":[...], "caption":..., "access_token":...}
        include_media_type=False:
           send {"children":[...], "caption":..., "access_token":...}
        returns (ok, final_media_id or None)
        """

        create_parent_url = f"{IG_API_BASE}/{IG_BUSINESS_ACCOUNT_ID}/media"

        if include_media_type:
            parent_payload = {
                "media_type": "CAROUSEL",
                "children": child_ids,
                "caption": caption_text or "",
                "access_token": IG_ACCESS_TOKEN,
            }
        else:
            parent_payload = {
                "children": child_ids,
                "caption": caption_text or "",
                "access_token": IG_ACCESS_TOKEN,
            }

        headers = {
            "Content-Type": "application/json",
        }

        # 1. create parent container
        try:
            r_parent = requests.post(
                create_parent_url,
                data=json.dumps(parent_payload),
                headers=headers,
                timeout=10,
            )

            if r_parent.status_code != 200:
                log(f"❌ IG carousel parent create failed {r_parent.status_code}: {r_parent.text}")
                return (False, None)

            parent_creation_id = r_parent.json().get("id")
            if not parent_creation_id:
                log("❌ IG carousel parent response missing id")
                return (False, None)

        except Exception as e:
            log(f"❌ Exception during IG carousel parent create: {e}")
            return (False, None)

        # 2. publish
        publish_url = f"{IG_API_BASE}/{IG_BUSINESS_ACCOUNT_ID}/media_publish"
        publish_params = {
            "creation_id": parent_creation_id,
            "access_token": IG_ACCESS_TOKEN,
        }

        try:
            r_pub = requests.post(publish_url, data=publish_params, timeout=10)
            if r_pub.status_code != 200:
                log(f"❌ IG carousel publish failed {r_pub.status_code}: {r_pub.text}")
                return (False, None)

            final_media_id = r_pub.json().get("id")
            if not final_media_id:
                log("❌ IG carousel publish missing final media id")
                return (False, None)

            log(f"✅ IG carousel published media_id={final_media_id}")
            return (True, final_media_id)

        except Exception as e:
            log(f"❌ Exception during IG carousel publish: {e}")
            return (False, None)

    #
    # STEP 2 TRY A: send JSON with media_type=CAROUSEL
    #
    ok, media_id = _create_and_publish_parent_json(include_media_type=True)
    if ok:
        return (True, media_id)

    #
    # STEP 2 TRY B: fallback without media_type (some API surfaces infer carousel from children[])
    #
    log("ℹ Retrying carousel parent create WITHOUT media_type=CAROUSEL (JSON mode)...")

    ok2, media_id2 = _create_and_publish_parent_json(include_media_type=False)
    if ok2:
        return (True, media_id2)

    #
    # both attempts failed
    #
    return (False, None)




# ---------- headline builder: shrink to fit ----------
def fit_headline_for_panel(draw, base_text: str, serif_candidates: List[str], max_width_px: int) -> Tuple[str, ImageFont.FreeTypeFont]:
    """
    Tek satır headline:
    - ALL CAPS
    - truncate ~32 char
    - serif (Times-like) font
    - font size küçülterek sığdır
    """
    txt = base_text.strip().upper()
    if len(txt) > 32:
        txt = txt[:29].rstrip() + "…"

    size = 72  # start big
    while size >= 28:
        f_try = load_font(serif_candidates, size)
        w_try, _ = measure_text(draw, txt, f_try)
        if w_try <= max_width_px:
            return txt, f_try
        size -= 4

    f_fallback = load_font(serif_candidates, 28)
    return txt, f_fallback

def split_headline_two_lines(raw_text: str, max_chars_line1: int = 24) -> List[str]:
    txt = raw_text.strip().upper()
    words = txt.split()
    if not words:
        return [txt]

    line1_words = []
    line2_words = []
    cur_len = 0
    for w in words:
        extra = (1 if line1_words else 0) + len(w)
        if cur_len + extra <= max_chars_line1:
            line1_words.append(w)
            cur_len += extra
        else:
            line2_words.append(w)

    if not line2_words:
        return [" ".join(line1_words)]
    return [" ".join(line1_words), " ".join(line2_words)]


# =====================
# STEP 1. trending-now post seç
# =====================

def fetch_recent_trends_for_insta(con, limit_n: int = 1, content_type: str = "post"):
    base = BASE_URL.rstrip('/')
    rows = con.execute(
        """
        WITH latest_trends AS (
            SELECT tp.idea_id, tp.published_url, tp.published_at
            FROM trend_publications tp
            WHERE tp.published_url LIKE ? || '/trending-now/%'
            ORDER BY tp.published_at DESC
        ),
        uploaded AS (
            SELECT DISTINCT idea_id
            FROM trend_instagram_posts
            WHERE json_valid(caption_json)
              AND json_extract_string(caption_json, '$.status') = 'uploaded'
              AND COALESCE(json_extract_string(caption_json, '$.content_type'), 'post') = ?
        )
        SELECT lt.idea_id,
               lt.published_url,
               CAST(lt.published_at AS VARCHAR) AS published_at
        FROM latest_trends lt
        LEFT JOIN uploaded u
               ON u.idea_id = lt.idea_id
        WHERE u.idea_id IS NULL
        LIMIT ?
        """,
        [base, content_type, limit_n]
    ).fetchall()
    return rows



# =====================
# STEP 2. idea_id -> ürünler
# =====================

def pick_products_for_idea(con, idea_id: int, limit_n: int = 5) -> List[Dict[str, Any]]:
    rows = con.execute("""
        SELECT
            p.product_title,
            p.brand,
            p.category_slug,
            m.image_url
        FROM idea_products ip
        JOIN products p
          ON p.parent_asin = ip.parent_asin
        LEFT JOIN product_media m
          ON m.parent_asin = ip.parent_asin
        WHERE
            CAST(ip.idea_id AS VARCHAR) = CAST(? AS VARCHAR)
            AND m.image_url IS NOT NULL
        ORDER BY
            p.product_title ASC
        LIMIT ?
    """, [idea_id, limit_n]).fetchall()

    out = []
    for (product_title, brand, category_slug, image_url) in rows:
        if not image_url:
            continue
        out.append({
            "title": (product_title or "").strip(),
            "brand": (brand or "").strip(),
            "category_hint": (category_slug or "").strip().lower(),
            "image_url": image_url.strip(),
        })
    return out


# =====================
# STEP 3. tek headline üret (LLM)
# =====================

def llm_make_headline(product: Dict[str, Any], trend_url: str) -> str:
    # fallback headline
    cat = (product.get("category_hint","") or "").lower()
    title_lower = product["title"].lower()
    if "watch" in cat or "watch" in title_lower:
        default_head = "RATE THIS WATCH"
    elif "costume" in cat or "halloween" in title_lower:
        default_head = "RATE THIS COSTUME"
    elif "tee" in title_lower or "shirt" in title_lower:
        default_head = "SHOP THE TREND"
    else:
        default_head = "SHOP THE TREND"

    if client is None or not OPENAI_API_KEY:
        return default_head

    prompt = f"""
You write ONE short hype headline for an IG fashion/find carousel.
Rules:
- ALL CAPS vibe.
- ~28 chars max.
- No emoji, no price.
We'll reuse it on every slide.
Example product: "{product['title']}"
URL: "{trend_url}"
Return ONLY the headline text.
"""
    try:
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role":"user","content":prompt}],
            temperature=0.4,
            max_tokens=60,
        )
        raw = (resp.choices[0].message.content or "").strip()
        raw = raw.strip('"').strip("'")
        return raw if raw else default_head
    except Exception as e:
        log(f"LLM fallback: {e}")
        return default_head



# =====================
# STEP 4. RATE / SWIPE satırını çizen helper
# =====================

def draw_icon_text_line_centered(bg_img, draw, center_x, y,
                                 text_left, font_left,
                                 icon_img,
                                 text_right, font_right,
                                 color_left, color_right):
    w_left, h_left = measure_text(draw, text_left, font_left) if text_left else (0,0)
    icon_w = icon_img.width if icon_img else 0
    icon_h = icon_img.height if icon_img else 0
    w_right, h_right = measure_text(draw, text_right, font_right) if text_right else (0,0)

    gap1 = 20 if (text_left and icon_img) else 0
    gap2 = 20 if (icon_img and text_right) else 0

    total_w = w_left + gap1 + icon_w + gap2 + w_right
    start_x = center_x - total_w//2
    cur_x = start_x

    if text_left:
        draw.text((cur_x, y), text_left, font=font_left, fill=color_left)
        cur_x += w_left + gap1

    if icon_img:
        max_h = max(h_left, icon_h, h_right)
        icon_y = y + (max_h - icon_h)//2
        bg_img.paste(icon_img, (cur_x, icon_y), icon_img)
        cur_x += icon_w + gap2

    if text_right:
        draw.text((cur_x, y), text_right, font=font_right, fill=color_right)


def pick_bg_variant() -> dict:
    """
    Arka planı seçer.
    - int_post_bg1.png, int_post_bg2.png => açık tema (is_dark=False)
    - int_post_bg3.png, int_post_bg4.png => koyu tema (is_dark=True)
    - hepsi varsa hepsi arasından random
    - hiçbiri yoksa fallback solid dark
    """
    candidates = []

    p1 = os.path.join(OUT_DIR, "int_post_bg1.png")
    if os.path.exists(p1):
        candidates.append({"bg_path": p1, "is_dark": False})

    p2 = os.path.join(OUT_DIR, "int_post_bg2.png")
    if os.path.exists(p2):
        candidates.append({"bg_path": p2, "is_dark": False})

    p3 = os.path.join(OUT_DIR, "int_post_bg3.png")
    if os.path.exists(p3):
        candidates.append({"bg_path": p3, "is_dark": True})

    p4 = os.path.join(OUT_DIR, "int_post_bg4.png")
    if os.path.exists(p4):
        candidates.append({"bg_path": p4, "is_dark": True})

    if not candidates:
        # hiçbir bg yoksa tek renk fallback
        return {"bg_path": "", "is_dark": True}

    return random.choice(candidates)



def local_path_to_public_url(local_path: str) -> str:
    fname = os.path.basename(local_path)
    return f"https://mintistudio.com/instagram_out/{fname}"


# =====================
# STEP 5. tek slide çiz
# =====================

def generate_story_image(
    idea_id: int,
    product: Dict[str, Any],
    headline_text: str,
    index_num: int,
    total_slides: int,  # <— YENİ
    fonts: Dict[str,ImageFont.FreeTypeFont],
    serif_bold_candidates: List[str],
    bg_sel: dict,
):
    os.makedirs(OUT_DIR, exist_ok=True)

    bg_path = bg_sel["bg_path"]
    is_dark = bg_sel["is_dark"]

    if os.path.exists(bg_path):
        bg_base = Image.open(bg_path).convert("RGBA")
        bg_base = bg_base.resize((STORY_W, STORY_H), Image.LANCZOS)
        bg = Image.new("RGB", (STORY_W, STORY_H), (0,0,0))
        bg.paste(bg_base, (0,0), bg_base)
    else:
        fallback_col = (30,30,30) if is_dark else (240,240,240)
        bg = Image.new("RGB", (STORY_W, STORY_H), fallback_col)

    draw = ImageDraw.Draw(bg)

    # renkler
    main_text_color = (255,255,255) if is_dark else (0,0,0)
    swipe_text_color = main_text_color

    # headline font (bold serif)
    headline_font = load_font(serif_bold_candidates, 48)
    lines = split_headline_two_lines(headline_text, max_chars_line1=24)

    current_y = 40
    line_spacing = 12
    for line in lines[:2]:
        w_line, h_line = measure_text(draw, line, headline_font)
        draw.text(
            ((STORY_W - w_line)//2, current_y),
            line,
            font=headline_font,
            fill=main_text_color,
        )
        current_y += h_line + line_spacing

    # ürün görseli
    try:
        rr = requests.get(product["image_url"], timeout=10)
        prod_img = Image.open(BytesIO(rr.content)).convert("RGBA")
    except Exception:
        prod_img = Image.new("RGBA", (900,900), (60,60,60,255))

    prod_img.thumbnail((850, 850), Image.LANCZOS)

    pad = 50
    card_w = prod_img.width + pad*2
    card_h = prod_img.height + pad*2
    card_y = current_y + 100
    card_x = (STORY_W - card_w)//2

    card = Image.new("RGBA", (card_w, card_h), (255,255,255,255))
    mask_card = Image.new("L", (card_w, card_h), 0)
    ImageDraw.Draw(mask_card).rounded_rectangle([0,0,card_w,card_h], 32, fill=255)
    bg.paste(card, (card_x, card_y), mask_card)

    prod_x = card_x + pad
    prod_y = card_y + pad
    bg.paste(prod_img, (prod_x, prod_y), prod_img)

    # ikonlar
    STAR_ICON_PATH  = os.path.join(OUT_DIR, "star.png")
    ARROW_ICON_PATH = os.path.join(OUT_DIR, "arrow.png")

    star_img = Image.open(STAR_ICON_PATH).convert("RGBA") if os.path.exists(STAR_ICON_PATH) else None
    if star_img:
        star_img.thumbnail((90,90), Image.LANCZOS)

    arrow_img = Image.open(ARROW_ICON_PATH).convert("RGBA") if os.path.exists(ARROW_ICON_PATH) else None
    if arrow_img:
        arrow_img.thumbnail((90,90), Image.LANCZOS)

    rate_y = card_y + card_h + 60
    draw_icon_text_line_centered(
        bg_img=bg,
        draw=draw,
        center_x=STORY_W//2,
        y=rate_y,
        text_left="RATE:",
        font_left=fonts["rate_big"],
        icon_img=star_img,
        text_right="/10",
        font_right=fonts["rate_big"],
        color_left=main_text_color,
        color_right=main_text_color,
    )

    swipe_y = rate_y + 110
    draw_icon_text_line_centered(
        bg_img=bg,
        draw=draw,
        center_x=STORY_W//2,
        y=swipe_y,
        text_left="SWIPE",
        font_left=fonts["swipe_big"],
        icon_img=arrow_img,
        text_right="",
        font_right=fonts["swipe_big"],
        color_left=swipe_text_color,
        color_right=swipe_text_color,
    )

    out_path = os.path.join(OUT_DIR, f"trend_insta_{idea_id}_img_{index_num}.jpg")
    draw_slide_badge(bg, draw, index=index_num, total=total_slides, is_dark=is_dark)

    bg.save(out_path, "JPEG", quality=90)
    return out_path



# =====
# STEP 6. bundle üret
# =====================

def process_one_story_bundle(
    idea_id: int,
    published_url: str,
    max_slides: int = 5,
    content_type: str = "post",  # "post" = carousel feed, "story" = 24h story
):
    """
    1. DB'den ürünleri seçer (max_slides kadar)
    2. Tek ortak headline üretir (LLM fallback'li)
    3. Random bir background teması seçer (dark / light)
    4. Her ürün için slide görseli çizer ve diske kaydeder
    5. Her slide için trend_instagram_posts tablosuna status='generated' kaydı yazar
    6. content_type'e göre:
        - "post": tüm slideları tek carousel feed post olarak IG'ye yükler
                  ve DB status='uploaded' + ig_media_id ile günceller
        - "story": her slide'ı ayrı ayrı IG Story olarak yükler (24 saatlik)
                  ve DB status='uploaded' ile günceller
    Döner: üretilen görsellerin local path listesi
    """

    # daha önce bu idea_id + content_type için publish edilmişse atla
    with db_ro() as con_chk:
        if already_uploaded(con_chk, idea_id, content_type):
            log(f"⏭  Skip: idea_id={idea_id} already uploaded as {content_type}.")
            return []

    # her run farklı olsun diye random seed'i zamanı baz alıyoruz
    random.seed(time.time())

    log(f"▶ Generating IG Story bundle (mode={content_type}) for idea_id={idea_id}")

    # 1. ürünleri çek
    with db_ro() as con_ro:
        products = pick_products_for_idea(con_ro, idea_id, limit_n=max_slides)

    if not products:
        log(f"⚠ No usable products for idea_id={idea_id}")
        return []

    # 2. headline (tek tüm bundle için)
    headline_all = llm_make_headline(products[0], published_url)

    # 3. ortak tema seç (arka plan görseli + koyu/açık bilgisi)
    bg_sel = pick_bg_variant()
    bg_name_for_db = os.path.basename(bg_sel["bg_path"]) if bg_sel.get("bg_path") else "fallback"

    # 4. fontları hazırla
    bold_candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    ]
    serif_bold_candidates = [
        "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSerifBold.ttf",
        # istersen custom fontu buraya ilk elemana eklersin:
        # "/home/ubuntu/blog-factory/assets/TimesNewRomanBold.ttf",
    ]

    fonts = {
        "rate_big":   load_font(bold_candidates, 76),
        "swipe_big":  load_font(bold_candidates, 68),
    }

    # 5. slide görselleri üret, db'ye 'generated' olarak kaydet
    out_paths: List[str] = []
    insta_ids_for_slides: List[int] = []

    for idx, prod in enumerate(products, start=1):
        # tek tek slide çiz
        path = generate_story_image(
            idea_id=idea_id,
            product=prod,
            headline_text=headline_all,
            index_num=idx,
            total_slides=len(products),   # <— eklendi
            fonts=fonts,
            serif_bold_candidates=serif_bold_candidates,
            bg_sel=bg_sel,
        )

        out_paths.append(path)
        log(f"✅ Slide {idx}: {path}")

        # DB'ye status='generated' olarak yaz
        with duckdb.connect(DB_PATH, read_only=False) as con_w:
            insta_id = insert_instagram_row(
                con=con_w,
                idea_id=idea_id,
                image_path=path,
                headline=headline_all,
                bg_name=bg_name_for_db,
                slide_index=idx,
                status="generated",
                insta_id=None, 
                content_type=content_type,  
            )
            con_w.commit()

        insta_ids_for_slides.append(insta_id)

    # 6. INSTAGRAM'A YÜKLEME AŞAMASI
    #
    # content_type == "story" ise:
    #   - her slide tek tek story olarak gönderilir
    #   - story'ye tıklanabilir link olarak published_url koyuyoruz
    #
    # content_type == "post" ise:
    #   - tüm slide'lar tek carousel feed post olarak atılır
    #   - caption'a CTA koyabiliriz (rate/comment/link in bio)
    #

    if content_type == "story":
        # STORY MODE
        # her görseli story olarak uploadla
        # story caption kısa olmalı çünkü IG story caption overlay yazıyor
        story_caption_template = "{}  ⭐ RATE 1-10 👇"
        # published_url bizim trend landing page => tıklanabilir link
        
        success_cnt = 0
        for idx, img_path in enumerate(out_paths, start=1):
            public_url = f"{BASE_URL.rstrip('/')}/instagram_out/{os.path.basename(img_path)}"
            cap_txt = story_caption_template.format(headline_all)
            ok_story = upload_story_slide(image_url=public_url, link=published_url, caption=cap_txt)
            if ok_story:
                success_cnt += 1
                with duckdb.connect(DB_PATH, read_only=False) as con_u:
                    update_instagram_status_uploaded(con=con_u, insta_id=insta_ids_for_slides[idx-1], ig_media_id=None)
                    con_u.commit()

        if success_cnt > 0:
            with duckdb.connect(DB_PATH, read_only=False) as con_u:
                insert_instagram_row(
                    con=con_u,
                    idea_id=idea_id,
                    image_path="",
                    headline=headline_all,
                    bg_name=bg_name_for_db,
                    slide_index=0,
                    status="uploaded",
                    insta_id=None,
                    content_type=content_type,
                    ig_media_id=None,
                )
                con_u.commit()

        
        log(f"🎯 Story mode: {len(out_paths)} slides uploaded as IG Stories for idea_id={idea_id}")
        return out_paths

    else:
        # POST (carousel) MODE
        # caption'ı biraz daha uzun tutabiliriz çünkü feed post
        final_caption = build_carousel_caption(headline_all, products)

        ok_post, media_id = upload_bundle_as_carousel(
            image_paths=out_paths,
            caption_text=final_caption,
        )

        if ok_post and media_id:
            with duckdb.connect(DB_PATH, read_only=False) as con_u:
                for insta_id in insta_ids_for_slides:
                    update_instagram_status_uploaded(con=con_u, insta_id=insta_id, ig_media_id=media_id)

                # marker row (slide_index=0)
                insert_instagram_row(
                    con=con_u,
                    idea_id=idea_id,
                    image_path="",
                    headline=headline_all,
                    bg_name=bg_name_for_db,
                    slide_index=0,
                    status="uploaded",
                    insta_id=None,
                    content_type=content_type,
                    ig_media_id=media_id,
                )
                con_u.commit()


            log(f"🎉 Carousel uploaded for idea_id={idea_id} → media_id={media_id}")
        else:
            log(f"ℹ Carousel upload skipped/failed for idea_id={idea_id}")

        return out_paths



def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-per-run", type=int, default=1)
    ap.add_argument("--slides", type=int, default=5)
    ap.add_argument("--content-type",
                    type=str,
                    default="post",
                    choices=["post","story"],
                    help="Instagram content type: post (carousel feed) or story (24h story)")
    args = ap.parse_args()


    with db_ro() as con:
        cand_posts = fetch_recent_trends_for_insta(con, args.max_per_run, args.content_type)


    if not cand_posts:
        log("ℹ No trending-now publications found.")
        return

    for (idea_id, published_url, published_at) in cand_posts:
        process_one_story_bundle(
            int(idea_id),
            published_url,
            max_slides=args.slides,
            content_type=args.content_type,
        )



if __name__ == "__main__":
    main()
