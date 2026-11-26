ALTER TABLE main.niches
    ADD COLUMN IF NOT EXISTS channel_type_options JSONB,
    ADD COLUMN IF NOT EXISTS allowed_channel_types JSONB;

ALTER TABLE main.niche_channels
    ADD COLUMN IF NOT EXISTS channel_type TEXT;

UPDATE main.niches
SET channel_type_options = NULL,
    allowed_channel_types = NULL
WHERE slug = 'menopause_hormones';

UPDATE main.niches
SET channel_type_options = '[
      {"key":"own_speaker","description":"The scholar or speaker''s own channel."},
      {"key":"clip_compilation","description":"Clips or excerpts from one or more scholars'' talks."},
      {"key":"media_brand","description":"A production brand with talk-format content and multiple presenters."},
      {"key":"faceless_story","description":"Narrated stories or voiceover without an on-camera speaker, often using an AI voice."},
      {"key":"news","description":"News or current-events coverage."},
      {"key":"lifestyle_influencer","description":"Personal lifestyle or vlog creators."},
      {"key":"other","description":"None of the defined channel types."}
    ]'::jsonb,
    allowed_channel_types = '["own_speaker","clip_compilation","media_brand"]'::jsonb
WHERE slug = 'turkish_religious_talks';
