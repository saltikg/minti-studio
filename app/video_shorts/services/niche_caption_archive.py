from __future__ import annotations

import json
import hashlib
import logging
import os
import random
import re
import secrets
import shutil
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    import yt_dlp
except Exception:  # pragma: no cover - production dependency
    yt_dlp = None

from app.video_shorts.services.brand_niches import brand_publishing_channel_id
from app.video_shorts.services.db import get_db, get_db_readonly, table_columns
from app.video_shorts.services.niche_outliers import (
    PACIFIC,
    RunQuota,
    _duration_seconds,
    _optional_int,
    _parse_timestamp,
    _youtube_get,
)


ARCHIVE_TABLE = "niche_brand_video_archive"
UPLOADS_TABLE = "niche_brand_channel_uploads"
CAPTION_LOOKBACK_DAYS = 365
MIN_LONG_SECONDS = 180
logger = logging.getLogger(__name__)


class CaptionArchiveError(RuntimeError):
    pass


class CaptionArchiveBlocked(CaptionArchiveError):
    pass


def _inject_proxy_session(proxy_url: str) -> str:
    try:
        scheme, rest = proxy_url.split("://", 1)
        credentials, host = rest.split("@", 1)
        username, password = credentials.split(":", 1)
        return f"{scheme}://{username};sessid.{secrets.token_hex(8)}:{password}@{host}"
    except Exception:
        return proxy_url


def _proxy_options() -> dict[str, Any]:
    proxy_url = str(os.getenv("INGEST_PROXY_URL") or "").strip()
    if not proxy_url:
        raise CaptionArchiveError("INGEST_PROXY_URL is not configured for the caption worker")
    options: dict[str, Any] = {"proxy": _inject_proxy_session(proxy_url)}
    cookies = str(os.getenv("INGEST_PROXY_COOKIES") or "").strip()
    if cookies:
        options["cookiefile"] = cookies
    return options


def _safe_failure(exc: BaseException) -> str:
    text = str(exc or "").casefold()
    if "429" in text or "too many requests" in text:
        return "rate_limited"
    if "sign in to confirm" in text or "not a bot" in text:
        return "bot_check"
    return "caption_request_failed"


def _sleep_between_requests() -> None:
    time.sleep(random.uniform(2.0, 5.0))


def _parse_json3(path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    output = []
    for event in payload.get("events") or []:
        pieces = event.get("segs") or []
        text = "".join(str(piece.get("utf8") or "") for piece in pieces).strip()
        if not text or text == "\n":
            continue
        try:
            start = float(event.get("tStartMs") or 0) / 1000.0
            duration = float(event.get("dDurationMs") or 0) / 1000.0
        except (TypeError, ValueError):
            continue
        if duration > 0:
            output.append({"start": start, "end": start + duration, "text": " ".join(text.split())})
    return output


def _vtt_seconds(value: str) -> float | None:
    match = re.match(r"(?:(\d+):)?(\d+):(\d+(?:\.\d+)?)", value.strip())
    if not match:
        return None
    return int(match.group(1) or 0) * 3600 + int(match.group(2)) * 60 + float(match.group(3))


def _parse_vtt(path: Path) -> list[dict[str, Any]]:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    output: list[dict[str, Any]] = []
    index = 0
    while index < len(lines):
        if "-->" not in lines[index]:
            index += 1
            continue
        left, right = lines[index].split("-->", 1)
        start = _vtt_seconds(left)
        end = _vtt_seconds(right.split()[0])
        index += 1
        text_lines = []
        while index < len(lines) and lines[index].strip():
            text_lines.append(re.sub(r"<[^>]+>", "", lines[index]).strip())
            index += 1
        text = " ".join(part for part in text_lines if part)
        text = re.sub(r"\s+", " ", text).strip()
        if start is not None and end is not None and end > start and text:
            if not output or output[-1]["text"] != text or abs(output[-1]["start"] - start) > 0.2:
                output.append({"start": start, "end": end, "text": text})
    return output


def _subtitle_files(root: Path, video_id: str) -> list[Path]:
    return sorted(
        path for path in root.glob(f"{video_id}.*")
        if path.suffix.casefold() in {".vtt", ".json3"}
    )


def _download_subtitle_track(video_id: str, language: str, *, automatic: bool) -> list[dict[str, Any]]:
    if yt_dlp is None:
        raise CaptionArchiveError("yt-dlp is not installed")
    root = Path(tempfile.mkdtemp(prefix=f"niche_caps_{video_id}_"))
    try:
        last_reason = "caption_request_failed"
        for attempt in range(3):
            if attempt:
                time.sleep(2 ** attempt)
            _sleep_between_requests()
            options = {
                "skip_download": True,
                "quiet": True,
                "no_warnings": True,
                "outtmpl": str(root / f"{video_id}.%(ext)s"),
                "subtitleslangs": [language, f"{language}-.*"],
                "subtitlesformat": "json3/vtt/best",
                "writesubtitles": not automatic,
                "writeautomaticsub": automatic,
                "retries": 0,
                "extractor_retries": 0,
                "fragment_retries": 0,
                "extractor_args": {"youtube": {"player_client": ["mweb", "default"]}},
                "js_runtimes": {"node": {}},
                **_proxy_options(),
            }
            try:
                with yt_dlp.YoutubeDL(options) as downloader:
                    downloader.download([f"https://www.youtube.com/watch?v={video_id}"])
                files = _subtitle_files(root, video_id)
                if files:
                    selected = files[0]
                    return _parse_json3(selected) if selected.suffix.casefold() == ".json3" else _parse_vtt(selected)
                return []
            except Exception as exc:
                last_reason = _safe_failure(exc)
                if last_reason in {"rate_limited", "bot_check"}:
                    if attempt >= 2:
                        raise CaptionArchiveBlocked(last_reason) from None
                    continue
                if attempt >= 2:
                    raise CaptionArchiveError(last_reason) from None
        return []
    finally:
        shutil.rmtree(root, ignore_errors=True)


def fetch_caption_segments(video_id: str, language: str) -> tuple[list[dict[str, Any]], str]:
    manual = _download_subtitle_track(video_id, language, automatic=False)
    if manual:
        return manual, "manual"
    automatic = _download_subtitle_track(video_id, language, automatic=True)
    return automatic, "automatic" if automatic else ""


def _caption_windows(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    windows = []
    current: list[dict[str, Any]] = []
    for segment in segments:
        current.append(segment)
        if float(current[-1]["end"]) - float(current[0]["start"]) >= 30:
            windows.append({
                "start_seconds": round(float(current[0]["start"]), 1),
                "end_seconds": round(float(current[-1]["end"]), 1),
                "text": " ".join(str(item["text"]) for item in current)[:1800],
            })
            current = []
    if current and windows:
        windows[-1]["end_seconds"] = round(float(current[-1]["end"]), 1)
        windows[-1]["text"] = (windows[-1]["text"] + " " + " ".join(
            str(item["text"]) for item in current
        ))[:1800]
    return windows


def _brand_context(brand_id: str) -> dict[str, Any]:
    conn = get_db_readonly()
    try:
        row = conn.execute(
            """
            SELECT CAST(b.owner_user_id AS VARCHAR),b.name,n.id,n.slug,n.language
            FROM shorts_brands b JOIN niches n ON n.id=b.niche_id
            WHERE CAST(b.id AS VARCHAR)=CAST(? AS VARCHAR)
            """,
            [brand_id],
        ).fetchone()
        if not row:
            raise CaptionArchiveError("brand has no niche")
        channel_id = brand_publishing_channel_id(conn, str(row[0]), brand_id)
        if not channel_id:
            raise CaptionArchiveError("brand has no connected YouTube publishing channel")
        return {
            "owner_user_id": str(row[0]), "brand_name": str(row[1] or brand_id),
            "niche_id": int(row[2]), "niche_slug": str(row[3]),
            "language": str(row[4] or "en"), "channel_id": channel_id,
        }
    finally:
        conn.close()


def _list_uploads(channel_id: str, *, max_units: int = 1000) -> tuple[list[dict[str, Any]], int]:
    import requests

    quota = RunQuota(max_units)
    session = requests.Session()
    cutoff = datetime.now(PACIFIC) - timedelta(days=CAPTION_LOOKBACK_DAYS)
    uploads: list[dict[str, Any]] = []
    page_token = ""
    try:
        while True:
            params: dict[str, Any] = {
                "part": "snippet,contentDetails", "playlistId": "UU" + channel_id[2:], "maxResults": 50,
            }
            if page_token:
                params["pageToken"] = page_token
            payload = _youtube_get(session, quota, "playlistItems", "playlistItems.list", params)
            crossed = False
            for item in payload.get("items") or []:
                snippet, details = item.get("snippet") or {}, item.get("contentDetails") or {}
                published = _parse_timestamp(details.get("videoPublishedAt") or snippet.get("publishedAt"))
                if published and published.astimezone(PACIFIC) < cutoff:
                    crossed = True
                    continue
                video_id = str(details.get("videoId") or (snippet.get("resourceId") or {}).get("videoId") or "")
                if video_id and published:
                    uploads.append({
                        "youtube_video_id": video_id,
                        "title": " ".join(str(snippet.get("title") or "").split()),
                        "published_at": published,
                    })
            page_token = str(payload.get("nextPageToken") or "")
            if not page_token or crossed:
                break
        by_id = {item["youtube_video_id"]: item for item in uploads}
        ids = sorted(by_id)
        for offset in range(0, len(ids), 50):
            chunk = ids[offset:offset + 50]
            payload = _youtube_get(session, quota, "videos", "videos.list", {
                "part": "contentDetails", "id": ",".join(chunk), "maxResults": 50,
            })
            for item in payload.get("items") or []:
                video_id = str(item.get("id") or "")
                if video_id in by_id:
                    by_id[video_id]["duration_s"] = _duration_seconds(
                        (item.get("contentDetails") or {}).get("duration")
                    )
        return list(by_id.values()), quota.units_used
    finally:
        session.close()


def _persist_uploads(brand_id: str, channel_id: str, uploads: list[dict[str, Any]]) -> None:
    conn = get_db()
    try:
        if not table_columns(conn, UPLOADS_TABLE):
            raise CaptionArchiveError("weekly ideas migration has not been applied")
        for item in uploads:
            conn.execute(
                f"""
                INSERT INTO {UPLOADS_TABLE}
                (brand_id,youtube_video_id,title,published_at,duration_s,youtube_channel_id,refreshed_at)
                VALUES (?,?,?,?,?,?,CURRENT_TIMESTAMP)
                ON CONFLICT (brand_id,youtube_video_id) DO UPDATE SET
                    title=EXCLUDED.title,published_at=EXCLUDED.published_at,duration_s=EXCLUDED.duration_s,
                    youtube_channel_id=EXCLUDED.youtube_channel_id,refreshed_at=CURRENT_TIMESTAMP
                """,
                [brand_id, item["youtube_video_id"], item["title"], item["published_at"],
                 item.get("duration_s"), channel_id],
            )
        conn.commit()
    finally:
        conn.close()


def _already_archived(brand_id: str) -> set[str]:
    conn = get_db_readonly()
    try:
        if not table_columns(conn, ARCHIVE_TABLE):
            return set()
        return {str(row[0]) for row in conn.execute(
            f"SELECT youtube_video_id FROM {ARCHIVE_TABLE} WHERE CAST(brand_id AS VARCHAR)=CAST(? AS VARCHAR)",
            [brand_id],
        ).fetchall()}
    finally:
        conn.close()


def _whisper_fallback(video_id: str, brand_id: str) -> list[dict[str, Any]]:
    from app.video_shorts.worker import _brand_transcript_tracks, _download_youtube_audio_with_proxy

    audio_path = None
    try:
        audio_path, _proxy_used, _cookies = _download_youtube_audio_with_proxy(
            f"https://www.youtube.com/watch?v={video_id}", video_id,
        )
        _full_text, segments, _language, _full_text_en, _segments_en = _brand_transcript_tracks(
            audio_path, video_id=video_id, brand_id=brand_id,
        )
        return segments or []
    finally:
        if audio_path:
            shutil.rmtree(audio_path.parent, ignore_errors=True)


def _persist_archive_video(
    brand_id: str, video: dict[str, Any], language: str, source: str,
    caption_kind: str, segments: list[dict[str, Any]],
) -> None:
    conn = get_db()
    try:
        conn.execute(
            f"""
            INSERT INTO {ARCHIVE_TABLE}
            (brand_id,youtube_video_id,title,published_at,language,transcript_source,
             segments_json,windows_json,caption_kind,updated_at)
            VALUES (?,?,?,?,?,?,?::jsonb,?::jsonb,?,CURRENT_TIMESTAMP)
            ON CONFLICT (brand_id,youtube_video_id) DO UPDATE SET
                title=EXCLUDED.title,published_at=EXCLUDED.published_at,language=EXCLUDED.language,
                transcript_source=EXCLUDED.transcript_source,segments_json=EXCLUDED.segments_json,
                windows_json=EXCLUDED.windows_json,caption_kind=EXCLUDED.caption_kind,
                embedding_model=NULL,updated_at=CURRENT_TIMESTAMP
            """,
            [brand_id, video["youtube_video_id"], video["title"], video["published_at"], language,
             source, json.dumps(segments, ensure_ascii=False),
             json.dumps(_caption_windows(segments), ensure_ascii=False), caption_kind or None],
        )
        conn.commit()
    finally:
        conn.close()


def backfill_brand_caption_archive(
    brand_id: str, *, max_videos: int | None = None, whisper_budget_usd: float = 10.0,
) -> dict[str, Any]:
    context = _brand_context(brand_id)
    uploads, youtube_units = _list_uploads(context["channel_id"])
    _persist_uploads(brand_id, context["channel_id"], uploads)
    archived = _already_archived(brand_id)
    long_videos = sorted(
        [item for item in uploads if int(item.get("duration_s") or 0) > MIN_LONG_SECONDS],
        key=lambda item: item["published_at"], reverse=True,
    )
    pending = [item for item in long_videos if item["youtube_video_id"] not in archived]
    if max_videos is not None:
        pending = pending[:max(0, int(max_videos))]
    tried = captions = whisper_count = failed = 0
    whisper_spend = 0.0
    results = []
    for video in pending:
        tried += 1
        try:
            segments, caption_kind = fetch_caption_segments(video["youtube_video_id"], context["language"])
            source = "captions"
            if not segments:
                if whisper_spend >= whisper_budget_usd:
                    results.append({"video_id": video["youtube_video_id"], "status": "whisper_budget_exhausted"})
                    failed += 1
                    continue
                segments = _whisper_fallback(video["youtube_video_id"], brand_id)
                source, caption_kind = "whisper", ""
                whisper_count += 1
            if not segments:
                failed += 1
                results.append({"video_id": video["youtube_video_id"], "status": "no_transcript"})
                continue
            _persist_archive_video(brand_id, video, context["language"], source, caption_kind, segments)
            captions += source == "captions"
            results.append({
                "video_id": video["youtube_video_id"], "title": video["title"], "status": source,
                "caption_kind": caption_kind, "segments": len(segments),
            })
        except CaptionArchiveBlocked as exc:
            results.append({"video_id": video["youtube_video_id"], "status": str(exc)})
            return {
                **context, "stopped": str(exc), "tried": tried, "caption_successes": captions,
                "whisper_fallbacks": whisper_count, "whisper_spend_usd": whisper_spend,
                "failed": failed, "youtube_units": youtube_units, "results": results,
            }
        except Exception:
            logger.warning("Caption archive failed brand=%s video=%s", brand_id, video["youtube_video_id"])
            failed += 1
            results.append({"video_id": video["youtube_video_id"], "status": "failed"})
    return {
        **context, "stopped": "", "eligible_long_videos": len(long_videos), "already_archived": len(archived),
        "tried": tried, "caption_successes": captions, "whisper_fallbacks": whisper_count,
        "whisper_spend_usd": whisper_spend, "failed": failed, "youtube_units": youtube_units,
        "results": results,
    }


def enqueue_brand_caption_archive(
    brand_id: str, *, run_key: str, max_videos: int | None = None,
    whisper_budget_usd: float = 10.0,
) -> dict[str, Any]:
    from app.video_shorts.services.render_jobs import (
        JOB_TYPE_NICHE_CAPTION_ARCHIVE,
        enqueue_worker_job,
    )

    conn = get_db_readonly()
    try:
        row = conn.execute(
            "SELECT CAST(owner_user_id AS VARCHAR) FROM shorts_brands WHERE CAST(id AS VARCHAR)=CAST(? AS VARCHAR)",
            [brand_id],
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise CaptionArchiveError("brand not found")
    payload = {
        "brand_id": brand_id,
        "max_videos": max_videos,
        "whisper_budget_usd": whisper_budget_usd,
        "job_origin": "niche_weekly",
    }
    digest = hashlib.sha256(
        f"niche-caption:{brand_id}:{run_key}:{max_videos or 'all'}".encode("utf-8")
    ).hexdigest()
    return enqueue_worker_job(
        user_id=str(row[0]), job_type=JOB_TYPE_NICHE_CAPTION_ARCHIVE,
        payload=payload, input_hash=digest, max_attempts=1, priority=-20,
    )
