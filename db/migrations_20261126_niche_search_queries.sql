ALTER TABLE main.niches
    ADD COLUMN IF NOT EXISTS search_queries JSONB NOT NULL DEFAULT '[]'::jsonb;

UPDATE main.niches
SET name = 'İslami Sohbet & Manevi Konuşmalar',
    description = CASE
        WHEN description LIKE '%Qur''an recitation, religious news/politics and institutional mosque channels are NOT in this category as channels%'
            THEN description
        ELSE description || ' Qur''an recitation, religious news/politics and institutional mosque channels are NOT in this category as channels; individual Shorts on these topics from in-category channels may count as adjacent.'
    END,
    search_queries = '[
      "sohbet", "kısa sohbet", "dini sohbet", "vaaz", "Risale-i Nur dersi",
      "Risale-i Nur sohbeti", "tefsir dersi", "hadis dersi", "siyer sohbeti",
      "Peygamber Efendimizin hayatı", "sahabe hayatı", "imanî hakikatler",
      "tasavvuf sohbeti", "namaz sohbeti", "dua sohbeti", "ahlak sohbeti",
      "İslam''da aile sohbeti", "ahiret sohbeti", "manevi sohbet", "Kur''an meali sohbet"
    ]'::jsonb,
    definition_version = CASE
        WHEN name = 'İslami Sohbet & Manevi Konuşmalar'
         AND description LIKE '%Qur''an recitation, religious news/politics and institutional mosque channels are NOT in this category as channels%'
        THEN definition_version
        ELSE definition_version + 1
    END
WHERE slug = 'turkish_religious_talks';
