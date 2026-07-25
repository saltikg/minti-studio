# Backlog

## Free account soft cap

- What:
  - Add a soft cap for total free accounts, around `50`.
- Why deferred:
  - Emergency control already exists through `SIGNUPS_ENABLED`.
  - Current focus has been operational safety, not growth throttling policy.
- Trigger:
  - Do this before broad acquisition or if signups accelerate beyond support capacity.

## Real worker parallelism

- What:
  - Enable more than one job to run at once.
  - Current worker is one synchronous process; real concurrency is `1`.
- Why deferred:
  - Existing global cap does not provide real parallelism by itself.
  - Parallel render on a 2 GiB host needs fresh RAM / swap measurement first.
- Trigger:
  - Do this before paid traffic / ads.
  - Smallest likely implementation path: a second worker process, since job claiming is already `FOR UPDATE SKIP LOCKED` safe.
- Follow-up required when done:
  - Re-measure RAM, swap, and render times.
  - Two concurrent ffmpeg renders on 2 GiB RAM are still unproven.

## Queue priority for paid users

- What:
  - Prefer paid users' render jobs over free-tier jobs.
- Why deferred:
  - Queue order is still effectively FIFO today.
  - This matters only when the queue is regularly non-empty.
- Trigger:
  - Do this before ad-scale load or the first real queue complaints from paid users.

## t3.medium upgrade

- What:
  - Move from `t3.small` to `t3.medium` for 4 GiB RAM.
- Why deferred:
  - There is no point upgrading only for theoretical headroom.
  - Real worker parallelism has not been enabled yet.
- Trigger:
  - Upgrade if a multi-worker or true concurrent-render test shows swap pressure on `t3.small`.

## Gunicorn / browser load under real traffic

- What:
  - Measure web-server behavior under about `100` concurrent browsers.
- Why deferred:
  - The current focus has been background processing and host safety.
  - This load shape has not been tested.
- Trigger:
  - Do this before ads or any campaign that can send real multi-user browse traffic.

## Move EC2 and S3 into the same region

- What:
  - Eliminate the current cross-region path:
    - EC2 in `us-west-1`
    - S3 bucket in `us-east-1`
- Why deferred:
  - The system is working today.
  - Migration needs planning for buckets, URLs, and deployment sequencing.
- Trigger:
  - Do this once growth justifies the savings.
  - Current ops note: the cross-region setup wastes about `$3.6 / month` and grows with traffic.

## Evaluate `gpt-4o-mini-transcribe`

- What:
  - Test whether `gpt-4o-mini-transcribe` can replace the current Whisper transcription path.
- Why deferred:
  - It needs a real quality comparison on Turkish + English source videos.
  - Cost savings alone are not enough if transcript quality drops.
- Trigger:
  - Do this when variable OpenAI cost starts to matter, or before ads if transcript volume is expected to rise sharply.

## Revisit `FFMPEG_RENDER_TIMEOUT=3600`

- What:
  - Tighten the default render timeout if long real-world renders do not materialize.
- Why deferred:
  - Current measured renders were well under 170 seconds, but the code still supports long uploads and long compositions.
- Trigger:
  - Revisit after observing actual 2-3 hour source videos in production.

## Decide the fate of the nested prod `minti-studio/` directory

- What:
  - Confirm whether the stray nested `minti-studio/` directory on prod can be removed.
- Why deferred:
  - It is not part of the active app path, but it should be checked before deletion.
- Trigger:
  - Do this during a prod housekeeping pass.

## Surface-area review: dead code vs active product

- What:
  - Decide which of these are still product surfaces versus removable code:
    - `monthly_top_video`
    - `interview`
    - image-to-video slideshow flows
    - `quick_short`
- Why deferred:
  - They still exist in the codebase and some may still be in use.
  - Removing them without a product decision is risky.
- Trigger:
  - Do this before a cleanup/refactor cycle, or before onboarding another engineer who needs a clearer product boundary.
