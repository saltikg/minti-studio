from __future__ import annotations

from copy import deepcopy
from datetime import date


BLOG_ARTICLES = [
    {
        "slug": "publish-youtube-shorts-at-once",
        "title": "Should You Publish All Your YouTube Shorts at Once?",
        "description": (
            "Turning one long video into many Shorts is efficient, but publishing all of them at once "
            "usually is not. Here is a practical scheduling approach based on YouTube's own guidance."
        ),
        "published_on": date(2026, 8, 8),
        "author_name": "MintiStudio Team",
        "reading_time": "5 min read",
        "sections": [
            {
                "heading": "Making multiple Shorts from one video is smart",
                "paragraphs": [
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
                        "When several Shorts from the same source video go out at the same time, each one has less room to breathe. "
                        "Your audience gets multiple similar uploads at once, your team has fewer chances to learn from the first "
                        "results before publishing the next clip, and your release cadence becomes harder to maintain consistently."
                    ),
                ],
                "bullets": [
                    "You lose the chance to space attention across several days.",
                    "You miss easy iteration opportunities on titles, hooks, and thumbnails based on early results.",
                    "You train your workflow around bursts instead of a schedule you can keep repeating.",
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
                        "your audience, and whether each clip feels distinct enough to earn its own slot on the calendar."
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
