import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Iterable, Optional

from app.video_shorts.config import (
    FFMPEG_BIN,
    FFMPEG_SHORT_TIMEOUT,
    S3_BUCKET_NAME,
    SHORTS_DIR,
    VIDEOS_DIR,
)
from app.video_shorts.services.storage import get_media_storage


logger = logging.getLogger(__name__)
SOURCE_FASTSTART_TIMEOUT_SECONDS = 120
_FASTSTART_COMPATIBLE_SUFFIXES = {".mp4", ".mov", ".m4v"}


class MediaSubprocessTimeoutError(RuntimeError):
    def __init__(
        self,
        *,
        binary: str,
        timeout_seconds: int,
        operation: str = "",
        context: str = "",
    ) -> None:
        self.binary = binary
        self.timeout_seconds = int(timeout_seconds)
        self.operation = str(operation or "").strip()
        self.context = str(context or "").strip()
        details = [f"{binary} timed out after {self.timeout_seconds}s"]
        if self.operation:
            details.append(f"operation={self.operation}")
        if self.context:
            details.append(f"context={self.context}")
        super().__init__("; ".join(details))


def scale_media_timeout(
    base_timeout: int,
    *,
    duration_seconds: Optional[float] = None,
    multiplier: float = 2.0,
    extra_seconds: int = 0,
) -> int:
    timeout = max(1, int(base_timeout))
    if duration_seconds is None:
        return timeout
    try:
        scaled = int(max(1.0, float(duration_seconds)) * float(multiplier)) + int(extra_seconds)
    except (TypeError, ValueError):
        return timeout
    return max(timeout, scaled)


def _cleanup_partial_outputs(paths: Optional[Iterable[os.PathLike | str]]) -> None:
    for raw_path in paths or []:
        if not raw_path:
            continue
        try:
            path = Path(raw_path)
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
        except Exception:
            pass


def run_media_subprocess(
    cmd,
    *,
    timeout: int,
    operation: str,
    context: str = "",
    output_paths: Optional[Iterable[os.PathLike | str]] = None,
    log: Optional[logging.Logger] = None,
    **kwargs,
):
    active_logger = log or logger
    binary = Path(str(cmd[0])).name if cmd else "subprocess"
    try:
        return subprocess.run(cmd, timeout=timeout, **kwargs)
    except subprocess.TimeoutExpired as exc:
        _cleanup_partial_outputs(output_paths)
        active_logger.error(
            "Media subprocess timeout binary=%s timeout=%ss operation=%s context=%s",
            binary,
            timeout,
            operation,
            context or "-",
        )
        raise MediaSubprocessTimeoutError(
            binary=binary,
            timeout_seconds=timeout,
            operation=operation,
            context=context,
        ) from exc


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


def _top_level_atom_offsets(path: Path) -> dict[str, int]:
    offsets: dict[str, int] = {}
    try:
        total_size = int(path.stat().st_size)
    except Exception:
        return offsets
    try:
        with path.open("rb") as handle:
            offset = 0
            while offset + 8 <= total_size:
                header = handle.read(8)
                if len(header) < 8:
                    break
                size = int.from_bytes(header[:4], "big", signed=False)
                atom_type = header[4:8].decode("latin-1", errors="ignore")
                header_size = 8
                if size == 1:
                    extended = handle.read(8)
                    if len(extended) < 8:
                        break
                    size = int.from_bytes(extended, "big", signed=False)
                    header_size = 16
                elif size == 0:
                    size = total_size - offset
                if size < header_size:
                    break
                if atom_type not in offsets:
                    offsets[atom_type] = offset
                offset += size
                if offset >= total_size:
                    break
                handle.seek(offset)
    except Exception:
        return {}
    return offsets


def source_video_needs_faststart(path: Path) -> bool:
    suffix = path.suffix.lower()
    if suffix not in _FASTSTART_COMPATIBLE_SUFFIXES:
        return False
    atoms = _top_level_atom_offsets(path)
    moov_offset = atoms.get("moov")
    mdat_offset = atoms.get("mdat")
    if moov_offset is None or mdat_offset is None:
        return False
    return moov_offset > mdat_offset


def normalize_source_video_for_streaming(path: Path, *, log: logging.Logger | None = None) -> Path:
    active_logger = log or logger
    suffix = path.suffix.lower()
    if suffix not in _FASTSTART_COMPATIBLE_SUFFIXES:
        return path
    try:
        if not source_video_needs_faststart(path):
            active_logger.info("source faststart skip path=%s reason=already_streamable", path)
            return path
    except Exception:
        active_logger.exception("source faststart detection failed path=%s", path)
        return path

    temp_output = path.with_name(f"{path.stem}.faststart.{os.getpid()}{path.suffix}")
    resolved_ffmpeg = _resolve_ffmpeg()
    cmd = [
        resolved_ffmpeg,
        "-y",
        "-i",
        str(path),
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(temp_output),
    ]
    try:
        active_logger.info(
            "source faststart normalize start path=%s timeout=%ss",
            path,
            SOURCE_FASTSTART_TIMEOUT_SECONDS,
        )
        run_media_subprocess(
            cmd,
            operation="faststart_normalize",
            context=f"path={path}",
            output_paths=[temp_output],
            log=active_logger,
            check=True,
            timeout=max(SOURCE_FASTSTART_TIMEOUT_SECONDS, FFMPEG_SHORT_TIMEOUT),
            capture_output=True,
            text=True,
        )
        if not temp_output.exists() or temp_output.stat().st_size <= 0:
            raise RuntimeError("faststart output missing or empty")
        temp_output.replace(path)
        active_logger.info("source faststart normalize success path=%s", path)
        return path
    except Exception as exc:
        active_logger.warning("source faststart normalize fallback path=%s error=%s", path, exc)
        try:
            temp_output.unlink(missing_ok=True)
        except Exception:
            pass
        return path


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
    run_media_subprocess(
        cmd,
        operation="extract_audio_segment",
        context=f"src={src.name} start={start:.3f} end={end:.3f}",
        output_paths=[tmp_path],
        check=True,
        timeout=scale_media_timeout(
            FFMPEG_SHORT_TIMEOUT,
            duration_seconds=duration,
            multiplier=4.0,
            extra_seconds=60,
        ),
    )
    return tmp_path
