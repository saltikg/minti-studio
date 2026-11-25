UPDATE main.discovery_automation_control
SET offpeak_hours_pt_json = '[1, 9, 13, 17]',
    runs_per_day = 4,
    next_run_at = NULL,
    updated_at = CURRENT_TIMESTAMP
WHERE id = 1;
