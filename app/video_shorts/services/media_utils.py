import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable, Iterable, Optional

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


def run_ffmpeg_subprocess_with_progress(
    cmd,
    *,
    timeout: int,
    operation: str,
    duration_seconds: Optional[float] = None,
    context: str = "",
    output_paths: Optional[Iterable[os.PathLike | str]] = None,
    log: Optional[logging.Logger] = None,
    progress_callback: Optional[Callable[[int], None]] = None,
):
    active_logger = log or logger
    binary = Path(str(cmd[0])).name if cmd else "subprocess"
    process = None
    start_time = time.time()
    last_progress = -1
    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        stdout_lines: list[str] = []
        stderr_lines: list[str] = []
        progress_fields: dict[str, str] = {}
        while True:
            if process.poll() is not None:
                break
            if timeout and (time.time() - start_time) > timeout:
                raise subprocess.TimeoutExpired(cmd, timeout)
            if process.stdout is None:
                time.sleep(0.1)
                continue
            line = process.stdout.readline()
            if line:
                stdout_lines.append(line)
                stripped = line.strip()
                if "=" in stripped:
                    key, value = stripped.split("=", 1)
                    progress_fields[key.strip()] = value.strip()
                    if key.strip() == "progress":
                        out_time_ms_raw = progress_fields.get("out_time_ms") or "0"
                        try:
                            out_time_ms = int(out_time_ms_raw)
                        except (TypeError, ValueError):
                            out_time_ms = 0
                        if duration_seconds and duration_seconds > 0:
                            computed = int(round(max(0.0, min(100.0, (out_time_ms / 1_000_000.0) / float(duration_seconds) * 100.0))))
                            if progress_callback and computed > last_progress:
                                last_progress = computed
                                progress_callback(computed)
                        progress_fields = {}
                continue
            time.sleep(0.1)
        stdout_rest, stderr_rest = process.communicate(timeout=5)
        if stdout_rest:
            stdout_lines.append(stdout_rest)
        if stderr_rest:
            stderr_lines.append(stderr_rest)
        if process.returncode:
            raise subprocess.CalledProcessError(
                process.returncode,
                cmd,
                output="".join(stdout_lines),
                stderr="".join(stderr_lines),
            )
        if progress_callback and last_progress < 100:
            progress_callback(100)
        return subprocess.CompletedProcess(
            cmd,
            process.returncode,
            stdout="".join(stdout_lines),
            stderr="".join(stderr_lines),
        )
    except subprocess.TimeoutExpired as exc:
        if process is not None:
            try:
                process.kill()
            except Exception:
                pass
            try:
                process.communicate(timeout=5)
            except Exception:
                pass
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
        VIDEOS_DIR / f"{video_id}.webm",
        VIDEOS_DIR / f"{video_id}.mp3",
        VIDEOS_DIR / f"{video_id}.wav",
        VIDEOS_DIR / f"{video_id}.m4a",
        VIDEOS_DIR / f"{video_id}.aac",
        VIDEOS_DIR / f"{video_id}.ogg",
        VIDEOS_DIR / f"{video_id}.flac",
        SHORTS_DIR / f"{video_id}.mp4",
        SHORTS_DIR / f"{video_id}.mov",
        SHORTS_DIR / f"{video_id}.mkv",
        SHORTS_DIR / f"{video_id}.webm",
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

    for suffix in (".mp4", ".mov", ".mkv", ".webm", ".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"):
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


def _resolve_ffprobe() -> str:
    ffmpeg_path = Path(_resolve_ffmpeg())
    candidate = ffmpeg_path.with_name("ffprobe")
    if candidate.is_file():
        return str(candidate)
    resolved = shutil.which("ffprobe")
    if resolved:
        return resolved
    fallback = Path("/usr/bin/ffprobe")
    if fallback.is_file():
        return str(fallback)
    raise FileNotFoundError("ffprobe not found")


def _probe_video_stream_info(path: Path, *, log: logging.Logger | None = None) -> dict[str, float | int | None]:
    active_logger = log or logger
    result = run_media_subprocess(
        [
            _resolve_ffprobe(),
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height:format=duration",
            "-of",
            "json",
            str(path),
        ],
        operation="source_probe_video_stream",
        context=f"path={path.name}",
        timeout=max(SOURCE_FASTSTART_TIMEOUT_SECONDS, FFMPEG_SHORT_TIMEOUT),
        capture_output=True,
        text=True,
        check=True,
        log=active_logger,
    )
    payload = json.loads(result.stdout or "{}")
    stream = ((payload.get("streams") or [None])[0]) or {}
    format_info = payload.get("format") or {}
    duration_raw = format_info.get("duration")
    try:
        duration_seconds = float(duration_raw) if duration_raw is not None else None
    except (TypeError, ValueError):
        duration_seconds = None
    width = stream.get("width")
    height = stream.get("height")
    return {
        "width": int(width) if width is not None else None,
        "height": int(height) if height is not None else None,
        "duration_seconds": duration_seconds,
    }


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


def _source_video_remux_reason(path: Path) -> str | None:
    suffix = path.suffix.lower()
    if suffix not in _FASTSTART_COMPATIBLE_SUFFIXES:
        return None
    atoms = _top_level_atom_offsets(path)
    if not atoms:
        return None
    if "moof" in atoms:
        return "fragmented"
    moov_offset = atoms.get("moov")
    mdat_offset = atoms.get("mdat")
    if moov_offset is None:
        return "moov-missing"
    if mdat_offset is None:
        return None
    if moov_offset > mdat_offset:
        return "moov-behind-mdat"
    return None


def source_video_needs_remux(path: Path) -> bool:
    return _source_video_remux_reason(path) is not None


def _resolve_s3_source_video_key(video_id: str, preferred_suffix: str | None = None) -> tuple[object | None, str | None]:
    candidate_suffixes: list[str] = []
    if preferred_suffix:
        candidate_suffixes.append(str(preferred_suffix))
    candidate_suffixes.extend([".mp4", ".mov", ".mkv", ".webm", ".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"])
    seen: set[str] = set()
    suffixes = []
    for suffix in candidate_suffixes:
        normalized = str(suffix or "").strip().lower()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        suffixes.append(normalized)
    storages = []
    primary_storage = get_media_storage()
    if getattr(primary_storage, "backend_name", "local") == "s3":
        storages.append(primary_storage)
    elif S3_BUCKET_NAME:
        try:
            storages.append(get_media_storage("s3"))
        except Exception:
            logger.exception("source video explicit s3 storage init failed video_id=%s", video_id)
            return None, None
    for storage in storages:
        for suffix in suffixes:
            key = f"videos/{video_id}{suffix}"
            try:
                if storage.exists(key):
                    return storage, key
            except Exception:
                logger.exception("source video s3 exists check failed video_id=%s key=%s", video_id, key)
                continue
    return None, None


def remux_s3_source_video_if_needed(
    video_id: str,
    source_key: str,
    local_path: Path,
    *,
    target_key: str | None = None,
    log: logging.Logger | None = None,
) -> str | None:
    active_logger = log or logger
    suffix = local_path.suffix.lower()
    if suffix not in _FASTSTART_COMPATIBLE_SUFFIXES:
        return None
    try:
        reason = _source_video_remux_reason(local_path)
    except Exception:
        active_logger.exception("source remux detection failed video_id=%s path=%s", video_id, local_path)
        return None
    if not reason:
        active_logger.info(
            "normalize skip video_id=%s key=%s path=%s reason=already_streamable",
            video_id,
            source_key,
            local_path,
        )
        return None

    storage, resolved_source_key = _resolve_s3_source_video_key(video_id, suffix)
    if not storage:
        primary_storage = get_media_storage()
        if getattr(primary_storage, "backend_name", "local") == "s3":
            storage = primary_storage
        elif S3_BUCKET_NAME:
            try:
                storage = get_media_storage("s3")
            except Exception:
                logger.exception("source video explicit s3 storage init failed video_id=%s", video_id)
                return None
    upload_key = str(target_key or "").strip() or str(source_key or "").strip() or str(resolved_source_key or "").strip()
    if not storage or not upload_key:
        active_logger.warning(
            "normalize fallback video_id=%s key=%s path=%s reason=%s error=%s",
            video_id,
            source_key,
            local_path,
            reason,
            "s3-key-unresolved",
        )
        return None

    temp_output = local_path.with_name(f"{local_path.stem}.remux.{os.getpid()}{local_path.suffix}")
    resolved_ffmpeg = _resolve_ffmpeg()
    cmd = [
        resolved_ffmpeg,
        "-y",
        "-i",
        str(local_path),
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(temp_output),
    ]
    try:
        active_logger.info(
            "normalize start video_id=%s key=%s target_key=%s path=%s reason=%s timeout=%ss",
            video_id,
            source_key,
            upload_key,
            local_path,
            reason,
            SOURCE_FASTSTART_TIMEOUT_SECONDS,
        )
        run_media_subprocess(
            cmd,
            operation="source_remux_for_streaming",
            context=f"video_id={video_id} key={source_key} target_key={upload_key} reason={reason}",
            output_paths=[temp_output],
            log=active_logger,
            check=True,
            timeout=max(SOURCE_FASTSTART_TIMEOUT_SECONDS, FFMPEG_SHORT_TIMEOUT),
            capture_output=True,
            text=True,
        )
        if not temp_output.exists() or temp_output.stat().st_size <= 0:
            raise RuntimeError("remux output missing or empty")
        storage.put_file(temp_output, upload_key)
        active_logger.info(
            "normalize done video_id=%s key=%s path=%s reason=%s new_key=%s",
            video_id,
            source_key,
            local_path,
            reason,
            upload_key,
        )
        return upload_key
    except Exception as exc:
        active_logger.warning(
            "normalize fallback video_id=%s key=%s target_key=%s path=%s reason=%s error=%s",
            video_id,
            source_key,
            upload_key,
            local_path,
            reason,
            exc,
        )
        try:
            temp_output.unlink(missing_ok=True)
        except Exception:
            pass
        return None
    finally:
        try:
            temp_output.unlink(missing_ok=True)
        except Exception:
            pass


def normalize_s3_source_video_for_upload(
    video_id: str,
    source_key: str,
    local_path: Path,
    *,
    target_key: str | None = None,
    log: logging.Logger | None = None,
    progress_callback: Optional[Callable[[int], None]] = None,
) -> str | None:
    active_logger = log or logger
    suffix = local_path.suffix.lower()
    upload_key = str(target_key or "").strip() or str(source_key or "").strip()
    if not upload_key:
        active_logger.warning(
            "normalize fallback video_id=%s key=%s path=%s reason=missing-upload-key",
            video_id,
            source_key,
            local_path,
        )
        return None
    try:
        probe = _probe_video_stream_info(local_path, log=active_logger)
    except Exception as exc:
        active_logger.warning(
            "normalize probe fallback video_id=%s key=%s path=%s error=%s",
            video_id,
            source_key,
            local_path,
            exc,
        )
        return remux_s3_source_video_if_needed(
            video_id,
            source_key,
            local_path,
            target_key=upload_key,
            log=active_logger,
        )

    width = int(probe.get("width") or 0)
    height = int(probe.get("height") or 0)
    duration_seconds = probe.get("duration_seconds")
    try:
        source_size_bytes = int(local_path.stat().st_size)
    except Exception:
        source_size_bytes = 0
    active_logger.info(
        "normalize probe video_id=%s key=%s path=%s width=%s height=%s size_bytes=%s duration_seconds=%s",
        video_id,
        source_key,
        local_path,
        width or 0,
        height or 0,
        source_size_bytes,
        duration_seconds,
    )

    if height <= 720:
        active_logger.info(
            "normalize transcode skip video_id=%s key=%s path=%s width=%s height=%s reason=height_lte_720",
            video_id,
            source_key,
            local_path,
            width or 0,
            height or 0,
        )
        return remux_s3_source_video_if_needed(
            video_id,
            source_key,
            local_path,
            target_key=upload_key,
            log=active_logger,
        )

    if suffix not in _FASTSTART_COMPATIBLE_SUFFIXES:
        active_logger.warning(
            "normalize transcode fallback video_id=%s key=%s path=%s width=%s height=%s reason=unsupported_suffix",
            video_id,
            source_key,
            local_path,
            width or 0,
            height or 0,
        )
        return remux_s3_source_video_if_needed(
            video_id,
            source_key,
            local_path,
            target_key=upload_key,
            log=active_logger,
        )

    storage, _ = _resolve_s3_source_video_key(video_id, suffix)
    if not storage:
        primary_storage = get_media_storage()
        if getattr(primary_storage, "backend_name", "local") == "s3":
            storage = primary_storage
        elif S3_BUCKET_NAME:
            try:
                storage = get_media_storage("s3")
            except Exception:
                active_logger.exception("source video explicit s3 storage init failed video_id=%s", video_id)
                return remux_s3_source_video_if_needed(
                    video_id,
                    source_key,
                    local_path,
                    target_key=upload_key,
                    log=active_logger,
                )
    if not storage:
        active_logger.warning(
            "normalize transcode fallback video_id=%s key=%s path=%s reason=s3-unavailable",
            video_id,
            source_key,
            local_path,
        )
        return remux_s3_source_video_if_needed(
            video_id,
            source_key,
            local_path,
            target_key=upload_key,
            log=active_logger,
        )

    temp_output = local_path.with_name(f"{local_path.stem}.720p.{os.getpid()}{suffix}")
    transcode_started_at = time.time()
    timeout_seconds = scale_media_timeout(
        max(SOURCE_FASTSTART_TIMEOUT_SECONDS * 4, FFMPEG_SHORT_TIMEOUT),
        duration_seconds=duration_seconds,
        multiplier=4.0,
        extra_seconds=300,
    )
    cmd = [
        _resolve_ffmpeg(),
        "-y",
        "-i",
        str(local_path),
        "-progress",
        "pipe:1",
        "-nostats",
        "-vf",
        "scale=-2:720",
        "-c:v",
        "libx264",
        "-crf",
        "23",
        "-preset",
        "fast",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        str(temp_output),
    ]
    try:
        active_logger.info(
            "normalize transcode start video_id=%s key=%s target_key=%s path=%s width=%s height=%s size_bytes=%s timeout=%ss cmd=%s",
            video_id,
            source_key,
            upload_key,
            local_path,
            width or 0,
            height or 0,
            source_size_bytes,
            timeout_seconds,
            " ".join(cmd),
        )
        run_ffmpeg_subprocess_with_progress(
            cmd,
            operation="source_transcode_to_720p",
            context=f"video_id={video_id} key={source_key} target_key={upload_key}",
            duration_seconds=duration_seconds,
            output_paths=[temp_output],
            log=active_logger,
            timeout=timeout_seconds,
            progress_callback=progress_callback,
        )
        if not temp_output.exists() or temp_output.stat().st_size <= 0:
            raise RuntimeError("720p transcode output missing or empty")
        output_size_bytes = int(temp_output.stat().st_size)
        storage.put_file(temp_output, upload_key)
        active_logger.info(
            "normalize transcode success video_id=%s key=%s target_key=%s output_size_bytes=%s transcode_seconds=%s",
            video_id,
            source_key,
            upload_key,
            output_size_bytes,
            round(time.time() - transcode_started_at, 2),
        )
        return upload_key
    except Exception as exc:
        active_logger.warning(
            "normalize transcode fallback video_id=%s key=%s target_key=%s path=%s width=%s height=%s error=%s",
            video_id,
            source_key,
            upload_key,
            local_path,
            width or 0,
            height or 0,
            exc,
        )
        return remux_s3_source_video_if_needed(
            video_id,
            source_key,
            local_path,
            target_key=upload_key,
            log=active_logger,
        )
    finally:
        try:
            temp_output.unlink(missing_ok=True)
        except Exception:
            pass


def normalize_source_video_for_streaming(path: Path, *, log: logging.Logger | None = None) -> Path:
    active_logger = log or logger
    suffix = path.suffix.lower()
    if suffix not in _FASTSTART_COMPATIBLE_SUFFIXES:
        return path
    try:
        if not source_video_needs_remux(path):
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
