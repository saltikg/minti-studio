WITH pubs AS (
  SELECT 
    i.idea_id,
    i.idea_title,
    i.category_slug,
    b.blog_url,
    regexp_extract(b.blog_url, '.*/([^/]+)/?$', 1) AS slug
  FROM blog_posts b
  JOIN ideas i ON b.idea_id = i.idea_id
  WHERE lower(b.status)='published'
),
missing AS (
  SELECT p.*
  FROM pubs p
  LEFT JOIN blog_contents bc ON bc.idea_id = p.idea_id
  WHERE bc.idea_id IS NULL
)
INSERT INTO blog_contents (
  idea_id, title, slug, category_slug,
  front_matter, introduction, product_gallery,
  urunler, buyers_guide, faq, conclusion, recommendations, cta, md_full,
  updated_at
)
SELECT
  idea_id, idea_title, slug, category_slug,
  '',
  '<p>Introduction coming soon.</p>',
  '',
  '',
  '',
  '',
  '',
  '',
  '',
  '',
  now()
FROM missing;
