#!/usr/bin/env python3
import os
import duckdb
import pandas as pd
from datetime import datetime
from dotenv import load_dotenv

# load env (.env içindeki BASE_URL, DB_PATH falan için)
load_dotenv("/home/ubuntu/blog-factory/.env")

# config
DB_PATH   = os.getenv("DB_PATH", "/home/ubuntu/blog-factory/warehouse/blog_factory.duckdb")
BASE_URL  = os.getenv("BASE_URL", "https://mintiproduct.com").rstrip("/")
WEB_ROOT  = os.getenv("WEB_ROOT", "/var/www/html").rstrip("/")

SITEMAP_PATH = os.path.join(WEB_ROOT, "sitemap.xml")
ROBOTS_PATH  = os.path.join(WEB_ROOT, "robots.txt")

def fetch_published_posts(db_path: str):
    """
    blog_posts.status='published' olan entry'leri çek
    bunları blog_contents ile joinle
    URL + lastmod bilgisi döndür
    """
    con = duckdb.connect(db_path, read_only=True)
    df = con.execute("""
        SELECT
            bc.slug,
            bc.category_slug,
            COALESCE(bc.updated_at, bp.date_published, bp.created_at, now()) AS lastmod
        FROM blog_posts bp
        LEFT JOIN blog_contents bc
          ON bc.idea_id = bp.idea_id
        WHERE bp.status = 'published'
          AND bc.slug IS NOT NULL
          AND length(trim(bc.slug)) > 0
    """).df()
    con.close()
    return df


def build_url_list(df: pd.DataFrame):
    """
    df içinden tam URL + lastmod string listesi çıkar
    duplicate URL varsa tekilleştir
    lastmod ISO date (YYYY-MM-DD)
    """
    urls = []
    seen = set()

    for _, row in df.iterrows():
        slug = (row.get("slug") or "").strip("/")
        cat  = (row.get("category_slug") or "").strip("/")
        if not slug:
            continue

        # category_slug boşsa direkt /slug/ olarak yayınlama fallback'i websitede var mı?
        # routes.py'ye baktık: /<slug>/ route'u var, ama canonical aslında /<cat>/<slug>/
        # SEO için canonical olan formu basalım. cat boşsa sadece /slug/ yaparız.
        if cat:
            path = f"/{cat}/{slug}/"
        else:
            path = f"/{slug}/"

        full_url = BASE_URL.rstrip("/") + path

        if full_url in seen:
            continue
        seen.add(full_url)

        # lastmod normalize
        lm = row.get("lastmod")
        # lm hem string hem Timestamp olabilir, normalize edelim
        if pd.isna(lm):
            lm_dt = datetime.utcnow()
        else:
            try:
                # duckdb timestamp to pandas Timestamp -> to_pydatetime
                if hasattr(lm, "to_pydatetime"):
                    lm_dt = lm.to_pydatetime()
                else:
                    lm_dt = pd.to_datetime(lm).to_pydatetime()
            except Exception:
                lm_dt = datetime.utcnow()

        lastmod_str = lm_dt.strftime("%Y-%m-%d")
        urls.append((full_url, lastmod_str))

    return urls


def add_static_urls(urls: list[tuple[str,str]]):
    """
    site içi sabit sayfaları da ekle (home, about, privacy ...)
    hepsine lastmod = today veriyoruz
    """
    today = datetime.utcnow().strftime("%Y-%m-%d")

    static_paths = [
        "/",                # homepage
        "/about/",
        "/privacy-policy/",
        "/terms-of-service/",
        "/authors/",
    ]

    existing = {u for (u, _) in urls}
    for sp in static_paths:
        full = BASE_URL.rstrip("/") + sp
        if full not in existing:
            urls.append((full, today))

    return urls


def generate_sitemap_xml(urls: list[tuple[str,str]]) -> str:
    """
    urls -> XML string
    """
    items = []
    for loc, lastmod in urls:
        items.append(
            "  <url>\n"
            f"    <loc>{loc}</loc>\n"
            f"    <lastmod>{lastmod}</lastmod>\n"
            "    <changefreq>weekly</changefreq>\n"
            "    <priority>0.6</priority>\n"
            "  </url>"
        )

    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(items)
        + "\n</urlset>\n"
    )
    return xml


def write_file(path: str, content: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def write_robots_txt():
    robots_body = (
        "User-agent: *\n"
        "Allow: /\n"
        "\n"
        f"Sitemap: {BASE_URL.rstrip('/')}/sitemap.xml\n"
    )
    write_file(ROBOTS_PATH, robots_body)


def main():
    df = fetch_published_posts(DB_PATH)
    urls = build_url_list(df)
    urls = add_static_urls(urls)

    sitemap_xml = generate_sitemap_xml(urls)
    write_file(SITEMAP_PATH, sitemap_xml)
    write_robots_txt()

    print(f"OK: wrote {len(urls)} urls to {SITEMAP_PATH}")
    print(f"OK: wrote robots.txt to {ROBOTS_PATH}")


if __name__ == "__main__":
    main()
