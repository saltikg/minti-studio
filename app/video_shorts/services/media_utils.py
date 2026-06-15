import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from app.video_shorts.config import FFMPEG_BIN, FFMPEG_TIMEOUT, S3_BUCKET_NAME, SHORTS_DIR, VIDEOS_DIR
from app.video_shorts.services.storage import get_media_storage


logger = logging.getLogger(__name__)


def _format_time_label(seconds: float):
    try:
        total = float(seconds)
        if total < 0:
            total = 0
        minutes = int(total // 60)
        secs = int(total - minutes * 60)
        ms = int(round((total - int(total)) * 1000))
        return f"{minutes}:{secs:02d}.{ms:03d}"
    except Exception:
        return None


def _find_source_video(video_id: str):
    candidates = [
        VIDEOS_DIR / f"{video_id}.mp4",
        VIDEOS_DIR / f"{video_id}.mov",
        VIDEOS_DIR / f"{video_id}.mkv",
        VIDEOS_DIR / f"{video_id}.mp3",
        VIDEOS_DIR / f"{video_id}.wav",
        VIDEOS_DIR / f"{video_id}.m4a",
        VIDEOS_DIR / f"{video_id}.aac",
        VIDEOS_DIR / f"{video_id}.ogg",
        VIDEOS_DIR / f"{video_id}.flac",
        SHORTS_DIR / f"{video_id}.mp4",
        SHORTS_DIR / f"{video_id}.mov",
        SHORTS_DIR / f"{video_id}.mkv",
        SHORTS_DIR / f"{video_id}.mp3",
        SHORTS_DIR / f"{video_id}.wav",
        SHORTS_DIR / f"{video_id}.m4a",
        SHORTS_DIR / f"{video_id}.aac",
        SHORTS_DIR / f"{video_id}.ogg",
        SHORTS_DIR / f"{video_id}.flac",
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def _resolve_source_video(video_id: str):
    local_path = _find_source_video(video_id)
    if local_path and local_path.exists():
        return local_path, False

    storages = []
    primary_storage = get_media_storage()
    storages.append(primary_storage)
    if getattr(primary_storage, "backend_name", "local") != "s3" and S3_BUCKET_NAME:
        try:
            storages.append(get_media_storage("s3"))
        except Exception:
            logger.exception("source video explicit s3 storage init failed video_id=%s", video_id)

    for suffix in (".mp4", ".mov", ".mkv", ".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"):
        key = f"videos/{video_id}{suffix}"
        for storage in storages:
            if getattr(storage, "backend_name", "local") != "s3":
                continue
            try:
                if storage.exists(key):
                    logger.info("source video resolved from s3 video_id=%s key=%s", video_id, key)
                    return storage.download_to_temp(key), True
            except Exception:
                logger.exception("source video s3 download failed video_id=%s key=%s", video_id, key)
                continue
    return None, False


def _cleanup_resolved_source_video(path: Path | None, is_temp: bool) -> None:
    if not is_temp or not path:
        return
    try:
        path.unlink()
    except Exception:
        pass


def _resolve_ffmpeg():
    candidates = []
    if FFMPEG_BIN:
        candidates.append(FFMPEG_BIN)
    candidates.append("ffmpeg")
    candidates.append("/usr/bin/ffmpeg")
    for cand in candidates:
        cand = cand or ""
        resolved = shutil.which(cand) or cand
        if Path(resolved).is_file():
            return str(Path(resolved))
    raise FileNotFoundError(f"ffmpeg not found (FFMPEG_BIN={FFMPEG_BIN})")


def _extract_audio_segment(src: Path, start: float, end: float) -> Path:
    """Cut a small audio-only snippet for refinement."""
    resolved_ffmpeg = _resolve_ffmpeg()
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
    tmp_path = Path(tmp.name)
    tmp.close()
    duration = max(end - start, 0.3)
    cmd = [
        resolved_ffmpeg,
        "-y",
        "-ss",
        str(max(0.0, start)),
        "-i",
        str(src),
        "-t",
        str(duration),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-b:a",
        "64k",
        str(tmp_path),
    ]
    subprocess.run(cmd, check=True, timeout=FFMPEG_TIMEOUT)
    return tmp_path
