-- search.list has its own 100-call daily quota and does not consume the
-- 10,000-unit general Queries per day bucket.
UPDATE main.youtube_data_api_unit_usage
SET units = 0,
    updated_at = CURRENT_TIMESTAMP
WHERE method = 'search.list'
  AND units <> 0;
