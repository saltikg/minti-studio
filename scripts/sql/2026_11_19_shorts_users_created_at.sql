BEGIN;

ALTER TABLE shorts_users
    ALTER COLUMN created_at SET DEFAULT now();

UPDATE shorts_users AS users
SET created_at = signups.first_signup_at
FROM (
    SELECT CAST(user_id AS TEXT) AS user_id, MIN(created_at) AS first_signup_at
    FROM user_events
    WHERE event_name = 'signup'
    GROUP BY CAST(user_id AS TEXT)
) AS signups
WHERE users.created_at IS NULL
  AND CAST(users.id AS TEXT) = signups.user_id;

COMMIT;
