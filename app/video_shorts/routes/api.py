import json

from flask import current_app, g, jsonify, request

from app.video_shorts import video_shorts_bp
from app.video_shorts.config import CAPTION_API_TOKEN
from app.video_shorts.services.db import (
    _ensure_transcript_schema,
    _ensure_video_crop_schema,
    ensure_postgres_youtube_transcripts_id_default,
    get_db,
    get_db_readonly,
)
from app.video_shorts.services.render_jobs import get_job
from app.video_shorts.services.transcript_service import _normalize_segments_for_use
from app.video_shorts.services.usage_metering import add_transcription_minutes, get_usage_snapshot


def _duration_minutes(duration_seconds) -> float:
    try:
        seconds = float(duration_seconds or 0)
    except Exception:
        seconds = 0.0
    if seconds <= 0:
        return 0.0
    return round(seconds / 60.0, 2)


def _check_caption_token(req):
    token = req.headers.get("X-Api-Token")
    return bool(token and token == CAPTION_API_TOKEN)


@video_shorts_bp.route("/api/caption-tasks", methods=["GET"])
def caption_tasks():
    if not _check_caption_token(request):
        return jsonify({"error": "forbidden"}), 403

    try:
        limit = int(request.args.get("limit", "20"))
    except Exception:
        limit = 20

    conn = get_db_readonly()
    rows = conn.execute(
        """
        SELECT id, video_id, title AS video_title, video_url
        FROM youtube_videos
        WHERE fetch_transcript = TRUE
          AND lower(transcript_status) = 'pending'
        ORDER BY published_at DESC
        LIMIT ?
        """,
        [limit],
    ).fetchall()
    cols = [d[0] for d in conn.description]
    tasks = [dict(zip(cols, r)) for r in rows]
    conn.close()
    return jsonify({"tasks": tasks})


@video_shorts_bp.route("/api/usage", methods=["GET"])
def usage_api():
    current_user = getattr(g, "vs_current_user", None)
    if not current_user:
        return jsonify({"error": "unauthorized"}), 401
    try:
        return jsonify(get_usage_snapshot(current_user["id"]))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@video_shorts_bp.route("/api/jobs/<job_id>", methods=["GET"])
def render_job_status_api(job_id: str):
    current_user = getattr(g, "vs_current_user", None)
    if not current_user:
        return jsonify({"error": "unauthorized"}), 401
    try:
        job = get_job(job_id, user_id=str(current_user["id"]))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    if not job:
        return jsonify({"error": "not_found"}), 404
    return jsonify(
        {
            "id": job["id"],
            "status": job["status"],
            "priority": job["priority"],
            "created_at": job["created_at"],
            "started_at": job["started_at"],
            "finished_at": job["finished_at"],
            "result": job.get("result"),
            "error": job.get("error"),
            "queue_position": job.get("queue_position"),
        }
    )


@video_shorts_bp.route("/api/caption-result", methods=["POST"])
def caption_result():
    if not _check_caption_token(request):
        return jsonify({"error": "forbidden"}), 403

    data = request.get_json(silent=True) or {}
    video_db_id = data.get("video_db_id")
    caption_text = (data.get("caption_text") or "").strip()
    lang = (data.get("lang") or "en").strip()
    segments = data.get("segments")

    if not video_db_id or not caption_text:
        return jsonify({"error": "missing fields"}), 400

    segments_json = None
    whisper_segments_json = None
    if isinstance(segments, list):
        try:
            normalized = _normalize_segments_for_use(segments)
            whisper_segments_json = json.dumps(normalized, ensure_ascii=False)
            segments_json = whisper_segments_json
        except Exception:
            segments_json = None
            whisper_segments_json = None

    conn = get_db()
    _ensure_video_crop_schema(conn)
    _ensure_transcript_schema(conn)
    ensure_postgres_youtube_transcripts_id_default(conn)
    try:
        row = conn.execute(
            "SELECT video_id, owner_user_id, duration_seconds FROM youtube_videos WHERE id = ?",
            [video_db_id],
        ).fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "video not found"}), 404
        video_id = row[0]
        owner_user_id = row[1]
        duration_seconds = row[2]

        conn.execute(
            """
            INSERT INTO youtube_transcripts (video_id, full_text, segments_json, whisper_segments_json)
            VALUES (?, ?, ?, ?)
            """,
            [video_id, caption_text, segments_json, whisper_segments_json],
        )

        conn.execute(
            """
            UPDATE youtube_videos
            SET transcript_status = 'done',
                fetch_transcript = FALSE,
                last_checked_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            [video_db_id],
        )
        conn.commit()
        conn.close()
        if owner_user_id:
            minutes = _duration_minutes(duration_seconds)
            if minutes > 0:
                try:
                    add_transcription_minutes(str(owner_user_id), minutes)
                except Exception:
                    current_app.logger.exception(
                        "Failed to meter caption worker transcription usage for video_db_id=%s",
                        video_db_id,
                    )
        return jsonify({"ok": True}), 200
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()
        return jsonify({"error": str(e)}), 500


@video_shorts_bp.route("/api/caption-status", methods=["POST"])
def caption_status():
    """
    Worker can report non-success states (e.g., no transcript available or an error)
    so the same video does not keep re-appearing in the queue.
    """
    if not _check_caption_token(request):
        return jsonify({"error": "forbidden"}), 403

    data = request.get_json(silent=True) or {}
    video_db_id = data.get("video_db_id")
    status = (data.get("status") or "").strip().lower()
    if not video_db_id or not status:
        return jsonify({"error": "missing fields"}), 400

    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id FROM youtube_videos WHERE id = ?",
            [video_db_id],
        ).fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "video not found"}), 404

        conn.execute(
            """
            UPDATE youtube_videos
            SET transcript_status = ?,
                fetch_transcript = FALSE,
                last_checked_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            [status, video_db_id],
        )
        conn.commit()
        conn.close()
        return jsonify({"ok": True}), 200
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()
        return jsonify({"error": str(e)}), 500


@video_shorts_bp.route("/api/download-status", methods=["POST"])
def download_status():
    """
    Allow a local downloader to mark the video as downloaded (or failed) on the central DB.
    This mirrors how transcript workers report status so local/remote stay in sync.
    """
    if not _check_caption_token(request):
        return jsonify({"error": "forbidden"}), 403

    data = request.get_json(silent=True) or {}
    video_db_id = data.get("video_db_id")
    status = (data.get("status") or "").strip().lower()
    if not video_db_id or not status:
        return jsonify({"error": "missing fields"}), 400

    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id FROM youtube_videos WHERE id = ?",
            [video_db_id],
        ).fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "video not found"}), 404

        conn.execute(
            """
            UPDATE youtube_videos
            SET download_status = ?,
                downloaded_at = CASE WHEN ? = 'downloaded' THEN CURRENT_TIMESTAMP ELSE NULL END,
                last_checked_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            [status, status, video_db_id],
        )
        conn.commit()
        conn.close()
        return jsonify({"ok": True}), 200
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()
        return jsonify({"error": str(e)}), 500


@video_shorts_bp.route("/api/download-tasks", methods=["GET"])
def download_tasks():
    """
    Provide a queue for downloader workers based on download_status='pending'.
    """
    if not _check_caption_token(request):
        return jsonify({"error": "forbidden"}), 403

    try:
        limit = int(request.args.get("limit", "20"))
    except Exception:
        limit = 20

    conn = get_db_readonly()
    rows = conn.execute(
        """
        SELECT
          yv.id,
          yv.channel_id,
          ch.channel_name,
          yv.video_id,
          yv.title AS video_title,
          yv.video_url,
          yv.download_status
        FROM youtube_videos yv
        LEFT JOIN youtube_channels ch ON ch.channel_id = yv.channel_id
        WHERE lower(coalesce(yv.download_status,'')) = 'pending'
        ORDER BY yv.published_at DESC
        LIMIT ?
        """,
        [limit],
    ).fetchall()
    cols = [d[0] for d in conn.description]
    tasks = [dict(zip(cols, r)) for r in rows]
    conn.close()
    return jsonify({"tasks": tasks})
