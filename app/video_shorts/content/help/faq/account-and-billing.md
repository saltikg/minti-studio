---
title: Account & Billing — FAQ
slug: account-and-billing
description: Short answers about plans, usage limits, billing, and account settings.
category_slug: account-billing
topics: [Account, Billing, Plans, Usage, Subscription]
order: 40
---

Understand how plans, usage limits, billing, and account settings currently work in MintiStudio.

**What plans does MintiStudio offer?**

MintiStudio currently offers four plans:

- Free
- Starter
- Creator
- Studio

Each plan includes different monthly export, transcription, and storage limits.

You can compare plans and view your current plan from the `Plan & storage` page.

**Does MintiStudio have a Free plan?**

Yes.

New MintiStudio accounts normally start on the Free plan and no payment information is required to create an account.

The Free plan includes monthly usage allowances for exports, transcription, and storage.

**Where can I see my current plan and usage?**

Open the `Plan & storage` page.

There you can see your:

- current plan
- export usage
- transcription usage
- storage usage
- usage reset date

MintiStudio also shows a compact usage summary in the application navigation.

**What counts as an export?**

An export is counted when you start generating a final Short using `Generate Short Clip`.

MintiStudio reserves an export when the render job is queued.

If the render fails and no Short is created, the reserved export is released.

If MintiStudio finds that the same rendered clip already exists, or the same render is already in progress, it does not consume another export.

**What happens when I reach my monthly export limit?**

When you reach the export limit for your plan, MintiStudio pauses additional Short generation until your usage resets or you upgrade your plan.

You may see a message such as:

`Monthly export limit reached for your plan.`

or:

`Exports are paused - you've reached this month's export limit. Upgrade or wait for the reset.`

**How is transcription usage measured?**

Transcription usage is measured in minutes.

Before starting a transcription, MintiStudio checks whether enough transcription time remains for the source video.

If the video requires more transcription time than you have left for the month, transcription will not start.

Failed transcription attempts should not be described as consuming minutes unless a completed transcription is recorded.

**When do my monthly usage limits reset?**

Export and transcription usage are tracked in monthly usage periods.

The current implementation resets monthly usage at the beginning of the next monthly usage period.

Do not claim that unused usage rolls over to the next month.

**How do I upgrade my plan?**

You can upgrade from the `Plan & storage` page.

Paid plan upgrades use Stripe checkout inside MintiStudio.

After the checkout is successfully completed, MintiStudio updates your subscription and plan.

**How do I manage my subscription or payment method?**

If you have an active Stripe-managed subscription, use the `Manage subscription` option in MintiStudio.

This opens the Stripe Billing Portal, where supported subscription and payment settings can be managed.

Do not promise specific Stripe Portal actions beyond what is available for that customer's subscription configuration.

**What happens if I switch from a paid plan to Free?**

For Stripe-managed subscriptions, switching to Free is scheduled for the end of the current billing period.

Your paid plan remains active until that date.

MintiStudio may show:

`Subscription will switch to Free on <date>.`

Do not describe this as an immediate cancellation or immediate loss of content.

**Can I have more than one workspace or brand?**

Yes.

A MintiStudio account can have multiple brands/workspaces, and you can switch between them from the account interface.

Videos, connected social accounts, assets, and related workspace content are scoped to the selected brand/workspace.

**Does each workspace have its own usage limit?**

No.

Current plan and usage limits belong to the MintiStudio user account rather than being separately allocated to each brand/workspace.

If you use multiple workspaces, they share the same account-level export, transcription, and plan limits.

**What account settings can I change?**

From the account/profile area, you can currently manage settings including:

- name
- email
- password
- time zone
- brands/workspaces

Connected social platforms are managed separately from the Connections area.
