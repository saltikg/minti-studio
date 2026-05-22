import os, random, re, json
from pathlib import Path
from datetime import datetime

def slugify(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"-+", "-", s)
    return s.strip("-") or "post"

def save_markdown(path: str, text: str):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    print(f"📝 wrote: {p}")

def bullets_from_raw(text, max_items=5, max_len=160):
    if not text:
        return []
    parts = re.split(r'[\n\.!?]+', str(text))
    items = [p.strip(" -•\t") for p in parts if p and len(p.strip()) > 0]
    out = []
    for it in items:
        if len(out) >= max_items:
            break
        if len(it) > max_len:
            it = it[: max_len - 3] + "..."
        out.append(it)
    return out

def summarize_raw(text, max_items=5):
    return bullets_from_raw(text, max_items=max_items)

def reading_time_minutes(markdown_text: str, wpm=200) -> int:
    words = re.findall(r"\w+", markdown_text or "")
    return max(2, (len(words) + wpm - 1) // wpm)

def append_link_to_index(index_md_path, title, link_or_slug):
    """
    docs/blogs/index.md dosyasına yeni bir satır ekler.
    - Eski davranış: slug -> '- [Title](slug.md)'  ❌ artık istemiyoruz
    - Yeni davranış: 
        - Eğer '/...' ile başlayan bir URL gelirse aynen kullanılır (sona slash eklenir).
        - Eğer düz slug gelirse '/{slug}/' formatına dönüştürülür.
    Aynı satır varsa tekrar eklemez.
    """
    from pathlib import Path
    p = Path(index_md_path)

    # normalize href (hem eski "slug" hem yeni "blog_url" ile uyumlu)
    raw = str(link_or_slug).strip()
    if raw.startswith("/"):
        href = raw if raw.endswith("/") else raw + "/"
    else:
        # düz slug verilmişse flat yapıda /slug/ kullan
        href = f"/{raw.strip('/')}/"

    link_line = f"- [{title}]({href})"

    if not p.exists():
        header = "# Blog\n\n" + link_line + "\n"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(header, encoding="utf-8")
        print(f"➕ created index with first entry: {p}")
        return

    content = p.read_text(encoding="utf-8")
    if link_line not in content:
        if not content.endswith("\n"):
            content += "\n"
        content += link_line + "\n"
        p.write_text(content, encoding="utf-8")
        print(f"➕ appended to index: {p}")
    else:
        print("ℹ️ link already in index; skipped.")

def write_json(path, obj):
    from pathlib import Path
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)

def build_comparison_table(products_df):
    """
    Ürünler için HTML karşılaştırma tablosu üretir.
    LLM'e göndermeden biz oluşturuyoruz.
    """

    rows = []
    for _, row in products_df.iterrows():
        asin = str(row.get("parent_asin", ""))
        title = str(row.get("product_title", ""))
        image_url = str(row.get("image_url", ""))
        #price = str(row.get("price", ""))
        rating = str(row.get("avg_rating", ""))
        n_reviews = str(row.get("n_reviews", ""))
        amazon_link = row.get("amazon_link", f"https://www.amazon.com/dp/{asin}")

        pros = row.get("review_pros", [])
        cons = row.get("review_cons", [])
        if isinstance(pros, str):
            pros = [p.strip(" -•\t") for p in pros.split("\n") if p.strip()]
        if isinstance(cons, str):
            cons = [c.strip(" -•\t") for c in cons.split("\n") if c.strip()]

        pros_html = "<ul>" + "".join(f"<li>{p}</li>" for p in pros) + "</ul>"
        cons_html = "<ul>" + "".join(f"<li>{c}</li>" for c in cons) + "</ul>"

        row_html = f"""
        <tr>
          <td class="img"><a href="{amazon_link}" target="_blank" rel="nofollow noopener">
            <img src="{image_url}" alt="{title} thumbnail" width="72" height="72"
            loading="lazy" style="object-fit:cover;border-radius:8px" />
          </a></td>
          <td class="name"><a href="{amazon_link}" target="_blank" rel="nofollow noopener">{title}</a><br>
            <small>{n_reviews} reviews, {rating} ★</small>
          </td>
          <td class="pros">{pros_html}</td>
          <td class="cons">{cons_html}</td>
          <td class="cta"><a href="{amazon_link}" target="_blank" rel="nofollow noopener">View on Amazon</a></td>
        </tr>
        """
        rows.append(row_html)

    table_html = f"""
<div class="cmp-table">
  <table>
    <thead>
      <tr>
        <th>Image</th>
        <th>Product</th>
        <th>Pros</th>
        <th>Cons</th>
        <th>Amazon Link</th>
      </tr>
    </thead>
    <tbody>
      {''.join(rows)}
    </tbody>
  </table>
</div>
"""
    return table_html.strip()

def build_product_cards(products_df):
    """
    Ürün kartlarını HTML formatında üretir.
    İstenen sıra:
    - Ürün ismi
    - Ürün resmi + View on Amazon
    - Customer Insights:
        👥 What customers said
        ✨ Loved
        💡 Tips
        ✅ Pros
        ❌ Cons
    - 📖 Read Full Reviews
    """
    cards = []
    for _, row in products_df.iterrows():
        title = str(row.get("product_title", ""))
        asin = str(row.get("parent_asin", ""))
        amazon_link = row.get("amazon_link", f"https://www.amazon.com/dp/{asin}")
        image_url = str(row.get("image_url", ""))
        review_paragraph = str(row.get("review_paragraph", ""))

        # Pros & Cons
        pros = row.get("review_pros", [])
        cons = row.get("review_cons", [])
        if isinstance(pros, str):
            pros = [p.strip(" -•\t") for p in pros.split("\n") if p.strip()]
        if isinstance(cons, str):
            cons = [c.strip(" -•\t") for c in cons.split("\n") if c.strip()]

        pros_html = "".join(f"<li>{p}</li>" for p in pros) if pros else ""
        cons_html = "".join(f"<li>{c}</li>" for c in cons) if cons else ""

        pros_block = f"""
        <div style="margin:0.8em 0;">
          <strong style="color:green;">✅ Pros:</strong>
          <ul style="margin:0.3em 0 0 1.2em;color:#333;">
            {pros_html}
          </ul>
        </div>
        """ if pros_html else ""

        cons_block = f"""
        <div style="margin:0.8em 0;">
          <strong style="color:#c53030;">❌ Cons:</strong>
          <ul style="margin:0.3em 0 0 1.2em;color:#333;">
            {cons_html}
          </ul>
        </div>
        """ if cons_html else ""

        # Loved
        loved = row.get("review_loved", "")
        loved_block = ""
        if isinstance(loved, str) and loved.strip():
            loved_items = [p.strip(" -•\t") for p in loved.split("\n") if p.strip()]
            loved_html = "".join(f"<li>{li}</li>" for li in loved_items)
            loved_block = f"""
            <div style="margin:0.8em 0;">
              <strong style="color:#805ad5;">✨ What Customers Loved:</strong>
              <ul style="margin:0.3em 0 0 1.2em;color:#333;">
                {loved_html}
              </ul>
            </div>
            """

        # Tips
        tips = row.get("review_tips", "")
        tips_block = ""
        if isinstance(tips, str) and tips.strip():
            tips_items = [p.strip(" -•\t") for p in tips.split("\n") if p.strip()]
            tips_html = "".join(f"<li>{ti}</li>" for ti in tips_items)
            tips_block = f"""
            <div style="margin:0.8em 0;">
              <strong style="color:#3182ce;">💡 Tips from Users:</strong>
              <ul style="margin:0.3em 0 0 1.2em;color:#333;">
                {tips_html}
              </ul>
            </div>
            """

        card_html = f"""
<div class="product-card" style="display:flex;align-items:flex-start;margin-bottom:2em;gap:16px;">

  <!-- Product Image + CTA -->
  <div class="image" style="flex:0 0 160px;text-align:center;">
    <a href="{amazon_link}" target="_blank" rel="nofollow noopener">
      <img src="{image_url}" alt="{title} image" width="160" height="160"
           style="object-fit:cover;border-radius:8px;box-shadow:0 2px 6px rgba(0,0,0,0.15);margin-bottom:8px;" />
    </a>
    <a href="{amazon_link}" target="_blank" rel="nofollow noopener" 
       style="display:inline-block;padding:6px 10px;background:#2b6cb0;color:#fff;
              font-size:13px;border-radius:4px;font-weight:600;text-decoration:none;">
      🔗 View on Amazon
    </a>
  </div>

  <!-- Product Content -->
  <div class="content" style="flex:1;">
    <h3 style="margin-top:0;">
      <a href="{amazon_link}" target="_blank" rel="nofollow noopener" 
         style="text-decoration:none;color:#2b6cb0;">
        {title}
      </a>
    </h3>

    <!-- Review summary -->
    <p style="font-style:italic;margin:0.5em 0;color:#444;">
      <span style="font-weight:bold;">👥 What customers said:</span> {review_paragraph}
    </p>

    {loved_block}
    {tips_block}
    {pros_block}
    {cons_block}

    <!-- Secondary CTA -->
    <div style="margin-top:1em;">
      <a href="{amazon_link}" target="_blank" rel="nofollow noopener"
         style="padding:8px 14px;background:#444;color:#fff;border-radius:6px;
                font-weight:600;text-decoration:none;font-size:14px;">
        📖 Read Full Reviews
      </a>
    </div>
  </div>
</div>
"""
        cards.append("\n" + card_html.strip() + "\n")

    return "\n".join(cards)

def build_product_gallery(products_df):
    """
    'Products Featured in This Article' module.
    Each product has image + rating + CTA button.
    First product marked as Editor's Choice.
    """
    items = []
    for i, row in products_df.iterrows():
        title = str(row.get("product_title", ""))
        asin = str(row.get("parent_asin", ""))
        amazon_link = row.get("amazon_link", f"https://www.amazon.com/dp/{asin}")
        image_url = str(row.get("image_url", ""))
        rating = str(row.get("avg_rating", ""))
        n_reviews = str(row.get("n_reviews", ""))

        badge_html = ""
        if i == 0:  # sadece ilk ürüne ekle
            badge_html = '<div class="gallery-card-badge">Editor\'s Choice</div>'

        items.append(f"""
        <div class="gallery-card">
            {badge_html}
            <a href="{amazon_link}" target="_blank" rel="nofollow noopener" title="{title}">
                <img src="{image_url}" alt="{title} image" class="gallery-card-img" />
            </a>
            <div class="gallery-card-body">
                <div class="gallery-card-title">{title}</div>
                <div class="gallery-card-rating">
                    ⭐ {rating} ({n_reviews} reviews)
                </div>
                <a href="{amazon_link}" target="_blank" rel="nofollow noopener" class="gallery-card-cta">
                    See on Amazon
                </a>
            </div>
        </div>
        """)

    html = f"""
<div class="product-gallery">
  <h2>Products Featured in This Article</h2>
  <div class="gallery-grid">
    {''.join(items)}
  </div>
</div>
"""
    return html


def build_product_cards_responsive(products_df):
    """
    Responsive product card layout:
    - 2–4 ürün: hepsi grid yan yana
    - 5+ ürün: ilk 3 grid, kalanlar stacked
    """
    n = len(products_df)

    def build_single_card(row):
        title = str(row.get("product_title", ""))
        asin = str(row.get("parent_asin", ""))
        amazon_link = row.get("amazon_link", f"https://www.amazon.com/dp/{asin}")
        image_url = str(row.get("image_url", ""))

        return f"""
        <div class="product-card" style="flex:1;min-width:220px;max-width:240px;
                    border:1px solid #eee;border-radius:8px;padding:12px;background:#fff;">
            <a href="{amazon_link}" target="_blank" rel="nofollow noopener">
                <img src="{image_url}" alt="{title}" 
                     style="width:100%;height:160px;object-fit:cover;border-radius:6px;" />
            </a>
            <h3 style="font-size:14px;margin:0.6em 0;">{title}</h3>
            <a href="{amazon_link}" target="_blank" rel="nofollow noopener" 
               style="display:inline-block;width:100%;padding:8px 0;background:#2b6cb0;
                      color:#fff;text-align:center;border-radius:4px;font-weight:600;">
                🔗 See on Amazon
            </a>
        </div>
        """

    # 2–4 ürün → grid
    if 2 <= n <= 4:
        cards = [build_single_card(row) for _, row in products_df.iterrows()]
        return '<div style="display:flex;flex-wrap:wrap;gap:20px;margin:1em 0;">' + "".join(cards) + '</div>'

    # 5+ ürün → ilk 3 grid, kalanlar stacked
    elif n >= 5:
        first_cards = [build_single_card(row) for _, row in products_df.head(3).iterrows()]
        grid_html = '<div style="display:flex;flex-wrap:wrap;gap:20px;margin:1em 0;">' + "".join(first_cards) + '</div>'

        rest_cards = [build_single_card(row) for _, row in products_df.iloc[3:].iterrows()]
        stacked_html = "".join(f'<div style="margin-bottom:2em;">{c}</div>' for c in rest_cards)

        return grid_html + stacked_html

    # Tek ürün → normal kart
    elif n == 1:
        return build_single_card(products_df.iloc[0])

    return ""


def build_single_card(row):
    # burası build_product_cards içindeki card_html mantığını kullanabilir
    # yani tek ürün için kart HTML döndürür
    title = str(row.get("product_title", ""))
    asin = str(row.get("parent_asin", ""))
    amazon_link = row.get("amazon_link", f"https://www.amazon.com/dp/{asin}")
    image_url = str(row.get("image_url", ""))

    return f"""
    <div class="product-card" style="flex:1;min-width:220px;max-width:240px;
                border:1px solid #eee;border-radius:8px;padding:12px;background:#fff;">
        <a href="{amazon_link}" target="_blank" rel="nofollow noopener">
            <img src="{image_url}" alt="{title}" 
                 style="width:100%;height:160px;object-fit:cover;border-radius:6px;" />
        </a>
        <h3 style="font-size:14px;margin:0.6em 0;">{title}</h3>
        <a href="{amazon_link}" target="_blank" rel="nofollow noopener" 
           style="display:inline-block;width:100%;padding:8px 0;background:#2b6cb0;
                  color:#fff;text-align:center;border-radius:4px;font-weight:600;">
            🔗 See on Amazon
        </a>
    </div>
    """


def build_random_recommendations(blogs_dir, current_slug=None, max_posts=3, base_url=None):
    """
    Related posts direkt .md dosyalarından çekilir.
    - İlk <h1> başlığı = title
    - İlk meta-card içindeki <img src=...> = hero image
    """
    import os, random, re

    print("=== build_random_recommendations (from .md files) ===")
    files = [f for f in os.listdir(blogs_dir) if f.endswith(".md")]
    slugs = [os.path.splitext(f)[0] for f in files]

    if current_slug:
        slugs = [s for s in slugs if s != current_slug]

    if not slugs:
        return ""

    picks = random.sample(slugs, min(max_posts, len(slugs)))
    cards = []

    for slug in picks:
        path = os.path.join(blogs_dir, slug + ".md")
        try:
            text = open(path, encoding="utf-8").read()
        except:
            continue

        # Başlık (ilk h1)
        m = re.search(r"^# (.+)", text, re.MULTILINE)
        title = m.group(1).strip() if m else slug.replace("-", " ").title()

        # Hero image (ilk <img src=...>)
        m = re.search(r'<img src="([^"]+)"', text)
        hero = m.group(1) if m else "/static/default.jpg"

        url = f"{base_url.rstrip('/')}/{slug}/"

        cards.append(f"""
        <div class="related-card" style="flex:1;min-width:200px;max-width:240px;
                    border:1px solid #ccc;border-radius:8px;padding:10px;margin:6px;
                    background:#fff;display:flex;flex-direction:column;justify-content:space-between;">
          <div>
            <a href="{url}" style="text-decoration:none;color:inherit;display:block;text-align:center;">
              <img src="{hero}" alt="{title}" style="width:100%;height:100px;object-fit:cover;border-radius:6px;margin-bottom:8px;" />
              <h3 style="font-size:14px;margin:0.4em 0;text-align:center;font-weight:500;color:#333;">
                {title}
              </h3>
            </a>
          </div>
          <div style="text-align:center;margin-top:auto;">
            <a href="{url}" style="display:inline-block;text-align:center;
               padding:6px 10px;background:#805ad5;color:#fff;border-radius:5px;
               font-size:12px;text-decoration:none;font-weight:600;">
               👥 See What Others Said
            </a>
          </div>
        </div>
        """)

    html = f"""
<div class="related-posts" style="margin:2em 0;">
  <h2>You Might Also Like</h2>
  <div style="display:flex;flex-wrap:wrap;gap:16px;justify-content:center;">
    {''.join(cards)}
  </div>
</div>
"""
    print("=== build_random_recommendations END ===")
    return html
