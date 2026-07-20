# src/hero_image.py
import os, re, io, base64
from datetime import datetime
from PIL import Image
from dotenv import load_dotenv

load_dotenv()  # .env okuyalım (cron/daily_job ortamında da)

WEB_ROOT = os.getenv("WEB_ROOT", "/var/www/html")
BASE_URL = os.getenv("BASE_URL", "https://mintistudio.com")
OPENAI_MODEL_IMAGE = os.getenv("OPENAI_MODEL_IMAGE", "dall-e-3")

SIZES_OUT = {
    "banner": (1920, 1080),
    "hero":   (1200, 675),
    "thumb":  (800, 450),
}

def _safe_slug(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9\-]+", "-", s).strip("-")
    return s or "post"

def _default_prompt(title: str) -> str:
    return (f"{title}. 16:9 lifestyle photo, soft natural light, clean background, "
            "no text, no brand logos, high detail, product category implied.")

def _crop_to_16x9(img: Image.Image) -> Image.Image:
    w, h = img.size
    target = 16/9
    r = w / h
    if abs(r - target) < 0.01:
        return img
    if r > target:
        new_w = int(h * target); left = (w - new_w) // 2
        return img.crop((left, 0, left + new_w, h))
    new_h = int(w / target); top = (h - new_h) // 2
    return img.crop((0, top, w, top + new_h))

def _save_jpg(img: Image.Image, path: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    img.save(path, format="JPEG", quality=90, optimize=True, progressive=True)

def generate_hero_assets(slug: str, title: str, prompt: str | None) -> dict:
    """
    1) OpenAI (dall-e-3) ile 1792x1024 görsel üretir (yatay).
    2) 16:9 crop yapar, 3 boyuta kaydeder (banner/hero/thumb).
    3) DÜZ klasöre yazar: /assets/hero/<type>-<timestamp>.jpg  (slug YOK)
       Örn: /assets/hero/hero-20250926061024.jpg
    """
    from openai import OpenAI
    import requests, io, base64

    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        # Key yoksa pipeline'ı durdurma
        return {
            "banner_url": "",
            "hero_url": "",
            "thumb_url": "",
            "alt": f"Illustrative hero image for: {title}",
        }

    client = OpenAI(api_key=api_key)

    # Prompt hazırla
    prompt = (prompt or "").strip() or _default_prompt(title)

    # DÜZ klasör (slug yok)
    out_dir = os.path.join(WEB_ROOT, "assets", "hero")
    os.makedirs(out_dir, exist_ok=True)
    url_base = BASE_URL.rstrip("/") + "/assets/hero"
    ts = datetime.utcnow().strftime("%Y%m%d%H%M%S")

    try:
        # Model ve boyut
        resp = client.images.generate(
            model=os.getenv("OPENAI_MODEL_IMAGE", "dall-e-3"),
            prompt=prompt,
            size="1024x1024",   # daha ucuz boyut
            n=1,
        )


        # Image verisini al (b64_json varsa onu, yoksa URL’den indir)
        raw = None
        d = resp.data[0]
        if getattr(d, "b64_json", None):
            raw = base64.b64decode(d.b64_json)
        elif getattr(d, "url", None):
            r = requests.get(d.url, timeout=20)
            r.raise_for_status()
            raw = r.content
        if not raw:
            raise RuntimeError("No image data returned from API")

        # Pillow: crop + resize
        base_img = Image.open(io.BytesIO(raw)).convert("RGB")
        img_16x9 = _crop_to_16x9(base_img)

        filenames = {}
        for key, (tw, th) in SIZES_OUT.items():
            im = img_16x9.resize((tw, th), Image.LANCZOS)
            filename = f"{key}-{ts}.jpg"           # <-- slug yok
            p = os.path.join(out_dir, filename)
            _save_jpg(im, p)
            print(f"✅ hero image saved: {p}")   # DEBUG log
            filenames[key] = filename


        return {
            "banner_url": f"{url_base}/{filenames['banner']}",
            "hero_url":   f"{url_base}/{filenames['hero']}",
            "thumb_url":  f"{url_base}/{filenames['thumb']}",
            "alt":        f"Illustrative hero image for: {title}",
        }

    except Exception as e:
        print(f"⚠️ hero image generation failed: {e}")
        return {"banner_url": "", "hero_url": "", "thumb_url": "", "alt": ""}
