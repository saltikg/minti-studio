import json
import os
from datetime import date
from openai import OpenAI

from .youtube_trend_agent import run_youtube_trend_agent  # NEW
from app.db import connect_ro, connect_rw

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


def _get_db(read_only=True):
    return connect_ro() if read_only else connect_rw()


def _fetch_captions_with_no_ideas(limit_per_channel: int = 2):
    """
    Her aktif kanal için son N videosunun caption ını çek.
    Daha önce youtube_trend_ideas tablosuna düşmüş videoları atlar.
    """
    con = _get_db(True)
    rows = con.execute(
        """
        WITH ranked AS (
          SELECT
            c.id AS channel_id,
            c.channel_title,
            v.id AS video_row_id,
            v.video_title,
            v.video_url,
            v.published_at,
            cap.caption_text,
            ROW_NUMBER() OVER (
              PARTITION BY c.id
              ORDER BY v.published_at DESC
            ) AS rn
          FROM youtube_channels c
          JOIN youtube_videos v
            ON v.channel_id = c.id
          JOIN youtube_captions cap
            ON cap.video_id = v.id
          LEFT JOIN youtube_trend_ideas t
            ON t.video_id = v.id
          WHERE c.is_active
            AND cap.caption_text IS NOT NULL
            AND t.video_id IS NULL
        )
        SELECT
          channel_id,
          channel_title,
          video_row_id,
          video_title,
          video_url,
          published_at,
          caption_text
        FROM ranked
        WHERE rn <= ?
        ORDER BY published_at DESC
        """,
        [limit_per_channel],
    ).fetchall()
    cols = [d[0] for d in con.description]
    con.close()
    return [dict(zip(cols, r)) for r in rows]


def _extract_trend_ideas_from_caption(caption_text: str):
    """
    Eski yöntem - Caption metninden doğrudan OpenAI ile trend fikirleri çıkarır.
    Dönüş: [{"idea_text": "...", "reason": "..."}]
    Bu fonksiyonu agent başarısız olursa fallback olarak kullanıyoruz.
    """
    if not caption_text:
        return []

    # Token patlamasın diye caption ı kısaltalım
    text = caption_text.strip()
    if len(text) > 8000:
        text = text[:8000]

    system_msg = (
        "You are a shopping trend analyst. "
        "You read YouTube video transcripts and extract product or shopping related content ideas. "
        "Your job is to find ideas that could inspire an ecommerce or affiliate blog post."
    )

    user_msg = f"""
Transcript:
{text}

Task:
1. If there are no clear product or shopping related angles, respond with an empty JSON list: [].
2. Otherwise, output up to 3 ideas in JSON.

Return ONLY valid JSON, with this exact structure:
[
  {{
    "idea_text": "short, concrete content idea focusing on shopping",
    "reason": "why this idea is shoppable"
  }}
]
"""

    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_msg},
        ],
        temperature=0.3,
    )

    content = resp.choices[0].message.content
    try:
        data = json.loads(content)
    except Exception:
        return []

    if not isinstance(data, list):
        return []

    ideas = []
    for item in data:
        idea_text = (item.get("idea_text") or "").strip()
        if not idea_text:
            continue
        ideas.append(
            {
                "idea_text": idea_text,
                "reason": (item.get("reason") or "").strip(),
            }
        )
    return ideas


def _extract_trend_ideas_with_agent(video_meta: dict, caption_text: str):
    """
    Yeni yöntem - LangChain YouTube Trend Agent kullanır.
    Dönüş formatı yine eski fonksiyon ile aynı:
    [{"idea_text": "...", "reason": "..."}]
    Böylece research_youtube_trends içinde ek bir değişiklik gerekmez.
    """
    try:
        raw = run_youtube_trend_agent(video_meta=video_meta, caption_text=caption_text)
    except Exception:
        # Agent hata verirse hiçbir şey bozmasın, boş liste dönsün
        return []

    if not isinstance(raw, list):
        return []

    ideas = []
    for item in raw:
        idea_text = (item.get("idea_text") or "").strip()
        if not idea_text:
            continue
        ideas.append(
            {
                "idea_text": idea_text,
                "reason": (item.get("reason") or "").strip(),
            }
        )
    return ideas


def research_youtube_trends(limit_per_channel: int = 2) -> int:
    """
    Aktif kanalların son videolarının caption larından trend fikri çıkarır.

    Akış:
    1) _fetch_captions_with_no_ideas ile işlenecek videoları bul
    2) Önce YouTube Trend Agent ile fikir üretmeye çalış
    3) Agent hiç fikir üretmezse eski _extract_trend_ideas_from_caption ile dene
    4) Çıkan fikirleri youtube_trend_ideas tablosuna 'pending' olarak yaz

    Dönüş: eklenen toplam idea sayısı.
    """
    videos = _fetch_captions_with_no_ideas(limit_per_channel=limit_per_channel)
    if not videos:
        return 0

    con = _get_db(False)
    inserted = 0

    for v in videos:
        caption_text = v["caption_text"]

        video_meta = {
            "channel_title": v["channel_title"],
            "video_title": v["video_title"],
            "video_url": v["video_url"],
            "published_at": v["published_at"],
        }

        # 1 - Agent ile dene
        ideas = _extract_trend_ideas_with_agent(video_meta, caption_text)

        # 2 - Agent hiç fikir üretmezse eski metoda düş
        if not ideas:
            ideas = _extract_trend_ideas_from_caption(caption_text)

        if not ideas:
            # Bu video için fikir çıkmadı
            continue

        for idea in ideas:
            con.execute(
                """
                INSERT INTO youtube_trend_ideas
                  (idea_date, channel_id, video_id,
                   channel_title, video_title, video_url,
                   idea_text, status, notes)
                VALUES
                  (?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                [
                    date.today(),
                    v["channel_id"],
                    v["video_row_id"],
                    v["channel_title"],
                    v["video_title"],
                    v["video_url"],
                    idea["idea_text"],
                    idea["reason"],
                ],
            )
            inserted += 1

    con.commit()
    con.close()
    return inserted
