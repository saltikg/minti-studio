import logging
import mimetypes
from pathlib import Path
from typing import Optional, Tuple

from botocore.exceptions import ClientError
from flask import Response, current_app, request, send_from_directory, stream_with_context

from app.video_shorts import video_shorts_bp
from app.video_shorts.config import SHORTS_DIR, VIDEOS_DIR
from app.video_shorts.services.instagram_media_proxy import (
    InstagramMediaProxyError,
    read_instagram_media_proxy_token,
)
from app.video_shorts.services.instagram_queue import get_instagram_queue_entry
from app.video_shorts.services.storage import S3Storage, get_media_storage


logger = logging.getLogger(__name__)


def _parse_single_range(range_header: Optional[str], total_size: int) -> Optional[Tuple[int, int]]:
    raw = str(range_header or "").strip()
    if not raw or not raw.startswith("bytes="):
        return None
    value = raw[6:].strip()
    if "," in value:
        raise ValueError("Multiple ranges are not supported.")
    start_raw, _, end_raw = value.partition("-")
    if not _:
        raise ValueError("Malformed range header.")
    if start_raw == "":
        suffix_length = int(end_raw)
        if suffix_length <= 0:
            raise ValueError("Invalid suffix range.")
        start = max(0, total_size - suffix_length)
        end = total_size - 1
    else:
        start = int(start_raw)
        end = total_size - 1 if end_raw == "" else int(end_raw)
    if start < 0 or end < start or start >= total_size:
        raise ValueError("Unsatisfiable range.")
    end = min(end, total_size - 1)
    return start, end


def _resolve_instagram_proxy_clip(queue_id: str):
    entry = get_instagram_queue_entry(queue_id)
    if not entry:
        return None, None
    clip_filename = str(entry.get("clip_filename") or "").strip()
    if not clip_filename:
        return entry, None
    key = f"shorts/{clip_filename}"
    local_path = SHORTS_DIR / clip_filename
    storage = get_media_storage()
    resolved = storage.resolve_local_or_s3(key, fallback_local_paths=[local_path])
    if not resolved.exists:
        return entry, None
    return entry, resolved


def _request_log_fields() -> dict:
    return {
        "method": request.method,
        "path": request.path,
        "range": request.headers.get("Range") or "",
        "ua": request.headers.get("User-Agent") or "",
    }


def _log_proxy_request(*, status_code: int, bytes_served: int, note: str = "", request_meta: Optional[dict] = None) -> None:
    meta = request_meta or _request_log_fields()
    logger.info(
        "IG media proxy method=%s path=%s range=%s ua=%s status=%s bytes=%s note=%s",
        meta.get("method") or "",
        meta.get("path") or "",
        meta.get("range") or "",
        meta.get("ua") or "",
        status_code,
        bytes_served,
        note,
    )


def _s3_proxy_response(storage: S3Storage, key: str) -> Response:
    request_meta = _request_log_fields()
    metadata = storage.client.head_object(Bucket=storage.bucket_name, Key=key)
    total_size = int(metadata.get("ContentLength") or 0)
    content_type = metadata.get("ContentType") or "video/mp4"
    range_tuple = _parse_single_range(request.headers.get("Range"), total_size)
    headers = {
        "Content-Type": content_type,
        "Accept-Ranges": "bytes",
    }
    status_code = 200
    object_kwargs = {"Bucket": storage.bucket_name, "Key": key}
    expected_bytes = total_size
    if range_tuple:
        start, end = range_tuple
        expected_bytes = end - start + 1
        headers["Content-Range"] = f"bytes {start}-{end}/{total_size}"
        object_kwargs["Range"] = f"bytes={start}-{end}"
        status_code = 206
    headers["Content-Length"] = str(expected_bytes)

    if request.method == "HEAD":
        _log_proxy_request(status_code=status_code, bytes_served=0, note="s3-head", request_meta=request_meta)
        return Response(status=status_code, headers=headers)

    s3_obj = storage.client.get_object(**object_kwargs)
    body = s3_obj["Body"]
    counter = {"served": 0}

    def generate():
        try:
            for chunk in body.iter_chunks(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                counter["served"] += len(chunk)
                yield chunk
        finally:
            try:
                body.close()
            except Exception:
                pass

    response = Response(stream_with_context(generate()), status=status_code, headers=headers)
    response.call_on_close(
        lambda: _log_proxy_request(
            status_code=status_code,
            bytes_served=counter["served"],
            note="s3-get",
            request_meta=request_meta,
        )
    )
    return response


def _local_proxy_response(local_path: Path) -> Response:
    request_meta = _request_log_fields()
    stat = local_path.stat()
    total_size = int(stat.st_size)
    content_type = mimetypes.guess_type(local_path.name)[0] or "video/mp4"
    range_tuple = _parse_single_range(request.headers.get("Range"), total_size)
    headers = {
        "Content-Type": content_type,
        "Accept-Ranges": "bytes",
    }
    status_code = 200
    start = 0
    end = total_size - 1
    if range_tuple:
        start, end = range_tuple
        status_code = 206
        headers["Content-Range"] = f"bytes {start}-{end}/{total_size}"
    expected_bytes = max(0, end - start + 1)
    headers["Content-Length"] = str(expected_bytes)

    if request.method == "HEAD":
        _log_proxy_request(status_code=status_code, bytes_served=0, note="local-head", request_meta=request_meta)
        return Response(status=status_code, headers=headers)

    counter = {"served": 0}

    def generate():
        with open(local_path, "rb") as handle:
            handle.seek(start)
            remaining = expected_bytes
            while remaining > 0:
                chunk = handle.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                counter["served"] += len(chunk)
                remaining -= len(chunk)
                yield chunk

    response = Response(stream_with_context(generate()), status=status_code, headers=headers)
    response.call_on_close(
        lambda: _log_proxy_request(
            status_code=status_code,
            bytes_served=counter["served"],
            note="local-get",
            request_meta=request_meta,
        )
    )
    return response


@video_shorts_bp.route("/media/<path:filename>")
def serve_media(filename):
    target = (VIDEOS_DIR / filename).resolve()
    try:
        target.relative_to(VIDEOS_DIR.resolve())
    except Exception:
        return "forbidden", 403
    if not target.exists() or not target.is_file():
        return "not found", 404
    guessed_type, _ = mimetypes.guess_type(target.name)
    return send_from_directory(
        VIDEOS_DIR,
        filename,
        mimetype=guessed_type or "application/octet-stream",
    )


@video_shorts_bp.route("/ig-media/<token>", methods=["GET", "HEAD"])
def serve_instagram_media_proxy(token):
    try:
        token_data = read_instagram_media_proxy_token(token)
    except InstagramMediaProxyError as exc:
        message = str(exc)
        status = 410 if "Expired token" in message else 403
        _log_proxy_request(status_code=status, bytes_served=0, note=message)
        return message, status
    entry, resolved = _resolve_instagram_proxy_clip(token_data["queue_id"])
    if not entry:
        _log_proxy_request(status_code=404, bytes_served=0, note="missing-job")
        return "not found", 404
    if not resolved:
        _log_proxy_request(status_code=404, bytes_served=0, note="missing-clip")
        return "not found", 404
    try:
        if resolved.local_path:
            return _local_proxy_response(Path(resolved.local_path))
        storage = get_media_storage()
        if isinstance(storage, S3Storage):
            return _s3_proxy_response(storage, resolved.key)
    except ValueError as exc:
        total_size = int(resolved.size_bytes or 0)
        _log_proxy_request(status_code=416, bytes_served=0, note=str(exc))
        return Response(
            status=416,
            headers={"Content-Range": f"bytes */{total_size}", "Accept-Ranges": "bytes"},
        )
    except ClientError as exc:
        status = int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") or 502)
        _log_proxy_request(status_code=status, bytes_served=0, note=f"s3-error:{exc}")
        return "upstream error", status
    except Exception as exc:
        current_app.logger.exception("Instagram media proxy failed for queue_id=%s", token_data["queue_id"])
        _log_proxy_request(status_code=500, bytes_served=0, note=f"proxy-error:{exc}")
        return "proxy error", 500
    _log_proxy_request(status_code=404, bytes_served=0, note="unsupported-storage")
    return "not found", 404
