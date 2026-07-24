## Instagram Publishing — How It Works and Why

This note captures the working Instagram Reels publish architecture in `minti-studio`, the traps we already hit, and the symptom-to-cause mappings that matter during debugging.

Relevant files:
- `app/video_shorts/tasks/process_instagram_queue.py`
- `app/video_shorts/services/instagram_media_proxy.py`
- `app/video_shorts/routes/media.py`
- `app/video_shorts/routes/auth.py`
- `scripts/run_social_jobs_cron.sh`

### 1. The Key Difference From Facebook

Facebook and Instagram do **not** publish the same way in this codebase.

- Facebook publish uploads video bytes directly to Meta.
  - The worker downloads the private clip from S3 server-side, then byte-uploads it to Meta.
  - No public or fetchable video URL is required.
- Instagram publish is different.
  - Our reconnect flow uses **Instagram Login**, so publish runs on the `graph.instagram.com` family.
  - In that family, container-create **requires** `video_url`.
  - Meta fetches the video from that URL itself during container processing.
  - Direct-byte-upload-only is **not** a working substitute here.

If someone assumes “Instagram should work like Facebook,” they will likely rebuild the same dead-end we already hit.

### 2. The Token Family Trap

The reconnect flow stores an Instagram Login token. That token belongs to the `graph.instagram.com` API family, not `graph.facebook.com`.

- Publish must use `graph.instagram.com`.
- In this repo, that means Instagram publish should use `IG_GRAPH_API_BASE`, not `IG_API_BASE`.
- The publish account id that worked in production is the `instagram_business_account_id` stored with the connection and used successfully by webhook subscribe.

Recognizable failure:

- Symptom: `Invalid OAuth access token / Cannot parse access token (code 190)`
- Cause: a valid Instagram Login token was sent to `graph.facebook.com`, which cannot parse that token family.

This is the fastest mental check when Instagram publish suddenly fails after a reconnect flow that otherwise looks healthy.

Working example:
- Reel permalink: `https://www.instagram.com/reel/DbACUhVCdxM/`
- Production run date: July 20, 2026
- Container reached `FINISHED` in about 35 seconds before `media_publish`

### 3. The Private-S3 / `video_url` Problem and the Proxy Solution

Clips are private in S3 and should stay private. Instagram still needs a fetchable `video_url`.

We do **not** solve this by making S3 public.

Instead, publish uses a short-lived signed proxy route on our own domain:

- Route: `/video_shorts/ig-media/<token>`
- Token logic lives in `app/video_shorts/services/instagram_media_proxy.py`
- The token carries:
  - queue id
  - expiry timestamp
  - version
- The token is signed with HMAC using `SECRET_KEY`
- The token does **not** expose the raw S3 key

Important properties:

- Expiry is 60 minutes
  - long enough for asynchronous Meta fetch + container processing
  - short enough to avoid leaving a durable public URL behind
- The route must be in the login-guard allowlist in `app/video_shorts/routes/auth.py`
  - if not, Meta gets a `302` redirect to `/video_shorts/login`
  - that makes publish fail even though the proxy route exists
- The route supports:
  - `HEAD`
  - full `GET`
  - `Range` requests
- The route streams from S3 in chunks
  - no full-file load into memory
  - safe for the current EC2 shape (`t3.small`)

Observed production behavior on the successful publish:

- Meta fetched with a plain `GET`
- User-Agent was `facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)`
- No separate `HEAD` appeared in the successful publish log

Even so, `HEAD` and `Range` support stay in place defensively in case Meta changes fetch behavior later.

### 4. The Publish Flow

The Instagram publish flow is:

1. `process_instagram_queue.py` generates a short-lived signed proxy URL for the queued clip.
2. It calls container-create on `graph.instagram.com` with:
   - `media_type=REELS`
   - `upload_type=resumable`
   - `video_url=<signed proxy URL>`
3. Meta fetches the video during container processing.
4. The worker polls `status_code` until it becomes `FINISHED`.
5. The worker calls `media_publish`.
6. It stores:
   - `instagram_media_id`
   - `permalink`
   - publish timestamp

Operational meaning of container states:

- `FINISHED` means Meta successfully fetched and processed the clip.
- `ERROR` usually means the problem is around the proxy URL, fetchability, expiry, or remote access path.

### 5. Things Not To Do

- Do **not** make S3 objects public just to satisfy Instagram fetch.
- Do **not** try to force direct-byte-upload-only for Instagram Login publish.
  - `video_url` is required in this flow.
- Do **not** send the Instagram Login token to `graph.facebook.com`.
  - If you see code 190 “Cannot parse access token,” check this first.
- Do **not** forget to allowlist the proxy route in the video-shorts login guard.
  - Otherwise Meta sees a `302` to login, not the clip.
- Do **not** make the proxy URL expiry too short.
  - Meta fetch happens asynchronously during container processing, not necessarily immediately at create time.

### 6. Related Gotcha: Cron Environment

There is a separate failure mode that looks like “nothing publishes” even when the publish code is fine.

- The social publish cron runs through `scripts/run_social_jobs_cron.sh`
- That cron must load the same `.env` file the systemd services use
- If it does not, publish can fail before any platform logic runs

Recognizable failure:

- Symptom: jobs stay `pending`, nothing goes out
- Cause: cron env is missing DB config, producing errors like `VIDEO_SHORTS_DB is not set`

This is not an Instagram API problem, but it produces a similar user-facing symptom: queued posts never publish.

### 7. Quick Debug Map

- `Cannot parse access token (code 190)`
  - token family mismatch
  - check for `graph.facebook.com` in the Instagram publish path
- `The parameter video_url is required`
  - wrong assumption that direct upload is enough
  - container-create still needs `video_url`
- Meta gets redirected to login
  - proxy route missing from auth allowlist
- Container never reaches `FINISHED` or ends in `ERROR`
  - suspect proxy fetchability, domain reachability, expiry, or proxy response semantics
- Jobs remain `pending` and no platform work starts
  - suspect cron env loading, not Instagram logic
