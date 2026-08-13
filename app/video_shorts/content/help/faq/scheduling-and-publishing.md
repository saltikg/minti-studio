---
title: Scheduling & Publishing — FAQ
slug: scheduling-and-publishing
description: Short answers to the questions creators hit most when scheduling clips.
order: 10
---

Short answers to the questions creators hit most when scheduling clips. Written for the in-app FAQ; grow this list as new questions come up from users.

**Why does the publish time show a specific time zone?**

Every scheduled time has to be anchored to a time zone, or "8:00 AM" is meaningless. The Schedule & Publish window shows your account's time zone next to the publish-time field so you always know which clock you're picking in. If that zone matches where you are, the time you type is the time your clip goes out.

**How do I change my time zone?**

Click the small edit icon next to the "Publish time" label in the Schedule & Publish window. Pick your zone from the list (US zones, UK, Central Europe, Turkey, UTC) and your account updates immediately — you don't have to leave the scheduling screen or visit your [profile page](/video_shorts/profile). You can also change it anytime under [Account → Time zone](/video_shorts/profile).

**If I change my time zone, does my typed publish time shift?**

No. Changing your zone keeps the number you typed the same — "8:00 AM" stays "8:00 AM" — it only changes which zone that time is anchored to. So if you meant 8:00 in your local time, fix the zone first, then the time you entered is interpreted correctly.

**Will my clip really publish at the exact time I picked?**

Yes. Your selected time is converted to a precise UTC timestamp using your account's time zone, then used to publish on each platform at that exact moment. The conversion works the same wherever you are — the only thing that matters is that your account time zone is set correctly.

**Can I publish to YouTube and Instagram at different times?**

Yes. In the Schedule & Publish window you have three options for Instagram: publish at the **same time** as YouTube, publish **immediately** on Instagram, or choose a **separate Instagram time**. Separate times are useful because each platform's audience is usually most active at different hours.

**What do the "Planned" and "Scheduled" labels mean?**

In the Schedule & Publish window, "Planned" is a live hint on the YouTube side that you currently have a publish time entered (it shows "Now" if you haven't). Once you submit, a YouTube clip with a publish time moves to a "scheduled" status. On the Instagram side, "scheduled" means the queued clip has a specific future publish time, while "queued" means it's queued without a separate future time. In short: "Planned" is a hint that a time is entered; "scheduled" is the saved state after you submit.

**How does Instagram scheduling work?**

MintiStudio handles Instagram scheduled publishing with its own queue and worker: your chosen time is stored in UTC, and the clip is published when that time is reached. Instagram's own publishing API doesn't hold a Reel for a future time, so MintiStudio manages the timing on our side — which is why your scheduled Instagram time is reliable.

**What's the difference between "Share as Reel" and "Share as feed video"?**

These are two independent toggles. "Share as Reel" publishes your clip to the Reels tab, where short vertical videos get discovered. "Share as feed video" places it in your regular feed. You can turn on either or both — reach from Reels is driven by discovery, while feed placement is seen mainly by your existing followers.

**Why does YouTube show a different date than the one I picked?**

YouTube's watch page shows the public date based on Pacific Time as a display convention. This is a YouTube display quirk, not a change to when your video actually published — your clip still goes public at the exact moment you scheduled.

**How do I choose the best time to publish?**

Check your [Video Analytics](/video_shorts/video-analytics) for the publish windows that produced your strongest early views — traffic from the algorithm and your subscribers tends to build in the hours right after a clip goes live. Schedule new clips into those proven windows, and keep your time zone consistent so the comparison stays accurate over time.
