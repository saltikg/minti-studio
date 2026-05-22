## TikTok Queue Cron

TikTok publish queue icin ayri cron satiri:

```cron
*/2 * * * * /usr/bin/flock -n /tmp/tiktok_queue.lock -c "cd /home/ubuntu/blog-factory && PYTHONPATH=/home/ubuntu/blog-factory /home/ubuntu/blog-factory/.venv/bin/python app/video_shorts/tasks/process_tiktok_queue.py --max 5 >> /home/ubuntu/blog-factory/logs/tiktok_queue.log 2>&1"
```

Notlar:
- Cron'u ilgili kullanici altinda `crontab -e` ile ekleyin.
- Loglar `logs/tiktok_queue.log` dosyasina gider.
