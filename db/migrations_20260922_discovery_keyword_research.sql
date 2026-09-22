-- Discovery automation throughput and winning-keyword research cadence.
-- The control row is authoritative for max_keywords_per_cycle.

UPDATE main.discovery_automation_control
SET max_keywords_per_cycle = 5,
    updated_at = CURRENT_TIMESTAMP
WHERE id = 1;
