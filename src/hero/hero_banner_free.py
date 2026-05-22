#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Free, high-quality hero banner generator (no Canva).
- Pulls one product image for a post
- Composes a premium-looking 1600x900 hero:
  * blurred background from product image + dark gradient
  * product image on the left with soft shadow
  * big title on the right, CTA pill underneath
- Saves to /var/www/html/assets/hero-<timestamp>.jpg
- Updates blog_contents.hero_image_url = /assets/hero-<timestamp>.jpg
"""

import os, io, time, math, requests, textwrap, duckdb
from typing import Optional
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from io import BytesIO
from datetime import datetime


load_dotenv("/home/ubuntu/blog-factory/.env")

DB_PATH           = os.getenv("DB_PATH", "/home/ubuntu/blog-factory/warehouse/blog_factory.duckdb")
ASSETS_DIR        = os.getenv("ASSETS_DIR", "/var/www/html/assets")
ASSETS_URL_PREFIX = os.getenv("ASSETS_URL_PREFIX", "/assets")
WIDTH, HEIGHT     = int(os.getenv("HERO_W", "1600")), int(os.getenv("HERO_H", "900"))
BANNER_TEMPLATE = "/home/ubuntu/blog-factory/app/static/img/banner-1.png"

# Candidate font paths (first existing will be used)
FONT_CANDIDATES_BOLD = [
    "/usr/share/fonts/truetype/inter/Inter-Bold.ttf",
    "/usr/share/fonts/truetype/montserrat/Montserrat-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]
FONT_CANDIDATES_REG = [
    "/usr/share/fonts/truetype/inter/Inter-Regular.ttf",
    "/usr/share/fonts/truetype/montserrat/Montserrat-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]

def pick_font(cands, size):
    for p in cands:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    # fallback
    return ImageFont.load_default()


def generate_banner_with_template(hero_img_url: str, title_text: str, output_path: str):
    base = Image.open(BANNER_TEMPLATE).convert("RGBA")
    W, H = base.size
    draw = ImageDraw.Draw(base)

    # ----- GEOMETRY (yüzdeyle; template orantısı bozulsa bile sapmaz) -----
    # Açık renkli kutu (text box) ~ sol orta
    tx1, ty1, tx2, ty2 = (int(W*0.18), int(H*0.25), int(W*0.52), int(H*0.70))
    # Sağda ürün görsel alanı
    ix1, iy1, ix2, iy2 = (int(W*0.58), int(H*0.08), int(W*0.96), int(H*0.92))

    # ----- PRODUCT IMAGE: contain + merkezle -----
    try:
        resp = requests.get(hero_img_url, timeout=12)
        fg = Image.open(BytesIO(resp.content)).convert("RGBA")
        # hedef kutu boyutu
        TW, TH = (ix2 - ix1), (iy2 - iy1)
        scale = min(TW/fg.width, TH/fg.height)
        nw, nh = max(1, int(fg.width*scale)), max(1, int(fg.height*scale))
        fg = fg.resize((nw, nh), Image.LANCZOS)
        px = ix1 + (TW - nw)//2
        py = iy1 + (TH - nh)//2
        base.paste(fg, (px, py), fg)
    except Exception as e:
        print("Hero image load error:", e)

    # ----- FONTS -----
    def _pick(cands, sz):
        for p in cands:
            if os.path.exists(p):
                try: return ImageFont.truetype(p, sz)
                except: pass
        return ImageFont.load_default()

    FONT_BOLD = [
        "/usr/share/fonts/truetype/montserrat/Montserrat-Bold.ttf",
        "/usr/share/fonts/truetype/inter/Inter-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]
    FONT_REG  = [
        "/usr/share/fonts/truetype/montserrat/Montserrat-Regular.ttf",
        "/usr/share/fonts/truetype/inter/Inter-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]

    # ----- TEXT FIT: kutuya sığdırmak için ikili arama -----
    pad = int(min(W, H) * 0.02)
    box_w, box_h = (tx2 - tx1 - pad*2), (ty2 - ty1 - pad*2)
    title_color = (35, 28, 24, 255)  # koyu kahverengi/siyahımsı: açık zeminde net
    max_lines = 3

    def wrap_to_width(text, font):
        words, lines, cur = text.split(), [], []
        for w in words:
            test = (" ".join(cur+[w])).strip()
            if draw.textlength(test, font=font) > box_w and cur:
                lines.append(" ".join(cur))
                cur = [w]
            else:
                cur.append(w)
        if cur: lines.append(" ".join(cur))
        return "\n".join(lines[:max_lines])

    # ikili arama ile en büyük font
    lo, hi = int(H*0.03), int(H*0.10)
    best_font, best_text, best_bbox = None, None, None
    while lo <= hi:
        mid = (lo+hi)//2
        f = _pick(FONT_BOLD, mid)
        wrapped = wrap_to_width(title_text or "", f)
        bbox = draw.multiline_textbbox((0,0), wrapped, font=f, spacing=6)
        w, h = bbox[2]-bbox[0], bbox[3]-bbox[1]
        if w <= box_w and h <= box_h:
            best_font, best_text, best_bbox = f, wrapped, bbox
            lo = mid + 1
        else:
            hi = mid - 1

    if best_font is None:
        best_font = _pick(FONT_BOLD, int(H*0.04))
        best_text = wrap_to_width(title_text or "", best_font)
        best_bbox = draw.multiline_textbbox((0,0), best_text, font=best_font, spacing=6)

    tw, th = best_bbox[2]-best_bbox[0], best_bbox[3]-best_bbox[1]
    tx = tx1 + pad
    ty = ty1 + pad + (box_h - th)//6  # biraz yukarıdan başlat, altta CTA’ya yer kalsın

    # çok hafif gölge
    for dx, dy in ((1,1),(0,1),(1,0)):
        draw.multiline_text((tx+dx, ty+dy), best_text, font=best_font, fill=(0,0,0,70), spacing=6)
    draw.multiline_text((tx, ty), best_text, font=best_font, fill=title_color, spacing=6)

    # ----- CTA (kutunun içinde, başlığın altında) -----
    cta_text = "Read More"
    cta_font = _pick(FONT_BOLD, max(18, int(best_font.size*0.55)))
    cta_text_w = int(draw.textlength(cta_text, font=cta_font))
    cta_h = int(cta_font.size*1.8)
    cta_w = cta_text_w + pad*2
    cta_x = tx
    cta_y = ty + th + int(pad*0.8)
    draw.rounded_rectangle([cta_x, cta_y, cta_x+cta_w, cta_y+cta_h], radius=cta_h//2, fill=(255,255,255,245))
    draw.text((cta_x + pad, cta_y + (cta_h - cta_font.size)//2 - 2), cta_text, font=cta_font, fill=(30,30,30,255))

    # ----- Kaydet -----
    base.convert("RGB").save(output_path, quality=92, optimize=True, progressive=True)
    return output_path



def get_conn():
    return duckdb.connect(DB_PATH, read_only=False)

def fetch_post(conn, post_id: Optional[str]=None, post_slug: Optional[str]=None):
    if post_id:
        q = """SELECT idea_id, title FROM blog_contents WHERE idea_id=? LIMIT 1"""
        row = conn.execute(q, [post_id]).fetchone()
    else:
        q = """SELECT idea_id, title FROM blog_contents WHERE slug=? LIMIT 1"""
        row = conn.execute(q, [post_slug]).fetchone()
    if not row:
        raise RuntimeError("Post not found")
    return {"id": row[0], "title": row[1]}


def fetch_best_image_url(conn, post_id: str) -> Optional[str]:
    """
    Finds the best available image URL for a given post (idea_id).
    Priority:
      1. idea_products → product_media.image_url
      2. idea_products → products.image_url
      3. blog_post_products_map → product_media.image_url or products.image_url
    """
    # 1️⃣ Try idea_products → product_media
    idea_row = conn.execute(
        "SELECT idea_id FROM blog_contents WHERE idea_id=? LIMIT 1", [post_id]
    ).fetchone()
    idea_id = idea_row[0] if idea_row else None

    if idea_id:
        q1 = """
        SELECT image_url FROM (
            SELECT pm.image_url,
                   ROW_NUMBER() OVER (
                       PARTITION BY pm.parent_asin
                       ORDER BY pm.created_at DESC
                   ) AS rn
            FROM idea_products ip
            JOIN product_media pm ON pm.parent_asin = ip.parent_asin
            WHERE ip.idea_id = ?
              AND pm.image_url IS NOT NULL
              AND TRIM(pm.image_url) <> ''
        )
        WHERE rn = 1
        LIMIT 1;
        """
        r1 = conn.execute(q1, [idea_id]).fetchone()
        if r1 and r1[0]:
            return r1[0]

    # 2️⃣ Try idea_products → products.image_url
    if idea_id:
        q2 = """
        SELECT p.image_url
        FROM idea_products ip
        JOIN products p ON p.parent_asin = ip.parent_asin
        WHERE ip.idea_id = ?
          AND p.image_url IS NOT NULL
          AND TRIM(p.image_url) <> ''
        LIMIT 1;
        """
        r2 = conn.execute(q2, [idea_id]).fetchone()
        if r2 and r2[0]:
            return r2[0]

    # 3️⃣ Try blog_post_products_map → product_media.image_url or products.image_url
    q3 = """
    SELECT img FROM (
        SELECT 
            COALESCE(pm.image_url, p.image_url) AS img,
            ROW_NUMBER() OVER (
                PARTITION BY m.parent_asin
                ORDER BY pm.created_at DESC
            ) AS rn
        FROM blog_post_products_map m
        LEFT JOIN products p ON p.parent_asin = m.parent_asin
        LEFT JOIN product_media pm ON pm.parent_asin = m.parent_asin
        WHERE m.post_id = ?
          AND COALESCE(pm.image_url, p.image_url) IS NOT NULL
          AND TRIM(COALESCE(pm.image_url, p.image_url)) <> ''
    )
    WHERE rn = 1
    LIMIT 1;
    """
    r3 = conn.execute(q3, [post_id]).fetchone()
    if r3 and r3[0]:
        return r3[0]

    return None




def update_post_hero(conn, post_id: str, hero_url_path: str):
    conn.execute("UPDATE blog_contents SET hero_image_url = ? WHERE idea_id = ?", [hero_url_path, post_id])
    conn.commit()


def download_image(url: str) -> Image.Image:
    r = requests.get(url, timeout=40)
    r.raise_for_status()
    img = Image.open(io.BytesIO(r.content)).convert("RGBA")
    return img

def fit_cover(img: Image.Image, w: int, h: int) -> Image.Image:
    ratio = max(w / img.width, h / img.height)
    new_size = (max(1, int(img.width*ratio)), max(1, int(img.height*ratio)))
    im = img.resize(new_size, Image.LANCZOS)
    x = (im.width - w) // 2
    y = (im.height - h) // 2
    return im.crop((x, y, x+w, y+h))

def draw_gradient_overlay(canvas: Image.Image, top_alpha=40, bottom_alpha=220):
    # vertical black gradient (top to bottom)
    overlay = Image.new("RGBA", canvas.size, (0,0,0,0))
    w, h = canvas.size
    grad = Image.new("L", (1, h))
    for y in range(h):
        # ease-in alpha
        a = int(top_alpha + (bottom_alpha - top_alpha) * (y / h)**0.9)
        grad.putpixel((0, y), max(0, min(255, a)))
    grad = grad.resize((w, h))
    overlay.putalpha(grad)
    return Image.alpha_composite(canvas, overlay)

def add_soft_shadow(base: Image.Image, obj: Image.Image, box: tuple, blur=24, expand=32, opacity=180):
    """
    Adds a soft drop shadow behind 'obj' then pastes the object.
    box = (x, y) top-left where obj will be pasted
    """
    x, y = box
    shadow = Image.new("RGBA", (obj.width + expand*2, obj.height + expand*2), (0,0,0,0))
    sdraw = ImageDraw.Draw(shadow)
    sdraw.rectangle((expand, expand, expand+obj.width, expand+obj.height), fill=(0,0,0,opacity))
    shadow = shadow.filter(ImageFilter.GaussianBlur(blur))
    base.alpha_composite(shadow, (x-expand, y-expand))
    base.alpha_composite(obj, (x, y))

def wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_w: int, max_lines: int=3):
    words = text.strip().split()
    lines, cur = [], []
    for w in words:
        cur.append(w)
        t = " ".join(cur)
        if draw.textlength(t, font=font) > max_w:
            cur.pop()
            lines.append(" ".join(cur))
            cur = [w]
            if len(lines) == max_lines:
                break
    if cur and len(lines) < max_lines:
        lines.append(" ".join(cur))
    # ellipsis if overflow
    if len(lines) > max_lines:
        lines = lines[:max_lines]
    if len(lines) == max_lines and " ".join(words) != " ".join(lines):
        # append ellipsis to last line if needed
        last = lines[-1]
        ell = "…"
        while draw.textlength(last + ell, font=font) > max_w and last:
            last = last[:-1]
        lines[-1] = last + ell
    return "\n".join(lines)

def build_banner(product_img_url: str, title: str, cta_text="Shop Now") -> bytes:
    # Base canvas RGBA
    canvas = Image.new("RGBA", (WIDTH, HEIGHT), (10, 12, 14, 255))

    # Download product image
    prod = download_image(product_img_url)

    # Background: blurred cover from product
    bg = fit_cover(prod, WIDTH, HEIGHT).filter(ImageFilter.GaussianBlur(48))
    # slightly desaturate by blending with gray
    gray = Image.new("RGBA", bg.size, (30,30,30,255))
    bg = Image.blend(bg, gray, 0.25)
    canvas = Image.alpha_composite(canvas, bg)

    # Dark gradient overlay for readability
    canvas = draw_gradient_overlay(canvas, top_alpha=30, bottom_alpha=220)

    # Foreground product: fit height ~70% of canvas height, keep aspect
    target_h = int(HEIGHT * 0.7)
    scale = target_h / prod.height
    fw = int(prod.width * scale)
    fh = int(prod.height * scale)
    prod_fg = prod.resize((fw, fh), Image.LANCZOS)

    # Place product left area with margins
    margin = 60
    left_w = int(WIDTH * 0.5)
    px = margin
    py = int((HEIGHT - fh) / 2)

    # Add soft shadow and paste product
    add_soft_shadow(canvas, prod_fg, (px, py), blur=28, expand=40, opacity=170)

    draw = ImageDraw.Draw(canvas)
    # Fonts
    title_font = pick_font(FONT_CANDIDATES_BOLD, 72)
    cta_font   = pick_font(FONT_CANDIDATES_BOLD, 36)

    # Title area on right
    right_x = left_w + margin
    right_w = WIDTH - right_x - margin
    # Wrap title
    title_wrapped = wrap_text(draw, title, title_font, right_w, max_lines=3)

    # Title shadow (subtle)
    tw, th = draw.multiline_textbbox((0,0), title_wrapped, font=title_font, spacing=8)[2:]
    tx, ty = right_x, int(HEIGHT*0.28) - th//2
    shadow = Image.new("RGBA", (tw+8, th+8), (0,0,0,0))
    sdraw = ImageDraw.Draw(shadow)
    sdraw.multiline_text((4,4), title_wrapped, font=title_font, fill=(0,0,0,160), spacing=8)
    shadow = shadow.filter(ImageFilter.GaussianBlur(2))
    canvas.alpha_composite(shadow, (tx-4, ty-4))
    draw.multiline_text((tx, ty), title_wrapped, font=title_font, fill=(255,255,255,240), spacing=8)

    # CTA pill under title
    cta_pad_x, cta_pad_y = 26, 14
    cta_w = int(draw.textlength(cta_text, font=cta_font)) + cta_pad_x*2
    cta_h = 60
    cta_x = tx
    cta_y = ty + th + 30
    # pill bg (white)
    draw.rounded_rectangle([cta_x, cta_y, cta_x+cta_w, cta_y+cta_h], radius=cta_h//2, fill=(255,255,255,255))
    # text
    draw.text((cta_x + cta_pad_x, cta_y + (cta_h - cta_font.size)//2 - 4), cta_text,
              font=cta_font, fill=(10,10,10,255))

    # optional small watermark/brand
    brand = "MintiProduct"
    brand_font = pick_font(FONT_CANDIDATES_REG, 22)
    bw = draw.textlength(brand, font=brand_font)
    draw.text((WIDTH - bw - 24, HEIGHT - 32 - 8), brand, font=brand_font, fill=(220,220,220,220))

    out = io.BytesIO()
    canvas.convert("RGB").save(out, format="JPEG", quality=92, optimize=True, progressive=True)
    return out.getvalue()

def save_hero(img_bytes: bytes):
    ts = int(time.time())
    fname = f"hero-{ts}.jpg"
    os.makedirs(ASSETS_DIR, exist_ok=True)
    fpath = os.path.join(ASSETS_DIR, fname)
    with open(fpath, "wb") as f:
        f.write(img_bytes)
    return fpath, f"{ASSETS_URL_PREFIX}/{fname}"


def main(post_id: Optional[str], post_slug: Optional[str]):
    import os, time, logging
    from datetime import datetime

    conn = get_conn()
    post = fetch_post(conn, post_id=post_id, post_slug=post_slug)
    img_url = fetch_best_image_url(conn, post["id"])
    if not img_url:
        raise RuntimeError("No product image found for this post.")

    public_url = None

    # 1) TEMPLATE (Canva’dan koyduğun arka plan) ile dene
    try:
        ts = int(time.time())
        fname = f"hero-{ts}.jpg"
        os.makedirs(ASSETS_DIR, exist_ok=True)
        output_path = os.path.join(ASSETS_DIR, fname)

        # Bu fonksiyon dosyayı DISKE kaydediyor (bytes dönmüyor)
        generate_banner_with_template(img_url, post["title"], output_path)

        public_url = f"{ASSETS_URL_PREFIX}/{fname}"
        logging.info(f"Template banner created: {public_url}")
    except Exception as e:
        logging.warning(f"Template banner failed, falling back. Reason: {e}")

    # 2) FALLBACK: free banner (PIL) ile üret
    if not public_url:
        jpeg = build_banner(img_url, post["title"], cta_text="Read More")
        _, public_url = save_hero(jpeg)

    # 3) DB’yi tek bir URL ile güncelle
    update_post_hero(conn, post["id"], public_url)
    print(public_url)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Generate free hero banner without Canva")
    ap.add_argument("--post-id", help="blog_contents.idea_id", default=None)
    ap.add_argument("--post-slug", help="blog_contents.slug", default=None)
    
    args = ap.parse_args()
    if not args.post_id and not args.post_slug:
        ap.error("Provide --post-id or --post-slug")
    main(args.post_id, args.post_slug)
