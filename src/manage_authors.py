import os
import re
import json
import argparse
import duckdb
from datetime import datetime
from openai import OpenAI
from dotenv import load_dotenv
import random

# --- Config & Setup ---
load_dotenv()
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "warehouse", "blog_factory.duckdb")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL_GPT = os.getenv("OPENAI_MODEL_GPT", "gpt-4o-mini")
client = OpenAI(api_key=OPENAI_API_KEY)

def slugify(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"-+", "-", s)
    return s.strip("-")

def connect_db():
    return duckdb.connect(DB_PATH, read_only=False)

def _existing_names(con):
    rows = con.execute("SELECT display_name FROM authors").fetchall()
    return [r[0] for r in rows] if rows else []

def generate_author_profile(category: str) -> dict:
    print(f"🤖 Generating author profile for category: {category}...")
    con = connect_db()
    avoid = _existing_names(con)
    con.close()

    seed = random.randint(1000, 9999)

    prompt = f"""
You are a creative director for a product review blog. Create a new, fictional author persona who is an expert in the '{category}' category.

Return only a JSON object with EXACTLY these three lowercase keys: "name", "bio", "image_prompt". Do not include code fences or extra text.

Requirements:
1) name: A realistic full name (obvious gender). Rotate across different cultural backgrounds, not too generic. Avoid reusing recent names: {avoid}.
2) bio: 2–3 sentences. Must be consistent with gender of the name. Mention expertise in '{category}' plus a personal detail (family, hobby).
3) image_prompt: A natural, candid, photorealistic portrait description (NOT digital art). Use realistic everyday scenes. Include slight imperfections.

Randomizer: {seed}
"""

    resp = client.chat.completions.create(
        model=OPENAI_MODEL_GPT,
        messages=[
            {"role": "system", "content": "You generate concise persona data strictly as json."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.9,
        presence_penalty=0.3,
        frequency_penalty=0.2,
        max_tokens=500,
        response_format={"type": "json_object"},
    )

    raw = resp.choices[0].message.content
    print("🔍 Raw profile:", raw)
    profile = json.loads(raw)
    profile = {k.lower(): v for k, v in profile.items()}  # normalize keys

    print("✅ LLM generated profile successfully.")
    return profile

# --- Image Generation ---
def generate_author_avatar(author_id: str, base_prompt: str, style: str = "candid") -> str:
    from src.hero_image import generate_hero_assets
    import random

    # iPhone 8 vibe: küçük sensör, sınırlı DR, hafif keskinlik halo, JPEG artefaktı
    candid_scenes = [
        "standing curbside on a quiet residential street, late afternoon",
        "sitting at a small neighborhood cafe by a window, mixed indoor daylight",
        "on a sidewalk near parked cars, slightly overcast day",
        "in a public park with patchy tree shade, uneven lighting on the face",
        "next to a front door on a small porch with a single warm porch light in the evening",
    ]
    scene = random.choice(candid_scenes)

    # DSLR/Studio/CGI çağrışımlarını bastır
    negative = (
        "DSLR, mirrorless, medium format, interchangeable lens, shallow DOF, "
        "creamy bokeh, studio backdrop, ring light, softbox, rim light, "
        "professional retouch, airbrushed, perfect skin, glamour editorial, "
        "hdr tonemapping, 3d, render, cgi, illustration, anime, painting, "
        "plastic skin, beauty dish, makeup heavy, watermark, text overlay, logo, frame, border"
    )

    # Biraz EXIF-benzeri ayrıntı amatör hissi güçlendirir (model bunu gerçek EXIF sanmayacak ama stili etkiler)
    iso = random.choice([80, 100, 125, 160, 200, 250])
    speed = random.choice(["1/60s", "1/90s", "1/120s", "1/180s"])
    salt = random.randint(1000, 9999)

    iphone8_meta = (
        f"shot on Apple iPhone 8 rear camera, 4.0mm f/1.8, ISO {iso}, {speed}, "
        "no portrait mode, no HDR, vertical smartphone framing 3:4, sRGB"
    )

    # Küçük sensör görünümü + amatör eksikler
    phone_look = (
        "amateur unposed smartphone photo, natural ambient light, slight hand-held motion blur, "
        "limited dynamic range with mildly clipped highlights, auto white balance slightly warm, "
        "subtle sharpening halos on edges, slight chroma noise in shadows, mild JPEG compression artifacts, "
        "realistic skin texture and pores, flyaway hair, a touch of uneven lighting, "
        f"{scene}"
    )

    # Nihai prompt
    final_prompt = (
        f"{base_prompt} ({salt}). {iphone8_meta}. {phone_look}. "
        f"not a studio, not staged, everyday candid. negative: ({negative})"
    )
    

    print("🎨 Generating author avatar...")
    assets = generate_hero_assets(
        slug=f"author-{author_id}",
        title=author_id,
        prompt=final_prompt
        # Eğer generate_hero_assets aspect/size kabul ediyorsa kullanışlı olur:
        # , aspect='3:4', size='1024x1365'
    )

    avatar_url = (assets or {}).get("thumb_url") or ""
    return avatar_url

# --- Database Operations ---
def create_author(args):
    category_slug = args.category
    profile = generate_author_profile(category_slug)

    author_id = slugify(profile.get("name", ""))
    display_name = profile.get("name", "")
    author_bio = profile.get("bio", "")
    avatar_url = generate_author_avatar(author_id, profile.get("image_prompt", ""))

    con = connect_db()
    _assert_category_exists(con, category_slug)
    con.execute("""
        INSERT INTO authors (
            author_id, display_name, avatar_url, author_bio,
            created_at, primary_category_slug
        )
        VALUES (?, ?, ?, ?, ?, ?)
    """, [author_id, display_name, avatar_url, author_bio,
          datetime.utcnow(), category_slug])
    con.close()

    print(f"✅ Author '{display_name}' created with category '{category_slug}'")

def delete_author(args):
    author_id = args.author_id
    con = connect_db()
    author = con.execute("SELECT display_name FROM authors WHERE author_id = ?", [author_id]).fetchone()
    if not author:
        print(f"❌ Author with ID '{author_id}' not found.")
        return
    confirm = input(f"Are you sure you want to delete '{author[0]}' (ID: {author_id})? (y/n): ").lower()
    if confirm == "y":
        con.execute("DELETE FROM authors WHERE author_id = ?", [author_id])
        print(f"✅ Author '{author[0]}' deleted.")
    else:
        print("Aborted.")
    con.close()

def list_authors(args):
    con = connect_db()
    rows = con.execute("SELECT author_id, display_name, primary_category_slug FROM authors").fetchall()
    for r in rows:
        print(r)
    con.close()

def _assert_category_exists(con, slug: str):
    ok = con.execute("SELECT 1 FROM categories_tree WHERE slug = ? LIMIT 1", [slug]).fetchone()
    if not ok:
        raise ValueError(f"Unknown category slug: {slug}")


# --- CLI Parser ---
def main():
    parser = argparse.ArgumentParser(description="Manage blog authors.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_parser = subparsers.add_parser("create", help="Create a new author using AI.")
    create_parser.add_argument("--category", type=str, required=True, help="Category slug (e.g., 'electronics')")
    create_parser.set_defaults(func=create_author)

    delete_parser = subparsers.add_parser("delete", help="Delete an author by ID.")
    delete_parser.add_argument("--author-id", type=str, required=True)
    delete_parser.set_defaults(func=delete_author)

    list_parser = subparsers.add_parser("list", help="List authors.")
    list_parser.set_defaults(func=list_authors)

    args = parser.parse_args()
    args.func(args)

if __name__ == "__main__":
    main()
