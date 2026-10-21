---
title: Editor guide — from long video to Shorts
slug: editor-guide
type: guide
description: A complete guide to the MintiStudio editor — clips, AI suggestions, layout, captions, title, shapes, music and regenerating.
category_slug: creating-shorts
topics: [Shorts, AI, Editor, Captions, Layout, Music, Shapes, Templates]
order: 25
---

Turn one long video into several Shorts: add the video, pick your moments, design them, and render. This guide walks through every part of the editor.

**On this page:** [Add a video](#add-a-video) · [Your Clips](#your-clips) · [Make a Clip](#make-a-clip) · [Edit a clip's text](#edit-clip-text) · [Design](#edit-shorts) · [Style](#style) · [Layout](#layout) · [Captions](#captions) · [Title](#title) · [Shapes](#shapes) · [Music](#music) · [Background](#background) · [Subscribe](#subscribe) · [Save & regenerate](#saving) · [Schedule](#schedule) · [Common questions](#faq)

## Add a video {#add-a-video}

Go to [My Videos](/video_shorts/my-videos) and paste a YouTube link, or upload a video file.

MintiStudio downloads the video and creates a transcript. Long videos can take a few minutes. The editor opens as soon as the transcript is ready.

![Adding a video in My Videos](/video_shorts/static/help/editor/01-add-video.png "Paste a YouTube link or upload a file [screenshot]")

## Your Clips — the clip list {#your-clips}

`Your Clips` lists every clip made from this video. Each row shows the thumbnail, title, time range and length.

- **AI score** — AI-suggested clips show a score (for example `60`). Higher means the moment is more likely to work as a Short.
- **More AI suggestions** — when AI found more moments you'll see `N more AI suggestions` with a `Create N clips` button.
- **AI Suggest** — ask AI for new clip ideas at any time.
- **Status** — `Not created yet`, `In queue`, `Rendering…` or `Rendered`. You can keep working while clips render.

Click a clip to open it. Under the preview of a rendered clip you'll find:

| Button | What it does |
|---|---|
| `Schedule` | Schedule the Short to your connected channels |
| ↗ Open | Open the video in a new tab |
| ⤓ Download | Download the MP4 |
| ↻ Regenerate | Render the clip again with its current text and design |

## Make a Clip — pick your own moment {#make-a-clip}

:::steps
### Find the moment
Play the video, or type in `Search transcript` and use ↑ ↓ to jump between matches. Click ▶ next to a timestamp to play from there.

### Select the text and click + Add
Drag over the words you want. A small toolbar appears: `▶ Preview` plays the selection and shows its length. Click `+ Add` — the part turns green.

### Combine moments (optional)
Select another passage and click `+ Add` again. All parts are joined into one Short, in order. The bar at the bottom shows the count and total length, for example `1 part · 0:34`.

### Title (optional)
Type a title, or leave it empty and AI writes one for you.

### Create the clip
Click `▶ Preview all` to hear the whole Short, then `Create clip`. The clip appears in `Your Clips` and starts rendering.
:::

![Select text, then click + Add](/video_shorts/static/help/editor/03b-make-a-clip-select.png "Select text, preview it, then + Add [screenshot]")

![Parts added — ready to create](/video_shorts/static/help/editor/03-make-a-clip.png "The bottom bar shows parts, length and title [screenshot]")

![A new clip rendering](/video_shorts/static/help/editor/02b-rendering.png "The new clip renders in Your Clips [screenshot]")

:::tip
Start and end on full sentences. Shorts that start mid-sentence lose viewers in the first second.
:::

## Edit a clip's text and title {#edit-clip-text}

Open a clip in `Your Clips`. The middle column shows the transcript around the clip.

- **Normal text** is in the Short. **Struck-through text** is not.
- **Remove a part** — select words inside the clip and click `Remove part`. They're cut from the video and the captions.
- **Add a part back** — select struck-through words, `▶ Preview`, then `+ Add`.
- **Title** — click the title at the top to rename the clip. This title appears on the Short.

Click `Save`, or `Save & regenerate` to render the new version right away.

## Design your Shorts {#edit-shorts}

Click `Design` to change how your Shorts look. The menu on the left has eight sections; the preview on the right updates as you go.

:::info
The preview is labelled **Preview — not rendered**. It's a live preview. Your changes are written into the video file when you regenerate the clip.
:::

### Style {#style}

Pick a ready-made look: `Bold Pop`, `Blue Pop`, `Silver Pop`, `Amber Gold`, `Modern Pink`, `Modern Purple`, `Sky Cyan`, `Electric Lime`, `Serif Bordo` or `Word Highlight`. A style sets caption and title fonts, colors and the highlighted-word effect together. You can fine-tune everything afterwards in `Captions` and `Title`.

![Style templates](/video_shorts/static/help/editor/06-style.png "Style templates [screenshot]")

### Layout {#layout}

Choose how your horizontal video fits the vertical 9:16 frame.

| Layout | What it does | Best for |
|---|---|---|
| **Auto** (face-aware) | Picks a layout for each part of the clip by counting faces: 1 face → Fill, 2 faces → Fill split, no face → Fit | Most videos — recommended |
| **Fill** | Crops the video to fill the frame | One person talking to camera |
| **Fill split** | Two crops stacked top and bottom | Interviews, two-person podcasts |
| **Fit** | Whole 16:9 video in the middle, with a background | Slides, screen recordings, wide shots |
| **Original** | Keeps the video's original shape | No cropping at all |

With `Fill` and `Fill split`, drag the frame under **Drag to reframe** to choose what's shown.

:::tip Auto also places your captions
Below the face in Fill, on the middle line in Fill split, and under the video in Fit — always above the Subscribe button. Move the captions yourself and your position is kept.
:::

![Layout options](/video_shorts/static/help/editor/07-layout.png "Layout — drag to reframe [screenshot]")

### Captions {#captions}

Captions come from the transcript and are synced word by word.

- `Visible` — show or hide captions.
- `Font`, `Size`, `Text color` (with opacity).
- `Background color` — a box behind the captions, with its own opacity. `None` means no box.
- `Anchor` — `Top`, `Center` or `Bottom`: the edge that stays fixed when a line wraps. With `Top`, extra lines grow downward.
- **Position** — drag the captions in the preview, or use the ↑ ↓ keys.
- `More options` — additional caption settings.

![Caption settings](/video_shorts/static/help/editor/08-captions.png "Captions [screenshot]")

### Title {#title}

The title sits at the top of the Short.

- `Visible`, `Font`, `Size`, `Line spacing`, `Text color`.
- `Background` — color and opacity of the title bar.
- **Position** — drag the title in the preview, or use the ↑ ↓ keys.

To change the words, rename the clip — see [Edit a clip's text](#edit-clip-text).

### Shapes {#shapes}

Shapes are rectangles placed over the video — perfect for **covering burned-in subtitles, a logo or a watermark** in the source.

:::steps
### Add a shape
Click `Add shape`.

### Place it
Drag and resize it in the preview.

### Style it
Set `Color`, `Opacity` and `Corner radius`. Use `Duplicate` or `Delete` as needed.
:::

Shapes cover the same area for the whole clip.

### Music {#music}

Add background music to a Short. Music is set **per clip** — each Short can have its own song.

:::steps
### Upload or choose a song
Click `Upload music` (MP3, WAV or M4A). Uploaded songs stay in your library for any Short. Use `Change` to pick another one — search by name, and click ✎ to rename a song.

### Listen with your voice
Press ▶ to hear your voice and the music together. Drag the white line to jump to any moment.

### Choose the part of the song
Drag the music waveform left or right. The label shows what's used, for example `0:09 – 0:44 of 1:18`.

### Set the level
Use the `Music` slider. Your voice always stays at 100%; around 50% is a good background level.

### Lower music when speaking
When on, the music gets quieter while you speak and comes back up in pauses.

### Save and regenerate
Music is written into the video only when the clip is rendered.
:::

Click ✕ to remove the music from this Short.

![Background music](/video_shorts/static/help/editor/11-music.png "Music — voice lane, draggable song and level [screenshot]")

:::warning
Use only music you own or have the rights to. YouTube may block or mute Shorts with copyrighted music.
:::

### Background {#background}

Used with the `Fit` layout, or wherever the video doesn't fill the frame. Pick an image or color to show behind the video.

### Subscribe {#subscribe}

Turn on a **Subscribe** animation near the bottom of the Short and choose its style.

## Save, Save as default and Regenerate {#saving}

| Button | What it does |
|---|---|
| `Save design` | Saves your changes for this video's Shorts |
| `Save as default` | Also uses these settings as the starting design for future videos |
| `Cancel` | Discards unsaved changes |

:::key
Saving doesn't re-render your Shorts. A rendered Short keeps its look until you regenerate it — so Shorts that are already scheduled or published are never changed by accident.
:::

When a rendered clip no longer matches its saved design, a small **orange dot** appears on the clip in `Your Clips` and on the ↻ button, with a note like *Music changed — regenerate to apply*. Click ↻ to render the new version. Each render counts toward your monthly export limit.

## Schedule and publish {#schedule}

- `Schedule` under a rendered clip — pick channels and a publish time.
- `Schedule all` at the top — schedule every ready clip at once.

Details: [Scheduling & Publishing](/video_shorts/help/faq/scheduling-and-publishing/).

## Common questions {#faq}

**The captions cover the face.** Use the `Auto` layout — captions are then placed below the face. Or drag the captions in the preview, then save and regenerate.

**I changed the design but my Short looks the same.** Changes reach the video only after you regenerate. Look for the orange dot and click ↻.

**The preview and the final video look slightly different.** The preview is a live approximation; the rendered video is the final result. Tell us if something looks off.

**I added music but it's not in my video.** Save, then regenerate the clip.

**Can each Short have different music?** Yes — music is set per clip.

**The source video has burned-in subtitles.** Cover them with a [Shape](#shapes), then use MintiStudio captions.

**I reached my monthly export limit.** You can't render more Shorts until the limit resets or you upgrade.

:::action Need help?
Write to us at info@mintistudio.com — we usually reply within a day.
:::
