# Operations

## Infrastructure

- App host: EC2 `t3.small` in `us-west-1`.
- CPU / RAM class: 2 vCPU class, about 2 GiB RAM.
  Host snapshot on July 25, 2026: `MemTotal 1907 MiB`.
- Credit mode: production is currently operated as `Unlimited`.
  Note: this is an AWS-side setting, not a repo config value.
- Root filesystem: ext4 on `/dev/nvme0n1p1`.
  The root disk was grown from about `29G` to about `58G`.
  Current snapshot on July 25, 2026: `58G total`, `19G used`, `39G free`, `33% used`.

- Systemd services:
  - `minti_studio.service`
    - Gunicorn web app
    - Working dir: `/home/ubuntu/apps/minti_studio`
    - Exec: `.../.venv/bin/gunicorn --workers 2 --timeout 300 --bind 127.0.0.1:8000 ... wsgi:app`
  - `minti_studio_worker.service`
    - Background job worker
    - Working dir: `/home/ubuntu/apps/minti_studio`
    - Exec: `.../.venv/bin/python -m app.video_shorts.worker`

- Cron-backed jobs:
  - Instagram / Facebook / TikTok publish is not a systemd service.
    It runs from user crontab every 10 minutes through:
    `scripts/run_social_jobs_cron.sh publish`
  - Hourly YouTube comments sync runs through:
    `scripts/run_social_jobs_cron.sh youtube-comments`
  - Disk alarm runs from root crontab:
    `scripts/disk_usage_alarm.py --threshold 80 --recipient info@mintistudio.com`
  - Temp cleanup runs from root crontab:
    `find /home/ubuntu/apps/minti_studio/app/video_shorts/tmp -type f -mmin +60 -delete`

- S3:
  - Bucket: `minti-studio-media`
  - Config region: `us-east-1`
  - Important prefixes:
    - `videos/<video_id>.<ext>`: uploaded or downloaded source videos
    - `shorts/<clip_filename>`: rendered short outputs
    - `user_images/<user_id>/<filename>`: uploaded still images
    - `user_audio/<user_id>/<filename>`: uploaded static audio
    - `user_podcasts/<user_id>/<filename>`: uploaded podcast audio
  - Instagram publish does not make S3 objects public.
    Meta fetches via the app proxy route `/video_shorts/ig-media/<token>`.

## Worker & Job Processing

- Critical current behavior:
  - Production worker is a single process.
  - `WORKER_CONCURRENCY=1`.
  - The worker loop is synchronous:
    claim one job, run it to completion, then claim the next.
  - Real render concurrency today is `1`.

- Why the global cap of `2` is currently unreachable:
  - `MAX_GLOBAL_CONCURRENT_JOBS=2` is only a pre-claim ceiling.
  - The worker never starts a second job while one is still running.
  - A load test on July 25, 2026 showed:
    - `max_processing_jobs_seen=1`
    - `max_ffmpeg_count_seen=1`
  - So the cap is not the bottleneck.
    The single-process, synchronous worker loop is.

- Claim path safety:
  - Postgres claim uses `SELECT ... FOR UPDATE SKIP LOCKED`.
  - DuckDB claim uses an in-process mutex.
  - This means the claim path is already safe for multiple worker processes.
  - Smallest path to real parallelism: run a second worker process / service instance.

- Stale-job reaper:
  - Runs inside the worker loop, not via cron.
  - Called on every loop tick before claim:
    `requeue_timed_out_jobs(timeout_seconds=STALE_JOB_TIMEOUT_SECONDS)`
  - `STALE_JOB_TIMEOUT_SECONDS=5400`
  - This is intentionally above `FFMPEG_RENDER_TIMEOUT=3600`.

- Job classes:
  - Transcription jobs:
    - Stage 1: local ffmpeg work to prepare or chunk audio
    - Stage 2: OpenAI Whisper API call
    - Net effect: mixed workload, with meaningful I/O wait during API calls
  - Render jobs:
    - Heavy path is ffmpeg composition / trim / subtitle burn-in
    - Net effect: CPU-bound on this host

## Safety Guards

- Media subprocess timeouts:
  - `FFPROBE_TIMEOUT=60`
  - `FFMPEG_SHORT_TIMEOUT=300`
  - `FFMPEG_RENDER_TIMEOUT=3600`
  - `FFMPEG_TIMEOUT=3600`
  - ffprobe-style probe calls use flat timeouts.
  - Many render paths use scaled timeouts on top of the configured floor.
  - Timeout behavior:
    - raises `MediaSubprocessTimeoutError`
    - logs technical detail at error level
    - marks the owning job terminally failed
    - does not retry render jobs on timeout

- Disk guard:
  - `DISK_GUARD_PCT=85`
  - Request-time guard blocks:
    - upload
    - transcribe start
    - render enqueue
  - Worker also checks disk again before claiming a job.

- Permanent media failures:
  - `ENOSPC` and `FileNotFoundError` go straight to terminal failed state.
  - They do not requeue.

- Concurrency guards:
  - Per-user concurrency comes from plan config:
    - Free: `1`
    - Starter: `2`
    - Creator: `2`
    - Studio: `3`
  - It counts only `processing` jobs, not queued jobs.
  - Global cap:
    - `MAX_GLOBAL_CONCURRENT_JOBS=2`
    - Today this is only a ceiling; real execution is still 1.

- Signup kill switch:
  - `SIGNUPS_ENABLED`
  - Evaluated at request time.
  - Blocks new account creation only.
  - Existing users, including existing Google users, can still sign in.

- Upload limits by plan:
  - Free:
    - `MAX_UPLOAD_DURATION_SECONDS_FREE=3600`
    - `MAX_UPLOAD_SIZE_BYTES_FREE=2147483648`
  - Paid plans:
    - `MAX_UPLOAD_DURATION_SECONDS_PAID=10800`
    - `MAX_UPLOAD_SIZE_BYTES_PAID=5368709120`
  - Duration is probed and enforced before any further processing.
  - Rejected uploaded S3 source objects are deleted.

- Transcription quota pre-check:
  - Transcription minutes are checked before S3 download to EC2, audio extraction, or Whisper API spend.
  - Current monthly plan limits:
    - Free: `60` min
    - Starter: `180` min
    - Creator: `540` min
    - Studio: `1620` min

- Disk usage alarm:
  - Script: `scripts/disk_usage_alarm.py`
  - Cron: root crontab, minute `5` of every hour
  - Threshold: `80%`
  - Behavior:
    - logs every check
    - emails only on threshold transition
    - persists state in `data/disk_usage_alarm_state.json`

## Cost Model

- Main variable cost:
  - OpenAI Whisper transcription
  - Current reference rate used in operations: `$0.006 / minute`
  - This price is external to the repo; the code only shows that transcription goes through the OpenAI audio transcription API.

- AWS cost shape:
  - Mostly fixed monthly rent for EC2 + EBS + S3
  - Current ops estimate: about `$18-20 / month` before traffic-driven growth

- OpenAI account guardrails:
  - Spend limit and alerts are account-side settings, not repo config
  - Current ops note: limit is set to `$120`

- Plan-level Whisper exposure at current minute pricing:
  - Free: `60 min` -> about `$0.36`
  - Starter: `180 min` -> about `$1.08`
  - Creator: `540 min` -> about `$3.24`
  - Studio: `1620 min` -> about `$9.72`

## Known Operational Lessons

- Deploy means:
  - `git pull`
  - restart `minti_studio.service`
  - restart `minti_studio_worker.service`
  - verify both are active

- Restart both services every time.
  Restarting only the web service leaves worker-side changes unapplied.

- Production must not become a source of truth again.
  Do not copy files directly to prod.
  Prod should be resettable from git.

- Backups belong in S3, not `/tmp`.
  A local `~4GB` tarball once pinned the root disk near the alarm threshold.

## Verification Commands

- Disk / memory:
```bash
ssh minti-prod-ec2 'df -h / && echo && free -m | sed -n "1,3p"'
```

- Service status:
```bash
ssh minti-prod-ec2 'systemctl is-active minti_studio.service minti_studio_worker.service'
```

- Worker topology:
```bash
ssh minti-prod-ec2 'systemctl cat minti_studio_worker.service && echo && ps -eo pid,ppid,etimes,nlwp,cmd | grep -E "app.video_shorts.worker|python -m app.video_shorts.worker" | grep -v grep'
```

- Resolved worker config:
```bash
ssh minti-prod-ec2 'cd ~/apps/minti_studio && set -a && source .env && set +a && .venv/bin/python - <<'"'"'\"'"'"'PY'"'"'\"'"'"'
from app.video_shorts.config import WORKER_CONCURRENCY, MAX_GLOBAL_CONCURRENT_JOBS, DISK_GUARD_PCT, STALE_JOB_TIMEOUT_SECONDS
print("WORKER_CONCURRENCY", WORKER_CONCURRENCY)
print("MAX_GLOBAL_CONCURRENT_JOBS", MAX_GLOBAL_CONCURRENT_JOBS)
print("DISK_GUARD_PCT", DISK_GUARD_PCT)
print("STALE_JOB_TIMEOUT_SECONDS", STALE_JOB_TIMEOUT_SECONDS)
PY'
```

- Social cron topology:
```bash
ssh minti-prod-ec2 'crontab -l && echo && sudo crontab -l'
```

- Normal render smoke test:
  - Open a known owner video at `/video_shorts/generate/<video_pk>`
  - Render one pending plan entry
  - Confirm worker marks the job `done`
  - Confirm the plan entry moves to `created`
