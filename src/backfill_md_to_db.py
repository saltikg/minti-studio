import os, re, glob, duckdb, datetime
from pathlib import Path

DB_PATH = "warehouse/blog_factory.duckdb"
DOCS = Path("docs/blogs")

def slug_from_path(p: Path) -> str:
    # category/slug/index.md  -> slug
    # slug/index.md           -> slug
    # slug.md                 -> slug
    if p.name.lower() == "index.md":
        return p.parent.name
    return p.stem

def find_md_files():
    # docs/blogs/**/index.md ve docs/blogs/**/*.md
    for path in DOCS.rglob("*.md"):
        # mkdocs ana index vs. atla
        if path.name == "index.md" and path.parent == DOCS:
            continue
        yield path

def extract_intro(md: str) -> str:
    # İlk boş satıra kadar olan kısmı "introduction" say
    parts = re.split(r"\n\s*\n", md.strip(), maxsplit=1)
    first = parts[0] if parts else ""
    # markdown paragrafını basitçe <p> sarmala
    return f"<p>{first.strip()}</p>" if first.strip() else ""

def main():
    con = duckdb.connect(DB_PATH)
    inserted = 0
    for p in find_md_files():
        try:
            md = p.read_text(encoding="utf-8")
        except Exception:
            continue
        slug = slug_from_path(p)
        # category’yi tahmin et
        parts = p.relative_to(DOCS).parts
        category = parts[0] if len(parts) >= 3 else ("" if len(parts) == 1 else parts[0])

        # başlık: ilk H1 ya da dosya adı
        m = re.search(r"^\s*#\s+(.+)$", md, flags=re.M)
        title = (m.group(1).strip() if m else slug.replace("-", " ").title())

        intro = extract_intro(md)

        # blog_contents’ta slug var mı?
        df = con.execute("SELECT idea_id FROM blog_contents WHERE slug = ? LIMIT 1", [slug]).df()
        if df.empty:
            # idea_id yoksa slug üzerinden ideas/blog_posts ile eşlemeyi deneyelim
            # yoksa rastgele bir placeholder idea_id kullanma — atla
            row = con.execute("""
              SELECT i.idea_id, i.category_slug
              FROM ideas i
              JOIN blog_posts b ON b.idea_id=i.idea_id
              WHERE regexp_extract(b.blog_url, '.*/([^/]+)/?$', 1) = ?
              LIMIT 1
            """, [slug]).fetchone()
            if not row:
                continue
            idea_id, category_slug = row
            con.execute("""
              INSERT INTO blog_contents
              (idea_id, title, slug, category_slug, front_matter, introduction, product_gallery,
               urunler, buyers_guide, faq, conclusion, recommendations, cta, md_full, updated_at)
              VALUES (?, ?, ?, ?, '', ?, '', '', '', '', '', '', '', ?, now())
            """, [idea_id, title, slug, (category or category_slug or ""), intro, md])
            inserted += 1
        else:
            # var ise sadece md_full/introduction boşsa güncelle
            con.execute("""
              UPDATE blog_contents
              SET introduction = CASE WHEN coalesce(nullif(introduction,''),'')='' THEN ? ELSE introduction END,
                  md_full      = CASE WHEN coalesce(nullif(md_full,''),'')='' THEN ? ELSE md_full END,
                  updated_at   = now()
              WHERE slug = ?
            """, [intro, md, slug])
    con.close()
    print(f"Backfill done. Inserted new rows: {inserted}")

if __name__ == "__main__":
    main()