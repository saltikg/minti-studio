# app/admin/youtube_sync.py
import os
import time
from datetime import datetime
import requests
from youtube_transcript_api import YouTubeTranscriptApi
import re
from app.db import connect_ro, connect_rw

YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")


def _get_db(read_only=True):
    return connect_ro() if read_only else connect_rw()

def _parse_duration_iso8601(s: str) -> int:
    """
    PT1H2M3S gibi süreleri saniyeye çevirir.
    """
    if not s:
        return 0
    # PT1H2M3S, PT15M, PT45S gibi formatlar
    pattern = r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?"
    m = re.match(pattern, s)
    if not m:
        return 0
    h = int(m.group(1) or 0)
    m_ = int(m.group(2) or 0)
    s_ = int(m.group(3) or 0)
    return h * 3600 + m_ * 60 + s_


def _fetch_video_stats(video_ids: list[str]) -> dict[str, dict]:
    """
    videos.list ile contentDetails + statistics çeker.
    Dönüş: { videoId: {duration_seconds, is_short, view_count, like_count, comment_count} }
    """
    if not video_ids or not YOUTUBE_API_KEY:
        return {}

    stats_map: dict[str, dict] = {}
    # YouTube API en fazla 50 id alıyor
    chunk_size = 50
    for i in range(0, len(video_ids), chunk_size):
        chunk = video_ids[i:i + chunk_size]
        ids_param = ",".join(chunk)
        url = (
            "https://www.googleapis.com/youtube/v3/videos"
            f"?part=contentDetails,statistics&id={ids_param}&key={YOUTUBE_API_KEY}"
        )
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        items = data.get("items") or []

        for item in items:
            vid = item.get("id")
            if not vid:
                continue
            content_details = item.get("contentDetails", {}) or {}
            statistics = item.get("statistics", {}) or {}

            dur_iso = content_details.get("duration") or ""
            duration_seconds = _parse_duration_iso8601(dur_iso)

            # Basit short tanımı: 60 saniye ve altı
            is_short = bool(duration_seconds and duration_seconds <= 60)

            def _to_int(val):
                try:
                    return int(val)
                except Exception:
                    return None

            stats_map[vid] = {
                "duration_seconds": duration_seconds or None,
                "is_short": is_short,
                "view_count": _to_int(statistics.get("viewCount")),
                "like_count": _to_int(statistics.get("likeCount")),
                "comment_count": _to_int(statistics.get("commentCount")),
            }

    return stats_map



def _get_active_channels():
    con = _get_db(True)
    rows = con.execute(
        """
        SELECT id, channel_handle, channel_url, channel_title
        FROM youtube_channels
        WHERE is_active = TRUE
        """
    ).fetchall()
    cols = [d[0] for d in con.description]
    con.commit()
    con.close()
    return [dict(zip(cols, r)) for r in rows]


def _resolve_channel_id_from_url(channel_url: str) -> str | None:
    """
    Eğer url /channel/ID formatındaysa direkt o ID yi kullan.
    Handle ise (https://www.youtube.com/@Zhirelle) Data API yi kullanacağız.
    """
    if not channel_url:
        return None

    if "/channel/" in channel_url:
        # örn: https://www.youtube.com/channel/UCxxxxx
        return channel_url.split("/channel/")[-1].strip("/")

    return None  # handle için ayrı fonksiyonda çözümleyeceğiz


def _resolve_channel_id_from_handle(handle: str) -> str | None:
    if not handle or not YOUTUBE_API_KEY:
        return None

    # handle "@Zhirelle" gibi, başındaki @ işaretini silelim
    clean = handle.strip()
    if clean.startswith("@"):
        clean = clean[1:]

    url = (
        "https://www.googleapis.com/youtube/v3/channels"
        f"?part=id,snippet&forHandle={clean}&key={YOUTUBE_API_KEY}"
    )
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    items = data.get("items") or []
    if not items:
        return None
    return items[0]["id"]


def _fetch_recent_videos_for_channel(
    channel_row_id: int,
    handle: str,
    url: str,
    max_results: int = 5,
):
    """
    YouTube Data API ile son videoları çekip youtube_videos tablosuna yazar.
    Artık duration, view sayısı gibi istatistikleri de saklıyoruz.

    Ek kural:
      - Eğer video 60 saniyeden kısa ise VE
      - Yorum sayısı 10 dan az ise
      bu videoyu almıyoruz (skip).
    """
    if not YOUTUBE_API_KEY:
        print("YOUTUBE_API_KEY is missing, cannot fetch videos")
        return 0

    yt_channel_id = _resolve_channel_id_from_url(url)
    if not yt_channel_id:
        yt_channel_id = _resolve_channel_id_from_handle(handle)

    if not yt_channel_id:
        print(f"Could not resolve YouTube channel id for {handle} ({url})")
        return 0

    search_url = (
        "https://www.googleapis.com/youtube/v3/search"
        f"?part=snippet&channelId={yt_channel_id}"
        "&order=date&type=video"
        f"&maxResults={max_results}"
        f"&key={YOUTUBE_API_KEY}"
    )

    resp = requests.get(search_url, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    items = data.get("items") or []

    con = _get_db(False)
    videos_to_insert = []

    for item in items:
        vid = item["id"]["videoId"]
        snippet = item.get("snippet", {}) or {}
        title = snippet.get("title", "")
        published_at = snippet.get("publishedAt")
        video_url = f"https://www.youtube.com/watch?v={vid}"

        # DB de var mı kontrol et
        exists = con.execute(
            "SELECT 1 FROM youtube_videos WHERE video_id = ?",
            [vid],
        ).fetchone()
        if exists:
            continue

        dt = None
        if published_at:
            try:
                dt = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
            except Exception:
                dt = None

        videos_to_insert.append(
            {
                "video_id": vid,
                "title": title,
                "published_at": dt,
                "video_url": video_url,
            }
        )

    if not videos_to_insert:
        con.close()
        return 0

    # Bu yeni videolar için istatistikleri çek
    stats_map = _fetch_video_stats([v["video_id"] for v in videos_to_insert])

    inserted = 0
    for v in videos_to_insert:
        vid = v["video_id"]
        title = v["title"]
        stats = stats_map.get(vid, {})

        duration = stats.get("duration_seconds")
        comment_count = stats.get("comment_count") or 0

        # Yeni kural:
        # Videoyu sadece şu durumda al:
        #  - Süre en az 60 saniye
        #  - Yorum sayısı 10 dan fazla
        #
        # Bunlardan biri bile sağlanmıyorsa videoyu atla.
        if duration is None or duration < 60 or comment_count <= 10:
            print(f"  - skipped video {vid}: {title} (duration={duration}, comments={comment_count})")
            continue

        con.execute(
            """
            INSERT INTO youtube_videos
              (channel_id, video_id, video_title, video_url,
               published_at,
               has_captions, caption_lang, last_checked_at,
               duration_seconds, is_short,
               view_count, like_count, comment_count,
               stats_fetched_at)
            VALUES (?, ?, ?, ?, ?, FALSE, NULL, CURRENT_TIMESTAMP,
                    ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            [
                channel_row_id,
                vid,
                title,
                v["video_url"],
                v["published_at"],
                stats.get("duration_seconds"),
                stats.get("is_short"),
                stats.get("view_count"),
                stats.get("like_count"),
                stats.get("comment_count"),
            ],
        )
        print(f"  + inserted video {vid}: {title}")
        inserted += 1
    con.commit()
    con.close()
    return inserted



def _fetch_recent_videos_via_search(
    channel_row_id: int,
    yt_channel_id: str,
    max_results: int = 5,
):
    """
    Fallback method: original search API based fetch.
    Used only if uploads playlist cannot be resolved.
    """
    api_url = (
        "https://www.googleapis.com/youtube/v3/search"
        f"?part=snippet&channelId={yt_channel_id}"
        "&order=date&type=video"
        f"&maxResults={max_results}"
        f"&key={YOUTUBE_API_KEY}"
    )

    resp = requests.get(api_url, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    items = data.get("items") or []

    con = _get_db(False)
    inserted = 0

    for item in items:
        vid = item["id"]["videoId"]
        snippet = item.get("snippet", {})
        title = snippet.get("title", "")
        published_at = snippet.get("publishedAt")
        video_url = f"https://www.youtube.com/watch?v={vid}"

        exists = con.execute(
            "SELECT 1 FROM youtube_videos WHERE video_id = ?",
            [vid],
        ).fetchone()
        if exists:
            continue

        dt = None
        if published_at:
            try:
                dt = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
            except Exception:
                dt = None

        con.execute(
            """
            INSERT INTO youtube_videos
              (channel_id, video_id, video_title, video_url,
               published_at, has_captions, caption_lang, last_checked_at)
            VALUES (?, ?, ?, ?, ?, FALSE, NULL, CURRENT_TIMESTAMP)
            """,
            [channel_row_id, vid, title, video_url, dt],
        )
        inserted += 1
        print(f"  + inserted video via search {vid}: {title}")

    con.close()
    return inserted



def _fetch_caption_for_video(video_db_id: int, video_id: str):
    """
    Yeni youtube-transcript-api versiyonu ile altyazı alma.
    get_transcript yerine YouTubeTranscriptApi().fetch kullanıyoruz.
    """
    con = _get_db(False)

    # caption zaten var mı
    exists = con.execute(
        "SELECT 1 FROM youtube_captions WHERE video_id = ?",
        [video_db_id],
    ).fetchone()
    if exists:
        con.close()
        return False

    try:
        api = YouTubeTranscriptApi()
        fetched = api.fetch(video_id, languages=["en"])
    except Exception as e:
        print(f"Error fetching transcript for {video_id}: {e}")
        con.execute(
            "UPDATE youtube_videos SET last_checked_at = CURRENT_TIMESTAMP WHERE id = ?",
            [video_db_id],
        )
        con.commit()
        con.close()
        return False

    # snippet ler artık dict değil, objeler. .text özelliği var.
    caption_text = " ".join(
        (getattr(snippet, "text", "") or "").strip()
        for snippet in fetched
    ).strip()

    if not caption_text:
        con.execute(
            "UPDATE youtube_videos SET last_checked_at = CURRENT_TIMESTAMP WHERE id = ?",
            [video_db_id],
        )
        con.commit()
        con.close()
        return False

    # caption kaydı ekle
    con.execute(
        """
        INSERT INTO youtube_captions
          (video_id, caption_text, source, lang)
        VALUES (?, ?, 'api', ?)
        """,
        [video_db_id, caption_text, "en"],
    )

    # video yu güncelle
    con.execute(
        """
        UPDATE youtube_videos
        SET has_captions = TRUE,
            caption_lang = ?,
            last_checked_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        ["en", video_db_id],
    )

    con.commit()
    con.close()
    return True



def sync_youtube_metadata_only(max_videos_per_channel: int = 5):
    """
    1) Aktif kanallardan son videolari alir.
    2) Sadece youtube_videos tablosunu doldurur.
    """
    channels = _get_active_channels()
    print(f"Found {len(channels)} active channels")

    total_new_videos = 0
    for ch in channels:
        print(f"\nChannel: {ch['channel_title']} ({ch['channel_handle']})")
        added = _fetch_recent_videos_for_channel(
            channel_row_id=ch["id"],
            handle=ch["channel_handle"],
            url=ch["channel_url"],
            max_results=max_videos_per_channel,
        )
        print(f"  New videos inserted: {added}")
        total_new_videos += added

    print(f"\nDone. New videos: {total_new_videos}")
    return total_new_videos

def _get_uploads_playlist_id(yt_channel_id: str) -> str | None:
    """
    A channel in YouTube has a special "uploads" playlist that
    contains all uploaded videos in reverse chronological order.
    We use that instead of the search API to reliably get
    the latest videos.
    """
    if not YOUTUBE_API_KEY or not yt_channel_id:
        return None

    url = (
        "https://www.googleapis.com/youtube/v3/channels"
        f"?part=contentDetails&id={yt_channel_id}&key={YOUTUBE_API_KEY}"
    )
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    items = data.get("items") or []
    if not items:
        return None

    details = items[0].get("contentDetails", {})
    playlists = details.get("relatedPlaylists", {})
    return playlists.get("uploads")


if __name__ == "__main__":
    print("Using backend: app.db")
    sync_youtube_metadata_only()
