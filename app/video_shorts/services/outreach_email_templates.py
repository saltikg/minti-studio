from __future__ import annotations

import html
from dataclasses import dataclass
from typing import Literal

from app.video_shorts.services.trial_copy import trial_duration_text


OutreachLanguage = Literal["EN", "TR"]
OutreachStage = Literal["first", "followup"]


@dataclass(frozen=True)
class OutreachEmailTemplate:
    key: str
    subject: str
    text: str


OUTREACH_EMAIL_TEMPLATES: dict[str, OutreachEmailTemplate] = {
    "FIRST_EN": OutreachEmailTemplate(
        key="FIRST_EN",
        subject="I made a Short from your video — first month free",
        text="""Hi [Name],

I'm the founder of Minti Studio, based in San Francisco. Instead of explaining what we do, I thought I'd show you.

I turned one of your recent videos into a Short - here's how it came out:

[link]

Here's the idea: you keep making your long videos, and we grow your channel with Shorts - without you lifting a finger. We're not a clip tool you have to learn. Connect your channel once, and we handle everything - finding the best moments, captioning, and publishing to YouTube, Instagram, and Facebook, every month.

Your first month is on us - 15 Shorts, made and published for you, free.

Take a look at your Short, and tap "Watch all Shorts" to start. Or reply with any questions.

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

[link]

Buradaki örnekte uzun videonuzun içinden kısa videoya uygun bir bölüm Minti ile seçilerek altyazılı, dikey bir videoya dönüştürüldü.

Minti klip üretmekle kalmıyor - YouTube, Instagram ve Facebook'a yayınlıyor; yorumları yönetiyor ve hepsinin performansını tek yerden karşılaştırıyorsunuz.

Kendi videolarınızla denemek isterseniz, örnek sayfasındaki "Watch all Shorts" butonuna basmanız yeterli. [trial] ücretsiz erişim otomatik olarak tanımlanıyor.

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

Did you get the Short we made from your video? [link]

Here's the thing - even though our tool makes it easy, I know that for a lot of creators, making Shorts is still one more task on top of an already busy schedule.

So here's the simpler version: you keep making your long videos, and we grow your channel with Shorts - without you lifting a finger. We're not a clip tool you have to learn. Connect your channel once, and we handle everything - finding the best moments, captioning, and publishing to your channels, every month.

Your first month is on us - 15 Shorts, made and published for you, free.

Take a look at your Short, and tap "Watch all Shorts" to start.

Best,
Gokhan""",
    ),
    "FOLLOWUP_TR": OutreachEmailTemplate(
        key="FOLLOWUP_TR",
        subject="Videonuzdan yaptığım Short'u gördünüz mü?",
        text="""Merhaba [Name],

Videonuzdan yaptığım Short gözden kaçmış olabilir. İşte burada: [link]

Devam etmek isterseniz, kendi Short'larınızı aynı şekilde oluşturabilirsiniz - o sayfada "Watch all Shorts" butonuna dokunmanız yeterli. [trial] ücretsiz, kayıt yok.

İyi çalışmalar,
Gokhan""",
    ),
}


def normalize_outreach_template_language(value: object) -> OutreachLanguage:
    return "TR" if str(value or "").strip().upper() == "TR" else "EN"


def normalize_outreach_template_stage(value: object) -> OutreachStage:
    return "followup" if str(value or "").strip().lower() == "followup" else "first"


def outreach_template_key(*, stage: object, language: object) -> str:
    normalized_stage = normalize_outreach_template_stage(stage)
    normalized_language = normalize_outreach_template_language(language)
    return f"{normalized_stage.upper()}_{normalized_language}"


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
    stage: object,
    language: object,
    recipient_name: object,
    share_url: str,
    trial_days: object,
) -> dict[str, str]:
    normalized_stage = normalize_outreach_template_stage(stage)
    normalized_language = normalize_outreach_template_language(language)
    key = outreach_template_key(stage=normalized_stage, language=normalized_language)
    template = OUTREACH_EMAIL_TEMPLATES.get(key) or OUTREACH_EMAIL_TEMPLATES["FIRST_EN"]
    safe_name = str(recipient_name or "").strip() or ("there" if normalized_language == "EN" else "Merhaba")
    trial_phrase = trial_duration_text(trial_days, normalized_language)
    text = (
        template.text.replace("[Name]", safe_name)
        .replace("[link]", str(share_url or "").strip())
        .replace("[trial]", trial_phrase)
    )
    return {
        "key": template.key,
        "stage": normalized_stage,
        "language": normalized_language,
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
) -> str:
    rendered = render_outreach_email(
        stage=stage,
        language=language,
        recipient_name=recipient_name,
        share_url=share_url,
        trial_days=trial_days,
    )
    return f"Subject: {rendered['subject']}\n\n{rendered['text']}"
