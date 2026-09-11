from __future__ import annotations

import html
from dataclasses import dataclass
from typing import Literal

from app.video_shorts.services.trial_copy import trial_duration_text


OutreachLanguage = Literal["EN", "TR"]


@dataclass(frozen=True)
class OutreachEmailTemplate:
    key: str
    subject: str
    text: str


OUTREACH_EMAIL_TEMPLATES: dict[str, OutreachEmailTemplate] = {
    "A_EN": OutreachEmailTemplate(
        key="A_EN",
        subject="A hands-off way to grow your channel - first month free",
        text="""Hi [Name],

Did you get the Short we made from your video last week? [link]

Here's the thing - even though our tool makes it easy, I know that for a lot of creators, making Shorts is still one more task on top of an already busy schedule.

So here's the simpler version: you keep making your long videos, and we grow your channel with Shorts - without you lifting a finger. We're not a clip tool you have to learn. Connect your channel once, and we handle everything - finding the best moments, captioning, and publishing to your channels, every month.

Your first month is on us - 15 Shorts, made and published for you, free.

Take a look at your Short, and tap "Watch all Shorts" to start.

Best,
Gokhan""",
    ),
    "A_TR": OutreachEmailTemplate(
        key="A_TR",
        subject="Short'unuz denemeye hazır",
        text="""Merhaba [Name],

Minti'nin videonuzdan ne çıkardığını gördünüz. Devam etmek isterseniz, kendi Short'larınızı aynı şekilde oluşturabilirsiniz - doğrudan o sayfadan.

Sizin gibi içerik üreticileri için tasarlandı: uzun videolarınızı Short'a dönüştürün, YouTube, Instagram ve Facebook'ta yayınlayın, ekstra düzenleme zamanı harcamadan kanalınızı büyütün.

Başlamak için "Watch all Shorts" butonuna dokunun - [trial] ücretsiz, kayıt yok.

İyi çalışmalar,
Gokhan""",
    ),
    "B_EN": OutreachEmailTemplate(
        key="B_EN",
        subject="A hands-off way to grow your channel - first month free",
        text="""Hi [Name],

Did you get the Short we made from your video last week? [link]

Here's the thing - even though our tool makes it easy, I know that for a lot of creators, making Shorts is still one more task on top of an already busy schedule.

So here's the simpler version: you keep making your long videos, and we grow your channel with Shorts - without you lifting a finger. We're not a clip tool you have to learn. Connect your channel once, and we handle everything - finding the best moments, captioning, and publishing to your channels, every month.

Your first month is on us - 15 Shorts, made and published for you, free.

Take a look at your Short, and tap "Watch all Shorts" to start.

Best,
Gokhan""",
    ),
    "B_TR": OutreachEmailTemplate(
        key="B_TR",
        subject="Videonuzdan yaptığım Short'u gördünüz mü?",
        text="""Merhaba [Name],

Geçen hafta videonuzdan yaptığım bir Short göndermiştim - gözden kaçmış olabilir. İşte burada: [link]

Devam etmek isterseniz, kendi Short'larınızı aynı şekilde oluşturabilirsiniz - o sayfada "Watch all Shorts" butonuna dokunmanız yeterli. [trial] ücretsiz, kayıt yok.

İyi çalışmalar,
Gokhan""",
    ),
}


def normalize_outreach_template_language(value: object) -> OutreachLanguage:
    return "TR" if str(value or "").strip().upper() == "TR" else "EN"


def outreach_template_key(*, engaged: bool, language: object) -> str:
    normalized_language = normalize_outreach_template_language(language)
    return f"{'A' if engaged else 'B'}_{normalized_language}"


def _simple_html_email(*, subject: str, body_text: str) -> str:
    paragraphs = []
    for block in body_text.split("\n\n"):
        escaped = html.escape(block).replace("\n", "<br>")
        paragraphs.append(f'<p style="margin:0 0 16px;">{escaped}</p>')
    rendered_body = "\n".join(paragraphs)
    return f"""
    <div style="margin:0;padding:0;background:#f6f8fb;">
      <div style="max-width:600px;margin:0 auto;padding:28px 16px;font-family:Arial,sans-serif;color:#101828;line-height:1.58;">
        <div style="background:#ffffff;border:1px solid #e5e7eb;border-radius:18px;padding:26px 24px;">
          <div style="font-size:20px;font-weight:800;margin:0 0 18px;">{html.escape(subject)}</div>
          {rendered_body}
        </div>
      </div>
    </div>
    """.strip()


def render_outreach_email(
    *,
    engaged: bool,
    language: object,
    recipient_name: object,
    share_url: str,
    trial_days: object,
) -> dict[str, str]:
    normalized_language = normalize_outreach_template_language(language)
    key = outreach_template_key(engaged=engaged, language=normalized_language)
    template = OUTREACH_EMAIL_TEMPLATES.get(key) or OUTREACH_EMAIL_TEMPLATES["B_EN"]
    safe_name = str(recipient_name or "").strip() or ("there" if normalized_language == "EN" else "Merhaba")
    trial_phrase = trial_duration_text(trial_days, normalized_language)
    text = (
        template.text.replace("[Name]", safe_name)
        .replace("[link]", str(share_url or "").strip())
        .replace("[trial]", trial_phrase)
    )
    return {
        "key": template.key,
        "subject": template.subject,
        "text": text,
        "html": _simple_html_email(subject=template.subject, body_text=text),
    }


def render_outreach_clipboard_text(
    *,
    engaged: bool,
    language: object,
    recipient_name: object,
    share_url: str,
    trial_days: object,
) -> str:
    rendered = render_outreach_email(
        engaged=engaged,
        language=language,
        recipient_name=recipient_name,
        share_url=share_url,
        trial_days=trial_days,
    )
    return f"Subject: {rendered['subject']}\n\n{rendered['text']}"
