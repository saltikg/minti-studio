import subprocess
import os
import sys
import duckdb

BASE_DIR = "/home/ubuntu/blog-factory"
DB_PATH = os.path.join(BASE_DIR, "warehouse", "blog_factory.duckdb")
STATE_FILE = os.path.join(BASE_DIR, ".last_category")

def get_categories_from_db():
    """ideas tablosundan mevcut kategorileri alır."""
    con = duckdb.connect(DB_PATH, read_only=True)
    df = con.execute("SELECT DISTINCT category_slug FROM ideas ORDER BY category_slug").df()
    con.close()
    return df["category_slug"].tolist()

def pick_next_category(categories, last_cat=None):
    """Sıradaki kategoriyi round-robin ile seçer."""
    if not categories:
        return None

    if last_cat in categories:
        idx = categories.index(last_cat)
        next_idx = (idx + 1) % len(categories)
        next_cat = categories[next_idx]
    else:
        next_cat = categories[0]  # ilk çalışmada ilk kategori

    return next_cat

def run_daily():
    python_executable = os.path.join(BASE_DIR, ".venv", "bin", "python")

    categories = get_categories_from_db()
    if not categories:
        print("⚠️ No categories found in DB. Exiting.")
        return

    last_cat = None
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            last_cat = f.read().strip()

    attempts = 0
    success = False
    tried = []

    while attempts < len(categories) and not success:
        category = pick_next_category(categories, last_cat)
        tried.append(category)

        print(f"▶️ Running pipeline for category: {category}")

        pipeline_cmd = [
            python_executable, "-m", "src.pipeline",
            "--max-blogs", "1",
            "--min-products", "4",
            "--availability-check", "1",
            "--amazon-strict", "0",
            "--request-delay", "1.2",
            "--request-timeout", "7",
            "--max-check", "5",
            "--source", "db",
            "--category", category,
        ]

        result = subprocess.run(pipeline_cmd, text=True, cwd=BASE_DIR, capture_output=True)

        # stdout’u yaz
        print(result.stdout)
        if result.stderr:
            print(result.stderr, file=sys.stderr)

        if result.returncode == 0 and "📝 wrote:" in result.stdout:
            print(f"✅ Success: Blog created for {category}")
            success = True
            # başarılı kategori state file’a yaz
            with open(STATE_FILE, "w") as f:
                f.write(category)
        else:
            print(f"⚠️ Skip or fail for {category}, trying next...")
            last_cat = category  # sıradaki için güncelle
            attempts += 1

    if not success:
        print(f"❌ No blog created after trying {attempts} categories: {tried}")

if __name__ == "__main__":
    run_daily()
