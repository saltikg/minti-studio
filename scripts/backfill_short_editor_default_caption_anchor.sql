-- Saved editor defaults created before caption_anchor stored sub_margin as a
-- distance from the bottom. Preserve that meaning explicitly.
WITH updated AS (
    UPDATE main.shorts_user_preferences
    SET preference_value = jsonb_set(
            preference_value::jsonb,
            '{caption_anchor}',
            '"bottom"'::jsonb,
            true
        )::text,
        updated_at = now()
    WHERE (preference_key = 'short_editor_defaults'
           OR preference_key LIKE 'short_editor_defaults:%')
      AND preference_value::jsonb ? 'sub_margin'
      AND NOT (preference_value::jsonb ? 'caption_anchor')
    RETURNING id
)
SELECT COUNT(*) AS backfilled_defaults
FROM updated;
