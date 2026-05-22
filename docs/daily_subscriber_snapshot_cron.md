## Daily Subscriber Snapshot Cron

Run the subscriber snapshot once per day after the Los Angeles day closes.

```cron
CRON_TZ=America/Los_Angeles
15 0 * * * cd /home/ubuntu/blog-factory && /home/ubuntu/blog-factory/.venv/bin/python3 -m app.video_shorts.tasks.daily_subscriber_snapshot >> /home/ubuntu/blog-factory/logs/daily_subscriber_snapshot.log 2>&1
```

- Uses the daily subscriber snapshot task only (independent of comment sync).
- Inserts or updates rows by `channel_type` + `channel_id` + `snapshot_date`.
- Check `logs/daily_subscriber_snapshot.log` for results and failures.
