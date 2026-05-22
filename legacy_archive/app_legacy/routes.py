from flask import Blueprint, Response, render_template, request, jsonify
from flask import make_response
from flask import send_from_directory
from flask import redirect, url_for

import unicodedata
import re, os , logging
from datetime import datetime, timezone
import subprocess
import os
from pathlib import Path
from uuid import uuid4
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
import json
load_dotenv()  # .env dosyasını oku
import math

from app.video_shorts.config import DEFAULT_STORAGE_PLANS
from app.video_shorts.routes.auth import _format_size_bytes, _current_user, _is_authenticated
from app.video_shorts.config import (
    STATIC_AUDIO_MAX_BYTES,
    STATIC_USER_AUDIO_DIR,
    VIDEOS_DIR,
)
from app.video_shorts.services.db import (
    ensure_image_to_video_jobs_schema,
    ensure_static_image_categories_schema,
    ensure_static_images_schema,
    ensure_storage_user_schema,
    get_db,
    get_db_readonly,
)
from app.video_shorts.services.storage import (
    get_media_storage,
    is_storage_reference,
    public_url_for_stored_media,
    storage_reference_key,
)
from app.db import connect_ro, json_int_expr, json_text_expr

logger = logging.getLogger(__name__)

try:
    from .epn import build_epn_link, make_custom_id   # paket içi çalışmada
except ImportError:
    from .epn import build_epn_link, make_custom_id
 

EPN_CAMPID   = os.getenv("EPN_DEFAULT_CAMPID", "5339128108")
EPN_TOOL_ID  = int(os.getenv("EPN_TOOL_ID", "10001"))
EPN_CH_ID    = int(os.getenv("EPN_CHANNEL_ID", "1"))
EPN_ROT_US   = os.getenv("EPN_ROTATION_ID_US", "711-53200-19255-0")

BASE_URL = os.getenv("BASE_URL", "https://mintiproduct.com").rstrip("/")
bp = Blueprint("bp", __name__) 

@bp.route("/web")
def web_landing():
    from app.video_shorts.routes.auth import _is_authenticated

    is_authed = _is_authenticated()
    quick_target = url_for("video_shorts_bp.quick_short")
    source_target = url_for("video_shorts_bp.channels_page")
    quick_url = quick_target if is_authed else url_for("video_shorts_bp.login", next=quick_target)
    source_url = source_target if is_authed else url_for("video_shorts_bp.login", next=source_target)
    return render_template(
        "web/index.html",
        quick_url=quick_url,
        source_url=source_url,
    )


@bp.route("/image-to-video")
def image_to_video_redirect():
    if not _is_authenticated():
        return redirect(url_for("video_shorts_bp.login", next=request.url))
    return redirect(url_for("video_shorts_bp.image_to_video_lab"))


def _image_to_video_user():
    user = _current_user()
    if not user:
        return None, (jsonify({"error": "unauthorized"}), 401)
    return user, None


def _image_to_video_log_path(job_id: str) -> Path:
    log_dir = Path("/home/ubuntu/blog-factory/logs/image_to_video")
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / f"{job_id}.log"


def _image_to_video_response_url(stored_output_url: str) -> str:
    output_url = str(stored_output_url or "").strip()
    if not output_url:
        return ""
    if is_storage_reference(output_url):
        return public_url_for_stored_media(output_url)
    return output_url


def _safe_float(value, default: float = 0.0) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(num):
        return default
    return num


def _estimate_image_to_video_total_seconds(payload: dict, image_count: int) -> float:
    if image_count <= 0:
        return 0.0
    duration_mode = str(payload.get("duration_mode") or "").strip().lower()
    seq = payload.get("image_duration_sequence") or []
    if isinstance(seq, list) and seq:
        total = 0.0
        for idx in range(image_count):
            raw = seq[idx] if idx < len(seq) else None
            val = _safe_float(raw, 0.0)
            total += max(val, 0.5) if val > 0 else 5.0
        return total
    if duration_mode == "total":
        total = _safe_float(payload.get("total_duration_seconds"), 0.0)
        if total > 0:
            return total
    if duration_mode == "per_image":
        per = _safe_float(payload.get("per_image_seconds"), 0.0)
        if per > 0:
            return max(per, 0.5) * image_count
    # fallback: renderer's "auto" default is effectively around 5s for most practical cases
    return 5.0 * image_count


def _render_stalled_threshold_seconds(payload: dict, image_count: int) -> int:
    # Keep short jobs responsive, while allowing long/heavy jobs enough time.
    total_seconds = _estimate_image_to_video_total_seconds(payload, image_count)
    transitions = payload.get("transition_type_sequence") or []
    if isinstance(transitions, list):
        transition_count = len(transitions)
    else:
        transition_count = max(image_count - 1, 0)
    has_music = bool(str(payload.get("music_filename") or "").strip())
    base = 120.0
    dynamic = (total_seconds * 8.0) + (transition_count * 20.0) + (60.0 if has_music else 0.0)
    threshold = max(base, dynamic)
    return int(min(threshold, 1800.0))


def _audio_meta_path(audio_path: Path) -> Path:
    return audio_path.with_suffix(f"{audio_path.suffix}.meta.json")


def _read_audio_meta(audio_path: Path) -> dict:
    meta_path = _audio_meta_path(audio_path)
    if not meta_path.exists() or not meta_path.is_file():
        return {}
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _write_audio_meta(audio_path: Path, *, label: str, original_name: str) -> None:
    meta_path = _audio_meta_path(audio_path)
    payload = {
        "label": (label or "").strip(),
        "original_name": (original_name or "").strip(),
    }
    try:
        meta_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except Exception:
        logger.warning("Failed to write audio metadata for %s", audio_path)


@bp.route("/api/static-images", methods=["GET"])
def api_static_images():
    user, error = _image_to_video_user()
    if error:
        return error

    conn = get_db()
    ensure_static_images_schema(conn)
    ensure_static_image_categories_schema(conn, user.get("id"))
    rows = conn.execute(
        """
        SELECT i.id, i.label, i.filename, i.created_at, i.file_size, c.id, c.name
        FROM shorts_static_images i
        LEFT JOIN shorts_static_image_categories c
          ON c.id = i.category_id AND c.user_id = i.user_id
        WHERE i.user_id = ? AND COALESCE(i.is_active, true) = true
        ORDER BY i.created_at DESC
        """,
        [user.get("id")],
    ).fetchall()
    conn.close()

    images = []
    for row in rows:
        image_url = url_for(
            "video_shorts_bp.static",
            filename=f"user_images/{user.get('id')}/{row[2]}",
        )
        created_at = row[3].isoformat() if row[3] else None
        images.append(
            {
                "id": str(row[0]),
                "label": row[1],
                "url": image_url,
                "created_at": created_at,
                "file_size": row[4],
                "category_id": str(row[5]) if row[5] else None,
                "category_name": row[6] if row[6] else None,
            }
        )
    return jsonify({"images": images})


@bp.route("/api/static-audios", methods=["GET", "POST"])
def api_static_audios():
    user, error = _image_to_video_user()
    if error:
        return error

    user_id = user.get("id")
    user_dir = STATIC_USER_AUDIO_DIR / user_id
    allowed_exts = {".mp3", ".wav", ".m4a", ".aac", ".ogg"}

    if request.method == "POST":
        upload = request.files.get("audio")
        label = (request.form.get("label") or "").strip()
        if not upload or not upload.filename:
            return jsonify({"error": "audio file required"}), 400

        original_name = secure_filename(upload.filename)
        ext = Path(original_name).suffix.lower()
        if ext not in allowed_exts:
            return jsonify({"error": "unsupported audio type"}), 400

        try:
            upload.stream.seek(0, 2)
            size_bytes = upload.stream.tell()
            upload.stream.seek(0)
        except Exception:
            size_bytes = None
        if size_bytes is not None and size_bytes > STATIC_AUDIO_MAX_BYTES:
            return jsonify({"error": "audio file too large"}), 400

        user_dir.mkdir(parents=True, exist_ok=True)
        stored_name = f"{uuid4().hex}{ext}"
        dest_path = user_dir / stored_name
        try:
            upload.save(dest_path)
        except Exception:
            return jsonify({"error": "failed to save audio"}), 500

        if not label:
            label = (Path(original_name).stem or "Audio").strip()[:80]
        _write_audio_meta(dest_path, label=label, original_name=original_name)
        audio_url = url_for(
            "video_shorts_bp.static",
            filename=f"user_audio/{user_id}/{stored_name}",
        )
        return jsonify(
            {
                "ok": True,
                "audio": {
                    "id": stored_name,
                    "label": label,
                    "display_name": original_name,
                    "original_name": original_name,
                    "filename": stored_name,
                    "url": audio_url,
                    "file_size": size_bytes,
                },
            }
        )

    audios = []
    if user_dir.exists():
        for path in sorted(user_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if not path.is_file():
                continue
            if path.suffix.lower() not in allowed_exts:
                continue
            audio_url = url_for(
                "video_shorts_bp.static",
                filename=f"user_audio/{user_id}/{path.name}",
            )
            meta = _read_audio_meta(path)
            original_name = str(meta.get("original_name") or "").strip() or path.name
            label = str(meta.get("label") or "").strip() or path.stem
            audios.append(
                {
                    "id": path.name,
                    "label": label,
                    "display_name": original_name,
                    "original_name": original_name,
                    "filename": path.name,
                    "url": audio_url,
                    "file_size": path.stat().st_size,
                    "created_at": datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat(),
                }
            )
    return jsonify({"audios": audios})


@bp.route("/api/static-audios/<audio_id>", methods=["DELETE"])
def api_static_audio_delete(audio_id):
    user, error = _image_to_video_user()
    if error:
        return error

    safe_name = Path(str(audio_id or "")).name
    if not safe_name or safe_name != str(audio_id):
        return jsonify({"error": "invalid audio id"}), 400

    allowed_exts = {".mp3", ".wav", ".m4a", ".aac", ".ogg"}
    if Path(safe_name).suffix.lower() not in allowed_exts:
        return jsonify({"error": "unsupported audio type"}), 400

    user_dir = STATIC_USER_AUDIO_DIR / user.get("id")
    target_path = user_dir / safe_name
    if not target_path.exists() or not target_path.is_file():
        return jsonify({"error": "audio not found"}), 404

    meta_path = _audio_meta_path(target_path)
    try:
        target_path.unlink()
    except Exception:
        return jsonify({"error": "failed to delete audio"}), 500
    try:
        if meta_path.exists() and meta_path.is_file():
            meta_path.unlink()
    except Exception:
        logger.warning("Failed to delete audio metadata for %s", target_path)

    return jsonify({"ok": True, "deleted_id": safe_name})


@bp.route("/api/image-to-video/jobs", methods=["GET", "POST"])
def image_to_video_jobs():
    user, error = _image_to_video_user()
    if error:
        return error

    conn = get_db()
    ensure_image_to_video_jobs_schema(conn)

    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        image_ids = data.get("image_ids") or []
        if not isinstance(image_ids, list) or not image_ids:
            conn.close()
            return jsonify({"error": "image_ids required"}), 400

        job_id = str(uuid4())
        payload_json = json.dumps(data, ensure_ascii=False)
        image_ids_json = json.dumps(image_ids, ensure_ascii=False)

        conn.execute(
            """
            INSERT INTO image_to_video_jobs (
              job_id,
              user_id,
              image_ids_json,
              payload_json,
              status,
              progress,
              output_url,
              error_message,
              created_at,
              updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, now(), now())
            """,
            [
                job_id,
                user.get("id"),
                image_ids_json,
                payload_json,
                "queued",
                0,
                None,
                None,
            ],
        )
        conn.commit()
        conn.execute(
            """
            UPDATE image_to_video_jobs
            SET status = ?, progress = ?, updated_at = now()
            WHERE job_id = ? AND user_id = ?
            """,
            ["rendering", 5, job_id, user.get("id")],
        )
        conn.commit()
        conn.close()
        env = os.environ.copy()
        if "VIDEO_SHORTS_DB" not in env or not env.get("VIDEO_SHORTS_DB"):
            env["VIDEO_SHORTS_DB"] = str(
                Path("/home/ubuntu/blog-factory/warehouse/video_shorts.duckdb")
            )
        env["IMAGE_TO_VIDEO_LOG_FFMPEG"] = "1"
        log_path = _image_to_video_log_path(job_id)
        try:
            with log_path.open("ab") as log_fp:
                log_fp.write(
                    f"\n=== image-to-video job {job_id} start {datetime.now(timezone.utc).isoformat()} ===\n".encode(
                        "utf-8"
                    )
                )
                subprocess.Popen(
                    [
                        "python3",
                        "-m",
                        "app.video_shorts.services.image_to_video",
                        "--job-id",
                        job_id,
                    ],
                    cwd=str(Path(__file__).resolve().parents[1]),
                    env=env,
                    stdout=log_fp,
                    stderr=subprocess.STDOUT,
                )
        except Exception:
            conn = get_db()
            ensure_image_to_video_jobs_schema(conn)
            conn.execute(
                """
                UPDATE image_to_video_jobs
                SET status = ?, error_message = ?, updated_at = now()
                WHERE job_id = ? AND user_id = ?
                """,
                [
                    "failed",
                    f"Worker failed to start. Check logs/image_to_video/{job_id}.log",
                    job_id,
                    user.get("id"),
                ],
            )
            conn.commit()
            conn.close()
        return jsonify(
            {
                "job_id": job_id,
                "status": "rendering",
                "progress": 5,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        )

    rows = conn.execute(
        """
        SELECT job_id, status, progress, output_url, error_message, created_at, image_ids_json, payload_json
        FROM image_to_video_jobs
        WHERE user_id = ?
        ORDER BY created_at DESC
        LIMIT 50
        """,
        [user.get("id")],
    ).fetchall()
    conn.close()

    jobs = []
    for row in rows:
        created_at = row[5].isoformat() if row[5] else None
        try:
            image_ids = json.loads(row[6]) if row[6] else []
        except Exception:
            image_ids = []
        try:
            payload = json.loads(row[7]) if row[7] else {}
        except Exception:
            payload = {}
        jobs.append(
            {
                "job_id": str(row[0]),
                "status": row[1] or "queued",
                "progress": row[2] or 0,
                "output_url": _image_to_video_response_url(row[3] or ""),
                "error_message": row[4] or "",
                "created_at": created_at,
                "image_ids": image_ids,
                "payload": payload,
            }
        )
    return jsonify({"jobs": jobs})


@bp.route("/api/image-to-video/jobs/<job_id>", methods=["GET", "DELETE"])
def image_to_video_job(job_id):
    user, error = _image_to_video_user()
    if error:
        return error

    conn = get_db()
    ensure_image_to_video_jobs_schema(conn)
    if request.method == "DELETE":
        row = conn.execute(
            "SELECT output_url FROM image_to_video_jobs WHERE job_id = ? AND user_id = ?",
            [job_id, user.get("id")],
        ).fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "not found"}), 404
        stored_output_url = str(row[0] or "").strip()
        output_kind = "s3" if is_storage_reference(stored_output_url) else "local"
        logger.info(
            "image_to_video delete begin job_id=%s user_id=%s output_kind=%s",
            job_id,
            user.get("id"),
            output_kind,
        )
        storage = get_media_storage()
        storage_key = ""
        if is_storage_reference(stored_output_url):
            storage_key = storage_reference_key(stored_output_url)
        elif getattr(storage, "backend_name", "local") == "s3":
            storage_key = f"image_to_video/{user.get('id')}/image_to_video_{job_id}.mp4"
            logger.info(
                "image_to_video s3 delete compatibility key job_id=%s user_id=%s key=%s",
                job_id,
                user.get("id"),
                storage_key,
            )
        if storage_key and getattr(storage, "backend_name", "local") == "s3":
            logger.info(
                "image_to_video s3 delete begin job_id=%s user_id=%s key=%s",
                job_id,
                user.get("id"),
                storage_key,
            )
            try:
                storage.delete(storage_key)
            except Exception:
                logger.exception(
                    "image_to_video s3 delete failed job_id=%s user_id=%s key=%s",
                    job_id,
                    user.get("id"),
                    storage_key,
                )
                conn.close()
                return jsonify({"error": "delete failed"}), 500
            logger.info(
                "image_to_video s3 delete success job_id=%s user_id=%s key=%s",
                job_id,
                user.get("id"),
                storage_key,
            )
        conn.execute(
            "DELETE FROM image_to_video_jobs WHERE job_id = ? AND user_id = ?",
            [job_id, user.get("id")],
        )
        conn.commit()
        conn.close()
        logger.info("image_to_video db delete success job_id=%s user_id=%s", job_id, user.get("id"))
        video_path = VIDEOS_DIR / "image_to_video" / f"image_to_video_{job_id}.mp4"
        if video_path.exists():
            try:
                video_path.unlink()
                logger.info(
                    "image_to_video local file delete success job_id=%s user_id=%s path=%s",
                    job_id,
                    user.get("id"),
                    video_path,
                )
            except Exception:
                logger.exception(
                    "image_to_video local file delete failed job_id=%s user_id=%s path=%s",
                    job_id,
                    user.get("id"),
                    video_path,
                )
        return jsonify({"ok": True})
    row = conn.execute(
        """
        SELECT job_id, status, progress, output_url, error_message, created_at, updated_at, image_ids_json, payload_json
        FROM image_to_video_jobs
        WHERE job_id = ? AND user_id = ?
        """,
        [job_id, user.get("id")],
    ).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "not found"}), 404
    status = row[1] or "queued"
    progress = row[2] or 0
    stored_output_url = row[3] or ""
    output_url = _image_to_video_response_url(stored_output_url)
    try:
        image_ids = json.loads(row[7]) if row[7] else []
    except Exception:
        image_ids = []
    try:
        payload = json.loads(row[8]) if row[8] else {}
    except Exception:
        payload = {}
    created_at_value = row[5]
    updated_at_value = row[6] if len(row) > 6 else None
    if status in {"queued", "rendering"} and not stored_output_url:
        video_path = VIDEOS_DIR / "image_to_video" / f"image_to_video_{job_id}.mp4"
        if video_path.exists():
            stored_output_url = f"/video_shorts/media/image_to_video/{video_path.name}"
            output_url = stored_output_url
            status = "done"
            progress = 100
            conn.execute(
                """
                UPDATE image_to_video_jobs
                SET status = ?, progress = ?, output_url = ?, updated_at = now()
                WHERE job_id = ? AND user_id = ?
                """,
                [status, progress, stored_output_url, job_id, user.get("id")],
            )
            conn.commit()

    if status == "queued" and updated_at_value:
        try:
            age_seconds = (datetime.now(timezone.utc) - updated_at_value.replace(tzinfo=timezone.utc)).total_seconds()
        except Exception:
            age_seconds = 0
        if age_seconds > 30:
            conn.execute(
                """
                UPDATE image_to_video_jobs
                SET status = ?, error_message = ?, updated_at = now()
                WHERE job_id = ? AND user_id = ?
                """,
                ["failed", "Worker did not start. Please retry.", job_id, user.get("id")],
            )
            conn.commit()
            status = "failed"
            progress = 0

    if status == "rendering" and updated_at_value:
        try:
            age_seconds = (datetime.now(timezone.utc) - updated_at_value.replace(tzinfo=timezone.utc)).total_seconds()
        except Exception:
            age_seconds = 0
        stalled_threshold = _render_stalled_threshold_seconds(payload, len(image_ids))
        if age_seconds > stalled_threshold:
            conn.execute(
                """
                UPDATE image_to_video_jobs
                SET status = ?, error_message = ?, updated_at = now()
                WHERE job_id = ? AND user_id = ?
                """,
                ["failed", f"Render stalled (>{stalled_threshold}s). Please retry.", job_id, user.get("id")],
            )
            conn.commit()
            status = "failed"
            progress = 0
    conn.close()

    created_at = created_at_value.isoformat() if created_at_value else None

    return jsonify(
        {
            "job_id": str(row[0]),
            "status": status,
            "progress": progress,
            "output_url": output_url,
            "error_message": row[4] or "",
            "created_at": created_at,
            "image_ids": image_ids,
            "payload": payload,
        }
    )


def _web_cta_links():
    from app.video_shorts.routes.auth import _is_authenticated

    is_authed = _is_authenticated()
    quick_target = url_for("video_shorts_bp.quick_short")
    source_target = url_for("video_shorts_bp.channels_page")
    quick_url = quick_target if is_authed else url_for("video_shorts_bp.login", next=quick_target)
    source_url = source_target if is_authed else url_for("video_shorts_bp.login", next=source_target)
    return quick_url, source_url


def _web_pricing_plans():
    plan_label_map = {
        "plan_free": "Free",
        "plan_2gb": "Starter",
        "plan_10gb": "Creator",
        "plan_100gb": "Studio",
    }
    plan_order = ["plan_free", "plan_2gb", "plan_10gb", "plan_100gb"]
    plan_rows = None
    try:
        conn = get_db_readonly()
        try:
            plan_rows = conn.execute(
                "SELECT plan_id, label, quota_bytes FROM shorts_storage_plans ORDER BY sort_order, label"
            ).fetchall()
        finally:
            conn.close()
    except Exception as exc:
        error_text = str(exc).lower()
        if "shorts_storage_plans" in error_text or "video_shorts_db" in error_text:
            try:
                conn = get_db()
                try:
                    ensure_storage_user_schema(conn)
                    plan_rows = conn.execute(
                        "SELECT plan_id, label, quota_bytes FROM shorts_storage_plans ORDER BY sort_order, label"
                    ).fetchall()
                finally:
                    conn.close()
            except Exception:
                plan_rows = None
        else:
            raise

    if not plan_rows:
        plan_rows = [
            (plan["plan_id"], plan["label"], plan["quota_bytes"])
            for plan in DEFAULT_STORAGE_PLANS
        ]

    plan_lookup = {
        row[0]: {
            "plan_id": row[0],
            "label": plan_label_map.get(row[0], row[1]),
            "quota_bytes": row[2],
            "quota_label": _format_size_bytes(row[2]),
        }
        for row in plan_rows
        if row[0] in plan_order
    }
    return [plan_lookup[plan_id] for plan_id in plan_order if plan_id in plan_lookup]


@bp.route("/web/how-it-works")
def web_how_it_works():
    quick_url, source_url = _web_cta_links()
    return render_template(
        "web/how_it_works.html",
        quick_url=quick_url,
        source_url=source_url,
    )


@bp.route("/web/use-cases")
def web_use_cases():
    quick_url, source_url = _web_cta_links()
    return render_template(
        "web/use_cases.html",
        quick_url=quick_url,
        source_url=source_url,
    )


@bp.route("/web/use-cases/daily-shorts-system")
def web_use_case_daily_shorts_system():
    quick_url, source_url = _web_cta_links()
    return render_template(
        "web/use_case_daily_shorts_system.html",
        quick_url=quick_url,
        source_url=source_url,
    )


@bp.route("/web/use-cases/repurpose-youtube-library")
def web_use_case_repurpose_youtube_library():
    quick_url, source_url = _web_cta_links()
    return render_template(
        "web/use_case_repurpose_youtube_library.html",
        quick_url=quick_url,
        source_url=source_url,
    )


@bp.route("/web/use-cases/grow-instagram-reels")
def web_use_case_grow_instagram_reels():
    quick_url, source_url = _web_cta_links()
    return render_template(
        "web/use_case_grow_instagram_reels.html",
        quick_url=quick_url,
        source_url=source_url,
    )


@bp.route("/web/use-cases/creator-consistency")
def web_use_case_creator_consistency():
    quick_url, source_url = _web_cta_links()
    return render_template(
        "web/use_case_creator_consistency.html",
        quick_url=quick_url,
        source_url=source_url,
    )


@bp.route("/web/pricing")
def web_pricing():
    quick_url, source_url = _web_cta_links()
    plans = _web_pricing_plans()
    return render_template(
        "web/pricing.html",
        quick_url=quick_url,
        source_url=source_url,
        plans=plans,
    )


@bp.route("/web/templates")
def web_templates():
    quick_url, source_url = _web_cta_links()
    return render_template(
        "web/templates.html",
        quick_url=quick_url,
        source_url=source_url,
    )


@bp.route("/web/privacy")
def web_privacy():
    quick_url, source_url = _web_cta_links()
    return render_template(
        "web/privacy.html",
        quick_url=quick_url,
        source_url=source_url,
    )


@bp.route("/web/terms")
def web_terms():
    quick_url, source_url = _web_cta_links()
    return render_template(
        "web/terms.html",
        quick_url=quick_url,
        source_url=source_url,
    )



# --- JSON → HTML yardımcıları (buyers_guide_json / faq_json için) ---
def _json_list(val):
    """list döndür; string ise JSON parse dene; olmazsa []."""
    if val is None:
        return []
    if isinstance(val, list):
        return val
    if isinstance(val, str):
        try:
            j = json.loads(val)
            return j if isinstance(j, list) else []
        except Exception:
            return []
    return []

def _clean_head(txt: str) -> str:
    t = (txt or "").strip()
    # baştaki **...** kalınları ve baştaki numarayı temizle
    t = t.strip("* ").lstrip("0123456789). -–").strip()
    return t

def render_buyers_guide_json(bg_val) -> str | None:
    """[{title, desc}] → <section>…</section> HTML; boşsa None."""
    rows = _json_list(bg_val)
    if not rows:
        return None
    items = []
    for it in rows:
        if not isinstance(it, dict):
            continue
        head = _clean_head(it.get("title") or it.get("heading") or it.get("head") or "")
        body = (it.get("desc") or it.get("body") or it.get("content") or it.get("text") or "").strip()
        if not head and not body:
            continue
        items.append(f"<li><div class='bg-item'><div class='bg-title'><strong>{head}</strong></div><div class='bg-desc'>{_md(body)}</div></div></li>")
    if not items:
        return None
    return f"""
<section class="buyers-guide" aria-label="Buyer’s Guide">
  <h2>Buyer's Guide</h2>
  <ol class="guide-list">
    {''.join(items)}
  </ol>
</section>""".strip()

def render_faq_json(faq_val) -> str | None:
    """[{q,a}] → <section>…</section> HTML; boşsa None."""
    rows = _json_list(faq_val)
    if not rows:
        return None
    blocks = []
    for it in rows:
        if isinstance(it, dict):
            q = _clean_head(it.get("q") or it.get("question") or it.get("title") or "")
            a = (it.get("a") or it.get("answer") or it.get("body") or "").strip()
        else:
            q, a = str(it), ""
        if not q and not a:
            continue
        blocks.append(f"""
<details class="faq-item">
  <summary><strong>{q}</strong></summary>
  <div class="faq-answer">{_md(a)}</div>
</details>""".strip())
    if not blocks:
        return None
    return f"""
<section class="faq" aria-label="FAQ">
  <h2>FAQ</h2>
  {''.join(blocks)}
</section>""".strip()



def _lastmod_iso(ts) -> str:
    # DuckDB'den gelen timestamp string ya da datetime olabilir
    if not ts:
        return None
    if isinstance(ts, str):
        try:
            # 2025-09-24 21:03:11.123 gibi
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except Exception:
            return None
    elif isinstance(ts, datetime):
        dt = ts
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

from datetime import datetime, timezone




def _eta_as_days(val):
    """ETA değerini (int/float, ISO string, datetime) 'gün' (int) olarak döndürür."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return int(val)
    if isinstance(val, datetime):
        now = datetime.now(timezone.utc) if val.tzinfo else datetime.utcnow()
        return max(0, (val - now).days)
    if isinstance(val, str):
        s = val.strip()
        if s.isdigit():
            return int(s)
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc) if dt.tzinfo else datetime.utcnow()
            return max(0, (dt - now).days)
        except Exception:
            return None
    return None


def _round_half(x):   # 0.5 adım
    return max(0.0, min(5.0, round(x * 2) / 2.0))

def _floor_half(x):   # 0.5 adım aşağı yuvarla (cap uyumu için)
    return max(0.0, math.floor(x * 2) / 2.0)

def _wilson_lower_bound(p, n, z=1.96):
    if n is None or n <= 0 or p is None:
        return 0.60  # veri yoksa muhafazakâr taban
    a = p + z*z/(2*n)
    b = z * math.sqrt((p*(1-p))/n + (z*z)/(4*n*n))
    c = 1 + z*z/n
    return max(0.0, (a - b) / c)

def _credibility_cap(n):
    if n is None: return 4.5
    if n < 100:    return 4.5
    if n < 500:    return 4.7
    if n < 5000:   return 4.9
    return 5.0

def _compute_seller_rating(
    fb_pct, fb_count, seller_score, trust_level,
    returns_accepted=None, return_shipping_payer=None,
    ship_free=None, ship_type=None,
    ship_min_eta=None, ship_max_eta=None,
    seller_level=None, defect_rate=None   # opsiyonel dış API sinyalleri
):
    """
    ÇIKTI:
      - score: 1 ondalıklı puan (cap sonrası, ör. 4.9)
      - stars_whole: tam yıldız (ör. 4.9 → 5, 4.2 → 4)
      - label: etiket (Top Rated / Excellent / ...)
      - tier: rozet rengi (high/medium/low)
    """

    # 1) kalite tabanı: Wilson ya da seller_score normalizasyonu
    if fb_pct is not None and fb_count:
        L = _wilson_lower_bound(fb_pct/100.0, fb_count, 1.96)
    elif seller_score is not None:
        norm = seller_score/100.0 if seller_score > 5 else seller_score/5.0
        L = max(0.0, min(1.0, norm))
    else:
        L = 0.6
    base = L * 5.0

    # 2) mikro bonus/ceza (±0.3★)
    delta = 0.0
    if returns_accepted is True and str(return_shipping_payer or '').upper() == 'SELLER':
        delta += 0.10
    elif returns_accepted is False:
        delta -= 0.20
    if ship_free:
        delta += 0.05
    if (ship_type or '').lower() == 'expedited':
        delta += 0.05

    # ETA: her tipten (int/float/str/datetime) gün'e çevir
    eta_min_days = _eta_as_days(ship_min_eta)
    eta_max_days = _eta_as_days(ship_max_eta)
    if eta_min_days is None and eta_max_days is None:
        delta -= 0.05
    elif eta_max_days is not None and eta_max_days > 14:
        delta -= 0.05

    # (opsiyonel) dış API sinyalleri
    if (seller_level or '').upper() == 'TOP_RATED':
        delta += 0.20
    if defect_rate is not None:
        if defect_rate >= 2.0:
            delta -= 0.20
        elif defect_rate <= 0.5:
            delta += 0.10

    delta = max(-0.3, min(0.3, delta))
    raw = max(0.0, min(5.0, base + delta))

    # 3) güvenilirlik tavanı ve yıldız kuralı
    cap = _credibility_cap(fb_count)
    capped = min(raw, cap)
    score = round(capped, 1)                  # metin: 4.9
    stars_whole = int(math.floor(score + 0.5))# yıldız: 4.9→5, 4.2→4, 4.5→5

    # 4) etiket + rozet
    n = fb_count or 0
    if score >= 4.8 and n >= 5000:
        label = "Top Rated"
    elif score >= 4.6 and n >= 500:
        label = "Excellent"
    elif score >= 4.2 and n >= 100:
        label = "Very Good"
    elif score >= 4.2:
        label = "Very Good (limited history)"
    elif score >= 3.8:
        label = "Good"
    elif score >= 3.2:
        label = "Fair"
    else:
        label = "Unproven"

    if score >= 4.5:
        tier = "high"
    elif score >= 3.5:
        tier = "medium"
    else:
        tier = "low"

    return {"score": score, "stars_whole": stars_whole, "label": label, "tier": tier}

 
# --------------------------
# DB helpers
# --------------------------
def split_intro_lead(html: str):
    """
    Introduction HTML'ini ikiye böler:
    - lead: ilk paragraf(lar) (Quick Take başlamadan önceki kısım)
    - rest: Quick Take ve devamı (başlığı, listesi vs. dahil)
    Ayırma işaretleri: 'Quick Take:' başlığı, <blockquote>, <ul> vb.
    """
    if not html:
        return "", ""
    txt = html.strip()

    # Quick Take başlığına göre ayır
    m = re.search(r"(?i)(<h\d[^>]*>\s*quick\s*take\s*:?\s*</h\d>|<strong>\s*quick\s*take\s*:?\s*</strong>|Quick\s*Take:)", txt)
    if m:
        return txt[:m.start()].strip(), txt[m.start():].strip()

    # Blok alıntı ya da ilk <ul> varsa onları 'rest'e al
    m = re.search(r"(<blockquote[\s>]|<ul[\s>])", txt, flags=re.I)
    if m:
        return txt[:m.start()].strip(), txt[m.start():].strip()

    # fallback: ilk paragrafı lead yap
    m = re.search(r"</p>", txt, flags=re.I)
    if m:
        return txt[:m.end()].strip(), txt[m.end():].strip()

    return txt, ""

def pretty_season_label(season_name: str, fallback_slug: str) -> str:
    """
    'thanksgiving-2025' → 'Thanksgiving 2025'
    'halloween-2025'   → 'Halloween 2025'
    seasonal değilse fallback slug'ı Title Case döndürür.
    """
    if not season_name or fallback_slug != "seasonal":
        return (fallback_slug or "uncategorized").replace("-", " ").title()
    # 'base-year' ayrıştır
    m = re.match(r"^(.+?)-(\d{4})$", season_name)
    if m:
        base, year = m.group(1), m.group(2)
        return f"{base.replace('-', ' ').title()} {year}"
    # yıl yoksa sadece base'i güzelleştir
    return season_name.replace("-", " ").title()


def _connect_ro():
    return connect_ro()

def fetch_row_by_slug(slug: str):
    con = _connect_ro()
    result = con.execute("""
        SELECT
            bc.*,
            bp.date_published,
            a.display_name AS author_name,
            a.avatar_url AS author_avatar_url,
            a.author_bio
        FROM blog_contents bc
        LEFT JOIN blog_posts bp ON bc.idea_id = bp.idea_id
        LEFT JOIN authors a ON bp.author_id = a.author_id
        WHERE bc.slug = ?
        LIMIT 1
    """, [slug])
    row = result.fetchone()
    cols = [d[0] for d in result.description] if result.description else []
    con.close()
    if not row:
        return None
    return dict(zip(cols, row))

def get_categories():
    con = _connect_ro()
    cats = [r[0] for r in con.execute("""
        SELECT COALESCE(category_slug,'uncategorized') AS c
        FROM blog_contents
        GROUP BY 1
        ORDER BY 1
    """).fetchall()]
    con.close()
    return cats



# --------------------------
# Routes
# --------------------------

# --- title stripping helpers ---


# --- Markdown yardimcilar ---
try:
    from markdown import markdown as md_to_html
except Exception:
    md_to_html = None

def _md(s: str) -> str:
    """Markdown varsa HTML'e çevir, yoksa metni döndür."""
    if not s:
        return ""
    return md_to_html(s) if md_to_html else s


def split_faq_and_tail(raw: str):
    """
    FAQ metnini Q&A HTML'i ve sonda kalan 'tail' (çoğunlukla conclusion) olarak ayırır.
    - JSON (dict/list) gelirse parse eder ve <details> blokları üretir.
    - Markdown kalıbı gelirse mevcut regex mantığıyla ayrıştırır.
    - En sonda tail başındaki '## Conclusion' başlığını temizler.
    """
    
    if not raw:
        return "", ""

    txt = raw.strip()
    txt = txt.replace("\\'", "'")

    # Başta '## FAQ' varsa temizle
    txt = re.sub(r"^\s*##\s*faq\s*\r?\n?", "", txt, flags=re.I)

    # --- Yardımcılar ---
    def _render_faq(pairs):
        details_html = []
        for q, a in pairs:
            q = (q or "").strip()
            a = (a or "").strip()
            details_html.append(f"""
<details class="faq-item">
  <summary>{q}</summary>
  <div class="faq-answer">{_md(a)}</div>
</details>
""".strip())
        return f"""
<section class="faq">
  <h2>FAQ</h2>
  {''.join(details_html)}
</section>
""".strip()

    def _try_json(txt0):
        if not txt0 or txt0[0] not in "{[":
            return None
        try:
            return json.loads(txt0)
        except Exception:
            return None

    faq_html = ""
    tail = ""

    # 0) JSON parse dene
    parsed = _try_json(txt)
    if isinstance(parsed, dict):
        # --- YENİ: {"faq": [{"question":"...","answer":"..."} , ...]} desteği ---
        if "faq" in parsed and isinstance(parsed["faq"], list):
            pairs = []
            for it in parsed["faq"]:
                if isinstance(it, dict):
                    q = (it.get("question") or it.get("q") or it.get("title") or "").strip()
                    a = (it.get("answer")   or it.get("a") or it.get("body")  or "").strip()
                    if q:
                        pairs.append((q, a))
                else:
                    # liste içinde düz string varsa soru say
                    pairs.append((str(it).strip(), ""))
            faq_html = _render_faq(pairs)
            return faq_html, ""   # tail yok, direkt dön
        # --- /YENİ ---
        
        # {"1. Soru?": "cevap", "2. ...": "..."}
        def srt(k):
            m = re.match(r"\s*(\d+)", k)
            return int(m.group(1)) if m else 10**9
        pairs = []
        for k in sorted(parsed.keys(), key=srt):
            q = re.sub(r"^\s*\d+\.\s*", "", str(k)).strip()
            a = str(parsed[k]).strip()
            pairs.append((q, a))
        faq_html = _render_faq(pairs)

    elif isinstance(parsed, list):
        # [{"question": "...", "answer": "..."}, ...] ya da ["S1", "S2", ...]
        pairs = []
        for it in parsed:
            if isinstance(it, dict):
                q = it.get("question") or it.get("q") or it.get("title") or it.get("head") or ""
                a = it.get("answer")   or it.get("a") or it.get("body")  or it.get("text") or ""
                q = re.sub(r"^\s*\d+\.\s*", "", str(q)).strip()
                pairs.append((q, a))
            else:
                pairs.append((str(it), ""))
        faq_html = _render_faq(pairs)

    else:
        # 1) Markdown kalıbı: **1. Soru?** Cevap ... (mevcut davranış)
        pat = re.compile(
            r"(?:^|\n)\s*(?:[-*]\s*)?\*\*\s*(?:\d+\.?\s*)?(.+?)\s*\*\*\s*(.*?)(?=(?:\n\s*(?:[-*]\s*)?\*\*\s*(?:\d+\.?\s*)?.+?\s*\*\*|\Z))",
            re.S
        )

        qa = []
        last_end = 0
        for m in pat.finditer(txt):
            q = m.group(1).strip()
            a = m.group(2).strip()
            qa.append([q, a])
            last_end = m.end()

        tail = txt[last_end:].strip() if last_end else ""

        # SON CEVABIN İÇİNDEN 'Conclusion'u AYIR (varsa)
        if qa:
            concl_split_pat = re.compile(r"(?im)^\s*(?:##\s*conclusion\b|in\s+conclusion\b[:,]?)\s*")
            last_q, last_a = qa[-1][0], qa[-1][1]
            m = concl_split_pat.search(last_a)
            if m:
                body = last_a[:m.start()].rstrip()
                tail_inside = last_a[m.start():].lstrip()
                qa[-1][1] = body
                tail = (tail + "\n\n" + tail_inside).strip() if tail else tail_inside

            # HTML üret
            pairs = []
            for q, a in qa:
                q_clean = re.sub(r"^\s*\d+\.?\s*", "", q)               # "1." veya "1" at
                q_clean = re.sub(r"^\*+\s*|\s*\*+$", "", q_clean).strip()

                pairs.append((q_clean, a))
            faq_html = _render_faq(pairs)
        else:
            # 2) Regex tutmazsa düz MD → HTML
            faq_html = f"""
<section class="faq">
  <h2>FAQ</h2>
  {_md(txt)}
</section>
""".strip()

    # --- tail başında '## Conclusion' varsa temizle (senin bloğun) ---
    if tail:
        tail = re.sub(r"^\s*##\s*conclusion\s*:?[\r\n]*", "", tail, flags=re.I)

    return faq_html, tail


# --- Section formatter'lar ---
def format_buyers_guide(raw: str) -> str:
    """
    'Buyer's Guide' içeriğini sağlam HTML'ye çevirir.
    - JSON dict/list gelirse parse eder.
    - Düz metin gelirse mevcut regex'lerle numaralı liste çıkarır.
    """
    if not raw:
        return ""
    txt = raw.strip()

    # Geçici olarak HTML kartlarını kenara ayır
    #card_pat = re.compile(r'(<div style="border:1px solid #eee;.*?</div>)', re.DOTALL)
    card_pat = re.compile(
        r'(<div[^>]*style=(["\']).*?border:\s*1px\s*solid\s*#eee;.*?\2[^>]*>.*?</div>)',
        re.IGNORECASE | re.DOTALL
    )
    
    cards = card_pat.findall(txt)
    cards = [m[0] if isinstance(m, tuple) else m for m in cards]
    txt = card_pat.sub("", txt)
    
    # "1. <başlık>" kısmını yakalayıp hemen ardından bir satır sonu enjekte ediyoruz.
    txt = re.sub(r"(?m)^\s*(\d+)\.\s+([^\n]+?)\s+(?=[A-Za-z])", r"\1. \2\n", txt)


    # Başta '## Buyer...' başlığı varsa sök
    txt = re.sub(r"^\s*##\s*buyer[''`]?s?\s*guide\s*\n?", "", txt, flags=re.I)

    # 0) JSON olarak gelmiş mi? ({"1. ...": "...", ...} veya [...])
    parsed = None
    if txt and txt[0] in "{[":
        try:
            parsed = json.loads(txt)
        except Exception:
            parsed = None

    def _render_ol(items_html):
        return f"""
<section class="buyers-guide">
  <h2>Buyer's Guide</h2>
  <ol class="guide-list">
    {''.join(items_html)}
  </ol>
</section>
""".strip()

    # 0.a) Dict ise: key = "1. Başlık" / value = body
    if isinstance(parsed, dict):
        def _sort_key(k):
            m = re.match(r"\s*(\d+)", k)
            return int(m.group(1)) if m else 10**9
        items = []
        for k in sorted(parsed.keys(), key=_sort_key):
            head = re.sub(r"^\s*\d+\.\s*", "", str(k)).strip()
            body = str(parsed[k]).strip()
            items.append(f"<li><strong>{head}</strong><br>{_md(body)}</li>")
        return _render_ol(items)

    # 0.b) Liste ise: {title/body} ya da string maddeleri destekle
    if isinstance(parsed, list):
        items = []
        for it in parsed:
            if isinstance(it, dict):
                head = it.get("title") or it.get("heading") or it.get("head") or ""
                body = it.get("body") or it.get("content") or it.get("text") or ""
                head = re.sub(r"^\s*\d+\.\s*", "", str(head)).strip() or "Tip"
                items.append(f"<li><strong>{head}</strong><br>{_md(str(body).strip())}</li>")
            else:
                items.append(f"<li>{_md(str(it))}</li>")
        return _render_ol(items)

    # 1) "#### 1. Başlık" / "### 1. Başlık" → normalize
    txt = re.sub(r"^\s*#{3,6}\s+(\d+\.\s+)", r"\1", txt, flags=re.M)

    # 2) "1. Başlık\n<gövde>\n2. Başlık\n..." kalıbını yakala
    pat = re.compile(
        r"^\s*(\d+)\.\s+([^\n]+)\n+([\s\S]*?)(?=^\s*\d+\.\s+[^\n]+\n+|\Z)",
        re.M
    )
    items = []
    for m in pat.finditer(txt):
        head = m.group(2).strip()
        body = m.group(3).strip()
        block_md = f"**{head}**\n\n{body}" if body else f"**{head}**"
        items.append(f"<li>{_md(block_md)}</li>")

    if items:
        return _render_ol(items)

    # 3) Hiçbiri değilse dümdüz renderla
    html_out = f"""
<section class="buyers-guide">
  <h2>Buyer's Guide</h2>
  {_md(txt)}
</section>
""".strip()

    # Kenara ayırdığımız kartları sona ekle
    #if cards:
    #    html_out += "\n" + "\n".join(cards)

    return html_out



def _product_card_html_from_ebay(p):
    title = p.get("title") or "Product"
    img   = p.get("image") or "/static/img/placeholder.png"
    price = p.get("price")
    url   = p.get("url") or "#"
    price_txt = f"${price:,.2f}" if isinstance(price, (int,float)) else (str(price) if price else "")
    return f"""
<div style="border:1px solid #eee;padding:10px;margin:15px 5px;text-align:center;max-width:200px;display:inline-block;vertical-align:top;box-shadow:0 2px 4px rgba(0,0,0,0.1);border-radius:5px;">
  <img src="{img}" alt="{title}" style="max-width:100%;height:150px;object-fit:cover;display:block;margin:0 auto 10px;border-radius:3px;">
  <h4 style="font-size:0.9em;margin:0 0 10px;line-height:1.2;font-weight:normal;height:4.5em;overflow:hidden;">{title}</h4>
  <p style="font-weight:bold;color:#e53935;font-size:1.1em;margin:0 0 10px;">{price_txt}</p>
  <a href="{url}" target="_blank" rel="noopener sponsored" style="display:inline-block;padding:8px 12px;background-color:#3665f3;color:#fff;text-decoration:none;border-radius:20px;font-size:0.9em;font-weight:bold;">View on eBay</a>
</div>
""".strip()

def _inject_product_cards_into_text(txt: str, ebay_products: list):
    if not txt:
        return txt
    # İlk 5 ürünü kart olarak hazırla
    cards = [ _product_card_html_from_ebay(p) for p in (ebay_products or [])[:5] ]
    # Hem {{PRODUCT_CARD_n}} hem {PRODUCT_CARD_n} destekle
    for i, html in enumerate(cards, start=1):
        for pat in (rf"\{{\{{\s*PRODUCT_CARD_{i}\s*\}}\}}", rf"\{{\s*PRODUCT_CARD_{i}\s*\}}"):
            if re.search(pat, txt):
                txt = re.sub(pat, html, txt, count=1)
                break
        else:
            # Hiç placeholder yoksa, bu kartı sona ekle
            txt += f"\n\n{html}\n"
    return txt



def format_conclusion(raw: str) -> str:
    if not raw:
        return ""
    txt = raw.strip()
    # Başta '## Conclusion' varsa sök
    txt = re.sub(r"^\s*##\s*conclusion\s*\n?", "", txt, flags=re.I)
    return f"""
<section class="conclusion">
  <h2>Conclusion</h2>
  {_md(txt)}
</section>
""".strip()


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").strip().lower()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"[^a-z0-9\s]", "", s)
    return s

def strip_leading_title_from_md(md_text: str, title: str) -> str:
    """Markdown başında title ile aynı olan H1 (ATX # veya Setext ===/---) varsa kaldır."""
    if not md_text or not title:
        return md_text

    lines = md_text.splitlines()
    # ilk boş olmayan satır
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i >= len(lines):
        return md_text

    first = lines[i].strip()
    title_norm = _norm(title)

    # ATX başlık: "# ..." (H1-H6 kabul ediyoruz)
    m = re.match(r"^\s*#{1,6}\s+(.*)$", first)
    if m and _norm(m.group(1)) == title_norm:
        j = i + 1
        if j < len(lines) and not lines[j].strip():
            j += 1
        return "\n".join(lines[:i] + lines[j:])

    # Setext başlık: "Title" + "====" veya "----"
    if _norm(first) == title_norm:
        j = i + 1
        if j < len(lines) and re.match(r"^\s*(=+|-+)\s*$", lines[j]):
            k = j + 1
            if k < len(lines) and not lines[k].strip():
                k += 1
            return "\n".join(lines[:i] + lines[k:])

    return md_text

def _affiliatize_products(products, *, post_slug: str, season: str|None, placement: str):
    """products[*]['url'] üzerinde EPN parametrelerini uygular."""
    subid = make_custom_id(season=season, post_slug=post_slug, placement=placement)
    for p in products:
        raw = p.get("url")
        if not raw:
            continue
        p["url"] = build_epn_link(
            raw, EPN_CAMPID,
            custom_id=subid,
            marketplace="US",
            tool_id=EPN_TOOL_ID,
            channel_id=EPN_CH_ID,
            rotation_id=EPN_ROT_US
        )
    return products


def strip_leading_h1_from_html(html: str, title: str) -> str:
    """HTML başında <h1>Title</h1> varsa ve title ile eşleşiyorsa kaldır."""
    if not html or not title:
        return html
    pat = re.compile(r"^\s*<h1[^>]*>\s*(.*?)\s*</h1>\s*", re.IGNORECASE | re.DOTALL)
    m = pat.match(html)
    if m and _norm(re.sub("<.*?>", "", m.group(1))) == _norm(title):
        return html[m.end():]
    return html

def get_recent_seasons(limit: int = 5, group: str = "seasonal"):
    con = _connect_ro()
    rows = con.execute("""
        SELECT season_name
        FROM seasons
        WHERE COALESCE(season_group,'seasonal') = ?
        ORDER BY created_at DESC NULLS LAST, id DESC
        LIMIT ?
    """, [group, limit]).fetchall()
    con.close()
    return [r[0] for r in rows]


# routes.py (blog_detail_with_cat tanımından ÖNCE ekleyebilirsin, ama 3 segment olduğu için şart değil)

@bp.route("/special-deals/g/<group_key>/", methods=["GET", "HEAD"])
def special_deals_group_page(group_key):
    cats = get_categories()
    con = _connect_ro()
    rows = con.execute("""
        WITH ranked AS (
            SELECT
                ip.idea_id,
                pm.image_url,
                ROW_NUMBER() OVER (PARTITION BY ip.idea_id ORDER BY ip.parent_asin) AS rn
            FROM idea_products ip
            LEFT JOIN product_media pm ON pm.parent_asin = ip.parent_asin
            WHERE pm.image_url IS NOT NULL
        ),
        main_image AS (
            SELECT idea_id, image_url FROM ranked WHERE rn = 1
        )
        SELECT
            bc.title,
            bc.slug,
            COALESCE(bc.category_slug,'uncategorized') AS c,
            COALESCE(
                mi.image_url,
                NULLIF(bc.hero_image_url, ''),
                '/static/img/placeholder.png'
            ) AS image_url,
            CASE WHEN DATE(bc.updated_at) = CURRENT_DATE THEN TRUE ELSE FALSE END AS is_new
        FROM blog_contents bc
        JOIN idea_rules_deal r ON r.idea_id = bc.idea_id
        LEFT JOIN main_image mi ON mi.idea_id = bc.idea_id
        WHERE lower({json_text_expr('r.rules_json', 'category_key')}) = lower(?)
          AND bc.slug IS NOT NULL AND length(trim(bc.slug)) > 0
        ORDER BY bc.updated_at DESC
        LIMIT 200
    """, [group_key]).fetchall()
    con.close()

    posts = [{
        "title": t,
        "url": f"/{c}/{s}/",
        "category": c,
        "category_display": "Special Deals",
        "image": img,
        "excerpt": None,
        "is_new": bool(is_new)
    } for (t, s, c, img, is_new) in rows]

    label_map = {
        'watches': 'Watches',
        'cell_phones': 'Cell Phone',
        'jewelry': 'Jewelry',
        'handbags': 'Handbag',
        'fashion': 'Fashion',
        'other': 'Others'
    }
    pretty = label_map.get(group_key.lower(), group_key.replace('_',' ').title())

    return render_template("category.html",
                           categories=cats,
                           current_category=f"Special Deals: {pretty}",
                           posts=posts)



@bp.route("/<category>/<slug>/", methods=["GET", "HEAD"])
def blog_detail_with_cat(category, slug):
    # trending-now yönlendirmesi
    if category == "trending-now":
        return redirect(url_for("trend_bp.trend_detail", slug=slug), code=302)

    # 🔹 DB'den içerik çek
    row = fetch_row_by_slug(slug)
    if not row:
        # 🔸 Düzgün 404 dönüş (HTML sayfası varsa)
        try:
            return render_template("404.html"), 404
        except Exception:
            return Response("Not found", status=404)

    # artık row None olamaz
    title = (row.get("title") or "").strip()
    kw = row.get("idea_id")

    # --- Bu yazı "deal" mi? (trend değilse grid/top5 gösterilecek) ---
    # "special-deals", "watches", "handbags", "jewelry", "fashion", "seasonal"
    # gibi kategorilerde ürün gridini göstermek istiyoruz.
    is_legacy_post_type = False
    try:
        con = _connect_ro()
        is_deal_rule_present = bool(con.execute(
            "SELECT 1 FROM idea_rules_deal WHERE idea_id = ? LIMIT 1", [kw]
        ).fetchone())
        con.close()

        core_deal_cats = {"special-deals", "watches", "handbags", "jewelry", "fashion"}

        is_legacy_post_type = (
            is_deal_rule_present
            or (category == "seasonal")
            or (category in core_deal_cats)
        )

    except Exception:
        # fallback: seasonal olanlar en azından bozulmasın
        is_legacy_post_type = (category == "seasonal")


     

    # --- eBay verileri (in-article injection için yine çekiyoruz) ---
    ebay_products = []
    top5_html = ""
    insights_html = ""

    # Auth Guarantee?
    auth_guarantee = False
    try:
        con = _connect_ro()
        auth_row = con.execute("""
            SELECT {auth_expr}
            FROM idea_rules_deal r
            WHERE r.idea_id = ?
            LIMIT 1
        """.format(auth_expr=json_int_expr("r.rules_json", "auth_guarantee", 0)), [kw]).fetchone()
        con.close()
        auth_guarantee = bool(auth_row and (auth_row[0] or 0) == 1)
    except Exception:
        auth_guarantee = False

    if kw and not str(kw).startswith("i-"):
        # Ürünleri çek (en fazla 15)
        ebay_products = fetch_ebay_products(kw, limit=15)

        # AG bilgisini ürünlere ekle
        for p in ebay_products:
            p["auth_guarantee"] = auth_guarantee

        # Affiliate parametreleri uygula
        _affiliatize_products(ebay_products, post_slug=slug, season=None, placement="in-article")

        # Sadece DEAL yazılarında Top5 tablo göster
        if is_legacy_post_type:
            top5_html = build_top5_table(ebay_products)

        # Insights (ekranda gösterdiğimiz ürünlerden hesap)
        product_ids = [p.get("id") for p in ebay_products if p.get("id")]
        insights_html = build_enrichment_insights_table(kw, product_ids)

    # --- Metin alanları ---
    intro   = str(row.get("overview_updated") or row.get("introduction") or "").strip()
    gallery = str(row.get("product_gallery") or "").strip()
    urunler = str(row.get("urunler") or "").strip()
    buyers  = str(row.get("buyers_guide") or "").strip()
    faq_raw = str(row.get("faq") or "").strip()
    concl_raw = str(row.get("conclusion") or "").strip()
    # --- JSON sütunları (varsa JSON'u tercih et) ---
    buyers_json = row.get("buyers_guide_json")
    faq_json    = row.get("faq_json")


    # --- Buyer's Guide içine inline kart enjektesi ---
    # JSON varsa onu kullan, yoksa eski markdown/string'i işle
    buyers_html = render_buyers_guide_json(buyers_json) or (format_buyers_guide(buyers) if buyers else "")

    # --- Related links ---
    related_links_raw = row.get("related_links_json")
    related_links = None
    if related_links_raw:
        try:
            related_links = json.loads(related_links_raw)
        except Exception:
            related_links = None

    # --- FAQ + Conclusion ---
    if faq_json:
        # JSON varsa direkt onu render et; tail yok
        faq_html = render_faq_json(faq_json) or ""
        faq_tail = ""
    else:
        # Eski markdown/stil -> mevcut ayrıştırıcı
        faq_html, faq_tail = split_faq_and_tail(faq_raw)

    final_conclusion = concl_raw or faq_tail

    # --- Intro'yu lead/rest olarak böl ---
    lead_intro, rest_intro = split_intro_lead(_md(intro))

    # --- Post sözlüğü ---
    post = {
        "title": title,
        "author_name": row.get("author_name"),
        "author_avatar_url": row.get("author_avatar_url"),
        "author_bio": row.get("author_bio"),
        "date_published": row.get("date_published"),
        "hero_image_url": row.get("hero_image_url") or (ebay_products[0].get("image") if ebay_products else None),
        "hero_alt": row.get("hero_alt") or title,
        "introduction": _md(intro),
        "gallery": gallery,
        "urunler": _md(urunler),
        "buyers_guide": buyers_html, # Şablonda {{ post.buyers_guide | safe }} kullanılmalı
        "related_links": related_links,
        "faq": faq_html,
        "conclusion": format_conclusion(final_conclusion) if final_conclusion else "",
        "insights_table": insights_html,
        "intro_lead": lead_intro,
        "intro_rest": rest_intro,
        "top5_table": top5_html if is_legacy_post_type else "",   # trend yazılarda gizle
        "auth_guarantee": auth_guarantee,
    }

    cats = get_categories()

    # Grid'i de yalnızca deal yazılarında gönder
    ebay_products_for_template = ebay_products if is_legacy_post_type else []

    # "coming soon" placeholder'larını tespit et
    intro_text = (row.get("introduction") or "").lower()
    is_placeholder = "introduction coming soon" in intro_text

    response = make_response(render_template(
        "post.html",
        categories=cats,
        post=post,
        ebay_products=ebay_products_for_template
    ))

    # meta noindex ekle
    if is_placeholder:
        response.headers["X-Robots-Tag"] = "noindex, follow"

    return response


    return render_template(
        "post.html",
        categories=cats,
        post=post,
        ebay_products=ebay_products_for_template
    )


@bp.route("/authors/", methods=["GET"])
def authors_page():
    con = _connect_ro()
    df = con.execute("""
        SELECT 
            a.author_id,
            a.display_name,
            COALESCE(a.avatar_url, '/static/img/placeholder.png') AS avatar_url,
            a.author_bio,
            a.created_at,
            c.name AS category_name,
            c.slug AS category_slug
        FROM authors a
        LEFT JOIN categories c
          ON a.primary_category_slug = c.slug
        ORDER BY a.created_at DESC
    """).fetchdf()
    con.close()

    authors = df.to_dict("records")

    if not authors:
        authors = [{
            "author_id": "test-author",
            "display_name": "Test Author",
            "avatar_url": "/static/img/placeholder.png",
            "author_bio": "This is a sample bio for testing.",
            "created_at": None,
            "category_name": "Electronics",
            "category_slug": "electronics",
        }]

    return render_template("authors.html", authors=authors)

@bp.route("/ethics-policy/", methods=["GET"])
def ethics_policy_page():
    cats = get_categories()
    return render_template("ethics.html", categories=cats)

@bp.route("/privacy-policy/", methods=["GET"])
def privacy_policy_page():
    cats = get_categories()
    return render_template("privacy.html", categories=cats)

@bp.route("/terms-of-service/", methods=["GET"])
def terms_of_service_page():
    cats = get_categories()
    return render_template("terms.html", categories=cats)

@bp.route("/about/", methods=["GET"])
def about_page():
    cats = get_categories()
    return render_template("about.html", categories=cats)





def build_enrichment_insights_table(idea_id: str, product_ids: list[str] | None = None) -> str:
    if product_ids:
        # VALUES listesi oluştur (duckdb VALUES ...)
        placeholders = ",".join([f"('{pid}')" for pid in product_ids])
        items_cte = f"(VALUES {placeholders}) AS v(pid)"
        join_src = "SELECT pid AS product_id FROM " + items_cte
    else:
        join_src = "SELECT ip.parent_asin AS product_id FROM idea_products ip WHERE ip.idea_id = ?"

    con = _connect_ro()
    params = [] if product_ids else [idea_id]
    agg = con.execute(f"""
        WITH items AS (
          {join_src}
        )
        SELECT
          COUNT(*) AS total_items,
          SUM(CASE WHEN e.ship_free THEN 1 ELSE 0 END) AS free_shipping_count,
          ROUND(100.0 * SUM(CASE WHEN e.ship_free THEN 1 ELSE 0 END) / NULLIF(COUNT(*),0), 1) AS free_shipping_pct,
          MIN(e.ship_min_eta) AS eta_min_min,
          MAX(e.ship_max_eta) AS eta_max_max,
          SUM(CASE WHEN e.returns_accepted THEN 1 ELSE 0 END) AS returns_yes,
          ROUND(100.0 * SUM(CASE WHEN e.returns_accepted THEN 1 ELSE 0 END) / NULLIF(COUNT(*),0), 1) AS returns_yes_pct,
          SUM(CASE WHEN e.returns_accepted AND e.return_shipping_payer='SELLER' THEN 1 ELSE 0 END) AS free_returns_count,
          SUM(CASE WHEN e.returns_accepted AND e.return_shipping_payer='BUYER' THEN 1 ELSE 0 END) AS returns_buyer_pays_count,
          SUM(CASE WHEN NOT e.returns_accepted THEN 1 ELSE 0 END) AS returns_no,
          SUM(CASE WHEN e.condition_name ILIKE 'Pre-owned%' THEN 1 ELSE 0 END) AS preowned_count,
          SUM(CASE WHEN e.condition_name ILIKE 'New%' THEN 1 ELSE 0 END) AS new_count,
          SUM(CASE WHEN e.ship_min_eta IS NULL AND e.ship_max_eta IS NULL THEN 1 ELSE 0 END) AS eta_na_count
        FROM product_enrichment e
        JOIN items i ON i.product_id = e.product_id
    """, params).fetchone()

    # shipping types
    ship_rows = con.execute("""
        WITH items AS (SELECT ip.parent_asin AS product_id FROM idea_products ip WHERE ip.idea_id = ?)
        SELECT COALESCE(e.ship_type,'n/a') AS ship_type, COUNT(*) AS c
        FROM product_enrichment e JOIN items i ON i.product_id = e.product_id
        GROUP BY 1 ORDER BY c DESC LIMIT 3
    """, [idea_id]).fetchall()

    # payer breakdown
    payer_rows = con.execute("""
        WITH items AS (SELECT ip.parent_asin AS product_id FROM idea_products ip WHERE ip.idea_id = ?)
        SELECT COALESCE(e.return_shipping_payer,'n/a') AS payer, COUNT(*) AS c
        FROM product_enrichment e JOIN items i ON i.product_id = e.product_id
        GROUP BY 1 ORDER BY c DESC LIMIT 3
    """, [idea_id]).fetchall()

    # tüm ETA'ları çek → ortalama gün hesapla
    eta_rows = con.execute("""
        WITH items AS (SELECT ip.parent_asin AS product_id FROM idea_products ip WHERE ip.idea_id = ?)
        SELECT e.ship_min_eta, e.ship_max_eta
        FROM product_enrichment e JOIN items i ON i.product_id = e.product_id
    """, [idea_id]).fetchall()
    con.close()

    if not agg or agg[0] is None or agg[0] == 0:
        return ""

    (total_items, free_ship_cnt, free_ship_pct, eta_min, eta_max,
     ret_yes, ret_yes_pct, free_ret_cnt, ret_buyer_cnt, ret_no,
     preowned_cnt, new_cnt, eta_na_cnt) = agg

    # Yardımcılar
    ship_types_txt = ", ".join([f"{t} ({c})" for (t, c) in ship_rows]) or "—"
    payer_txt = ", ".join([f"{p} ({c})" for (p, c) in payer_rows]) or "—"

    # Ortalama ETA (gün) — min/max'tan orta nokta
    mids = []
    for (emin, emax) in eta_rows:
        dmin = _eta_as_days(emin)
        dmax = _eta_as_days(emax)
        if dmin is None and dmax is None:
            continue
        if dmin is not None and dmax is not None:
            mids.append((dmin + dmax) / 2.0)
        elif dmin is not None:
            mids.append(float(dmin))
        else:
            mids.append(float(dmax))
    avg_eta_days = int(round(sum(mids) / len(mids))) if mids else None

    # ikonlar
    ico_truck = """<svg viewBox='0 0 24 24' class='ql-ico'><path d='M3 7h10v7h3.5l1.8-3H21V7h-2l-2-3H3v3Zm3 9a2 2 0 1 0 0 4 2 2 0 0 0 0-4Zm10 0a2 2 0 1 0 0 4 2 2 0 0 0 0-4Z' /></svg>"""
    ico_return = """<svg viewBox='0 0 24 24' class='ql-ico'><path d='M7 7v3L3 6l4-4v3h6a6 6 0 0 1 0 12H7v-2h6a4 4 0 0 0 0-8H7Z'/></svg>"""
    ico_clock = """<svg viewBox='0 0 24 24' class='ql-ico'><path d='M12 2a10 10 0 1 0 .001 20.001A10 10 0 0 0 12 2Zm1 5h-2v6l5 3 1-1.732-4-2.268V7Z'/></svg>"""
    ico_box = """<svg viewBox='0 0 24 24' class='ql-ico'><path d='M3 7 12 3l9 4-9 4-9-4Zm0 4 9 4 9-4v7l-9 4-9-4v-7Z'/></svg>"""
    ico_money = """<svg viewBox='0 0 24 24' class='ql-ico'><path d='M3 6h18v12H3V6Zm2 2v8h14V8H5Zm3 4a4 4 0 1 0 8 0 4 4 0 0 0-8 0Z'/></svg>"""
    ico_ship  = """<svg viewBox='0 0 24 24' class='ql-ico'><path d='M2 12h20v2H2zM4 8h16v2H4zM6 16h12v2H6z'/></svg>"""

    # Quick Look (tek satır 6 kart)
    quick = f"""
<section class="insights insights--quicklook">
  <h2 class="ql-title">What we analyzed for you</h2>
  <div class="ql-grid ql-grid--six">
    <div class="ql-card">
      <div class="ql-icon">{ico_truck}</div>
      <div class="ql-head">Free shipping</div>
      <div class="ql-val">{free_ship_cnt} / {total_items}</div>
      <div class="ql-subtle">sellers offer free shipping</div>
    </div>
    <div class="ql-card">
      <div class="ql-icon">{ico_return}</div>
      <div class="ql-head">Returns</div>
      <div class="ql-val">{ret_yes} sellers accept returns</div>
      <div class="ql-subtle">&nbsp;</div>
    </div>
    <div class="ql-card">
      <div class="ql-icon">{ico_clock}</div>
      <div class="ql-head">Average ETA</div>
      <div class="ql-val">{(str(avg_eta_days)+' days') if avg_eta_days is not None else '—'}</div>
      <div class="ql-subtle">{'n/a for '+str(eta_na_cnt)+' items' if eta_na_cnt else '&nbsp;'}</div>
    </div>
    <div class="ql-card">
      <div class="ql-icon">{ico_box}</div>
      <div class="ql-head">Condition mix</div>
      <div class="ql-val">New {new_cnt or 0} / Pre-owned {preowned_cnt or 0}</div>
      <div class="ql-subtle">{total_items} total items</div>
    </div>
    <div class="ql-card">
      <div class="ql-icon">{ico_ship}</div>
      <div class="ql-head">Shipping types</div>
      <div class="ql-val">{ship_types_txt or '—'}</div>
      <div class="ql-subtle">&nbsp;</div>
    </div>
    <div class="ql-card">
      <div class="ql-icon">{ico_money}</div>
      <div class="ql-head">Return shipping payer</div>
      <div class="ql-val">{payer_txt or '—'}</div>
      <div class="ql-subtle">&nbsp;</div>
    </div>
  </div>

  <details class="ql-details">
    <summary>View details</summary>
    <div class="insights-table-wrap">
      <table class="insights-table">
        <tbody>
          <tr><th>Total items</th><td>{total_items}</td></tr>
          <tr><th>Free shipping</th><td>{free_ship_cnt} items</td></tr>
          <tr><th>Average ETA</th><td>{(str(avg_eta_days)+' days') if avg_eta_days is not None else '—'}</td></tr>
          <tr><th>Returns accepted</th><td>{ret_yes} sellers</td></tr>
          <tr><th>Free returns (seller pays)</th><td>{free_ret_cnt} items</td></tr>
          <tr><th>Returns (buyer pays)</th><td>{ret_buyer_cnt} items</td></tr>
          <tr><th>No returns</th><td>{ret_no} items</td></tr>
          <tr><th>Condition mix</th><td>New: {new_cnt or 0} • Pre-owned: {preowned_cnt or 0}</td></tr>
          <tr><th>Shipping types (top)</th><td>{ship_types_txt}</td></tr>
          <tr><th>Return shipping payer (top)</th><td>{payer_txt}</td></tr>
        </tbody>
      </table>
    </div>
  </details>
</section>
""".strip()

    return quick

def build_top5_table(ebay_products):
    if not ebay_products:
        return ""

    # skora göre sırala (yüksek önce), eşitlikte review sayısı yüksek olan öne
    prods = sorted(
        ebay_products,
        key=lambda x: (x.get("score") or 0.0, x.get("fb_score") or 0),
        reverse=True
    )[:5]

    def star_row(n):
        n = max(0, min(5, int(n or 0)))
        full = "★" * n
        empty = "☆" * (5 - n)
        return f"<span class='stars-int'>{full}{empty}</span>"

    rows_html = []
    for p in prods:
        img         = p.get("image") or "/static/img/placeholder.png"
        url         = p.get("url") or "#"
        title       = p.get("title") or "Product"

        score       = p.get("score")
        score_txt   = f"{score:.1f}" if score is not None else "—"   # = Minti Point
        stars_whole = p.get("stars_whole") or 0
        label       = p.get("seller_label") or ""

        fb_score    = p.get("fb_score") or 0
        fb_pct      = p.get("fb_pct")
        fb_pct_txt  = f"{fb_pct:.1f}%" if fb_pct is not None else "—"

        ship_free   = p.get("ship_free")
        ship_free_txt = "Yes" if ship_free is True else ("No" if ship_free is False else "—")

        returns_txt = p.get("returns") or "—"
        payer_raw   = (p.get("return_shipping_payer") or "").upper()
        payer_txt   = {"SELLER": "seller pays", "BUYER": "buyer pays"}.get(
            payer_raw, payer_raw.lower() if payer_raw else ""
        )
        ret_col     = returns_txt if not payer_txt else f"{returns_txt} • {payer_txt}"

        eta_txt     = p.get("eta_days") or "—"
        ship_type   = p.get("ship_type") or "—"

        rows_html.append(f"""
<tr>
  <!-- Product (image only, tooltip with title) -->
  <td class="col-img">
    <a class="tab-img" href="{url}" target="_blank" rel="noopener sponsored" title="{title}">
      <img src="{img}" alt="Product image" loading="lazy" decoding="async">
    </a>
  </td>

  <!-- Minti Point -->
  <td class="col-minti">{score_txt}</td>

  <!-- Stars (filled/empty) + label küçük -->
  <td class="col-stars">
    <div class="score-wrap">
      {star_row(stars_whole)}
      <span class="score-label">{label}</span>
    </div>
  </td>

  <!-- Free Ship -->
  <td class="col-shipfree">{ship_free_txt}</td>

  <!-- Returns / Payer -->
  <td class="col-returns">{ret_col}</td>

  <!-- Reviews -->
  <td class="col-rev">{fb_score:,}</td>

  <!-- Positive % -->
  <td class="col-pos">{fb_pct_txt}</td>

  <!-- ETA (days text like 2–5) -->
  <td class="col-eta">{eta_txt}</td>

  <!-- Ship Type -->
  <td class="col-shiptype">{ship_type}</td>
</tr>
""".strip())

    return f"""
<section class="rating-table">
  <h2 class="rt-title">Top 5 Sellers (by Minti Point)</h2>
  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th class="col-img">Product</th>
          <th class="col-minti">Minti Point</th>
          <th class="col-stars">Stars</th>
          <th class="col-shipfree">Free Ship</th>
          <th class="col-returns">Returns / Payer</th>
          <th class="col-rev">Reviews</th>
          <th class="col-pos">Positive %</th>
          <th class="col-eta">ETA</th>
          <th class="col-shiptype">Ship Type</th>
        </tr>
      </thead>
      <tbody>
        {''.join(rows_html)}
      </tbody>
    </table>
  </div>
</section>
""".strip()

def fetch_ebay_products(idea_id: str, limit: int = 20):
    con = _connect_ro()
    rows = con.execute("""
        WITH one_image AS (
            SELECT parent_asin, ANY_VALUE(image_url) AS image_url
            FROM product_media
            GROUP BY parent_asin
        ),
        -- product_metrics_ebay: her product_id için tek satıra indir
        latest_m AS (
            SELECT
              product_id,
              ANY_VALUE(seller_score)  AS seller_score,
              ANY_VALUE(trust_level)   AS trust_level,
              ANY_VALUE(feedback_pct)  AS feedback_pct,
              ANY_VALUE(feedback_score)AS feedback_score
            FROM product_metrics_ebay
            GROUP BY product_id
        ),
        -- product_enrichment: ihtiyaç duyulan alanları tek satıra indir
        latest_e AS (
            SELECT
              product_id,
              ANY_VALUE(returns_accepted)       AS returns_accepted,
              ANY_VALUE(return_shipping_payer)  AS return_shipping_payer,
              ANY_VALUE(ship_free)              AS ship_free,
              ANY_VALUE(ship_type)              AS ship_type,
              ANY_VALUE(ship_min_eta)           AS ship_min_eta,
              ANY_VALUE(ship_max_eta)           AS ship_max_eta
            FROM product_enrichment
            GROUP BY product_id
        ),
        -- idea_products konsolidasyonu (aynı ürün birden fazlaysa)
        ipx AS (
            SELECT
              ip.parent_asin,
              MAX(ip.discount_pct)   AS discount_pct,
              MAX(ip.original_price) AS original_price,
              MAX(ip.sale_price)     AS sale_price
            FROM idea_products ip
            WHERE ip.idea_id = ?
            GROUP BY ip.parent_asin
        )
        SELECT 
            p.product_title,
            p.price,
            oi.image_url,
            p.external_id,
            m.seller_score,
            m.trust_level,
            m.feedback_pct,
            m.feedback_score,
            e.returns_accepted,
            e.return_shipping_payer,
            e.ship_free,
            e.ship_type,
            e.ship_min_eta,
            e.ship_max_eta,
            ipx.discount_pct,
            ipx.original_price,
            ipx.sale_price,
            p.parent_asin AS parent_asin
        FROM products p
        JOIN ipx               ON ipx.parent_asin = p.parent_asin
        LEFT JOIN one_image oi ON oi.parent_asin  = p.parent_asin
        LEFT JOIN latest_m m   ON m.product_id    = p.parent_asin
        LEFT JOIN latest_e e   ON e.product_id    = p.parent_asin
        WHERE p.source = 'ebay'
    """, [idea_id]).fetchall()
    con.close()

    # --- Python tarafı: emniyetli dedupe (başlık+fiyat) ve veri hazırlığı ---
    def _eta_days(val):
        if val is None:
            return None
        if isinstance(val, (int, float)):
            return int(val)
        if isinstance(val, str):
            try:
                dt = datetime.fromisoformat(val.replace("Z","+00:00"))
                now = datetime.now(timezone.utc) if dt.tzinfo else datetime.utcnow()
                return max(0, (dt - now).days)
            except Exception:
                return None
        return None

    def _norm_title(s):
        import re
        return re.sub(r'[^a-z0-9]+', '', (s or '').lower())

    products, seen = [], set()

    for (t, pr, img, eid, s, trust, fb_pct, fb_score,
         ret_acc, ret_payer, ship_free, ship_type, eta_min, eta_max,
         ip_disc, ip_orig, ip_sale, pid) in rows:   # 👈 pid eklendi

        eta_min_days = _eta_days(eta_min)
        eta_max_days = _eta_days(eta_max)
        eta_txt = (f"{eta_min_days}–{eta_max_days}"
                   if (eta_min_days is not None and eta_max_days is not None)
                   else (str(eta_min_days) if eta_min_days is not None else None))

        # indirim %
        disc_pct = None
        try:
            if ip_disc is not None:
                disc_pct = float(ip_disc)
            elif ip_orig and ip_sale and float(ip_orig) > 0:
                disc_pct = max(0.0, 100.0 * (float(ip_orig) - float(ip_sale)) / float(ip_orig))
        except Exception:
            disc_pct = None

        # eBay URL (pipe'lı external_id'ler için)
        try:
            url = f"https://www.ebay.com/itm/{eid.split('|')[1]}" if (eid and '|' in eid) else f"https://www.ebay.com/itm/{eid}"
        except Exception:
            url = "#"

        # dedupe anahtarı: normalize başlık + efektif fiyat
        eff_price = (ip_sale if ip_sale is not None else pr) or 0
        key = (_norm_title(t), int(float(eff_price)))
        if key in seen:
            continue
        seen.add(key)

        rating = _compute_seller_rating(
            fb_pct, fb_score, s, trust,
            returns_accepted=ret_acc, return_shipping_payer=ret_payer,
            ship_free=ship_free, ship_type=ship_type,
            ship_min_eta=eta_min, ship_max_eta=eta_max
        )

        products.append({
            "id": pid,  # 👈 EKLE
            "title": t,
            "price": float(pr) if pr is not None else None,
            "image": img,
            "url": url,
            "seller_score": s,
            "trust": rating["tier"],
            "fb_pct": fb_pct,
            "fb_score": fb_score,
            "score": rating["score"],
            "stars_whole": rating["stars_whole"],
            "seller_label": rating["label"],
            "returns": ("Yes (seller pays)" if (ret_acc and str(ret_payer or '').upper()=='SELLER')
                        else "Yes" if ret_acc else "No"),
            "eta_days": eta_txt,
            "ship_free": bool(ship_free) if ship_free is not None else None,
            "ship_type": ship_type,
            "return_shipping_payer": ret_payer,
            "discount_pct": disc_pct,
            "original_price": float(ip_orig) if ip_orig is not None else None,
            "sale_price": float(ip_sale) if ip_sale is not None else None,
        })

    # en yüksek indirim üstte; None'lar en sona
    products.sort(key=lambda p: (p["discount_pct"] is None, -(p["discount_pct"] or 0.0)))
    return products[:limit]

 
# 2) Düz slug → önce blog dene, yoksa kategori fallback
@bp.route("/<slug>/", methods=["GET", "HEAD"])
def blog_detail_or_category(slug):
    row = fetch_row_by_slug(slug)
    if row:
        return blog_detail_with_cat(row.get("category_slug") or "uncategorized", slug)

    # kategori adı mı?
    con = _connect_ro()
    exists = con.execute(
        "SELECT 1 FROM blog_contents WHERE category_slug = ? LIMIT 1", [slug]
    ).fetchone()
    con.close()
    if exists:
        return category_page(slug)

    return Response("Not found", status=404)


@bp.route("/ebatlist.html", methods=["GET"])
def serve_ebatlist():
    return send_from_directory("/var/www/html", "ebatlist.html")



# 3) Kategori listesi
@bp.route("/<category>/", methods=["GET", "HEAD"])
def category_page(category):
    cats = get_categories()
    con = _connect_ro()

    if category == "special-deals":
        rows = con.execute("""
            WITH ranked AS (
              SELECT ip.idea_id, pm.image_url,
                     ROW_NUMBER() OVER (PARTITION BY ip.idea_id ORDER BY ip.parent_asin) AS rn
              FROM idea_products ip
              LEFT JOIN product_media pm ON pm.parent_asin = ip.parent_asin
              WHERE pm.image_url IS NOT NULL
            ),
            main_image AS (SELECT idea_id, image_url FROM ranked WHERE rn = 1)
            SELECT
              bc.title, bc.slug,
              COALESCE(bc.category_slug,'uncategorized') AS c,
              COALESCE(mi.image_url, NULLIF(bc.hero_image_url,''), '/static/img/placeholder.png') AS image_url,
              CASE WHEN DATE(bc.updated_at) = CURRENT_DATE THEN TRUE ELSE FALSE END AS is_new,
              s.season_name
            FROM blog_contents bc
            LEFT JOIN main_image mi ON mi.idea_id = bc.idea_id
            LEFT JOIN season_phrases sp ON sp.phrase = bc.idea_id
            LEFT JOIN seasons s        ON s.id = sp.season_id
            WHERE bc.slug IS NOT NULL
              AND length(trim(bc.slug)) > 0
              AND bc.category_slug IN ('special-deals','watches','handbags','jewelry','fashion')
            ORDER BY bc.updated_at DESC
            LIMIT 200
        """).fetchall()
    else:
        rows = con.execute("""
            WITH ranked AS (
            SELECT
                ip.idea_id,
                pm.image_url,
                ROW_NUMBER() OVER (PARTITION BY ip.idea_id ORDER BY ip.parent_asin) AS rn
            FROM idea_products ip
            LEFT JOIN product_media pm ON pm.parent_asin = ip.parent_asin
            WHERE pm.image_url IS NOT NULL
            ),
            main_image AS (
            SELECT idea_id, image_url FROM ranked WHERE rn = 1
            ),
            season_join AS (
            SELECT
                bc.title,
                bc.slug,
                COALESCE(bc.category_slug,'uncategorized') AS c,
                COALESCE(
                    mi.image_url,
                    NULLIF(bc.hero_image_url,''),
                    '/static/img/placeholder.png'
                ) AS image_url,
                CASE WHEN CAST(bc.updated_at AS DATE) = CURRENT_DATE THEN TRUE ELSE FALSE END AS is_new,
                s.season_name,
                bc.updated_at AS updated_at   -- 👈 ekledik
            FROM blog_contents bc
            LEFT JOIN main_image mi ON mi.idea_id = bc.idea_id
            LEFT JOIN season_phrases sp ON sp.phrase = bc.idea_id
            LEFT JOIN seasons s ON s.id = sp.season_id
            WHERE bc.category_slug = ?
                AND bc.slug IS NOT NULL
                AND length(trim(bc.slug)) > 0
            )
            SELECT * FROM season_join
            ORDER BY updated_at DESC              -- 👈 artık mevcut
            LIMIT 200
        """, [category]).fetchall()


    con.close()
    posts = []
    for row in rows:
        safe_row = list(row) + [None] * max(0, 7 - len(row))
        t, s, c, img, is_new, season_name, _updated_at = safe_row[:7]
        posts.append({
            "title": t,
            "url": f"/{c}/{s}/",
            "category": c,
            "category_display": pretty_season_label(season_name, c),
            "image": img,
            "excerpt": None,
            "is_new": bool(is_new)
        })


    return render_template("category.html",
                           categories=cats,
                           current_category=category,
                           posts=posts)


# routes.py (uygun bir yere ekle)

def get_deal_groups():
    """
    Special Deals altında hangi alt gruplar (watches, cell_phones, ...) var?
    rules_json.category_key'ye göre unique liste + sayım döndürür.
    """
    con = _connect_ro()
    rows = con.execute("""
        SELECT
          lower(coalesce({json_text_expr('r.rules_json', 'category_key')}, 'other')) AS gkey,
          COUNT(*) AS cnt
        FROM idea_rules_deal r
        JOIN blog_contents bc ON bc.idea_id = r.idea_id
        WHERE lower({json_text_expr('r.rules_json', 'category_key')}) = lower(?)

        GROUP BY 1
        ORDER BY cnt DESC, gkey ASC
    """).fetchall()
    con.close()

    label_map = {
        'watches': 'Watches Deals',
        'cell_phones': 'Cell Phone Deals',
        'jewelry': 'Jewelry Deals',
        'handbags': 'Handbag Deals',
        'fashion': 'Fashion Deals',
        'other': 'Other Deals'
    }
    return [{"key": k, "label": label_map.get(k, k.replace('_',' ').title()), "count": c} for (k, c) in rows]






@bp.route("/season/<season_name>/", methods=["GET", "HEAD"])
def season_page(season_name):
    cats = get_categories()
    con = _connect_ro()

    row = con.execute(
        "SELECT id FROM seasons WHERE season_name = ? LIMIT 1",
        [season_name]
    ).fetchone()
    if not row:
        con.close()
        return Response("Season not found", status=404)
    season_id = row[0]

    rows = con.execute("""
        WITH kept_phr AS (
            SELECT phrase
            FROM season_phrases
            WHERE season_id = ? AND kept = TRUE
            ),
            ranked AS (
            SELECT
                ip.idea_id,
                pm.image_url,
                ROW_NUMBER() OVER (PARTITION BY ip.idea_id ORDER BY ip.parent_asin) AS rn
            FROM idea_products ip
            LEFT JOIN product_media pm ON pm.parent_asin = ip.parent_asin
            WHERE pm.image_url IS NOT NULL
            ),
            main_image AS (
            SELECT idea_id, image_url FROM ranked WHERE rn = 1
            ),
            season_join AS (
            SELECT bc.*
            FROM blog_contents bc
            JOIN kept_phr kp ON kp.phrase = bc.idea_id
            ),
            category_fallback AS (
            SELECT bc.*
            FROM blog_contents bc
            WHERE bc.category_slug = (SELECT season_name FROM seasons WHERE id = ? LIMIT 1)
            )
            SELECT
            bc.title,
            bc.slug,
            COALESCE(bc.category_slug,'uncategorized') AS c,
            COALESCE(
                mi.image_url,
                NULLIF(bc.hero_image_url,''),
                '/static/img/placeholder.png'
            ) AS image_url,
            CASE WHEN CAST(bc.updated_at AS DATE) = CURRENT_DATE THEN TRUE ELSE FALSE END AS is_new
            FROM (
            SELECT * FROM season_join
            UNION ALL
            SELECT * FROM category_fallback
            ) bc
            LEFT JOIN main_image mi ON mi.idea_id = bc.idea_id
            WHERE bc.slug IS NOT NULL AND length(trim(bc.slug)) > 0
            ORDER BY bc.updated_at DESC
            LIMIT 200
        """, [season_id, season_id]).fetchall()  # 👈 ikinci parametre eklendi
    con.close()

    posts = [{
        "title": t,
        "url": f"/{c}/{s}/",
        "image": img or "/static/img/placeholder.png",
        "is_new": bool(is_new),  # 👈 eklendi
    } for (t, s, c, img, is_new) in rows]


    pretty = (season_name or "").replace("-", " ").title()

    return render_template(
        "category.html",
        categories=cats,
        current_category=f"Season: {pretty}",  # header'da görünsün
        posts=posts
    )


# 4) Home
@bp.route("/", methods=["GET", "HEAD"])
def home():
    cats = get_categories()
    con = _connect_ro()

    # --- HERO SLIDES: son eklenen 5 yazı (yayın tarihi varsa ona göre, yoksa updated_at) ---
    slides_rows = con.execute("""
        SELECT
          bc.title,
          bc.slug,
          COALESCE(bc.category_slug,'uncategorized') AS c,
          bc.hero_image_url,
          COALESCE(bp.date_published, bc.updated_at) AS sort_ts
        FROM blog_contents bc
        LEFT JOIN blog_posts bp ON bc.idea_id = bp.idea_id
        WHERE bc.hero_image_url IS NOT NULL
          AND length(trim(bc.hero_image_url)) > 0
          AND bc.slug IS NOT NULL
          AND length(trim(bc.slug)) > 0
        ORDER BY sort_ts DESC
        LIMIT 5
    """).fetchall()
    # ... (senin mevcut posts sorgun burada devam ediyor)

    rows = con.execute("""
    WITH ranked AS (
        SELECT
            ip.idea_id,
            pm.image_url,
            ROW_NUMBER() OVER (PARTITION BY ip.idea_id ORDER BY ip.parent_asin) AS rn
        FROM idea_products ip
        LEFT JOIN product_media pm ON pm.parent_asin = ip.parent_asin
        WHERE pm.image_url IS NOT NULL
    ),
    main_image AS (
        SELECT idea_id, image_url FROM ranked WHERE rn = 1
    )
    SELECT
        bc.title,
        bc.slug,
        COALESCE(bc.category_slug,'uncategorized') AS c,
        COALESCE(
            NULLIF(bc.hero_image_url, ''),  -- önce bizim ürettiğimiz hero
            mi.image_url,
            '/static/img/placeholder.png'
        ) AS image_url,
        CASE WHEN CAST(bc.updated_at AS DATE) = CURRENT_DATE THEN TRUE ELSE FALSE END AS is_new,
        s.season_name  -- 👈 yeni
    FROM blog_contents bc
    LEFT JOIN main_image mi ON mi.idea_id = bc.idea_id
    LEFT JOIN season_phrases sp ON sp.phrase = bc.idea_id           -- 👈 yeni
    LEFT JOIN seasons s        ON s.id = sp.season_id               -- 👈 yeni
    WHERE bc.slug IS NOT NULL AND length(trim(bc.slug)) > 0
    ORDER BY bc.updated_at DESC
    LIMIT 30
""").fetchall()

    
    con.close()

    posts = [{
        "title": t,
        "url": f"/{c}/{sslug}/",
        "category": c,
        "category_display": pretty_season_label(season_name, c),
        "image": img,
        "excerpt": None,
        "is_new": bool(is_new)
    } for (t, sslug, c, img, is_new, season_name) in rows]

    hero_slides = [{
        "title": t,
        "url": f"/{c}/{s}/",
        "image": img
    } for (t, s, c, img, _ts) in slides_rows]

    return render_template(
        "index.html",
        categories=cats,
        posts=posts,
        hero_slides=hero_slides   # 👈 eklendi
    )

    
