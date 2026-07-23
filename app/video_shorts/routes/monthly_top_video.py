from __future__ import annotations

import json
import re
import secrets
import subprocess
import tempfile
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Mapping, Optional

from flask import current_app, flash, g, jsonify, render_template, request, url_for
from zoneinfo import ZoneInfo

from app.video_shorts import video_shorts_bp
from app.video_shorts.config import FFMPEG_RENDER_TIMEOUT, FFPROBE_TIMEOUT, SHORTS_DIR, YT_DLP_COOKIES
from app.video_shorts.services.facebook_queue import enqueue_facebook_clip
from app.video_shorts.services.brands import current_brand_id
from app.video_shorts.services.instagram_queue import enqueue_instagram_clip
from app.video_shorts.services.db import get_db_readonly, table_columns
from app.video_shorts.services.media_utils import (
    _cleanup_resolved_source_video,
    _find_source_video,
    _resolve_ffmpeg,
    _resolve_source_video,
    run_media_subprocess,
    scale_media_timeout,
)
from app.video_shorts.services.storage import get_media_storage
from app.video_shorts.services.tiktok_queue import enqueue_tiktok_clip
from app.video_shorts.services.video_metrics import SNAPSHOT_TABLE, ensure_snapshot_table
from app.video_shorts.services.youtube_oauth import has_refresh_token, upload_video_with_refresh_token
from src.trends.facebook_page_tokens import FacebookTokenStoreError, get_facebook_page_data
from src.trends.instagram_tokens import InstagramTokenStoreError, get_instagram_credentials
from src.trends.tiktok_tokens import TikTokTokenStoreError, get_tiktok_data


CHANNEL_OPTIONS = [
    {"value": "all", "label": "All channels"},
    {"value": "youtube", "label": "YouTube"},
    {"value": "instagram", "label": "Instagram"},
    {"value": "facebook", "label": "Facebook"},
    {"value": "tiktok", "label": "TikTok"},
]
TOP_N_OPTIONS = [3, 5, 10, 20]
DEFAULT_TIME_ZONE = "America/Los_Angeles"


def _monthly_output_meta_path(output_path: Path) -> Path:
    return output_path.with_suffix(".meta.json")


def _write_monthly_output_meta(
    output_path: Path,
    month_value: str,
    channel_type: str,
    included_rows: List[Dict[str, object]],
) -> None:
    try:
        brand_id = current_brand_id()
        payload = {
            "month": month_value,
            "channel": channel_type,
            "brand_id": brand_id,
            "video_count": len(included_rows),
            "video_ids": [str(r.get("video_id") or "") for r in included_rows],
            "videos": [
                {
                    "rank": int(r.get("rank") or 0),
                    "video_id": str(r.get("video_id") or ""),
                    "title": str(r.get("video_title") or r.get("video_id") or ""),
                    "content_text": str(
                        r.get("content_text")
                        or r.get("video_content")
                        or r.get("summary")
                        or r.get("video_title")
                        or ""
                    ),
                    "channel_type": str(r.get("channel_type") or ""),
                }
                for r in included_rows
            ],
            "created_at": datetime.utcnow().isoformat() + "Z",
        }
        _monthly_output_meta_path(output_path).write_text(
            json.dumps(payload, ensure_ascii=True, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        current_app.logger.debug("Monthly output meta write failed: %s", exc)


def _extract_month_from_filename(filename: str) -> Optional[str]:
    m = re.match(r"^monthly_top_(\d{4}-\d{2})_", filename or "")
    return m.group(1) if m else None


def _list_generated_monthly_videos(limit: int = 12) -> List[Dict[str, object]]:
    rows: List[Dict[str, object]] = []
    if not SHORTS_DIR.exists():
        return rows
    brand_id = current_brand_id()
    candidates = sorted(
        SHORTS_DIR.glob("monthly_top_*.mp4"),
        key=lambda p: p.stat().st_mtime if p.exists() else 0,
        reverse=True,
    )
    for path in candidates[: max(1, int(limit))]:
        item = {
            "filename": path.name,
            "url": url_for("video_shorts_bp.static", filename=f"shorts/{path.name}"),
            "month": _extract_month_from_filename(path.name) or "-",
            "video_count": None,
            "videos": [],
            "created_at": datetime.utcfromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M UTC"),
        }
        meta_path = _monthly_output_meta_path(path)
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                meta_brand_id = str(meta.get("brand_id") or "").strip() or None
                if brand_id and meta_brand_id != brand_id:
                    continue
                if not brand_id and meta_brand_id:
                    continue
                if meta.get("month"):
                    item["month"] = str(meta.get("month"))
                if meta.get("video_count") is not None:
                    item["video_count"] = int(meta.get("video_count"))
                videos = meta.get("videos")
                if isinstance(videos, list):
                    cleaned: List[Dict[str, object]] = []
                    for v in videos:
                        if not isinstance(v, dict):
                            continue
                        cleaned.append(
                            {
                                "rank": int(v.get("rank") or 0),
                                "video_id": str(v.get("video_id") or ""),
                                "title": str(v.get("title") or ""),
                                "content_text": str(v.get("content_text") or ""),
                                "channel_type": str(v.get("channel_type") or ""),
                            }
                        )
                    item["videos"] = cleaned
                elif isinstance(meta.get("video_ids"), list):
                    item["videos"] = [
                        {
                            "rank": idx + 1,
                            "video_id": str(vid or ""),
                            "title": str(vid or ""),
                            "content_text": str(vid or ""),
                            "channel_type": "",
                        }
                        for idx, vid in enumerate(meta.get("video_ids") or [])
                        if str(vid or "").strip()
                    ]
                if meta.get("created_at"):
                    item["created_at"] = str(meta.get("created_at"))
            except Exception:
                pass
        else:
            # Legacy files without metadata are hidden to avoid cross-brand leakage.
            continue
        rows.append(item)
    return rows


def _parse_to_utc(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            dt = datetime.fromisoformat(raw[:-1] + "+00:00")
        else:
            dt = datetime.fromisoformat(raw)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _is_token_expired(expires_at: Optional[str]) -> bool:
    dt = _parse_to_utc(expires_at)
    if not dt:
        return False
    return dt <= datetime.now(timezone.utc)


def _local_to_utc_rfc3339(local_str: str, tz_name: Optional[str]) -> str:
    dt_local = datetime.fromisoformat(local_str)
    if dt_local.tzinfo is None:
        try:
            zone = ZoneInfo(tz_name or DEFAULT_TIME_ZONE)
        except Exception:
            zone = ZoneInfo("UTC")
        dt_local = dt_local.replace(tzinfo=zone)
    dt_utc = dt_local.astimezone(timezone.utc).replace(microsecond=0)
    return dt_utc.isoformat().replace("+00:00", "Z")


def _is_safe_monthly_output_name(filename: str) -> bool:
    name = (filename or "").strip()
    if not name or "/" in name or "\\" in name:
        return False
    return bool(re.match(r"^monthly_top_[a-z0-9_-]+\.mp4$", name))


def _month_bounds(month_value: Optional[str]) -> tuple[str, date, date]:
    raw = (month_value or "").strip()
    if re.match(r"^\d{4}-\d{2}$", raw):
        year_s, month_s = raw.split("-", 1)
        year = int(year_s)
        month = int(month_s)
        if 1 <= month <= 12:
            start = date(year, month, 1)
            if month == 12:
                next_month = date(year + 1, 1, 1)
            else:
                next_month = date(year, month + 1, 1)
            return raw, start, next_month - timedelta(days=1)
    today = date.today()
    normalized = f"{today.year:04d}-{today.month:02d}"
    start = date(today.year, today.month, 1)
    if today.month == 12:
        next_month = date(today.year + 1, 1, 1)
    else:
        next_month = date(today.year, today.month + 1, 1)
    return normalized, start, next_month - timedelta(days=1)


def _thumbnail_url(channel_type: str, video_id: str) -> Optional[str]:
    if not video_id:
        return None
    ct = (channel_type or "").lower()
    if ct == "youtube":
        return f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"
    if ct == "instagram":
        return f"https://www.instagram.com/p/{video_id}/media/?size=l"
    return None


def _video_url(channel_type: str, video_id: str) -> Optional[str]:
    if not video_id:
        return None
    ct = (channel_type or "").lower()
    if ct == "youtube":
        return f"https://www.youtube.com/shorts/{video_id}"
    if ct == "instagram":
        return f"https://www.instagram.com/p/{video_id}/"
    if ct == "facebook":
        return f"https://www.facebook.com/watch/?v={video_id}"
    if ct == "tiktok":
        return f"https://www.tiktok.com/video/{video_id}"
    return None


def _fetch_monthly_top_videos(
    month_start: date,
    month_end: date,
    channel_type: str,
    top_n: int,
) -> List[Dict[str, object]]:
    filter_sql = ""
    params: List[object] = [month_start.isoformat(), month_end.isoformat()]
    if channel_type != "all":
        filter_sql = " AND channel_type = ?"
        params.append(channel_type)

    conn = get_db_readonly()
    try:
        ensure_snapshot_table(conn)
        if current_brand_id() and "brand_id" in table_columns(conn, SNAPSHOT_TABLE):
            filter_sql += " AND brand_id = ?"
            params.append(current_brand_id())
        params.append(int(top_n))
        rows = conn.execute(
            f"""
            WITH source AS (
                SELECT
                    snapshot_date,
                    channel_type,
                    video_id,
                    channel_name,
                    video_title,
                    COALESCE(views, 0) AS views
                FROM {SNAPSHOT_TABLE}
                WHERE snapshot_date BETWEEN ? AND ?{filter_sql}
            ),
            ranked AS (
                SELECT
                    *,
                    ROW_NUMBER() OVER (
                        PARTITION BY channel_type, video_id
                        ORDER BY snapshot_date ASC
                    ) AS rn_first,
                    ROW_NUMBER() OVER (
                        PARTITION BY channel_type, video_id
                        ORDER BY snapshot_date DESC
                    ) AS rn_last
                FROM source
            ),
            agg AS (
                SELECT
                    channel_type,
                    video_id,
                    MAX(CASE WHEN rn_first = 1 THEN views END) AS first_views,
                    MAX(CASE WHEN rn_last = 1 THEN views END) AS last_views,
                    MAX(CASE WHEN rn_last = 1 THEN channel_name END) AS channel_name,
                    MAX(CASE WHEN rn_last = 1 THEN video_title END) AS video_title,
                    COUNT(*) AS days_covered
                FROM ranked
                GROUP BY channel_type, video_id
            )
            SELECT
                channel_type,
                video_id,
                channel_name,
                video_title,
                COALESCE(first_views, 0) AS first_views,
                COALESCE(last_views, 0) AS last_views,
                GREATEST(COALESCE(last_views, 0) - COALESCE(first_views, 0), 0) AS month_gain,
                days_covered
            FROM agg
            ORDER BY month_gain DESC, last_views DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    finally:
        conn.close()

    results: List[Dict[str, object]] = []
    for idx, row in enumerate(rows, start=1):
        item = {
            "rank": idx,
            "channel_type": row[0],
            "video_id": row[1],
            "channel_name": row[2],
            "video_title": row[3] or row[1],
            "first_views": row[4] or 0,
            "last_views": row[5] or 0,
            "month_gain": row[6] or 0,
            "days_covered": row[7] or 0,
        }
        item["thumbnail_url"] = _thumbnail_url(str(item["channel_type"]), str(item["video_id"]))
        item["video_url"] = _video_url(str(item["channel_type"]), str(item["video_id"]))
        results.append(item)
    return results


def _resolve_short_clip_file(clip_filename: str) -> tuple[Optional[Path], bool]:
    name = str(clip_filename or "").strip()
    if not name:
        return None, False
    local_path = SHORTS_DIR / name
    if local_path.exists():
        return local_path, False
    storage = get_media_storage()
    if getattr(storage, "backend_name", "local") != "s3":
        return None, False
    key = f"shorts/{name}"
    try:
        if storage.exists(key):
            return storage.download_to_temp(key), True
    except Exception:
        current_app.logger.exception("Monthly top clip resolve failed for key=%s", key)
    return None, False


def _build_short_id_to_clip_map() -> Dict[str, str]:
    mapped: Dict[str, str] = {}
    if not SHORTS_DIR.exists():
        return mapped
    brand_id = current_brand_id()
    allowed_source_ids: Optional[set[str]] = None
    if brand_id:
        conn = get_db_readonly()
        try:
            allowed_rows = conn.execute(
                "SELECT video_id FROM youtube_videos WHERE brand_id = ?",
                [brand_id],
            ).fetchall()
            allowed_source_ids = {str(row[0]) for row in allowed_rows if row and row[0]}
        finally:
            conn.close()
    for plan_path in SHORTS_DIR.glob("*_plan.json"):
        if not plan_path.is_file():
            continue
        source_video_id = plan_path.name[: -len("_plan.json")]
        if allowed_source_ids is not None and source_video_id not in allowed_source_ids:
            continue
        try:
            plan_payload = json.loads(plan_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        entries = plan_payload.get("plan") or plan_payload.get("clips") or []
        for entry in entries:
            short_id = str(
                (entry.get("yt_video_id") or entry.get("short_video_id") or "").strip()
            )
            clip_name = str(
                (
                    entry.get("clip_filename")
                    or entry.get("output_filename")
                    or ""
                ).strip()
            )
            if not short_id or not clip_name:
                continue
            existing = mapped.get(short_id)
            if not existing:
                mapped[short_id] = clip_name
                continue
            try:
                if plan_path.stat().st_mtime > (SHORTS_DIR / existing).stat().st_mtime:
                    mapped[short_id] = clip_name
            except Exception:
                mapped[short_id] = clip_name
    return mapped


def _annotate_source_status(rows: List[Dict[str, object]]) -> None:
    short_map = _build_short_id_to_clip_map()
    for row in rows:
        video_id = str(row.get("video_id") or "").strip()
        source_path: Optional[Path] = None
        source_is_temp = False
        clip_filename = short_map.get(video_id)
        if clip_filename:
            source_path, source_is_temp = _resolve_short_clip_file(clip_filename)
        if not source_path:
            source_path, source_is_temp = _resolve_source_video(video_id)
        is_ready = bool(source_path and source_path.exists())
        row["source_available"] = is_ready
        row["source_status_label"] = "Hazir" if is_ready else "Indirilemedi"
        _cleanup_resolved_source_video(source_path, source_is_temp)


def _escape_drawtext(value: str) -> str:
    return (
        (value or "")
        .replace("\n", " ")
        .replace("\r", " ")
        .replace("\\", r"\\")
        .replace(":", r"\:")
        .replace("'", r"\'")
        .replace("%", r"\%")
        .replace(",", r"\,")
        .replace("[", r"\[")
        .replace("]", r"\]")
    )


def _split_overlay_title(title: str, first_len: int = 34, second_len: int = 42) -> tuple[str, str]:
    words = [w for w in (title or "").strip().split() if w]
    if not words:
        return ("", "")

    lines: List[str] = []
    limits = [first_len, second_len]
    current = ""
    limit_idx = 0

    for word in words:
        limit = limits[min(limit_idx, len(limits) - 1)]
        candidate = f"{current} {word}".strip() if current else word
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            lines.append(current)
            limit_idx += 1
            current = word
            if len(lines) >= 2:
                break
        else:
            lines.append(word[: max(1, limit - 1)] + "…")
            limit_idx += 1
            current = ""
            if len(lines) >= 2:
                break

    if len(lines) < 2 and current:
        lines.append(current)

    line1 = lines[0] if lines else ""
    line2 = lines[1] if len(lines) > 1 else ""
    if len(lines) > 2:
        line2 = (line2[: max(1, second_len - 1)] + "…") if line2 else "…"
    return (line1, line2)


def _panel_title_lines(title: str, line_break: int = 18, max_total: int = 36) -> tuple[str, str]:
    words = [w for w in " ".join((title or "").strip().split()).split(" ") if w]
    if not words:
        return ("", "")

    # Build a max_total-length preview without breaking words.
    kept: List[str] = []
    for w in words:
        candidate = (" ".join(kept + [w])).strip()
        if len(candidate) <= max_total:
            kept.append(w)
        else:
            break
    truncated = len(kept) < len(words)
    if not kept:
        kept = [words[0][:max_total]]
        truncated = len(words[0]) > max_total or len(words) > 1

    preview = " ".join(kept).strip()
    if truncated:
        while preview and len(preview) + 3 > max_total:
            parts = preview.split()
            if len(parts) <= 1:
                preview = preview[: max(1, max_total - 3)].rstrip()
                break
            preview = " ".join(parts[:-1]).strip()
        preview = (preview or words[0][: max(1, max_total - 3)]).rstrip() + "..."

    # Split to 2 lines without cutting words.
    preview_words = [w for w in preview.split(" ") if w]
    line1_words: List[str] = []
    idx = 0
    while idx < len(preview_words):
        w = preview_words[idx]
        candidate = (" ".join(line1_words + [w])).strip()
        if len(candidate) <= line_break or not line1_words:
            line1_words.append(w)
            idx += 1
            continue
        break

    line1 = " ".join(line1_words).strip()
    line2 = " ".join(preview_words[idx:]).strip()
    return (line1, line2)


def _has_audio_stream(source: Path) -> bool:
    ffmpeg_bin = Path(_resolve_ffmpeg())
    ffprobe_bin = ffmpeg_bin.with_name("ffprobe")
    cmd = [
        str(ffprobe_bin),
        "-v",
        "error",
        "-select_streams",
        "a",
        "-show_entries",
        "stream=index",
        "-of",
        "csv=p=0",
        str(source),
    ]
    try:
        res = run_media_subprocess(
            cmd,
            operation="monthly_has_audio_stream",
            context=f"source={source.name}",
            check=False,
            capture_output=True,
            text=True,
            timeout=FFPROBE_TIMEOUT,
        )
        return bool((res.stdout or "").strip())
    except Exception:
        return False


def _probe_duration_seconds(source: Path) -> float:
    ffmpeg_bin = Path(_resolve_ffmpeg())
    ffprobe_bin = ffmpeg_bin.with_name("ffprobe")
    cmd = [
        str(ffprobe_bin),
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(source),
    ]
    try:
        res = run_media_subprocess(
            cmd,
            operation="monthly_probe_duration",
            context=f"source={source.name}",
            check=False,
            capture_output=True,
            text=True,
            timeout=FFPROBE_TIMEOUT,
        )
        val = float((res.stdout or "").strip() or 0.0)
        return max(0.0, val)
    except Exception:
        return 0.0


def _download_youtube_video_to_shorts(video_id: str) -> Optional[Path]:
    video_id = (video_id or "").strip()
    if not video_id:
        return None
    SHORTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = SHORTS_DIR / f"{video_id}.mp4"
    if out_path.exists():
        return out_path
    try:
        import yt_dlp as youtube_dl
    except Exception:
        return None

    base_opts = {
        "outtmpl": str((SHORTS_DIR / f"{video_id}.%(ext)s")),
        "merge_output_format": "mp4",
        "quiet": True,
        "noprogress": True,
    }
    if YT_DLP_COOKIES:
        cookie_path = Path(YT_DLP_COOKIES)
        if cookie_path.exists():
            base_opts["cookiefile"] = str(cookie_path)
    urls_to_try = [
        f"https://www.youtube.com/shorts/{video_id}",
        f"https://www.youtube.com/watch?v={video_id}",
    ]
    formats_to_try = ["bestvideo*+bestaudio/best", "best"]
    for url in urls_to_try:
        for fmt in formats_to_try:
            opts = dict(base_opts)
            opts["format"] = fmt
            try:
                with youtube_dl.YoutubeDL(opts) as ydl:
                    ydl.download([url])
                if out_path.exists():
                    return out_path
                candidates = sorted(SHORTS_DIR.glob(f"{video_id}.*"))
                if candidates:
                    try:
                        candidates[0].rename(out_path)
                    except Exception:
                        pass
                if out_path.exists():
                    return out_path
            except Exception as exc:
                current_app.logger.debug(
                    "Monthly top auto-download failed for %s (%s, %s): %s",
                    video_id,
                    url,
                    fmt,
                    exc,
                )
                continue
    return out_path if out_path.exists() else None


def _build_monthly_compilation(
    ordered_rows: List[Dict[str, object]],
    month_value: str,
    channel_type: str,
) -> Dict[str, object]:
    ffmpeg_bin = _resolve_ffmpeg()
    short_map = _build_short_id_to_clip_map()

    prepared_segments: List[Path] = []
    included_rows: List[Dict[str, object]] = []
    skipped: List[Dict[str, str]] = []
    work_dir = Path(tempfile.gettempdir()) / f"monthly_top_{secrets.token_hex(6)}"
    work_dir.mkdir(parents=True, exist_ok=True)
    channel_label = (channel_type or "all").strip().upper()
    if channel_label == "ALL":
        header_text = f"{month_value} En Cok Izlenen Videolar"
    else:
        header_text = f"{month_value} En Cok Izlenenler - {channel_label}"
    safe_header = _escape_drawtext(header_text[:110])
    rank_rows_sorted = sorted(
        [r for r in ordered_rows if int(r.get("rank") or 0) > 0],
        key=lambda r: int(r.get("rank") or 0),
    )[:3]
    rank_panel_items: List[tuple[int, str, str]] = []
    for rr in rank_rows_sorted:
        rr_rank = int(rr.get("rank") or 0)
        rr_title = str(rr.get("video_title") or rr.get("video_id") or "").strip()
        line1, line2 = _panel_title_lines(rr_title, line_break=18, max_total=36)
        rank_panel_items.append((rr_rank, _escape_drawtext(line1), _escape_drawtext(line2)))

    try:
        for row in ordered_rows:
            video_id = str(row.get("video_id") or "").strip()
            rank = int(row.get("rank") or 0)
            source_path: Optional[Path] = None
            source_is_temp = False
            clip_filename = short_map.get(video_id)
            if clip_filename:
                source_path, source_is_temp = _resolve_short_clip_file(clip_filename)
            if not source_path:
                source_path, source_is_temp = _resolve_source_video(video_id)
            download_attempted = False
            if (not source_path or not source_path.exists()) and str(row.get("channel_type") or "").lower() == "youtube":
                download_attempted = True
                source_path = _download_youtube_video_to_shorts(video_id)
                source_is_temp = False
            if not source_path or not source_path.exists():
                reason = "source_not_found"
                if download_attempted:
                    reason = "source_not_found (local clip yok, YouTube indirme basarisiz)"
                skipped.append(
                    {
                        "video_id": video_id,
                        "title": str(row.get("video_title") or video_id),
                        "reason": reason,
                    }
                )
                _cleanup_resolved_source_video(source_path, source_is_temp)
                continue

            segment_out = work_dir / f"seg_{rank:02d}_{secrets.token_hex(3)}.mp4"
            rank_list_filters: List[str] = []
            row_h = 52
            rank_box_h = max(156, len(rank_panel_items) * row_h + 24)
            for idx, (rv, title_l1, title_l2) in enumerate(rank_panel_items):
                color = "yellow" if rv == rank else "white"
                y_base = 118 + idx * row_h
                show_title = rv >= rank
                if show_title:
                    rank_list_filters.append(
                        f"drawtext=text='#{rv} - {title_l1}':x=42:y={y_base}:fontsize=22:fontcolor={color}"
                    )
                    if title_l2:
                        rank_list_filters.append(
                            f"drawtext=text='{title_l2}':x=112:y={y_base + 24}:fontsize=22:fontcolor={color}"
                        )
                else:
                    rank_list_filters.append(
                        f"drawtext=text='#{rv}':x=42:y={y_base}:fontsize=22:fontcolor={color}"
                    )
            filter_complex = (
                "[0:v]scale=1280:720:force_original_aspect_ratio=increase,"
                "crop=1280:720,boxblur=20:10[bg];"
                "[0:v]scale=1280:720:force_original_aspect_ratio=decrease[fg];"
                "[bg][fg]overlay=(W-w)/2:(H-h)/2,"
                "drawbox=x=0:y=0:w=iw:h=46:color=black@0.45:t=fill,"
                f"drawtext=text='{safe_header}':x=(w-text_w)/2:y=10:fontsize=28:fontcolor=white,"
                f"drawbox=x=28:y=104:w=340:h={rank_box_h}:color=black@0.50:t=fill,"
                + ",".join(rank_list_filters)
                + "[vout]"
            )
            cmd = [
                ffmpeg_bin,
                "-y",
                "-i",
                str(source_path),
            ]
            has_audio = _has_audio_stream(source_path)
            if not has_audio:
                cmd.extend(
                    [
                        "-f",
                        "lavfi",
                        "-i",
                        "anullsrc=channel_layout=stereo:sample_rate=48000",
                    ]
                )
            cmd.extend(
                [
                "-filter_complex",
                filter_complex,
                "-map",
                "[vout]",
                "-map",
                "0:a:0" if has_audio else "1:a:0",
                "-r",
                "30",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-ar",
                "48000",
                "-ac",
                "2",
                "-shortest",
                str(segment_out),
                ]
            )
            try:
                run_media_subprocess(
                    cmd,
                    operation="build_monthly_compilation_segment",
                    context=f"video_id={video_id} output={segment_out.name}",
                    output_paths=[segment_out],
                    check=True,
                    timeout=FFMPEG_RENDER_TIMEOUT,
                    capture_output=True,
                    text=True,
                )
            except subprocess.CalledProcessError as exc:
                err_line = ""
                try:
                    raw_err = (exc.stderr or "").strip().splitlines()
                    if raw_err:
                        err_line = raw_err[-1].strip()
                except Exception:
                    err_line = ""
                reason = f"ffmpeg_segment_failed ({exc.returncode})"
                if err_line:
                    reason = f"{reason}: {err_line[:180]}"
                skipped.append(
                    {
                        "video_id": video_id,
                        "title": str(row.get("video_title") or video_id),
                        "reason": reason,
                    }
                )
                _cleanup_resolved_source_video(source_path, source_is_temp)
                continue
            except Exception:
                skipped.append(
                    {
                        "video_id": video_id,
                        "title": title,
                        "reason": "ffmpeg_segment_failed",
                    }
                )
                _cleanup_resolved_source_video(source_path, source_is_temp)
                continue
            prepared_segments.append(segment_out)
            included_rows.append(row)
            _cleanup_resolved_source_video(source_path, source_is_temp)

        if not prepared_segments:
            return {
                "ok": False,
                "output_filename": None,
                "output_url": None,
                "created_count": 0,
                "skipped": skipped,
            }

        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        safe_channel = re.sub(r"[^a-z0-9_]+", "_", (channel_type or "all").lower())
        output_filename = f"monthly_top_{month_value}_{safe_channel}_{ts}.mp4"
        output_path = SHORTS_DIR / output_filename
        transition_d = 0.40
        cmd_concat: List[str] = [ffmpeg_bin, "-y"]
        for seg in prepared_segments:
            cmd_concat.extend(["-i", str(seg)])

        if len(prepared_segments) == 1:
            cmd_concat.extend(
                [
                    "-map",
                    "0:v:0",
                    "-map",
                    "0:a:0",
                    "-c:v",
                    "libx264",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "128k",
                    "-ar",
                    "48000",
                    "-ac",
                    "2",
                    "-pix_fmt",
                    "yuv420p",
                    "-movflags",
                    "+faststart",
                    str(output_path),
                ]
            )
        else:
            durations = [_probe_duration_seconds(seg) for seg in prepared_segments]
            filters: List[str] = []
            v_label = "0:v"
            a_label = "0:a"
            composed_d = max(0.01, durations[0] if durations and durations[0] > 0 else 6.0)
            for idx in range(1, len(prepared_segments)):
                next_v = f"{idx}:v"
                next_a = f"{idx}:a"
                out_v = f"vxf{idx}"
                out_a = f"axf{idx}"
                offset = max(0.0, composed_d - transition_d)
                filters.append(
                    f"[{v_label}][{next_v}]xfade=transition=fade:duration={transition_d:.2f}:offset={offset:.3f}[{out_v}]"
                )
                filters.append(
                    f"[{a_label}][{next_a}]acrossfade=d={transition_d:.2f}[{out_a}]"
                )
                v_label = out_v
                a_label = out_a
                next_d = durations[idx] if idx < len(durations) and durations[idx] > 0 else 6.0
                composed_d = composed_d + next_d - transition_d

            cmd_concat.extend(
                [
                    "-filter_complex",
                    ";".join(filters),
                    "-map",
                    f"[{v_label}]",
                    "-map",
                    f"[{a_label}]",
                    "-c:v",
                    "libx264",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "128k",
                    "-ar",
                    "48000",
                    "-ac",
                    "2",
                    "-pix_fmt",
                    "yuv420p",
                    "-movflags",
                    "+faststart",
                    str(output_path),
                ]
            )
        try:
            run_media_subprocess(
                cmd_concat,
                operation="build_monthly_compilation_concat",
                context=f"month={month_value} channel={channel_type} output={output_path.name}",
                output_paths=[output_path],
                check=True,
                timeout=scale_media_timeout(
                    FFMPEG_RENDER_TIMEOUT,
                    duration_seconds=sum(d for d in durations if d > 0) if 'durations' in locals() else None,
                    multiplier=2.0,
                    extra_seconds=120,
                ),
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            return {
                "ok": False,
                "output_filename": None,
                "output_url": None,
                "created_count": len(prepared_segments),
                "skipped": skipped,
                "error_reason": f"concat_failed ({exc.returncode})",
            }
        except Exception:
            return {
                "ok": False,
                "output_filename": None,
                "output_url": None,
                "created_count": len(prepared_segments),
                "skipped": skipped,
                "error_reason": "concat_failed",
            }
        _write_monthly_output_meta(
            output_path=output_path,
            month_value=month_value,
            channel_type=channel_type,
            included_rows=included_rows,
        )
        return {
            "ok": output_path.exists(),
            "output_filename": output_filename,
            "output_url": url_for("video_shorts_bp.static", filename=f"shorts/{output_filename}"),
            "created_count": len(prepared_segments),
            "skipped": skipped,
            "error_reason": None,
        }
    finally:
        for seg in prepared_segments:
            try:
                seg.unlink()
            except Exception:
                pass
        try:
            for extra in work_dir.glob("*"):
                if extra.exists():
                    extra.unlink()
            work_dir.rmdir()
        except Exception:
            pass


@video_shorts_bp.route("/workflow/monthly-top-video", methods=["GET", "POST"])
def workflow_monthly_top_video():
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    month_raw, month_start, month_end = _month_bounds(request.values.get("month"))
    channel_type = (request.values.get("channel") or "all").strip().lower()
    if channel_type not in {opt["value"] for opt in CHANNEL_OPTIONS}:
        channel_type = "all"
    try:
        top_n = int(request.values.get("top_n") or 3)
    except Exception:
        top_n = 3
    if top_n not in TOP_N_OPTIONS:
        top_n = 3

    top_rows = _fetch_monthly_top_videos(month_start, month_end, channel_type, top_n)
    _annotate_source_status(top_rows)
    render_order = list(reversed([row for row in top_rows if bool(row.get("source_available"))]))
    for order_idx, row in enumerate(render_order, start=1):
        row["render_order"] = order_idx

    output_video_url = None
    output_video_filename = None
    generation_meta: Optional[Mapping[str, object]] = None

    if request.method == "POST":
        try:
            generation_meta = _build_monthly_compilation(
                ordered_rows=render_order,
                month_value=month_raw,
                channel_type=channel_type,
            )
        except Exception as exc:
            generation_meta = {
                "ok": False,
                "output_filename": None,
                "output_url": None,
                "created_count": 0,
                "skipped": [],
                "error_reason": str(exc),
            }
        if generation_meta.get("ok"):
            output_video_url = str(generation_meta.get("output_url") or "")
            output_video_filename = str(generation_meta.get("output_filename") or "")
            message = "16:9 monthly compilation created successfully."
            flash(message, "success")
            if is_ajax:
                generated_videos = _list_generated_monthly_videos(limit=12)
                return jsonify(
                    {
                        "ok": True,
                        "message": message,
                        "output_video_url": output_video_url,
                        "output_video_filename": output_video_filename,
                        "skipped": generation_meta.get("skipped") or [],
                        "error_reason": None,
                        "generated_videos": generated_videos,
                    }
                )
        else:
            reason = (generation_meta.get("error_reason") or "").strip()
            if reason:
                message = f"Compilation failed: {reason}"
                flash(message, "warning")
            else:
                message = "Compilation could not be created. No local source clip was found for selected videos."
                flash(message, "warning")
            if is_ajax:
                generated_videos = _list_generated_monthly_videos(limit=12)
                return jsonify(
                    {
                        "ok": False,
                        "message": message,
                        "output_video_url": None,
                        "output_video_filename": None,
                        "skipped": generation_meta.get("skipped") or [],
                        "error_reason": generation_meta.get("error_reason"),
                        "generated_videos": generated_videos,
                    }
                ), 400

    generated_videos = _list_generated_monthly_videos(limit=12)
    current_user = getattr(g, "vs_current_user", None) or {}
    user_id = current_user.get("id")
    user_tz = current_user.get("time_zone") or DEFAULT_TIME_ZONE
    youtube_connected = has_refresh_token(user_id)
    instagram_connected = False
    facebook_connected = False
    tiktok_connected = False
    if user_id:
        try:
            instagram_connected = bool(get_instagram_credentials(user_id))
        except InstagramTokenStoreError:
            instagram_connected = False
        try:
            fb_info = get_facebook_page_data(user_id)
            facebook_connected = bool(fb_info and fb_info.get("page_access_token"))
        except FacebookTokenStoreError:
            facebook_connected = False
        try:
            tt_info = get_tiktok_data(user_id)
            tiktok_connected = bool(tt_info and tt_info.get("access_token") and not _is_token_expired(tt_info.get("expires_at")))
        except TikTokTokenStoreError:
            tiktok_connected = False
    return render_template(
        "workflow_monthly_top_video.html",
        month_value=month_raw,
        month_start=month_start,
        month_end=month_end,
        channel_type=channel_type,
        top_n=top_n,
        top_n_options=TOP_N_OPTIONS,
        channel_options=CHANNEL_OPTIONS,
        top_rows=top_rows,
        render_order=render_order,
        output_video_url=output_video_url,
        output_video_filename=output_video_filename,
        generation_meta=generation_meta,
        generated_videos=generated_videos,
        user_time_zone=user_tz,
        youtube_connected=youtube_connected,
        instagram_connected=instagram_connected,
        facebook_connected=facebook_connected,
        tiktok_connected=tiktok_connected,
    )


@video_shorts_bp.route("/workflow/monthly-top-video/delete", methods=["POST"])
def workflow_monthly_top_video_delete():
    payload = request.get_json(silent=True) or {}
    filename = str(payload.get("filename") or "").strip()
    if not _is_safe_monthly_output_name(filename):
        return jsonify({"ok": False, "message": "Invalid filename."}), 400

    target = SHORTS_DIR / filename
    meta = _monthly_output_meta_path(target)
    removed = False
    try:
        if target.exists() and target.is_file():
            target.unlink()
            removed = True
        if meta.exists() and meta.is_file():
            meta.unlink()
            removed = True or removed
    except Exception as exc:
        current_app.logger.warning("Monthly output delete failed for %s: %s", filename, exc)
        return jsonify({"ok": False, "message": "Delete failed."}), 500

    if not removed:
        return jsonify({"ok": False, "message": "File not found."}), 404

    return jsonify(
        {
            "ok": True,
            "message": "Video deleted.",
            "generated_videos": _list_generated_monthly_videos(limit=12),
        }
    )


@video_shorts_bp.route("/workflow/monthly-top-video/publish", methods=["POST"])
def workflow_monthly_top_video_publish():
    current_user = getattr(g, "vs_current_user", None) or {}
    user_id = current_user.get("id")
    user_tz = current_user.get("time_zone") or DEFAULT_TIME_ZONE
    data = request.get_json(silent=True) or {}
    filename = str(data.get("filename") or "").strip()
    if not _is_safe_monthly_output_name(filename):
        return jsonify({"ok": False, "message": "Invalid filename."}), 400
    media_path = SHORTS_DIR / filename
    if not media_path.exists():
        return jsonify({"ok": False, "message": "Video file not found."}), 404

    title = str(data.get("title") or filename.rsplit(".", 1)[0]).strip()[:100]
    description = str(data.get("description") or "").strip()[:5000]
    caption_text = (description or title)[:2200]
    monthly_video_id = filename.rsplit(".", 1)[0]

    def _as_bool(*keys: str) -> bool:
        for key in keys:
            value = data.get(key)
            if isinstance(value, bool):
                if value:
                    return True
                continue
            if value is None:
                continue
            if str(value).strip().lower() in {"1", "true", "yes", "on"}:
                return True
        return False

    youtube_enabled = _as_bool("youtube_enabled")
    instagram_reel = _as_bool("instagram_reel", "schedule_instagram_reel")
    instagram_feed = _as_bool("instagram_feed", "schedule_instagram_feed")
    facebook_reel = _as_bool("facebook_reel", "schedule_facebook_reel")
    facebook_feed = _as_bool("facebook_feed", "schedule_facebook_feed")
    tiktok_enabled = _as_bool("tiktok_enabled", "schedule_tiktok")
    if not any([youtube_enabled, instagram_reel, instagram_feed, facebook_reel, facebook_feed, tiktok_enabled]):
        return jsonify({"ok": False, "message": "Select at least one platform."}), 400

    def _schedule_iso(*field_names: str) -> Optional[str]:
        raw = ""
        for field_name in field_names:
            raw = str(data.get(field_name) or "").strip()
            if raw:
                break
        if not raw:
            return None
        return _local_to_utc_rfc3339(raw, user_tz)

    try:
        yt_publish_at = _schedule_iso("youtube_publish_at", "publish_at")
        ig_publish_at = _schedule_iso("instagram_publish_at")
        fb_publish_at = _schedule_iso("facebook_publish_at")
        tt_publish_at = _schedule_iso("tiktok_publish_at")
    except Exception:
        return jsonify({"ok": False, "message": "Invalid schedule date format."}), 400

    instagram_mode = str(data.get("instagram_mode") or "").strip().lower()
    facebook_mode = str(data.get("facebook_mode") or "").strip().lower()
    tiktok_mode = str(data.get("tiktok_mode") or "").strip().lower()

    if instagram_mode == "sync":
        ig_publish_at = yt_publish_at
    elif instagram_mode == "now":
        ig_publish_at = None
    if facebook_mode == "sync":
        fb_publish_at = yt_publish_at
    elif facebook_mode == "now":
        fb_publish_at = None
    if tiktok_mode == "sync":
        tt_publish_at = yt_publish_at
    elif tiktok_mode == "now":
        tt_publish_at = None

    results: Dict[str, object] = {"youtube": None, "instagram": [], "facebook": [], "tiktok": None}

    if youtube_enabled:
        if not has_refresh_token(user_id):
            return jsonify({"ok": False, "message": "YouTube account not connected."}), 403
        try:
            resp = upload_video_with_refresh_token(
                video_path=str(media_path),
                title=title,
                description=description,
                publish_at=yt_publish_at,
                privacy_status="private",
                user_id=user_id,
            )
            results["youtube"] = {"id": (resp or {}).get("id"), "scheduled_at": yt_publish_at}
        except Exception as exc:
            return jsonify({"ok": False, "message": f"YouTube publish failed: {exc}"}), 500

    if instagram_reel or instagram_feed:
        if not user_id:
            return jsonify({"ok": False, "message": "Login required for Instagram queue."}), 403
        try:
            ig_creds = get_instagram_credentials(user_id)
        except InstagramTokenStoreError:
            ig_creds = None
        if not ig_creds:
            return jsonify({"ok": False, "message": "Instagram account not connected."}), 403
        for media_type in [m for m in ["reel", "feed"] if (instagram_reel and m == "reel") or (instagram_feed and m == "feed")]:
            qid = enqueue_instagram_clip(
                user_id=user_id,
                video_id=monthly_video_id,
                plan_index="",
                clip_filename=filename,
                caption_text=caption_text,
                publish_at_iso=ig_publish_at,
                instagram_business_account_id=ig_creds.get("instagram_business_account_id"),
                instagram_username=ig_creds.get("instagram_username"),
                youtube_video_id=None,
                youtube_short_id=(results.get("youtube") or {}).get("id") if isinstance(results.get("youtube"), dict) else None,
                plan_title=title,
                media_type=media_type,
                force_requeue=False,
            )
            results["instagram"].append({"media_type": media_type, "queue_id": qid})

    if facebook_reel or facebook_feed:
        if not user_id:
            return jsonify({"ok": False, "message": "Login required for Facebook queue."}), 403
        try:
            fb_info = get_facebook_page_data(user_id)
        except FacebookTokenStoreError:
            fb_info = None
        if not fb_info or not fb_info.get("page_access_token"):
            return jsonify({"ok": False, "message": "Facebook account not connected."}), 403
        for media_type in [m for m in ["reel", "feed"] if (facebook_reel and m == "reel") or (facebook_feed and m == "feed")]:
            qid = enqueue_facebook_clip(
                user_id=user_id,
                video_id=monthly_video_id,
                plan_index="",
                clip_filename=filename,
                caption_text=caption_text,
                publish_at_iso=fb_publish_at,
                page_id=fb_info.get("page_id"),
                page_name=fb_info.get("page_name"),
                plan_title=title,
                media_type=media_type,
            )
            results["facebook"].append({"media_type": media_type, "queue_id": qid})

    if tiktok_enabled:
        if not user_id:
            return jsonify({"ok": False, "message": "Login required for TikTok queue."}), 403
        try:
            tt_info = get_tiktok_data(user_id)
        except TikTokTokenStoreError:
            tt_info = None
        if not tt_info or not tt_info.get("access_token") or _is_token_expired(tt_info.get("expires_at")):
            return jsonify({"ok": False, "message": "TikTok account not connected."}), 403
        qid = enqueue_tiktok_clip(
            user_id=user_id,
            video_id=monthly_video_id,
            plan_index="",
            clip_filename=filename,
            caption_text=caption_text,
            publish_at_iso=tt_publish_at,
            tiktok_open_id=tt_info.get("open_id"),
            tiktok_username=tt_info.get("username"),
            plan_title=title,
            force_requeue=False,
        )
        results["tiktok"] = {"queue_id": qid}

    return jsonify({"ok": True, "message": "Publish/queue created.", "results": results})
