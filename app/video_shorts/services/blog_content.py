from __future__ import annotations

from copy import deepcopy
from datetime import date


BLOG_ARTICLES = [
    {
        "slug": "getting-started-with-mintistudio",
        "title": "Getting Started: From Long Video to Published Short",
        "description": (
            "A step-by-step walk-through: sign in, upload a long video, let Minti find the moments, "
            "style your Short, and publish across platforms."
        ),
        "meta_title": "Getting Started: From Long Video to Published Short | MintiStudio",
        "meta_description": (
            "Follow the full MintiStudio workflow from sign-in to a published Short, including upload, "
            "transcript review, clip suggestions, styling, rendering, scheduling, and tracking."
        ),
        "published_on": date(2026, 8, 21),
        "author_name": "MintiStudio Team",
        "reading_time": "7 min read",
        "sections": [
            {
                "step_number": "01",
                "heading": "Open your account and land in My Videos",
                "paragraphs": [
                    (
                        "Start by signing in to MintiStudio, then head straight to My Videos. This is your working library: "
                        "the place where uploaded long videos, generated Shorts, and their current progress all come together."
                    ),
                ],
                "images": [
                    {
                        "src": "img/blog/getting-started/01-open-account.png",
                        "alt": "MintiStudio account sign-in screen",
                    },
                    {
                        "src": "img/blog/getting-started/02-my-videos.png",
                        "alt": "My Videos library in MintiStudio",
                    },
                ],
            },
            {
                "step_number": "02",
                "heading": "Upload a long video and start the analysis",
                "paragraphs": [
                    (
                        "Upload the long-form video you want to repurpose, then start analysis. MintiStudio prepares the video "
                        "for the rest of the workflow by pulling together the media, transcript, and clip-planning context."
                    ),
                ],
                "images": [
                    {
                        "src": "img/blog/getting-started/03-start-analysis.png",
                        "alt": "Start analysis action for a long video in MintiStudio",
                    },
                ],
            },
            {
                "step_number": "03",
                "heading": "Review and clean up the transcript",
                "paragraphs": [
                    (
                        "Once the transcript is ready, skim through it and make any fixes that matter before clipping. A clean "
                        "transcript gives Minti better material for suggestions and makes later caption editing much smoother."
                    ),
                ],
                "images": [
                    {
                        "src": "img/blog/getting-started/04-edit-transcript.png",
                        "alt": "Transcript editing interface in MintiStudio",
                    },
                ],
            },
            {
                "step_number": "04",
                "heading": "Let Minti suggest the strongest moments",
                "paragraphs": [
                    (
                        "Ask Minti to suggest clips from the full video. Instead of starting from a blank slate, you get a set "
                        "of candidate moments that are already shaped around hooks, self-contained ideas, and short-form pacing."
                    ),
                ],
                "images": [
                    {
                        "src": "img/blog/getting-started/05-suggest-clips.png",
                        "alt": "AI-generated clip suggestions in MintiStudio",
                    },
                ],
            },
            {
                "step_number": "05",
                "heading": "Choose a clip and style the frame",
                "paragraphs": [
                    (
                        "Open the clip in the editor and set the look you want: crop, background, subtitles, and overall framing. "
                        "This is where the Short starts to feel ready for publishing rather than just extracted."
                    ),
                ],
                "images": [
                    {
                        "src": "img/blog/getting-started/06-crop-style.png",
                        "alt": "Crop and style controls for a Short in MintiStudio",
                    },
                ],
            },
            {
                "step_number": "06",
                "heading": "Create the clip from the chosen moment",
                "paragraphs": [
                    (
                        "When the framing and styling look right, create the clip. Minti turns that selected range into a proper "
                        "Short project that can now be rendered, reviewed, and published."
                    ),
                ],
                "images": [
                    {
                        "src": "img/blog/getting-started/07-create-clip.png",
                        "alt": "Create clip action in MintiStudio",
                    },
                ],
            },
            {
                "step_number": "07",
                "heading": "Render the final Short",
                "paragraphs": [
                    (
                        "Run the render once everything is set. Minti composes the vertical video, captions, title treatment, "
                        "and background choices into the final export you can review and download."
                    ),
                ],
                "images": [
                    {
                        "src": "img/blog/getting-started/08-render.png",
                        "alt": "Render progress for a Short in MintiStudio",
                    },
                ],
            },
            {
                "step_number": "08",
                "heading": "Schedule and publish across platforms",
                "paragraphs": [
                    (
                        "After rendering, send the Short into your publishing flow. You can schedule the release and manage where "
                        "it goes next, so YouTube, Instagram, and Facebook all fit into the same workflow instead of separate tools."
                    ),
                ],
                "images": [
                    {
                        "src": "img/blog/getting-started/09-schedule-publish.png",
                        "alt": "Schedule and publish screen in MintiStudio",
                    },
                ],
            },
            {
                "step_number": "09",
                "heading": "Track the result and keep the loop going",
                "paragraphs": [
                    (
                        "Once the Short is live, use the tracking view to see how it performed and carry that learning into the "
                        "next batch. The workflow is meant to repeat: one long video in, several smarter Shorts out."
                    ),
                ],
                "images": [
                    {
                        "src": "img/blog/getting-started/10-track.png",
                        "alt": "Tracking and performance view for Shorts in MintiStudio",
                    },
                ],
            },
        ],
        "sources": [],
        "cta_title": "Turn one long video into a publishable Shorts workflow",
        "cta_body": (
            "Upload once, find the moments, style the clip, and publish across platforms from the same place."
        ),
    },
    {
        "slug": "your-scheduled-time-is-only-as-good-as-your-time-zone",
        "title": "Your Scheduled Time Is Only as Good as Your Time Zone",
        "description": (
            "You picked 8:00 AM. Did your Short actually go out at your 8:00 AM? Learn how YouTube, "
            "Instagram, and timezone-aware scheduling really work across platforms."
        ),
        "meta_title": "Your Scheduled Time Is Only as Good as Your Time Zone | MintiStudio",
        "meta_description": (
            "Scheduling a Short is only reliable when the time is anchored to the right timezone. Learn how "
            "MintiStudio handles YouTube and Instagram publishing across time zones."
        ),
        "published_on": date(2026, 8, 11),
        "author_name": "MintiStudio Team",
        "reading_time": "6 min read",
        "sections": [
            {
                "heading": "\"Schedule it for 8 AM\" hides a real question",
                "paragraphs": [
                    (
                        "Scheduling a Short feels like a solved problem. You type a time, you move on. But underneath "
                        "that one field is a question most tools quietly answer for you: 8:00 AM where?"
                    ),
                    (
                        "A publish time only means something once it is anchored to a time zone. 8:00 AM in Istanbul "
                        "is not 8:00 AM in New York. If the scheduling tool assumes one zone while you are thinking in "
                        "another, your clip goes out at the wrong hour and the audience you meant to reach never sees it "
                        "near the top of their feed."
                    ),
                    (
                        "That is not a rare edge case. It is one of the easiest ways a scheduled post quietly misses its "
                        "moment. Before worrying about cadence, notifications, or cross-platform timing, the first job is "
                        "to make sure the time you pick is actually your time."
                    ),
                ],
            },
            {
                "heading": "How the platforms actually handle scheduled time",
                "paragraphs": [
                    (
                        "YouTube and Instagram do not handle scheduling the same way, and that difference matters. "
                        "For YouTube, MintiStudio converts your chosen local time into UTC, uploads the video as private, "
                        "and sends YouTube a scheduled publish timestamp. YouTube then handles the public release on its side."
                    ),
                    (
                        "Instagram works differently. There is no native later-publish timestamp in the publishing call that "
                        "MintiStudio can hand off and forget about. Instead, Instagram publishing runs through MintiStudio's "
                        "own queue and worker, which hold the UTC due time and publish when that moment arrives."
                    ),
                    (
                        "That is the hidden operational difference between \"schedule to YouTube\" and \"schedule to Instagram.\" "
                        "One is a platform-native schedule handoff. The other depends on your publishing tool running the timer "
                        "correctly on your behalf."
                    ),
                ],
            },
            {
                "heading": "Why the timezone is the part that breaks",
                "paragraphs": [
                    (
                        "Your typed time is just a wall-clock number until a time zone is attached to it. Once the zone is "
                        "wrong, every step after that is wrong too: the UTC conversion is off, the YouTube handoff is off, and "
                        "the Instagram due-check is off by the same gap."
                    ),
                    (
                        "The annoying part is that nothing necessarily fails. The clip can publish successfully and still go out "
                        "hours away from what you intended. You usually notice only later, when the performance looks soft and "
                        "the publish time no longer matches the audience behavior you were aiming for."
                    ),
                ],
                "bullets": [
                    "Your typed time is only digits until a time zone turns it into a real moment.",
                    "The account time zone is the anchor the scheduler actually uses.",
                    "Get that anchor right once, and every future schedule inherits it.",
                ],
            },
            {
                "heading": "Making 8:00 AM mean your 8:00 AM",
                "paragraphs": [
                    (
                        "In MintiStudio, the Schedule & Publish window shows your account time zone next to the publish-time field, "
                        "so you can see which clock your schedule is anchored to. If that label is wrong, the time you pick will be "
                        "interpreted for the wrong place."
                    ),
                    (
                        "That is why the scheduling window includes an inline edit control right next to the time-zone label. "
                        "You can open the picker, choose your zone from the same account timezone list, and update your account "
                        "immediately without leaving the scheduling flow."
                    ),
                    (
                        "The important behavior is subtle but intentional: changing the zone updates the anchor, not the digits "
                        "you already typed. If you entered 8:00 AM, the field stays at 8:00 AM. What changes is which time zone "
                        "that 8:00 AM belongs to."
                    ),
                ],
            },
            {
                "heading": "Same time or separate times across platforms",
                "paragraphs": [
                    (
                        "Once the timezone is right, cross-platform timing becomes a deliberate choice instead of a guess. "
                        "You may want YouTube and Instagram to launch together, or you may want Instagram to go live earlier or later."
                    ),
                    (
                        "MintiStudio gives Instagram three timing modes relative to the YouTube schedule: publish at the same time "
                        "as YouTube, publish immediately on Instagram, or choose a separate Instagram time. That gives you one "
                        "shared timezone anchor with flexible platform timing on top of it."
                    ),
                ],
                "bullets": [
                    "Same time as YouTube for a coordinated launch.",
                    "Publish immediately on Instagram if you want it live now.",
                    "Choose a separate Instagram time if each platform needs a different slot.",
                ],
            },
            {
                "heading": "Let your analytics choose the hour",
                "paragraphs": [
                    (
                        "The timezone fix is mechanical, but it matters because it makes your analytics usable. Once your scheduled "
                        "times reliably mean what you think they mean, you can compare performance across days and actually trust "
                        "those comparisons."
                    ),
                    (
                        "That is where your own data becomes more useful than generic \"best time to post\" advice. Look at which "
                        "publish windows produced the strongest early traction, then schedule the next batch into those windows in "
                        "your own timezone."
                    ),
                ],
                "bullets": [
                    "Check which past publish hours produced your best early views.",
                    "Reuse those windows for the next batch.",
                    "Keep the timezone anchor consistent so the comparison stays honest.",
                ],
            },
            {
                "heading": "Where MintiStudio fits",
                "paragraphs": [
                    (
                        "MintiStudio anchors scheduled publishing to your account timezone, converts your selected local time to UTC, "
                        "hands the YouTube schedule off to YouTube, and runs Instagram publishing itself when the due time arrives."
                    ),
                    (
                        "That gives you one place to control the timezone, one place to choose whether platforms stay in sync, and a "
                        "schedule you can actually trust."
                    ),
                ],
            },
        ],
        "sources": [
            {
                "label": "YouTube Help: Schedule your video's publish time",
                "url": "https://support.google.com/youtube/answer/1270709",
            },
            {
                "label": "Google for Developers: Videos resource",
                "url": "https://developers.google.com/youtube/v3/docs/videos",
            },
            {
                "label": "Meta for Developers: Publish Content",
                "url": "https://developers.facebook.com/docs/instagram-platform/content-publishing/",
            },
        ],
        "cta_title": "Publish on your schedule — and mean it",
        "cta_body": (
            "Set your timezone once, schedule across platforms with confidence, and make every publish time mean exactly what you intended."
        ),
    },
    {
        "slug": "ai-can-suggest-your-clips-you-should-still-decide-which-ones-go-out",
        "title": "AI Can Suggest Your Clips. You Should Still Decide Which Ones Go Out.",
        "description": (
            "AI is good at finding candidate moments in a long video. Learn how to keep the speed of AI clip "
            "suggestions without giving up editorial control over what represents your channel."
        ),
        "meta_title": "AI Clip Suggestions Need Human Review | MintiStudio",
        "meta_description": (
            "AI can suggest strong clip candidates from a long video, but creators should still decide what gets "
            "published. Here is how to keep editorial control in your Shorts workflow."
        ),
        "published_on": date(2026, 8, 9),
        "author_name": "MintiStudio Team",
        "reading_time": "5 min read",
        "sections": [
            {
                "heading": "AI suggestions are a starting point, not a verdict",
                "paragraphs": [
                    (
                        "If you record one strong long-form video, there are usually several short clips hiding inside it. "
                        "Modern tools can scan the transcript, find candidate moments, and propose clips automatically. "
                        "That removes the blank-page problem and saves you from scrubbing through a long recording looking "
                        "for the good parts."
                    ),
                    (
                        "In MintiStudio, that is what Suggest clips with AI does: it reads the full transcript and proposes "
                        "a set of clips, each with a suggested title and time range, so you start with options instead of "
                        "an empty timeline."
                    ),
                    (
                        "But there is a real difference between proposing clips and deciding which clips actually go out "
                        "under your name. The first is a job AI does well. The second is an editorial call that belongs to you."
                    ),
                    (
                        "When those two ideas get blurred, the AI stops being a useful assistant and starts quietly shaping "
                        "the identity of your channel. That is the boundary worth protecting."
                    ),
                ],
            },
            {
                "heading": "Why the human decision still matters",
                "paragraphs": [
                    (
                        "An AI planner optimizes for generic signals: a self-contained moment, a clean hook, and a reasonable "
                        "length. It does not know when a clip oversimplifies a point you care about, repeats something you "
                        "posted last week, or lands the wrong way with your audience."
                    ),
                    (
                        "You know those things. The useful split is simple: the AI proposes candidates from the full video, "
                        "fast, so you are not starting from nothing. You review them, keep the ones that represent you well, "
                        "cut the ones that do not, and refine the rest."
                    ),
                    (
                        "That is not a step backward from automation. It is what makes the automation safe to use at all."
                    ),
                ],
                "bullets": [
                    "The tool handles breadth by finding multiple candidate moments quickly.",
                    "You handle judgment by deciding what is accurate, on-brand, and worth publishing.",
                    "The final publishing decision stays with the creator, not the model.",
                ],
            },
            {
                "heading": "What keeping control should actually feel like",
                "paragraphs": [
                    (
                        "In practice, staying in control is less about philosophy and more about whether your tools make the "
                        "boundary visible. You should be able to tell at a glance which clips are AI suggestions and which "
                        "ones you created or already committed to."
                    ),
                    (
                        "In MintiStudio, unbuilt AI suggestions are clearly marked as drafts and shown separately from the "
                        "clips you have actually built. A proposal should never get mistaken for a finished clip you chose."
                    ),
                    (
                        "Removing suggestions also needs to be safe. Clearing a batch of AI suggestions should affect only "
                        "the unbuilt proposals, not clips you have already built, scheduled, or published."
                    ),
                    (
                        "Once you turn a suggestion into a real clip, it should stop behaving like a suggestion. At that "
                        "point it is part of your library and should be treated and protected like the rest of your work."
                    ),
                ],
            },
            {
                "heading": "A simple working rhythm",
                "paragraphs": [
                    (
                        "A repeatable workflow is straightforward: generate suggestions once per video, review them as drafts, "
                        "and treat that review as the real decision point."
                    ),
                    (
                        "Keep the clips that earn their spot, remove the rest, and refine titles and hooks on the survivors. "
                        "Then move those into your publishing schedule the same way you would any clip you cut yourself."
                    ),
                    (
                        "You get the speed of automated clip-finding and the confidence that everything going out is something "
                        "you actually chose."
                    ),
                ],
                "bullets": [
                    "Generate AI suggestions once per source video.",
                    "Review them as drafts before anything is built or scheduled.",
                    "Promote only the clips you want to publish.",
                ],
            },
            {
                "heading": "Where MintiStudio fits",
                "paragraphs": [
                    (
                        "MintiStudio is built around this split. AI proposes clips from your long-form video, and those "
                        "proposals stay clearly marked as suggestions, separate from the clips you have already built."
                    ),
                    (
                        "You keep the ones you want, remove the rest without risking finished work, and any suggestion that "
                        "becomes a real clip is protected like the rest of your library."
                    ),
                    (
                        "The result is a workflow with both speed and control: one production session, several candidate clips, "
                        "and a publishing calendar filled only with clips you decided were worth it."
                    ),
                ],
            },
        ],
        "sources": [],
        "cta_title": "Turn one video into clips you actually chose",
        "cta_body": (
            "Let AI find the candidates, then keep control of what represents your channel."
        ),
    },
    {
        "slug": "publish-youtube-shorts-at-once",
        "title": "Should You Publish All Your YouTube Shorts at Once?",
        "description": (
            "Have 10, 20, or more YouTube Shorts ready to publish? Learn why spacing Shorts across a "
            "consistent schedule is usually better than publishing them all at once."
        ),
        "meta_title": "Should You Publish All YouTube Shorts at Once? | MintiStudio",
        "meta_description": (
            "Have 10, 20, or more YouTube Shorts ready to publish? Learn why spacing Shorts across a "
            "consistent schedule is usually better than publishing them all at once."
        ),
        "published_on": date(2026, 8, 8),
        "author_name": "MintiStudio Team",
        "reading_time": "5 min read",
        "sections": [
            {
                "heading": "Making multiple Shorts from one video is smart",
                "paragraphs": [
                    (
                        "Should you publish all your YouTube Shorts at once? Usually, no. If you have 10, 20, "
                        "or more Shorts ready, spreading them across several days is generally a better publishing "
                        "workflow than releasing them all at once."
                    ),
                    (
                        "If you record one strong long-form video, there is usually more than one short clip inside it. "
                        "A good editing workflow can turn one upload into several Shorts with different hooks, moments, "
                        "or takeaways."
                    ),
                    (
                        "That part is efficient. The question is what to do next: publish all of those Shorts immediately, "
                        "or spread them out over time?"
                    ),
                ],
            },
            {
                "heading": "Publishing everything at once is usually not the best default",
                "paragraphs": [
                    (
                        "Publishing in a burst is not automatically bad, and YouTube does not say channels are algorithmically "
                        "penalized just for bulk publishing. But YouTube's own creator guidance points in a different "
                        "direction: avoid publishing in bulk, build a schedule you can sustain, and think about the viewer experience."
                    ),
                    (
                        "When several Shorts from the same source video go live at the same time, you lose the opportunity "
                        "to spread your publishing activity across several days. You also have fewer chances to learn from "
                        "early performance before the next clips are published."
                    ),
                ],
                "bullets": [
                    "You lose the opportunity to spread your publishing activity across several days.",
                    "You have fewer chances to learn from early results before publishing the next clips.",
                    "Your content calendar becomes more dependent on bursts instead of a repeatable publishing rhythm.",
                ],
            },
            {
                "heading": "What YouTube actually says",
                "paragraphs": [
                    (
                        "YouTube's official Help Center says creators should avoid publishing videos in bulk, and separately says "
                        "that a consistent, sustainable release schedule is important for building audience expectations."
                    ),
                    (
                        "Just as important, YouTube does not say that bulk publishing causes an algorithm penalty. The guidance is "
                        "about sustainable publishing habits, audience expectations, and notification behavior, not a documented "
                        "ranking punishment for posting many Shorts at once."
                    ),
                    (
                        "YouTube also states that viewers can receive a maximum of three upload or live notifications from one "
                        "channel within a 24-hour period. That does not mean you cannot publish more than three Shorts in a day. "
                        "It means notifications are limited, so publishing many videos close together can reduce how much direct "
                        "notification reach those uploads get."
                    ),
                ],
            },
            {
                "heading": "A practical starting point: 2–3 Shorts per day",
                "paragraphs": [
                    (
                        "For many channels, two to three Shorts per day is a practical starting point when you have a backlog from "
                        "one long video. That is not an official YouTube rule. It is just a workable operating rhythm that lets you "
                        "publish consistently without dumping everything at once."
                    ),
                    (
                        "Some channels should post less. Some can support more. The right cadence depends on your production capacity, "
                        "your audience, and whether each clip feels distinct enough to earn its own slot on the calendar. You can "
                        "also refine titles, hooks, and packaging as you learn from the first clips in the sequence."
                    ),
                ],
                "bullets": [
                    "If you are just starting, try scheduling the clips across several days instead of one batch.",
                    "Review performance after the first few posts before locking in the rest of the week.",
                    "Prioritize consistency over volume spikes you cannot sustain.",
                ],
            },
            {
                "heading": "Where MintiStudio fits",
                "paragraphs": [
                    (
                        "MintiStudio is built for exactly this workflow: turn one long-form video into multiple Shorts, then spread "
                        "them across a schedule instead of publishing them all at once."
                    ),
                    (
                        "That gives you one production session, several reusable clips, and a cleaner publishing calendar. You can "
                        "keep output steady, review performance as the week unfolds, and avoid turning a useful content batch into a "
                        "same-day pileup."
                    ),
                ],
            },
        ],
        "sources": [
            {
                "label": "YouTube Help: Upload schedule tips",
                "url": "https://support.google.com/youtube/answer/13616979?co=YOUTUBE._YTVideoType%3Dvideo&hl=en",
            },
            {
                "label": "YouTube Help: Fix subscriber notification problems",
                "url": "https://support.google.com/youtube/answer/7389684?hl=en",
            },
            {
                "label": "YouTube Help: Skip sending upload notifications",
                "url": "https://support.google.com/youtube/answer/7457584?hl=en",
            },
        ],
        "cta_title": "Turn one video into a week of Shorts",
        "cta_body": (
            "Create multiple Shorts from one long-form video, then use MintiStudio to schedule them across several days."
        ),
    }
]


def _article_copy(article: dict) -> dict:
    return deepcopy(article)


def get_blog_articles() -> list[dict]:
    articles = [_article_copy(article) for article in BLOG_ARTICLES]
    articles.sort(key=lambda item: item["published_on"], reverse=True)
    return articles


def get_blog_article(slug: str) -> dict | None:
    for article in BLOG_ARTICLES:
        if article["slug"] == slug:
            return _article_copy(article)
    return None


def get_latest_blog_articles(limit: int = 3) -> list[dict]:
    return get_blog_articles()[: max(0, int(limit))]
