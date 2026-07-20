# scripts/ig_one_off_post.py
import os, sys, requests
from dotenv import load_dotenv
load_dotenv()

IG_ID = os.getenv("INSTAGRAM_ACCOUNT_ID")          # 1784...
PAGE_TOKEN = os.getenv("INSTAGRAM_PAGE_TOKEN")     # Page Access Token

if not IG_ID or not PAGE_TOKEN:
    sys.exit("ENV eksik: INSTAGRAM_ACCOUNT_ID ve INSTAGRAM_PAGE_TOKEN zorunlu.")

IMAGE_URL = os.getenv("IMAGE_URL") or "https://your-cdn.com/test.jpg"
CAPTION   = os.getenv("CAPTION")   or "🚀 Blog Factory test — mintistudio.com"

def post_image(image_url: str, caption: str):
    # 1) container
    r = requests.post(
        f"https://graph.facebook.com/v24.0/{IG_ID}/media",
        data={"image_url": image_url, "caption": caption, "access_token": PAGE_TOKEN},
        timeout=30
    )
    r.raise_for_status()
    cid = r.json().get("id")
    if not cid:
        raise SystemExit(f"Container hatası: {r.text}")

    # 2) publish
    r2 = requests.post(
        f"https://graph.facebook.com/v24.0/{IG_ID}/media_publish",
        data={"creation_id": cid, "access_token": PAGE_TOKEN},
        timeout=30
    )
    r2.raise_for_status()
    return r2.json()

if __name__ == "__main__":
    print(post_image(IMAGE_URL, CAPTION))
