ALTER TABLE main.niches
    ADD COLUMN IF NOT EXISTS language TEXT NOT NULL DEFAULT 'en',
    ADD COLUMN IF NOT EXISTS target_focus TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS focus_options JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active';

ALTER TABLE main.niches
    DROP CONSTRAINT IF EXISTS niches_status_check;

ALTER TABLE main.niches
    ADD CONSTRAINT niches_status_check
    CHECK (status IN ('active', 'draft'));

ALTER TABLE main.niche_channels
    DROP CONSTRAINT IF EXISTS niche_channels_primary_focus_check;

UPDATE main.niches
SET language = 'en',
    target_focus = 'hormones_midlife_health',
    focus_options = '[
      {"key":"hormones_midlife_health","description":"Primarily teaches or explains women''s hormones, menopause, and midlife health."},
      {"key":"fitness_training","description":"Primarily offers workouts, yoga, exercise classes, or physical training."},
      {"key":"nutrition_diet","description":"Primarily teaches diets, recipes, meal plans, or weight-loss nutrition."},
      {"key":"general_health_wellness","description":"Primarily covers broad health, wellness, lifestyle, or mindset."},
      {"key":"fertility_pregnancy","description":"Primarily covers fertility, pregnancy, postpartum, or reproductive planning."},
      {"key":"other","description":"None of the defined focuses is the channel''s primary subject."}
    ]'::jsonb,
    status = 'active'
WHERE slug = 'menopause_hormones';
