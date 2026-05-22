import os
from pathlib import Path

# .env yükle
try:
    from dotenv import load_dotenv
    ROOT = Path(__file__).resolve().parents[1]
    load_dotenv(ROOT / ".env")   # projenin kökündeki .env
except Exception:
    pass

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL_GPT = os.getenv("OPENAI_MODEL_GPT", "gpt-4.1-mini")
MAX_OUT_TOKENS = int(os.getenv("MAX_OUT_TOKENS", "4000"))
# === Site Ayarları ===
BASE_URL = os.getenv("BASE_URL", "https://mintiproduct.com")

# === Dizinler ===
ROOT = Path(__file__).resolve().parents[1]
DATA_CSV = str(ROOT / "data" / "beauty_products_summaries.csv")  # default; CLI ile override edilir

# ÇIKTI: Artık docs/blogs altına yazıyoruz
DOCS_DIR = ROOT / "docs"
BLOGS_DIR = DOCS_DIR / "blogs"
BLOGS_DIR.mkdir(parents=True, exist_ok=True)

DOCS_BLOG_INDEX = ROOT / "docs" / "blogs" / "index.md"
DOCS_BLOG_INDEX.parent.mkdir(parents=True, exist_ok=True)


IDX_DIR = ROOT / "indexes"
IDX_DIR.mkdir(parents=True, exist_ok=True)

BLOG_INDEX_CSV = IDX_DIR / "blog_index.csv"
DEBUG_LAST_RUN = IDX_DIR / "debug_last_run.json"
