from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Literal

from app.video_shorts.services.trial_copy import trial_duration_text


OutreachLanguage = Literal["EN", "TR"]
OutreachStage = Literal["first", "followup"]
OutreachFollowupBucket = Literal[
    "hot_repeat",
    "watched_no_convert",
    "visited_once",
    "sent_no_visit",
    "scheduled",
]


@dataclass(frozen=True)
class OutreachEmailTemplate:
    key: str
    subject: str
    text: str


OUTREACH_EMAIL_TEMPLATES: dict[str, OutreachEmailTemplate] = {
    "FIRST_EN": OutreachEmailTemplate(
        key="FIRST_EN",
        subject="I made a Short from your video",
        text="""Hi [Name],

I'm the founder of Minti Studio, based in San Francisco. Instead of explaining what we do, I thought I'd show you.

I turned one of your recent videos into a Short - here's how it came out:

[video_title]
[link]

Here's the idea: you keep making your long videos, and we grow your channel with Shorts - without you lifting a finger. We're not a clip tool you have to learn. Connect your channel once, and we handle everything - finding the best moments, captioning, and publishing to YouTube, Instagram, and Facebook, every month.

Your first month is on us - 15 Shorts, made and published for you, free. After that it's a flat $20/month - no credits to count, no surprise bills, cancel anytime. You always know exactly what it costs.

Take a look at your Short, and tap "Let us do it for you" to start. Or reply with any questions.

Best,
Gokhan Saltik
Founder, Minti Studio
mintistudio.com""",
    ),
    "FIRST_TR": OutreachEmailTemplate(
        key="FIRST_TR",
        subject="Videonuzdan hazırladığımız kısa bir örnek",
        text="""Merhaba [Name],

San Francisco'da Minti Studio adında bir video platformu geliştiriyoruz. Ne yaptığımızı uzun uzun anlatmak yerine, nasıl çalıştığını doğrudan kendi içeriğiniz üzerinden göstermek istedim.

Kanalınızdaki videonuzdan, sizin için hazırladığımız kısa bir örnek:

[video_title]
[link]

Buradaki örnekte uzun videonuzun içinden kısa videoya uygun bir bölüm Minti ile seçilerek altyazılı, dikey bir videoya dönüştürüldü.

Minti klip üretmekle kalmıyor - YouTube, Instagram ve Facebook'a yayınlıyor; yorumları yönetiyor ve hepsinin performansını tek yerden karşılaştırıyorsunuz.

Kendi videolarınızla denemek isterseniz, örnek sayfasındaki "Let us do it for you" butonuna basmanız yeterli. [trial] ücretsiz erişim otomatik olarak tanımlanıyor.

Herhangi bir sorunuz olursa memnuniyetle yardımcı olurum.

Selamlar,
Gokhan Saltik
Founder, Minti Studio
mintistudio.com""",
    ),
    "FOLLOWUP_EN": OutreachEmailTemplate(
        key="FOLLOWUP_EN",
        subject="A hands-off way to grow your channel - first month free",
        text="""Hi [Name],

Did you get the Short we made from your video?

[video_title]
[link]

Here's the thing - even though our tool makes it easy, I know that for a lot of creators, making Shorts is still one more task on top of an already busy schedule.

So here's the simpler version: you keep making your long videos, and we grow your channel with Shorts - without you lifting a finger. We're not a clip tool you have to learn. Connect your channel once, and we handle everything - finding the best moments, captioning, and publishing to your channels, every month.

Your first month is on us - 15 Shorts, made and published for you, free. After that it's a flat $20/month - no credits to count, no surprise bills, cancel anytime. You always know exactly what it costs.

Take a look at your Short, and tap "Let us do it for you" to start.

Best,
Gokhan""",
    ),
    "FOLLOWUP_TR": OutreachEmailTemplate(
        key="FOLLOWUP_TR",
        subject="Videonuzdan yaptığım Short'u gördünüz mü?",
        text="""Merhaba [Name],

Videonuzdan yaptığım Short gözden kaçmış olabilir. İşte burada:

[video_title]
[link]

Devam etmek isterseniz, kendi Short'larınızı aynı şekilde oluşturabilirsiniz - o sayfada "Let us do it for you" butonuna dokunmanız yeterli. [trial] ücretsiz, kayıt yok.

İyi çalışmalar,
Gokhan""",
    ),
}


OUTREACH_BUCKET_FOLLOWUP_TEMPLATES: dict[str, OutreachEmailTemplate] = {
    "SENT_NO_VISIT_SEQ2_EN": OutreachEmailTemplate(
        key="SENT_NO_VISIT_SEQ2_EN",
        subject="The Short I made from your video",
        text="""Hi [Name], a little while back I turned one of your videos into a Short - it might've slipped past you. Takes 30 seconds to see: [link].

Not for you? Just reply and I'll stop.""",
    ),
    "SENT_NO_VISIT_SEQ3_EN": OutreachEmailTemplate(
        key="SENT_NO_VISIT_SEQ3_EN",
        subject="I'll close the loop on this",
        text="""Hi [Name], last one from me - your Short is still up if you'd like a look: [link].

No reply and I won't email again.""",
    ),
    "VISITED_ONCE_EN": OutreachEmailTemplate(
        key="VISITED_ONCE_EN",
        subject="The rest of your Shorts are ready",
        text="""Hi [Name], thanks for checking out the Short. There are more from the same video, all ready - just tap "Watch all Shorts": [link].""",
    ),
    "WATCHED_NO_CONVERT_EN": OutreachEmailTemplate(
        key="WATCHED_NO_CONVERT_EN",
        subject="First month's on us - nothing for you to do",
        text="""Hi [Name], connect your channel once and we handle the rest - finding the moments, captioning, publishing to YouTube, Instagram and Facebook. First month free, 15 Shorts, cancel anytime: [link].""",
    ),
    "WATCHED_NO_CONVERT_2MO_EN": OutreachEmailTemplate(
        key="WATCHED_NO_CONVERT_2MO_EN",
        subject="Your complimentary access is still active",
        text="""Hi [Name], connect your channel once and we handle the rest - finding the moments, captioning, publishing to YouTube, Instagram and Facebook. Your complimentary access is still active: [link].""",
    ),
    "HOT_REPEAT_EN": OutreachEmailTemplate(
        key="HOT_REPEAT_EN",
        subject="Want me to just get you started?",
        text="""Hi [Name], looks like you've come back to your Shorts a few times - I'm happy to set the whole thing up for you, or hop on a quick call if that's easier. Just reply and we'll go from there.""",
    ),
}


_NAME_PREFIXES = {
    "dr",
    "doctor",
    "mr",
    "mrs",
    "ms",
    "miss",
    "prof",
    "professor",
    "coach",
    "by",
}
_NON_PERSON_FIRST_TOKENS = {
    "a",
    "an",
    "admin",
    "autism",
    "customer",
    "customerservice",
    "feedback",
    "god",
    "hello",
    "info",
    "inquiries",
    "media",
    "pcfgstudy",
    "speaking",
    "sfu",
    "support",
    "team",
    "the",
    "to",
}
_NAME_SUFFIX_TOKENS = {
    "astrologer",
    "business",
    "co",
    "company",
    "creator",
    "inc",
    "llc",
    "official",
    "psychic",
    "team",
}


def outreach_greeting_name(recipient_name: object, *, language: object = "EN") -> str:
    """Return a safe first name for outreach greetings, or a generic fallback."""
    normalized_language = normalize_outreach_template_language(language)
    fallback = "there"
    raw_name = str(recipient_name or "").strip()
    if not raw_name:
        return fallback
    candidate = re.split(r"\s[-|]\s|[-|]", raw_name, maxsplit=1)[0]
    candidate = re.sub(r"\([^)]*\)", " ", candidate)
    candidate = re.sub(r"[\[\]{}\"“”‘’]", " ", candidate)
    candidate = re.sub(r"\s+", " ", candidate).strip(" ,.;:/")
    if not candidate:
        return fallback
    tokens = [token.strip(" ,.;:/") for token in candidate.split() if token.strip(" ,.;:/")]
    while tokens and tokens[0].strip(".").lower() in _NAME_PREFIXES:
        tokens.pop(0)
    while tokens and tokens[-1].strip(".").lower() in _NAME_SUFFIX_TOKENS:
        tokens.pop()
    if not tokens:
        return fallback
    first = tokens[0].strip(" ,.;:/")
    first_key = first.strip(".").lower()
    if first_key in _NON_PERSON_FIRST_TOKENS:
        return fallback
    if not re.fullmatch(r"[A-Za-z][A-Za-z'’]*", first):
        return fallback
    if first.isupper() and len(first) > 1:
        return fallback
    if normalized_language == "TR" and first_key == "merhaba":
        return fallback
    return first


def normalize_outreach_template_language(value: object) -> OutreachLanguage:
    return "TR" if str(value or "").strip().upper() == "TR" else "EN"


def normalize_outreach_template_stage(value: object) -> OutreachStage:
    return "followup" if str(value or "").strip().lower() == "followup" else "first"


def normalize_outreach_followup_bucket(value: object) -> OutreachFollowupBucket:
    normalized = str(value or "").strip().lower()
    if normalized in {"hot_repeat", "watched_no_convert", "visited_once", "sent_no_visit"}:
        return normalized  # type: ignore[return-value]
    return "scheduled"


def outreach_template_key(*, stage: object, language: object) -> str:
    normalized_stage = normalize_outreach_template_stage(stage)
    normalized_language = normalize_outreach_template_language(language)
    return f"{normalized_stage.upper()}_{normalized_language}"


def _simple_html_email(*, subject: str, body_text: str) -> str:
    paragraphs = []
    for block in body_text.split("\n\n"):
        escaped_lines = []
        for line in str(block or "").split("\n"):
            raw_line = line.strip()
            if raw_line.startswith(("http://", "https://")):
                safe_url = html.escape(raw_line, quote=True)
                escaped_lines.append(f'<a href="{safe_url}">{safe_url}</a>')
            else:
                escaped_lines.append(html.escape(line))
        escaped = "<br>".join(escaped_lines)
        paragraphs.append(f"<p>{escaped}</p>")
    return "\n".join(paragraphs)


def render_outreach_email(
    *,
    stage: object,
    language: object,
    recipient_name: object,
    share_url: str,
    trial_days: object,
    video_title: object = "",
) -> dict[str, str]:
    normalized_stage = normalize_outreach_template_stage(stage)
    normalized_language = normalize_outreach_template_language(language)
    key = outreach_template_key(stage=normalized_stage, language=normalized_language)
    template = OUTREACH_EMAIL_TEMPLATES.get(key) or OUTREACH_EMAIL_TEMPLATES["FIRST_EN"]
    safe_name = outreach_greeting_name(recipient_name, language=normalized_language)
    trial_phrase = trial_duration_text(trial_days, normalized_language)
    safe_video_title = str(video_title or "").strip()
    text = (
        template.text.replace("[Name]", safe_name)
        .replace("[link]", str(share_url or "").strip())
        .replace("[trial]", trial_phrase)
        .replace("[video_title]\n", f"{safe_video_title}\n" if safe_video_title else "")
        .replace("[video_title]", safe_video_title)
    )
    return {
        "key": template.key,
        "stage": normalized_stage,
        "language": normalized_language,
        "subject": template.subject,
        "text": text,
        "html": _simple_html_email(subject=template.subject, body_text=text),
    }


def render_bucket_followup_outreach_email(
    *,
    bucket: object,
    sequence_number: object,
    language: object,
    recipient_name: object,
    share_url: str,
    trial_days: object = None,
) -> dict[str, str]:
    normalized_language = normalize_outreach_template_language(language)
    normalized_bucket = normalize_outreach_followup_bucket(bucket)
    try:
        normalized_sequence = int(sequence_number or 2)
    except (TypeError, ValueError):
        normalized_sequence = 2
    try:
        normalized_trial_days = int(trial_days or 0)
    except (TypeError, ValueError):
        normalized_trial_days = 0
    if normalized_bucket == "sent_no_visit":
        key = "SENT_NO_VISIT_SEQ3_EN" if normalized_sequence >= 3 else "SENT_NO_VISIT_SEQ2_EN"
    elif normalized_bucket == "visited_once":
        key = "VISITED_ONCE_EN"
    elif normalized_bucket == "watched_no_convert":
        key = "WATCHED_NO_CONVERT_2MO_EN" if normalized_trial_days >= 60 else "WATCHED_NO_CONVERT_EN"
    elif normalized_bucket == "hot_repeat":
        key = "HOT_REPEAT_EN"
    else:
        key = "SENT_NO_VISIT_SEQ3_EN" if normalized_sequence >= 3 else "SENT_NO_VISIT_SEQ2_EN"
    template = OUTREACH_BUCKET_FOLLOWUP_TEMPLATES[key]
    safe_name = outreach_greeting_name(recipient_name, language=normalized_language)
    text = template.text.replace("[Name]", safe_name).replace("[link]", str(share_url or "").strip())
    return {
        "key": template.key,
        "stage": "followup",
        "language": normalized_language,
        "bucket": normalized_bucket,
        "subject": template.subject,
        "text": text,
        "html": _simple_html_email(subject=template.subject, body_text=text),
    }


def render_outreach_clipboard_text(
    *,
    stage: object,
    language: object,
    recipient_name: object,
    share_url: str,
    trial_days: object,
    video_title: object = "",
) -> str:
    rendered = render_outreach_email(
        stage=stage,
        language=language,
        recipient_name=recipient_name,
        share_url=share_url,
        trial_days=trial_days,
        video_title=video_title,
    )
    return f"Subject: {rendered['subject']}\n\n{rendered['text']}"
