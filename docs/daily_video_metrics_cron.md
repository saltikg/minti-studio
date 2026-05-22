## Günlük Video Metrics Snapshot Cron

Aşağıdaki cron satırını sisteme eklediğinde `shorts_video_daily_snapshots` tablosu sabah 03:00, öğlen 11:00 ve akşam 19:00'da sırasıyla maraton yürüyecek:

```cron
0 3,11,19 * * * cd /home/ubuntu/blog-factory && /home/ubuntu/blog-factory/.venv/bin/python3 -m app.video_shorts.tasks.daily_video_metrics_snapshot >> /home/ubuntu/blog-factory/logs/daily_video_metrics_snapshot.log 2>&1
```

- İlk çalışmada tabloya snapshot veriyi insert eder.
- Aynı `channel_type` + `video_id` + `snapshot_date` üçlüsüne sahip satırlar zaten varsa `ON CONFLICT` içinde tanımlı `DO UPDATE` mantığı sayesinde güncellenir.
- Bu tablo günlük video snapshot'ları için tek source of truth olarak kullanılmalıdır.
- Log dosyası (`logs/daily_video_metrics_snapshot.log`) içinde her çalışmanın çıktısını görebilir, hataları bu dosyadan takip edebilirsin.

Eğer cron servisini elle düzenliyorsan, yeni satırı `sudo crontab -e` (veya `crontab -e` uygun kullanıcı için) komutuyla ekleyip kaydet. Sistemde `python3` yerine `.venv/bin/python3` kullandığımızdan, yolun doğru olduğundan emin ol.
