import os, json, time, random, math
from io import BytesIO
from typing import Dict, Any, List, Tuple, Optional
from dotenv import load_dotenv
import duckdb
import requests
from PIL import Image, ImageDraw, ImageFont
import numpy as np
from moviepy.editor import TextClip

from moviepy.editor import (
    VideoFileClip, ImageClip, CompositeVideoClip, concatenate_videoclips, AudioFileClip, ColorClip,TextClip
)
from moviepy.video.fx.all import fadein, fadeout, loop
from moviepy.video.VideoClip import VideoClip   # <-- EKLE
from src.trends.instagram_tokens import get_instagram_credentials
  

# =====================
# ENV / GLOBAL CONFIG
# =====================

load_dotenv("/home/ubuntu/blog-factory/.env")

DB_PATH        = os.getenv("DB_PATH", "/home/ubuntu/blog-factory/warehouse/blog_factory.duckdb")
BASE_URL       = os.getenv("BASE_URL", "https://mintistudio.com")
IG_API_BASE    = os.getenv("IG_API_BASE", "https://graph.facebook.com/v21.0")
IG_BUSINESS_ID = os.getenv("IG_BUSINESS_ACCOUNT_ID")
IG_TOKEN       = os.getenv("IG_ACCESS_TOKEN")

_INSTAGRAM_CREDS = get_instagram_credentials()
if _INSTAGRAM_CREDS:
    IG_TOKEN = IG_TOKEN or _INSTAGRAM_CREDS.get("page_access_token")
    IG_BUSINESS_ID = IG_BUSINESS_ID or _INSTAGRAM_CREDS.get("instagram_business_account_id")

OUT_DIR        = "/home/ubuntu/blog-factory/instagram_out"
BG_VIDEO_PATH  = os.path.join(OUT_DIR, "bg_reel.mp4")  # sen buraya arka plan videonu koyacaksın

# ÇIKTI BOYUTU
REEL_W, REEL_H = 1080, 1920   # Reel
FEED_W, FEED_H = 1080, 1350   # Feed

# =====================
# UTIL
# =====================


# --- simple progress bar ---
def pbar(i: int, n: int, msg: str = ""):
    width = 28
    done = int((i / max(n,1)) * width)
    bar = "█" * done + "·" * (width - done)
    print(f"[{i:02d}/{n:02d}] |{bar}| {msg}", flush=True)

def build_segment(bg_path: str,
                  pil_img: Image.Image,
                  W: int = 1080,
                  H: int = 1920,
                  seg_dur: float = 2.6,
                  fade_sec: float = 0.3) -> CompositeVideoClip:

    from moviepy.editor import VideoFileClip, ImageClip, CompositeVideoClip, TextClip
    import numpy as np
    from PIL import Image
    from moviepy.video.fx.all import fadein, fadeout, loop

    # 1) Arka plan
    bg = (VideoFileClip(bg_path)
          .resize((W, H))
          .without_audio()
          .fx(loop, duration=seg_dur))

    # 2) Ürün görselini boyutlandır (daha dar)
    pil_img = pil_img.convert("RGBA")
    pil_img.thumbnail((W * 0.75, H * 0.9), Image.LANCZOS)

    prod = (ImageClip(np.array(pil_img))
            .set_duration(seg_dur)
            .fx(fadein, fade_sec)
            .fx(fadeout, fade_sec))

    # Zoom efekti
    prod = prod.resize(lambda t: 1.05 + 0.04 * (t / seg_dur))

    # 📍 YENİ: Daha yukarı konum
    def prod_pos(t):
        shift = int(-40 + 80 * (t / seg_dur))
        return ("center", int(H * 0.10 + shift))  # üst 1/3 hizalama
    prod = prod.set_position(prod_pos)

    # 📍 CTA yazısı: sadece “Tap to shop” yazacak
    cta = (TextClip("🛍️ Tap to shop",
                    fontsize=72,  # daha uygun boyut
                    color='white',
                    font='LiberationSans-Bold',
                    method='label')
           .set_duration(seg_dur)
           .set_position(("center", H - 500)))  # çok altta değil

    # Birleştir
    return CompositeVideoClip([bg, prod, cta]).set_duration(seg_dur)



def log(msg: str):
    print(msg, flush=True)

def db_ro():
    return duckdb.connect(DB_PATH, read_only=True)

def load_font(path_candidates: List[str], size: int, fallback_default: bool = True):
    for p in path_candidates:
        try:
            if p and os.path.exists(p):
                return ImageFont.truetype(p, size)
        except Exception:
            continue
    return ImageFont.load_default() if fallback_default else None

def fetch_recent_trends_for_insta(con, limit_n: int = 1, content_type: str = "reel"):
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
        LEFT JOIN uploaded u ON u.idea_id = lt.idea_id
        WHERE u.idea_id IS NULL
        LIMIT ?
        """,
        [base, content_type, limit_n]
    ).fetchall()
    return rows


def pick_products_for_idea(con, idea_id: int, limit_n: int = 5) -> List[Dict[str, Any]]:
    """
    products şemasına dinamik uyum:
    - fiyat: price_str varsa onu; yoksa price/price_usd → VARCHAR
    - link: listing_url varsa onu; yoksa affiliate_url/buy_url/detail_url
    - görsel: product_media.image_url
    """
    # tablo kolonlarını topla
    prod_cols = {row[1] for row in con.execute("PRAGMA table_info('products')").fetchall()}
    media_cols = {row[1] for row in con.execute("PRAGMA table_info('product_media')").fetchall()}
    idea_cols  = {row[1] for row in con.execute("PRAGMA table_info('idea_products')").fetchall()}

    # güvenli alan adları
    pid_col = "parent_asin" if "parent_asin" in prod_cols else "product_id"

    # fiyat alanı seçimi
    if "price_str" in prod_cols:
        price_expr = "p.price_str"
    elif "price" in prod_cols:
        price_expr = "CAST(p.price AS VARCHAR)"
    elif "price_usd" in prod_cols:
        price_expr = "CAST(p.price_usd AS VARCHAR)"
    else:
        price_expr = "''"

    # link alanı seçimi
    link_candidates = ["listing_url", "affiliate_url", "buy_url", "detail_url", "url"]
    link_expr = None
    for c in link_candidates:
        if c in prod_cols:
            link_expr = f"p.{c}"
            break
    if not link_expr:
        link_expr = "NULL"

    # media image alanı
    img_expr = "m.image_url" if "image_url" in media_cols else "NULL"

    # idea_products tarafında join kolonu
    ip_pid_col = "parent_asin" if "parent_asin" in idea_cols else ("product_id" if "product_id" in idea_cols else None)
    if ip_pid_col is None:
        # minimum fallback: sadece products tablosundan çek
        rows = con.execute(f"""
            SELECT
                p.product_title,
                COALESCE(p.brand, '') AS brand,
                COALESCE(p.category_slug, '') AS category_slug,
                NULL AS image_url,
                COALESCE({price_expr}, '') AS price_str,
                {link_expr} AS listing_url
            FROM products p
            WHERE p.idea_id = CAST(? AS VARCHAR) -- eğer yoksa aşağıdaki else bloğuna düşecektir
            LIMIT ?
        """, [idea_id, limit_n]).fetchall()
    else:
        rows = con.execute(f"""
            SELECT
                p.product_title,
                COALESCE(p.brand, '') AS brand,
                COALESCE(p.category_slug, '') AS category_slug,
                {img_expr} AS image_url,
                COALESCE({price_expr}, '') AS price_str,
                {link_expr} AS listing_url
            FROM idea_products ip
            JOIN products p
              ON p.{pid_col} = ip.{ip_pid_col}
            LEFT JOIN product_media m
              ON m.{pid_col} = ip.{ip_pid_col}
            WHERE CAST(ip.idea_id AS VARCHAR) = CAST(? AS VARCHAR)
              AND {img_expr} IS NOT NULL
            ORDER BY p.product_title ASC
            LIMIT ?
        """, [idea_id, limit_n]).fetchall()

    out = []
    for (title, brand, category_slug, image_url, price_str, listing_url) in rows:
        if not image_url:
            # eğer media yoksa products içinden image_url benzeri bir alan var mı, deneyelim
            if "image_url" in prod_cols:
                # tek seferlik lookup
                img = con.execute(f"SELECT image_url FROM products WHERE {pid_col} IN (SELECT {ip_pid_col} FROM idea_products WHERE idea_id=?) AND image_url IS NOT NULL LIMIT 1", [idea_id]).fetchone()
                image_url = img[0] if img else None
        if not image_url:
            continue

        out.append({
            "title": (title or "").strip(),
            "brand": (brand or "").strip(),
            "category_hint": (category_slug or "").strip().lower(),
            "image_url": image_url.strip(),
            "price": (price_str or "").strip(),
            "listing_url": (listing_url or "").strip(),
        })
    return out


def ensure_dir(p: str):
    os.makedirs(p, exist_ok=True)

def local_path_to_public_url(local_path: str) -> str:
    return f"{BASE_URL.rstrip('/')}/instagram_out/{os.path.basename(local_path)}"

# =====================
# BADGE (alt bant) çizimi
# =====================

def render_badge_png(product: Dict[str, Any], is_dark_ui: bool, width: int = 980, height: int = 160) -> Image.Image:
    """
    Alt bant: ürün adı (kısaltılmış) + (varsa) fiyat + "View on eBay" butonu
    RGBA döndürür.
    """
    bg = (0,0,0,160) if is_dark_ui else (255,255,255,200)
    txt = (255,255,255) if is_dark_ui else (0,0,0)

    img = Image.new("RGBA", (width, height), (0,0,0,0))
    plate = Image.new("RGBA", (width, height), bg)
    mask = Image.new("L", (width, height), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0,0,width,height], radius=24, fill=255)
    img.paste(plate, (0,0), mask)

    draw = ImageDraw.Draw(img)
    title = (product.get("title") or "Product").strip()
    price = (product.get("price") or "").strip()

    font_paths_bold = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ]
    font_paths_reg = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ]
    f_title = load_font(font_paths_bold, 42)
    f_price = load_font(font_paths_bold, 40)
    f_btn   = load_font(font_paths_bold, 36)

    # Title truncation
    def text_w(draw, t, f): 
        if hasattr(draw, "textlength"):
            return int(draw.textlength(t, font=f))
        return draw.textbbox((0,0), t, font=f)[2]

    max_title_w = width - 360
    while text_w(draw, title, f_title) > max_title_w and len(title) > 8:
        title = title[:-4].rstrip() + "…"

    # Title
    draw.text((24, height//2 - 22), title, font=f_title, fill=txt)

    # Price (opsiyonel)
    if price:
        pr_w = text_w(draw, price, f_price)
        draw.text((24, height - 54), price, font=f_price, fill=txt)

    # Button
    btn_w, btn_h = 260, 80
    btn_x = width - btn_w - 24
    btn_y = height//2 - btn_h//2
    btn_col = (0, 122, 85, 255)  # mint
    btn = Image.new("RGBA", (btn_w, btn_h), (0,0,0,0))
    ImageDraw.Draw(btn).rounded_rectangle([0,0,btn_w,btn_h], radius=16, fill=btn_col)
    img.paste(btn, (btn_x, btn_y), btn)

    # Button label
    lbl = "View on eBay"
    lb_w = text_w(draw, lbl, f_btn)
    draw.text((btn_x + (btn_w - lb_w)//2, btn_y + 20), lbl, font=f_btn, fill=(255,255,255))

    return img

# =====================
# KEN BURNS helper
# =====================

def ken_burns_clip_for_image(
    pil_img: Image.Image,
    duration: float,
    out_w: int,
    out_h: int,
    zoom_start: float = 1.05,
    zoom_end: float = 1.15,
    pan_direction: str = "lr"  # "lr" or "rl" or "tb" or "bt"
) -> VideoClip:
    """
    PIL görselden süre boyunca zoom/pan yapan bir VideoClip döndürür.
    MoviePy 1.0.3'te ImageClip PIL beklemez; frame'i numpy array olarak üretiriz.
    """
    # Her ihtimale karşı RGB'ye çevir (alpha kapatıyoruz; kompoziti üstte badge ile yapıyoruz)
    pil_img = pil_img.convert("RGB")

    iw, ih = pil_img.size
    out_ratio = out_w / out_h
    in_ratio  = iw / ih

    # "cover" mantığı: ölçek=1 iken hedef boyuta göre kaplama ölçüleri
    if in_ratio > out_ratio:
        # geniş: yükseğe göre scale; yatay kırpma
        base_scale = out_h / ih
        sw, sh = iw * base_scale, out_h
    else:
        # dar: genişliğe göre scale; dikey kırpma
        base_scale = out_w / iw
        sw, sh = out_w, ih * base_scale

    sw, sh = float(sw), float(sh)

    # pan eksenine göre max pan mesafesi (zoom 1'deki temel referans)
    # zoom arttıkça pan alanı da artacak, bunu kadraj hesaplarında kullanacağız
    def make_frame(t: float):
        if duration <= 0:
            progress = 1.0
        else:
            progress = max(0.0, min(1.0, t / duration))

        # lineer zoom
        z = zoom_start + (zoom_end - zoom_start) * progress
        cur_w = int(sw * z)
        cur_h = int(sh * z)

        # pan aralığı (frame boyutu - out boyutu) / 2
        max_pan_x = max(0, (cur_w - out_w) // 2)
        max_pan_y = max(0, (cur_h - out_h) // 2)

        if pan_direction in ("lr", "rl"):
            # yatay pan
            px = int(-max_pan_x + 2 * max_pan_x * progress) if pan_direction == "lr" else int(max_pan_x - 2 * max_pan_x * progress)
            py = 0
        else:
            # dikey pan
            py = int(-max_pan_y + 2 * max_pan_y * progress) if pan_direction == "tb" else int(max_pan_y - 2 * max_pan_y * progress)
            px = 0

        # Görseli ölçekle
        frame = pil_img.resize((cur_w, cur_h), Image.LANCZOS)

        # Kadrajın merkezinden pan kaydırması
        cx = (cur_w - out_w) // 2 + px
        cy = (cur_h - out_h) // 2 + py

        # Emniyet (taşma olmasın)
        cx = max(0, min(cx, max(0, cur_w - out_w)))
        cy = max(0, min(cy, max(0, cur_h - out_h)))

        crop = frame.crop((cx, cy, cx + out_w, cy + out_h))
        return np.array(crop)

    return VideoClip(make_frame, duration=duration)


# =====================
# VIDEO PIPELINE
# =====================

def build_reel_for_idea(
    idea_id: int,
    bg_path: str,
    max_slides: int = 5,
    out_dir: str = OUT_DIR,
    W: int = 1080,
    H: int = 1920,
    seg_dur: float = 2.6,
    fade_sec: float = 0.3,
) -> str:
    """
    - Arka plan: bg_path'teki video FULLSCREEN ve her segmentte oynar (loop).
    - Ürün görselleri: ortada, 2.6 sn civarı; fade-in/out + hafif Ken Burns.
    - max_slides adet ürün ardı ardına eklenir → tek MP4 çıktısı.
    Geri dönüş: çıktı dosya yolu.
    """

    os.makedirs(out_dir, exist_ok=True)

    # 1) Ürünleri al
    with db_ro() as con:
        products = pick_products_for_idea(con, idea_id, limit_n=max_slides)

    if not products:
        raise RuntimeError(f"No products to render for idea_id={idea_id}")

    # 2) Her ürün için birer segment oluştur
    segments = []
    N = min(len(products), max_slides)
    print(f"🧩 {N} ürün için segment üretimi başlıyor...", flush=True)

    for idx, prod in enumerate(products[:N], start=1):
        pbar(idx-1, N, "görsel indiriliyor")
        # görseli indir
        try:
            rr = requests.get(prod["image_url"], timeout=10)
            pil_img = Image.open(BytesIO(rr.content))
        except Exception:
            pil_img = Image.new("RGBA", (900, 900), (60, 60, 255, 255))  # mavi placeholder

        pbar(idx, N, "segment oluşturuluyor")
        seg = build_segment(
            bg_path=bg_path,
            pil_img=pil_img,
            W=W, H=H,
            seg_dur=seg_dur,
            fade_sec=fade_sec
        )
        segments.append(seg)




    # 3) Stitch
    print(f"🔗 {len(segments)} segment birleştiriliyor...", flush=True)
    stitched = concatenate_videoclips(segments, method="compose")

    # 4) Dosya adı
    ts = int(time.time() * 1000)
    out_path = os.path.join(out_dir, f"reel_idea{idea_id}_{ts}.mp4")

    # 5) Yaz (hafif CPU ayarları)
    
    print("💾 MP4 yazılıyor (encode)…", flush=True)
    stitched.write_videofile(
        out_path,
        fps=24,
        codec="libx264",
        audio=False,
        bitrate="3M",
        threads=1,
        preset="ultrafast",
        ffmpeg_params=["-movflags","+faststart","-maxrate","3M","-bufsize","6M","-threads","1"],
        verbose=True,
        logger="bar",   # MoviePy'nin kendi progress bar'ı
    )
    print(f"✅ Bitti: {out_path}", flush=True)

    log(f"🎬 Reel hazır: {out_path}")
    # kaynakları serbest bırak
    stitched.close()
    for seg in segments:
        seg.close()

    return out_path



# =====================
# (OPSİYONEL) IG REEL UPLOAD
# =====================

def upload_reel_to_instagram(video_path: str, caption: str = "") -> Tuple[bool, Optional[str]]:
    """
    Instagram Graph API: video upload (reel). Basit sürüm.
    Reel için: /media (video_url) → /media_publish
    Not: Videonun PUBLIC url’i olmalı (NGINX üzerinden servis)
    """
    if not IG_BUSINESS_ID or not IG_TOKEN:
        log("⚠ IG creds yok, upload atlanıyor.")
        return (False, None)

    public_url = local_path_to_public_url(video_path)

    # 1) create media container (video_url)
    create_url = f"{IG_API_BASE}/{IG_BUSINESS_ID}/media"
    payload = {
        "media_type": "REELS",
        "video_url": public_url,
        "caption": caption or "",
        "access_token": IG_TOKEN,
        # "share_to_feed": "true",  # istersen
    }
    r1 = requests.post(create_url, data=payload, timeout=20)
    if r1.status_code != 200:
        log(f"❌ IG reels create failed {r1.status_code}: {r1.text}")
        return (False, None)
    creation_id = r1.json().get("id")
    if not creation_id:
        log("❌ creation_id yok")
        return (False, None)

    # 2) publish
    pub_url = f"{IG_API_BASE}/{IG_BUSINESS_ID}/media_publish"
    r2 = requests.post(pub_url, data={"creation_id": creation_id, "access_token": IG_TOKEN}, timeout=20)
    if r2.status_code != 200:
        log(f"❌ IG reels publish failed {r2.status_code}: {r2.text}")
        return (False, None)

    media_id = r2.json().get("id")
    log(f"✅ IG Reels published: media_id={media_id}")
    return (True, media_id)

# =====================
# CLI
# =====================

def main():
    import argparse, os
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-per-run", type=int, default=1)
    ap.add_argument("--slides", type=int, default=5)
    ap.add_argument("--target", type=str, default="reel", choices=["reel","feed"])
    ap.add_argument("--content-type", type=str, default="reel")  # DB filtresi için bırakıyoruz
    ap.add_argument("--bg", type=str, default="")
    ap.add_argument("--upload", action="store_true", help="(DİKKAT) üretim sonrası IG'ye yolla")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    # BG video yolu
    bgp = args.bg or BG_VIDEO_PATH
    if not bgp or not os.path.exists(bgp):
        log(f"❌ BG video bulunamadı: {bgp}. --bg ile tam yolu ver.")
        return

    # Aday trendleri çek
    with db_ro() as con:
        cand = fetch_recent_trends_for_insta(con, args.max_per_run, args.content_type)

    if not cand:
        log("ℹ Uygun trend yok (zaten upload edilmiş olabilir).")
        return

    # Hedef boyut
    if args.target == "reel":
        W, H = 1080, 1920
    else:
        W, H = 1080, 1350

    for (idea_id, _published_url, _published_at) in cand:
        out_path = build_reel_for_idea(
            idea_id=int(idea_id),
            bg_path=bgp,
            max_slides=args.slides,
            out_dir=OUT_DIR,
            W=W,
            H=H,
            seg_dur=2.6,   # her ürün ~2.6 sn
            fade_sec=0.3,  # giriş/çıkış fade
        )
        if not out_path:
            continue

        # İsteğe bağlı upload
        if args.upload and not args.dry_run:
            caption = "RATE IT 1–10 👇  #trending #deals #shopping"
            try:
                upload_reel_to_instagram(out_path, caption)  # mevcut fonksiyonunsa
            except NameError:
                log("ℹ upload_reel_to_instagram bulunamadı; sadece dosya üretildi.")


if __name__ == "__main__":
    main()
