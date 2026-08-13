---
title: Troubleshooting — FAQ
slug: troubleshooting
description: Short answers about common processing, connection, and publishing problems.
category_slug: troubleshooting
topics: [Troubleshooting, Upload, Transcription, Publishing, Errors]
order: 50
---

Find confirmed answers for common upload, transcription, generation, connection, and publishing problems in MintiStudio.

**Why is my source video still processing?**

MintiStudio may need time to prepare a source video before transcription and Short creation can begin.

While this is happening, you may see:

`We are preparing the source video. This can take a little while.`

Wait for the source preparation to finish before starting transcription or creating clips.

If the source never becomes available, try importing or uploading the source again.

**Why does MintiStudio say my source file is missing?**

If MintiStudio cannot find the source video file on the server, you may see a message such as:

`Source video file not found on server. Upload or download it first.`

The source needs to be available before transcription or rendering can continue.

Re-upload or re-import the source video and try again.

**Why can't MintiStudio transcribe my video?**

Transcription can fail or be blocked for several confirmed reasons, including:

- the source video is not ready
- the source file is missing
- the transcription is already running
- there is not enough transcription usage remaining for the month
- the transcription service did not return usable transcript segments

If a source file is missing, upload or import it again.

If you have reached your monthly transcription allowance, wait for the usage reset or change your plan.

**What happens if a transcription job gets stuck?**

MintiStudio monitors transcription jobs for stale processing.

If a transcription job remains stuck long enough, MintiStudio can mark the old job as failed and allow a fresh transcription run to start.

You do not need to keep multiple transcription jobs running for the same video.

**Why did AI clip suggestions fail?**

AI clip suggestions may occasionally be unavailable or fail while processing the video.

If this happens, MintiStudio may show:

`Clip plan generation is unavailable right now.`

You can try `Suggest clips with AI` again later.

You can also continue creating clips manually from the transcript without using AI suggestions.

**Can I still create a Short if AI suggestions are unavailable?**

Yes.

AI suggestions are optional.

You can manually select the start and end of a clip from the transcript and continue through the Editor even if AI clip suggestions are unavailable.

**Why does Generate Short Clip say a matching render is already in progress?**

MintiStudio avoids creating duplicate render jobs.

If the same Short is already being generated, you may see:

`Matching render job is already in progress.`

Wait for the existing render to finish instead of starting another identical render.

If the same rendered clip already exists, MintiStudio can reuse that result rather than consuming another export.

**Why did Generate Short Clip fail?**

Short generation can fail for reasons such as:

- the source video is unavailable
- the clip timing is invalid
- a temporary processing problem occurred
- your monthly export limit has been reached
- your storage limit has been reached

For temporary processing failures, try generating the Short again.

If the problem continues, verify that the source video is available and that your account still has export and storage capacity.

If a render fails and no Short is created, the reserved export is released.

**What happens when I reach my monthly export limit?**

When the monthly export allowance for your plan is reached, MintiStudio pauses additional Short generation.

You may see:

`Monthly export limit reached for your plan.`

You can wait for your monthly usage reset or upgrade your plan.

**What happens when my storage is full?**

If creating another file would exceed your storage allowance, MintiStudio can block additional exports.

Free up storage by deleting videos or rendered Shorts you no longer need, or upgrade to a plan with more storage.

Deleting source videos or rendered Shorts removes their associated media and can free storage space.

**Why does my social connection say Reconnect needed?**

A connected platform may require authorization again if its access has expired, been revoked, or can no longer be refreshed.

The Social Connections page may show:

`Connection expired - reconnect to keep publishing.`

Use the `Reconnect` action for that platform before trying to publish again.

**Why can't I publish to YouTube?**

YouTube publishing requires a valid YouTube connection and an available rendered Short.

If the authorization has expired or become invalid, reconnect YouTube from Social Connections and try again.

YouTube publishing does not have a confirmed automatic retry flow in the current application, so after fixing the connection or file issue, retry the publishing action.

**Why can't I publish to Instagram?**

Instagram publishing requires a valid connected Business or Creator account.

Publishing can fail if:

- the Instagram connection is missing or invalid
- the access token or Business account information is unavailable
- Instagram has not finished preparing the uploaded media

Reconnect Instagram if MintiStudio indicates that the connection is invalid.

Some temporary Instagram media-processing failures are retried automatically by MintiStudio.

**Why can't MintiStudio find my Facebook Page?**

Facebook publishing requires access to a Facebook Page.

Connection can fail if:

- no Pages are returned for the connected Facebook account
- the required Page permissions are missing
- the configured Page cannot be found
- multiple Pages are available but a target Page has not been resolved
- the Page access token is unavailable

Check that the Facebook account has access to the intended Page and reconnect Facebook if necessary.

**Which publishing problems does MintiStudio retry automatically?**

Retry behavior depends on the platform and operation.

Confirmed automatic retry behavior includes:

- some Instagram publishing failures when Instagram reports that media is not ready yet
- stale Instagram upload jobs
- some Facebook publishing failures caused by connection-level request errors
- timed-out Short render jobs, up to the configured retry limit

YouTube publishing does not have a confirmed automatic retry flow in the current implementation.

If an operation fails because a social connection is invalid, reconnect the platform and retry the action manually.
